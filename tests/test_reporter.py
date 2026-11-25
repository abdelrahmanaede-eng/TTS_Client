"""Unit tests for TimeTracker reporter."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import openpyxl

from config import Config
from database import Database
from reporter import export_report


class TestReporter(unittest.TestCase):
    def test_export_report_structure_and_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "data.db"
            db = Database(db_path)
            config = Config()

            # Record some input events on 2026-11-09 (Monday) inside work hours
            # 10:00 to 11:00 (1 hour = 3600s)
            import calendar
            from datetime import datetime, time
            d = date(2026, 11, 9)
            dt_start = datetime.combine(d, time(10, 0))
            base_ts = int(dt_start.timestamp())
            for i in range(61):
                db.record_input_event(base_ts + i * 60)

            # Export report
            out_file = export_report(
                db, config, start_date=d, end_date=d, open_folder=False, output_dir=Path(tmp_dir)
            )
            self.assertTrue(out_file.exists())

            # Inspect Excel contents
            wb = openpyxl.load_workbook(out_file)
            self.assertIn("Daily Summary", wb.sheetnames)
            self.assertIn("App Usage", wb.sheetnames)
            self.assertIn("Sessions Log", wb.sheetnames)
            ws = wb["Daily Summary"]
            ws_app = wb["App Usage"]
            ws_sess = wb["Sessions Log"]

            # Check header
            headers = [cell.value for cell in ws[1]]
            expected_headers = [
                "Date", "Day", "First Activity", "Last Activity",
                "Worked Hours", "Meeting Hours (included in Worked)", "Break Hours", "Overtime Hours", "Target", "Remaining",
                "Unclassified Meeting Hours",
            ]
            self.assertEqual(headers, expected_headers)
            self.assertEqual(ws.freeze_panes, "A2")

            # Check App Usage sheet
            app_headers = [cell.value for cell in ws_app[1]]
            expected_app_headers = [
                "Date", "Application", "Active Hours", "Time Spent", "% of Day Active Time"
            ]
            self.assertEqual(app_headers, expected_app_headers)
            self.assertEqual(ws_app.freeze_panes, "A2")

            # Check Sessions Log sheet
            sess_headers = [cell.value for cell in ws_sess[1]]
            expected_sess_headers = [
                "Date", "Type", "Source", "Start Time", "End Time", "Duration (Minutes)", "Duration (Hours)",
                "Note / Description", "Reason"
            ]
            self.assertEqual(sess_headers, expected_sess_headers)
            self.assertEqual(ws_sess.freeze_panes, "A2")

            # Check data row
            data_row = [cell.value for cell in ws[2]]
            self.assertEqual(data_row[0], "2026-11-09")
            self.assertEqual(data_row[1], "Mon")
            self.assertEqual(data_row[4], 1.05)  # Worked Hours (1 h + 3 min tail)
            self.assertEqual(data_row[5], 0.0)  # Meeting Hours
            self.assertEqual(data_row[6], 0.0)  # Break Hours
            self.assertEqual(data_row[7], 0.0)  # Overtime Hours
            self.assertEqual(data_row[8], 8.0)  # Target
            self.assertEqual(data_row[9], 6.95)  # Remaining

            # Check Totals row
            total_row = [cell.value for cell in ws[3]]
            self.assertEqual(total_row[0], "Total")
            self.assertEqual(total_row[4], 1.05)  # sum of worked
            self.assertEqual(total_row[5], 0.0)  # sum of meetings
            self.assertEqual(total_row[6], 0.0)  # sum of breaks
            self.assertEqual(total_row[7], 0.0)  # sum of overtime
            self.assertEqual(total_row[8], 8.0)
            self.assertEqual(total_row[9], 6.95)

            # Ensure static values, not formula strings
            for cell in ws[3]:
                if cell.value:
                    self.assertFalse(str(cell.value).startswith("="))

            # Check Chart existence and series (Worked, Overtime, Unworked)
            self.assertGreaterEqual(len(ws._charts), 1)
            chart = ws._charts[0]
            chart_title_str = str(chart.title.text.rich.p[0].r[0].t) if hasattr(chart.title, 'text') and hasattr(chart.title.text, 'rich') else str(chart.title)
            self.assertIn("Unworked", chart_title_str)
            self.assertEqual(len(chart.series), 3)

    def test_export_csv_report(self) -> None:
        import csv
        from datetime import datetime, time
        from reporter import export_csv_report

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "data.db"
            db = Database(db_path)
            config = Config()

            d = date(2026, 11, 9)
            dt_start = datetime.combine(d, time(10, 0))
            base_ts = int(dt_start.timestamp())
            for i in range(10):
                db.record_input_event(base_ts + i * 60)

            csv_file = export_csv_report(
                db, config, start_date=d, end_date=d, open_folder=False, output_dir=Path(tmp_dir)
            )
            self.assertTrue(csv_file.exists())

            with open(csv_file, encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
                self.assertGreater(len(reader), 2)
                headers = reader[0]
                self.assertIn("Worked Hours", headers)
                self.assertIn("Target Hours", headers)
                # Last non-empty row should be TOTALS
                totals_row = [r for r in reader if r and r[0] == "TOTALS"]
                self.assertEqual(len(totals_row), 1)


if __name__ == "__main__":
    unittest.main()
