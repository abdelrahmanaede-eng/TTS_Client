"""Tests for export duration formatting (issue: Excel showing '1:31')."""
from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

import openpyxl

from time_tracker.core.config import Config
from time_tracker.core.database import Database
from time_tracker.reporting.reporter import (
    _build_official_pdf_html,
    excel_safe_text,
    export_csv_bundle,
    export_report,
    hours_to_hm,
)

D = date(2026, 11, 9)


class TestExcelSafeText(unittest.TestCase):
    def test_prefixes_only_excel_autocast_strings(self) -> None:
        self.assertEqual(excel_safe_text("1h 31m"), "'1h 31m")
        self.assertEqual(excel_safe_text("14:00:00"), "'14:00:00")
        self.assertEqual(excel_safe_text("13:31:00"), "'13:31:00")
        self.assertEqual(excel_safe_text("2026-11-09"), "'2026-11-09")
        self.assertEqual(excel_safe_text("=HYPERLINK(\"x\")"), "'=HYPERLINK(\"x\")")
        self.assertEqual(excel_safe_text("+cmd"), "'+cmd")
        # Ordinary text and numeric data stay untouched.
        self.assertEqual(excel_safe_text("Meeting"), "Meeting")
        self.assertEqual(excel_safe_text("Mon"), "Mon")
        self.assertEqual(excel_safe_text("-"), "-")
        self.assertEqual(excel_safe_text(1.52), 1.52)
        self.assertEqual(excel_safe_text(None), None)


class TestDurationFormatting(unittest.TestCase):
    def test_hours_to_hm(self) -> None:
        self.assertEqual(hours_to_hm(1.5167), "1h 31m")
        self.assertEqual(hours_to_hm(8.0), "8h 00m")
        self.assertEqual(hours_to_hm("n/a"), "n/a")

    def test_pdf_uses_hours_and_minutes(self) -> None:
        rows = [[D.isoformat(), "Mon", "09:00:00", "15:31:00",
                 1.5167, 0.0, 0.0, 0.0, 8.0, "Partial"]]
        html = _build_official_pdf_html(
            logo_base64="", start_date=D, end_date=D, report_uid="R-1",
            now_str="now", employee_name="Tester", totals={"worked": 1.5167, "target": 8.0,
            "overtime": 0.0, "meeting": 0.0, "break": 0.0}, compliance_pct=19.0,
            rows=rows, projects=[],
        )
        self.assertIn("1h 31m", html)
        self.assertNotIn("1.5h", html)


class TestExcelAndCsvExports(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.db = Database(self.out / "data.db")
        self.cfg = Config()
        base = int(datetime.combine(D, time(9, 0)).timestamp())
        for i in range(92):  # ~1h31m of active work
            self.db.record_input_event(base + i * 60)
        s = int(datetime.combine(D, time(14, 0)).timestamp())
        self.db.add_past_session("meeting", s, s + 91 * 60, note="Demo", reason="client")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_csv_keeps_duration_literal_and_supports_raw_mode(self) -> None:
        files = export_csv_bundle(self.db, self.cfg, D, D, open_folder=False,
                                  output_dir=self.out)
        app_csv = next(p for p in files if "AppUsage" in p.name)
        with app_csv.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        cells = {c: r[i] for r in rows for i, c in enumerate(rows[0])}
        self.assertEqual(cells["Date"], "'2026-11-09")
        self.assertEqual(cells["Time Spent"], "'1h 31m")

        sess_csv = next(p for p in files if "Sessions" in p.name)
        with sess_csv.open(encoding="utf-8-sig", newline="") as handle:
            srows = list(csv.reader(handle))
        scells = {c: r[i] for r in srows for i, c in enumerate(srows[0])}
        self.assertEqual(scells["Start Time"], "'14:00:00")
        self.assertEqual(scells["End Time"], "'15:31:00")

        raw = export_csv_bundle(self.db, self.cfg, D, D, open_folder=False,
                                output_dir=self.out / "raw", excel_safe=False)
        app_raw = next(p for p in raw if "AppUsage" in p.name)
        with app_raw.open(encoding="utf-8-sig", newline="") as handle:
            raw_rows = list(csv.reader(handle))
        raw_cells = {c: r[i] for r in raw_rows for i, c in enumerate(raw_rows[0])}
        self.assertEqual(raw_cells["Date"], "2026-11-09")
        self.assertEqual(raw_cells["Time Spent"], "1h 31m")

    def test_xlsx_time_cells_are_forced_to_text(self) -> None:
        xlsx = export_report(self.db, self.cfg, D, D, open_folder=False, output_dir=self.out)
        wb = openpyxl.load_workbook(xlsx)

        self.assertEqual(wb["Daily Summary"]["C2"].number_format, "@")
        self.assertEqual(wb["Daily Summary"]["D2"].number_format, "@")
        self.assertEqual(wb["Daily Summary"]["D2"].value, "15:31:00")

        self.assertEqual(wb["App Usage"]["D2"].number_format, "@")
        self.assertEqual(wb["App Usage"]["D2"].value, "1h 31m")

        self.assertEqual(wb["Sessions Log"]["D2"].number_format, "@")
        self.assertEqual(wb["Sessions Log"]["E2"].number_format, "@")
        self.assertEqual(wb["Activity Timeline"]["D2"].number_format, "@")
        self.assertEqual(wb["Activity Timeline"]["E2"].number_format, "@")


if __name__ == "__main__":
    unittest.main()
