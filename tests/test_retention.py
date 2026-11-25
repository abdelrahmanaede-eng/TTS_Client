"""Tests for retention aggregation into daily_summary (Part A2)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

import openpyxl

from config import Config
from database import Database
from reporter import export_report
from retention import (
    get_day_app_usage,
    get_day_metrics,
    period_totals,
    purge_old_data,
    retention_cutoff_date,
)


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute)).timestamp())


class TestRetentionAggregation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db = Database(self.tmp_path / "data.db")
        self.config = Config(
            work_start="09:30",
            work_end="17:30",
            work_days=["Sun", "Mon", "Tue", "Wed", "Thu"],
            daily_target_hours=8.0,
            grace_period_seconds=180,
            enable_retention_cleanup=True,
            retention_days=30,
        )
        # "Today" is a Monday; the old day is 63 days earlier (also a Monday).
        self.today = date(2026, 11, 9)
        self.old_day = self.today - timedelta(days=63)
        self.assert_day_is_workday(self.old_day)

        # One hour of activity on the old day, 10:00-11:00.
        base = _ts(self.old_day, 10, 0)
        for i in range(61):
            self.db.record_input_event(base + i * 60, app_name="chrome.exe")
        # A 30-minute meeting on the old day, 12:00-12:30.
        self.db.add_past_session(
            session_type="meeting",
            start_ts=_ts(self.old_day, 12, 0),
            end_ts=_ts(self.old_day, 12, 30),
            source="manual",
            note="Client review",
        )
        # Some activity today so the database is not empty.
        self.db.record_input_event(_ts(self.today, 10, 0), app_name="word.exe")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def assert_day_is_workday(self, d: date) -> None:
        self.assertIn(d.strftime("%a"), self.config.work_days)

    def _snapshot(self) -> dict[str, int | float]:
        m = get_day_metrics(self.db, self.config, self.old_day)
        apps = {item.app_name: item.active_seconds for item in get_day_app_usage(self.db, self.config, self.old_day)}
        return {
            "worked": m.worked_seconds,
            "overtime": m.overtime_seconds,
            "meeting": m.meeting_seconds,
            "break": m.break_seconds,
            "target": m.target_seconds,
            "chrome": apps.get("chrome.exe", 0),
        }

    def test_cutoff_respects_configuration(self) -> None:
        self.assertEqual(retention_cutoff_date(self.config, self.today), self.today - timedelta(days=30))
        forever = Config(enable_retention_cleanup=True, retention_days=0)
        self.assertIsNone(retention_cutoff_date(forever, self.today))
        disabled = Config(enable_retention_cleanup=False, retention_days=30)
        self.assertIsNone(retention_cutoff_date(disabled, self.today))

    def test_totals_are_identical_before_and_after_purge(self) -> None:
        before = self._snapshot()
        self.assertGreater(before["worked"], 0)
        self.assertEqual(before["meeting"], 1800)

        result = purge_old_data(self.db, self.config, today=self.today)
        self.assertEqual(result["cutoff"], self.today - timedelta(days=30))
        self.assertGreaterEqual(result["aggregated"], 1)

        summary = self.db.get_daily_summary(self.old_day)
        self.assertIsNotNone(summary)

        # Raw events of the old day are gone, sessions (meetings) are kept.
        old_start, old_end = _ts(self.old_day, 0, 0), _ts(self.old_day, 23, 59)
        self.assertEqual(self.db.get_input_events_between(old_start, old_end), [])
        self.assertEqual(len(self.db.get_sessions_between(old_start, old_end)), 1)

        after = self._snapshot()
        self.assertEqual(before, after)

        # A second purge is a no-op and does not change the stored totals.
        second = purge_old_data(self.db, self.config, today=self.today)
        self.assertEqual(second["aggregated"], 0)
        self.assertEqual(self._snapshot(), before)

    def test_retention_zero_keeps_everything(self) -> None:
        config = Config(enable_retention_cleanup=True, retention_days=0)
        result = purge_old_data(self.db, config, today=self.today)
        self.assertEqual(result["deleted"], 0)
        old_start, old_end = _ts(self.old_day, 0, 0), _ts(self.old_day, 23, 59)
        self.assertGreater(len(self.db.get_input_events_between(old_start, old_end)), 0)

    def test_period_totals_use_summary_after_purge(self) -> None:
        before = period_totals(self.db, self.config, self.today - timedelta(days=90), self.today)
        purge_old_data(self.db, self.config, today=self.today)
        after = period_totals(self.db, self.config, self.today - timedelta(days=90), self.today)

        self.assertEqual(before.worked_sec, after.worked_sec)
        self.assertEqual(before.overtime_sec, after.overtime_sec)
        self.assertEqual(before.meeting_sec, after.meeting_sec)
        self.assertEqual(before.app_secs, after.app_secs)

    def test_excel_export_reads_purged_days_from_summary(self) -> None:
        before_metrics = get_day_metrics(self.db, self.config, self.old_day)
        purge_old_data(self.db, self.config, today=self.today)

        out_file = export_report(
            self.db,
            self.config,
            start_date=self.old_day,
            end_date=self.old_day,
            open_folder=False,
            output_dir=self.tmp_path,
        )
        wb = openpyxl.load_workbook(out_file)
        ws = wb["Daily Summary"]
        row = [cell.value for cell in ws[2]]
        self.assertEqual(row[0], self.old_day.isoformat())
        self.assertAlmostEqual(float(row[4]), before_metrics.worked_hours, places=2)
        self.assertAlmostEqual(float(row[5]), before_metrics.meeting_hours, places=2)
        # App usage still available from the aggregated summary.
        ws_app = wb["App Usage"]
        apps = [ws_app.cell(row=r, column=2).value for r in range(2, ws_app.max_row + 1)]
        self.assertIn("chrome.exe", apps)

    def test_recorded_dates_include_summaries(self) -> None:
        purge_old_data(self.db, self.config, today=self.today)
        dates = self.db.get_recorded_dates()
        self.assertIn(self.old_day.isoformat(), dates)


if __name__ == "__main__":
    unittest.main()
