"""Unit tests for TimeTracker visual dashboard generator."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from config import Config
from database import Database
from visualizer import build_dashboard_data, generate_dashboard_html, open_dashboard


class TestVisualizer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(self.db_path)
        self.config = Config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_dashboard_data_structure(self):
        d = date(2026, 11, 9)  # Monday
        base_ts = int(datetime.combine(d, time(10, 0)).timestamp())

        # Insert some events with apps
        self.db.record_input_events_batch([
            (base_ts, "chrome.exe"),
            (base_ts + 60, "chrome.exe"),
            (base_ts + 120, "Code.exe"),
            (base_ts + 180, "Code.exe"),
        ])

        # Insert a meeting session
        self.db.add_past_session("meeting", base_ts + 300, base_ts + 600, note="Daily Sync")

        data = build_dashboard_data(self.db, self.config, target_date=d)

        # Check required fields
        self.assertIn("date_str", data)
        self.assertIn("worked_hours", data)
        self.assertIn("hourly", data)
        self.assertEqual(len(data["hourly"]), 24)
        self.assertIn("status_blocks", data)
        self.assertIn("app_blocks", data)
        self.assertIn("history_14", data)
        self.assertEqual(len(data["history_14"]), 14)
        self.assertIn("app_usage", data)
        self.assertIn("sessions", data)
        self.assertIn("days", data)
        self.assertIn("available_dates", data)
        self.assertIn(d.strftime("%Y-%m-%d"), data["days"])

        # Check meeting session exists in sessions
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["type"], "Meeting")
        self.assertEqual(data["sessions"][0]["note"], "Daily Sync")

        # Check timeline blocks have percentages
        for b in data["status_blocks"]:
            self.assertIn("start_pct", b)
            self.assertIn("width_pct", b)
            self.assertGreaterEqual(b["start_pct"], 0.0)
            self.assertLessEqual(b["start_pct"], 100.0)

        for b in data["app_blocks"]:
            self.assertIn("start_pct", b)
            self.assertIn("width_pct", b)
            self.assertIn("color", b)

    def test_generate_dashboard_html_contents(self):
        d = date(2026, 11, 9)
        data = build_dashboard_data(self.db, self.config, target_date=d)
        html = generate_dashboard_html(data)

        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("BUSSMA_TTS — Time Tracking System", html)
        self.assertIn("Interactive Work-Window Timeline", html)
        self.assertIn("Top Applications", html)
        self.assertIn("Hourly Activity Breakdown", html)
        self.assertIn("Recorded Sessions Log", html)
        self.assertIn("day-filter-bar", html)
        self.assertIn("date-picker-input", html)
        self.assertIn("date-dropdown", html)

    def test_open_dashboard_writes_file(self):
        out_file = open_dashboard(self.db, self.config, target_date=date(2026, 11, 9), open_browser=False)
        self.assertTrue(out_file.exists())
        self.assertGreater(out_file.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
