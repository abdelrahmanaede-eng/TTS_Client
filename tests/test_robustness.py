"""Robustness and QA coverage (Part C2).

Covers day rollover during overtime, sleep/wake clock jumps, meetings that
overlap a break, orphan-session cleanup at startup, upgrades from the previous
database schema, retention aggregation for CSV export and CSV encoding.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

import openpyxl

from time_tracker.core.calculator import calculate_day_metrics
from time_tracker.core.config import Config
from time_tracker.core.database import Database
from time_tracker.reporting.reporter import export_csv_bundle, export_report
from time_tracker.core.tracker import ActivityTracker

WORK_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu"]


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute)).timestamp())


class _MockEnv:
    def __init__(self, start_dt: datetime) -> None:
        self.dt = start_dt
        self.wall_time = start_dt.timestamp()
        self.sys_ticks = int(self.wall_time * 1000) & 0xFFFFFFFF
        self.last_input_tick = self.sys_ticks
        self.is_locked = False

    def advance(self, seconds: float, generate_input: bool = False) -> None:
        self.wall_time += seconds
        self.dt += timedelta(seconds=seconds)
        self.sys_ticks = int(self.wall_time * 1000) & 0xFFFFFFFF
        if generate_input:
            self.last_input_tick = self.sys_ticks

    def get_time(self) -> float:
        return self.wall_time

    def get_dt(self) -> datetime:
        return self.dt

    def get_last_input_tick(self) -> int:
        return self.last_input_tick

    def get_system_tick_count(self) -> int:
        return self.sys_ticks

    def is_workstation_locked(self) -> bool:
        return self.is_locked


class _DummyThread:
    """Prevents the tracker loop from actually running during startup tests."""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        pass


class TestDayAndClockChanges(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp_dir.name) / "data.db")
        # Both Monday and Tuesday are work days, so the overtime state machine is
        # the only thing that closes the session across midnight.
        self.config = Config(
            work_start="09:30",
            work_end="17:30",
            work_days=["Mon", "Tue"] + WORK_DAYS,
            grace_period_seconds=180,
            active_check_enabled=False,
        )
        self.env = _MockEnv(datetime(2026, 11, 9, 23, 50, 0))  # Monday night
        self.tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            get_last_input_tick_fn=self.env.get_last_input_tick,
            get_system_tick_count_fn=self.env.get_system_tick_count,
            is_locked_fn=self.env.is_workstation_locked,
            time_fn=self.env.get_time,
            now_dt_fn=self.env.get_dt,
        )
        # The tracker seeds its day from the real clock; align it with the mock.
        self.tracker._current_date = self.env.get_dt().date()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_overtime_session_closes_at_day_change(self) -> None:
        start_ts = int(self.env.get_time())
        self.db.start_session("overtime", "auto", start_ts, reason="Stayed late")

        self.tracker._tick()  # registers the day / keeps the open session
        open_session = self.db.get_open_session("overtime")
        self.assertIsNotNone(open_session)

        self.env.advance(11 * 60)  # crosses midnight
        self.tracker._tick()

        self.assertIsNone(self.db.get_open_session("overtime"))
        sessions = self.db.get_sessions_between(start_ts - 60, int(self.env.get_time()) + 60)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_type"], "overtime")
        self.assertLessEqual(sessions[0]["end_ts"], _ts(date(2026, 11, 10), 0, 1))

    def test_sleep_wake_clock_jump_closes_overtime_at_last_input(self) -> None:
        start_ts = int(self.env.get_time())
        self.db.start_session("overtime", "auto", start_ts, reason="Late work")
        self.tracker._tick()

        self.env.advance(400)  # no input for 6.7 minutes after wake-up
        self.tracker._tick()

        self.assertIsNone(self.db.get_open_session("overtime"))
        sessions = self.db.get_sessions_between(start_ts - 60, int(self.env.get_time()) + 300)
        self.assertEqual(sessions[0]["end_ts"], start_ts)


class TestMeetingOverlappingBreak(unittest.TestCase):
    def test_meeting_time_is_not_double_counted_in_break(self) -> None:
        d = date(2026, 11, 9)
        sessions = [
            {
                "start_ts": _ts(d, 10, 0),
                "end_ts": _ts(d, 10, 30),
                "session_type": "meeting",
                "source": "tray",
                "note": "",
                "reason": "",
            },
            {
                "start_ts": _ts(d, 10, 10),
                "end_ts": _ts(d, 10, 20),
                "session_type": "break",
                "source": "manual",
                "note": "",
                "reason": "",
            },
        ]
        m = calculate_day_metrics(
            target_date=d,
            events=[],
            sessions=sessions,
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=WORK_DAYS,
            holidays=[],
            daily_target_hours=8.0,
            grace_seconds=180,
            is_live=False,
        )
        self.assertEqual(m.meeting_seconds, 1800)
        # D2: break is disjoint from meeting time, and the whole manual break sits
        # inside the meeting, so nothing is reported as Break.
        self.assertEqual(m.break_seconds, 0)
        self.assertEqual(m.worked_seconds, 1800)
        self.assertEqual(m.overtime_seconds, 0)


class TestStartupCleanup(unittest.TestCase):
    def test_orphan_sessions_closed_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "data.db")
            old_start = int(datetime(2026, 11, 9, 9, 0).timestamp())
            db.start_session("meeting", "tray", old_start)
            db.start_session("overtime", "tray", old_start)
            self.assertIsNotNone(db.get_open_session("meeting"))

            tracker = ActivityTracker(db, Config(active_check_enabled=False))
            with mock.patch("time_tracker.core.tracker.threading.Thread", _DummyThread):
                tracker.start()
                tracker.stop()

            self.assertIsNone(db.get_open_session("meeting"))
            self.assertIsNone(db.get_open_session("overtime"))
            sessions = db.get_sessions_between(old_start - 60, old_start + 60)
            self.assertEqual(len(sessions), 2)
            for session in sessions:
                self.assertIsNotNone(session["end_ts"])


class TestMigrationFromPreviousSchema(unittest.TestCase):
    """An old data.db must upgrade in place without losing any rows."""

    def _create_legacy_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE input_events (
                ts INTEGER PRIMARY KEY,
                app_name TEXT
            );
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER,
                session_type TEXT NOT NULL CHECK (session_type IN ('meeting', 'overtime')),
                source TEXT NOT NULL CHECK (source IN ('auto', 'tray'))
            );
            CREATE TABLE holidays (
                date TEXT PRIMARY KEY
            );
            """
        )
        conn.execute("INSERT INTO settings (key, value) VALUES ('language', 'ar')")
        conn.execute("INSERT INTO input_events (ts, app_name) VALUES (?, ?)", (1000, "chrome.exe"))
        conn.execute("INSERT INTO input_events (ts, app_name) VALUES (?, ?)", (1060, "excel.exe"))
        conn.execute(
            "INSERT INTO sessions (start_ts, end_ts, session_type, source) VALUES (?, ?, ?, ?)",
            (900, 1200, "meeting", "tray"),
        )
        conn.execute("INSERT INTO holidays (date) VALUES ('2026-01-07')")
        conn.commit()
        conn.close()

    def test_upgrade_preserves_data_and_adds_new_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.db"
            self._create_legacy_db(path)

            db = Database(path)

            # Old data survived.
            self.assertEqual(db.get_setting("language"), "ar")
            events = db.get_input_events_with_details(900, 2000)
            self.assertEqual([e[1] for e in events], ["chrome.exe", "excel.exe"])
            sessions = db.get_sessions_between(800, 2000)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_type"], "meeting")
            with closing(db._get_connection()) as conn:
                self.assertEqual([r[0] for r in conn.execute("SELECT date FROM holidays")], ["2026-01-07"])

            # New columns and tables are usable.
            with closing(db._get_connection()) as conn:
                event_columns = {row[1] for row in conn.execute("PRAGMA table_info(input_events)")}
                session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"app_name", "source", "project_id"} <= event_columns)
            self.assertTrue({"project_id", "note", "reason"} <= session_columns)
            self.assertTrue({"projects", "daily_summary"} <= tables)

            pid = db.add_project("Migration project")
            self.assertGreater(pid, 0)
            db.record_input_event(2000, app_name="word.exe", project_id=pid, source="confirm")
            details = db.get_input_events_with_details(1900, 2100)
            self.assertEqual(details[0][2], "confirm")
            self.assertEqual(details[0][3], pid)

            # The upgraded schema accepts break sessions and both sources.
            db.add_past_session("break", 2100, 2400, source="manual", note="Lunch")
            self.assertEqual(len(db.get_sessions_between(2000, 2500)), 1)

            # Upgrading twice is safe (idempotent).
            Database(path)


