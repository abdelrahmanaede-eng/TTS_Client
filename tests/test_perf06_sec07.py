"""Tests for the final two open findings: PERF-06 and SEC-07 residual."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest import mock

import database as database_module
from config import Config
from database import Database
from visualizer import build_dashboard_data, generate_dashboard_html


def _ts(d: date, hour: int) -> int:
    return int(datetime.combine(d, time(hour, 0)).timestamp())


class TestPerf06PrecomputedDays(unittest.TestCase):
    def test_new_database_stores_and_indexes_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "p.db")
            d = date(2026, 11, 9)
            ts = _ts(d, 10)
            db.record_input_event(ts, app_name="chrome.exe")
            db.add_past_session("meeting", ts, ts + 3600)

            self.assertIn(d.isoformat(), db.get_recorded_dates())
            self.assertIn(d.isoformat(), db.get_last_data_change_by_day())

            conn = sqlite3.connect(db.db_path)
            try:
                event_day = conn.execute("SELECT day FROM input_events WHERE ts = ?", (ts,)).fetchone()[0]
                session_day = conn.execute("SELECT day, change_day FROM sessions").fetchone()
                indexes = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )}
            finally:
                conn.close()
            self.assertEqual(event_day, d.isoformat())
            self.assertEqual(session_day[0], d.isoformat())
            self.assertEqual(session_day[1], d.isoformat())
            self.assertIn("idx_events_day", indexes)
            self.assertIn("idx_sessions_change_day", indexes)

    def test_end_session_updates_change_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "e.db")
            d1, d2 = date(2026, 11, 9), date(2026, 11, 10)
            st = _ts(d1, 23)
            db.start_session("break", "tray", st)
            db.end_session("break", _ts(d2, 1))
            conn = sqlite3.connect(db.db_path)
            try:
                day, change_day = conn.execute("SELECT day, change_day FROM sessions").fetchone()
            finally:
                conn.close()
            self.assertEqual(day, d1.isoformat())
            self.assertEqual(change_day, d2.isoformat())
            self.assertIn(d2.isoformat(), db.get_last_data_change_by_day())

    def test_legacy_database_backfills_day_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE input_events (ts INTEGER PRIMARY KEY, app_name TEXT);
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_ts INTEGER NOT NULL,
                    end_ts INTEGER,
                    session_type TEXT NOT NULL CHECK (session_type IN ('meeting', 'overtime')),
                    source TEXT NOT NULL CHECK (source IN ('auto', 'tray'))
                );
                """
            )
            ts = _ts(date(2026, 11, 9), 10)
            conn.execute("INSERT INTO input_events (ts, app_name) VALUES (?, ?)", (ts, "chrome.exe"))
            conn.execute(
                "INSERT INTO sessions (start_ts, end_ts, session_type, source) VALUES (?, ?, ?, ?)",
                (ts, ts + 60, "meeting", "tray"),
            )
            conn.commit()
            conn.close()

            with mock.patch.object(database_module, "get_backups_dir", return_value=Path(tmp) / "b"):
                db = Database(path)

            self.assertIn(date(2026, 11, 9).isoformat(), db.get_recorded_dates())
            self.assertIn(date(2026, 11, 9).isoformat(), db.get_last_data_change_by_day())


class TestSec07Residual(unittest.TestCase):
    def test_initial_token_is_removed_from_history_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "v.db")
            d = date(2026, 11, 9)
            db.record_input_event(_ts(d, 10), app_name="chrome.exe")
            html = generate_dashboard_html(build_dashboard_data(db, Config(), d))
            self.assertIn("history.replaceState", html)
            self.assertIn("token=", html)  # the initial auth token itself
            self.assertNotIn("/api/data?token=", html)  # API uses Authorization now

        source = Path(__import__("visualizer").__file__).read_text(encoding="utf-8")
        self.assertIn('"token=" in a.lower()', source)
        self.assertNotIn('Access-Control-Allow-Origin", "*"', source)


if __name__ == "__main__":
    unittest.main()
