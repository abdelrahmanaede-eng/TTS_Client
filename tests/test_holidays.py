"""Tests for the bundled Egyptian holiday data and import (Part B1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from time_tracker.localization.egypt_holidays import (
    count_new_holidays,
    get_egyptian_holidays,
    get_holidays_file,
    get_holiday_name,
    list_supported_years,
    load_holiday_data,
    merge_holidays,
)


class TestEgyptianHolidays(unittest.TestCase):
    def test_bundled_file_exists_and_is_valid_json(self) -> None:
        path = get_holidays_file()
        self.assertTrue(path.exists(), f"missing bundled data file: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("fixed", data)
        self.assertIn("years", data)

    def test_fixed_holidays_for_year(self) -> None:
        holidays = get_egyptian_holidays(2026)
        for expected in (
            "2026-01-07",
            "2026-01-25",
            "2026-04-25",
            "2026-05-01",
            "2026-06-30",
            "2026-07-23",
            "2026-10-06",
        ):
            self.assertIn(expected, holidays)
        # Movable holidays are stored per year.
        self.assertIn("2026-04-13", holidays)  # Sham El-Nessim
        self.assertEqual(holidays, sorted(set(holidays)))

    def test_year_without_movable_entries_still_has_fixed_dates(self) -> None:
        holidays = get_egyptian_holidays(2035)
        self.assertEqual(len(holidays), 7)
        self.assertIn("2035-01-07", holidays)
        self.assertTrue(all(h.startswith("2035-") for h in holidays))

    def test_fixed_holidays_recur_for_any_year(self) -> None:
        holidays = get_egyptian_holidays(1990)
        self.assertEqual(len(holidays), 7)
        self.assertTrue(all(h.startswith("1990-") for h in holidays))

    def test_supported_years_sorted(self) -> None:
        years = list_supported_years()
        self.assertIn(2026, years)
        self.assertEqual(years, sorted(years))

    def test_holiday_name_lookup(self) -> None:
        self.assertEqual(get_holiday_name("2026-01-07", "en"), "Coptic Christmas")
        self.assertEqual(get_holiday_name("2026-01-07", "ar"), "عيد الميلاد المجيد")
        self.assertEqual(get_holiday_name("2026-04-13", "en"), "Sham El-Nessim")
        self.assertEqual(get_holiday_name("2035-01-07", "en"), "Coptic Christmas")
        self.assertEqual(get_holiday_name("2026-05-26", "ar"), "يوم عرفة")
        # Custom / unknown dates simply have no label.
        self.assertIsNone(get_holiday_name("2026-02-14"))
        self.assertIsNone(get_holiday_name("not-a-date"))

    def test_merge_without_duplicates(self) -> None:
        existing = ["2026-01-01", "2026-01-07", "2026-04-13"]
        new = get_egyptian_holidays(2026)
        merged = merge_holidays(existing, new)
        self.assertEqual(len(merged), len(set(merged)))
        self.assertIn("2026-01-01", merged)  # user-added holiday is preserved
        self.assertIn("2026-01-07", merged)
        self.assertEqual(count_new_holidays(existing, new), len(set(new) - set(existing)))

    def test_merge_ignores_invalid_entries(self) -> None:
        merged = merge_holidays(["not-a-date", "2026-01-07"], ["2026-13-40"])
        self.assertEqual(merged, ["2026-01-07"])

    def test_custom_data_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "egypt_holidays.json"
            path.write_text(
                json.dumps(
                    {
                        "fixed": [{"month_day": "02-02"}],
                        "years": {"2026": [{"date": "2026-03-03"}, "2026-03-04", {"date": "2027-01-01"}]},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(get_egyptian_holidays(2026, path), ["2026-02-02", "2026-03-03", "2026-03-04"])
            self.assertEqual(load_holiday_data(path)["fixed"][0]["month_day"], "02-02")


if __name__ == "__main__":
    unittest.main()