class TestCsvExport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db = Database(self.tmp_path / "data.db")
        self.config = Config(work_days=WORK_DAYS)
        self.day = date(2026, 11, 9)
        pid = self.db.add_project("مشروع ألف")
        base = _ts(self.day, 10, 0)
        self.db.record_input_event(base, app_name="برنامج.exe", project_id=pid)
        self.db.record_input_event(base + 300, app_name="برنامج.exe", project_id=pid)
        self.db.add_past_session("meeting", base + 600, base + 1200, note="اجتماع الفريق")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_csv_files_have_bom_and_utf8_arabic(self) -> None:
        files = export_csv_bundle(
            self.db, self.config, self.day, self.day,
            open_folder=False, output_dir=self.tmp_path,
        )
        names = sorted(p.name for p in files)
        for expected in ("DailySummary", "Sessions", "AppUsage", "Projects"):
            self.assertTrue(any(expected in name for name in names), f"missing {expected}: {names}")
        for path in files:
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{path.name} is missing the UTF-8 BOM")
            # Strict UTF-8 decoding must work and preserve Arabic text.
            text = raw.decode("utf-8-sig")
            self.assertNotIn("\ufffd", text)

        joined = "".join(p.read_text(encoding="utf-8-sig") for p in files)
        self.assertIn("مشروع ألف", joined)
        self.assertIn("برنامج.exe", joined)

    def test_excel_export_survives_arabic_names(self) -> None:
        out = export_report(
            self.db, self.config, self.day, self.day,
            open_folder=False, output_dir=self.tmp_path,
        )
        wb = openpyxl.load_workbook(out)
        self.assertIn("مشروع ألف", str([c.value for c in wb["Projects"][2]]))


if __name__ == "__main__":
    unittest.main()
