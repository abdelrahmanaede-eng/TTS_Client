"""Coverage for the standalone manager report aggregator (QUAL-08)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import openpyxl

from scripts.aggregate_reports import aggregate_employee_reports


HEADER = [
    "Date", "Day", "First", "Last", "Worked", "Meeting", "Break",
    "Overtime", "Target", "Remaining",
]


def make_report(path: Path, rows: list[list[object]], apps: list[list[object]] | None = None) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Daily Summary"
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    if apps is not None:
        ws_app = wb.create_sheet("App Usage")
        ws_app.append(["Date", "Application", "Active Hours", "Time Spent", "%"])
        for row in apps:
            ws_app.append(row)
    wb.save(path)
    wb.close()


class TestAggregateEmployeeReports(unittest.TestCase):
    def test_no_reports_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(aggregate_employee_reports(Path(tmp)))

    def test_malformed_row_is_skipped_without_orphan_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            make_report(
                folder / "TimeReport_alice.xlsx",
                [
                    ["2026-11-09", "Mon", "09:00", "17:00", 8.0, 1.0, 0.5, 0.0, 8.0, 0.0],
                    ["2026-11-10", "Tue", "09:00", "17:00", "not-a-number", 0, 0, 0, 8, 8],
                    ["2026-11-11", "Wed", "09:00", "17:00", 7.5, 0.0, 0.0, 0.5, 8.0, 0.5],
                ],
            )
            make_report(
                folder / "TimeReport_bob.xlsx",
                [["2026-11-09", "Mon", "09:00", "17:00", 6.0, 0.0, 0.0, 0.0, 8.0, 2.0]],
            )

            out = aggregate_employee_reports(folder, output_file=folder / "team.xlsx")
            assert out is not None and out.exists()
            wb = openpyxl.load_workbook(out, data_only=True)
            try:
                summary = list(wb["Team Summary"].values)
                # header + 2 employees + team total
                self.assertEqual(len(summary), 4)
                self.assertEqual(summary[1][0], "alice")
                self.assertAlmostEqual(summary[1][1], 15.5)  # bad row skipped
                self.assertEqual(summary[2][0], "bob")
                self.assertAlmostEqual(summary[2][1], 6.0)
                self.assertEqual(summary[3][0], "Team Total")
                self.assertAlmostEqual(summary[3][1], 21.5)

                daily = list(wb["All Employees Daily Log"].values)
                # header + 2 valid alice rows + 1 bob row
                self.assertEqual(len(daily), 4)
                self.assertFalse(any(r[1] == "2026-11-10" for r in daily[1:]))
            finally:
                wb.close()

    def test_app_usage_from_multiple_employees_is_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            make_report(
                folder / "TimeReport_a.xlsx",
                [["2026-11-09", "Mon", "", "", 8, 0, 0, 0, 8, 0]],
                [["2026-11-09", "chrome.exe", 3.0, "3h", "50%"]],
            )
            make_report(
                folder / "TimeReport_b.xlsx",
                [["2026-11-09", "Mon", "", "", 8, 0, 0, 0, 8, 0]],
                [["2026-11-09", "chrome.exe", 2.0, "2h", "40%"]],
            )
            out = aggregate_employee_reports(folder, output_file=folder / "team.xlsx")
            assert out is not None
            wb = openpyxl.load_workbook(out, data_only=True)
            try:
                apps = list(wb["Team App Usage"].values)
                self.assertEqual(apps[1][0], "chrome.exe")
                self.assertAlmostEqual(apps[1][1], 5.0)
            finally:
                wb.close()


if __name__ == "__main__":
    unittest.main()
