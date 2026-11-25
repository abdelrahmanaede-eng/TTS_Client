"""Unit tests for ActivityTracker and Cattr-style 'Are you active?' presence check."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from calculator import calculate_day_metrics, get_day_boundaries
from config import Config
from database import Database
from tracker import ActivityTracker


class MockClockEnv:
    """Controllable environment for time, system ticks, input ticks, and lock state."""

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


class TestTrackerActiveCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = Database(self.db_path)

        # The single-dialog flag lives in the dialogs module, so another test
        # module that exercised the real prompt can leave it set and silently
        # suppress the popup these tests assert on. Start every test clean.
        import dialogs

        dialogs._is_dialog_active = False
        dialogs._dialog_heartbeat_ts = 0.0

        # Work window 09:30 to 17:30 on Mondays
        self.config = Config(
            work_start="09:30",
            work_end="17:30",
            work_days=["Mon", "Tue", "Wed", "Thu", "Sun"],
            daily_target_hours=8.0,
            grace_period_seconds=180,
            active_check_enabled=True,
            active_check_interval_min=3,
        )

        # Start on Monday 2026-11-09 at 10:00:00 (inside working hours)
        self.env = MockClockEnv(datetime(2026, 11, 9, 10, 0, 0))
        self.popup_calls: list[dict[str, Any]] = []

        def mock_show_dialog(on_confirmed, on_timeout=None, timeout_seconds=60, lang="en"):
            self.popup_calls.append({
                "on_confirmed": on_confirmed,
                "on_timeout": on_timeout,
                "timeout_seconds": timeout_seconds,
                "lang": lang,
            })

        self.mock_show_dialog = mock_show_dialog

        self.tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_active_dialog_fn=self.mock_show_dialog,
            get_last_input_tick_fn=self.env.get_last_input_tick,
            get_system_tick_count_fn=self.env.get_system_tick_count,
            is_locked_fn=self.env.is_workstation_locked,
            time_fn=self.env.get_time,
            now_dt_fn=self.env.get_dt,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_no_input_requests_popup_and_unconfirmed_not_counted(self) -> None:
        """(a) no input for interval -> popup requested, no input after -> period not counted."""
        # 1. First tick at 10:00:00 with real input
        self.tracker._tick()
        events_start = self.db.get_input_events_between(0, 2000000000)
        self.assertEqual(len(events_start), 1)
        t0 = events_start[0]

        # 2. Advance time past interval (180s = 3 min) without any input
        self.env.advance(180.0, generate_input=False)
        self.tracker._tick()

        # Popup MUST be requested
        self.assertEqual(len(self.popup_calls), 1)
        popup = self.popup_calls[0]
        self.assertEqual(popup["timeout_seconds"], 60)

        # 3. User does nothing (popup times out)
        if popup["on_timeout"]:
            popup["on_timeout"]()

        # Tracker is now in idle mode
        self.assertTrue(self.tracker._active_check_idle_mode)
        self.assertEqual(self.tracker.current_status, "Idle")

        # No synthetic confirmation event recorded in database
        sources = self.db.get_input_events_with_sources(0, 2000000000)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0][2], "input")
        self.assertNotIn("confirm", [s[2] for s in sources])

        # Verify period is not counted: calculate completed day metrics (is_live=False)
        # Because no event confirmed t0, t0 contribution is 0
        d = self.env.get_dt().date()
        m = calculate_day_metrics(
            target_date=d,
            events=[t0],
            sessions=[],
            work_start=self.config.work_start_time,
            work_end=self.config.work_end_time,
            work_days=self.config.work_days,
            daily_target_hours=8.0,
            grace_seconds=180,
            is_live=False,
        )
        # B12: a finished day keeps the day-close tail of its last event.
        self.assertEqual(m.worked_seconds, 180)

        # 4. Advance another 3 minutes with no input: popup should NOT be requested again while idle
        self.env.advance(180.0, generate_input=False)
        self.tracker._tick()
        self.assertEqual(len(self.popup_calls), 1)

    def test_input_during_popup_counted_active_and_timer_reset(self) -> None:
        """(b) input during popup -> counted active and timer reset."""
        # 1. First tick at 10:00:00 with input
        self.tracker._tick()
        t0 = self.db.get_input_events_between(0, 2000000000)[0]

        # 2. Advance 180s with no input -> triggers popup
        self.env.advance(180.0, generate_input=False)
        self.tracker._tick()
        self.assertEqual(len(self.popup_calls), 1)
        popup = self.popup_calls[0]

        # 3. User interacts 5 seconds later during popup (button click or input auto-dismiss)
        self.env.advance(5.0, generate_input=True)
        popup["on_confirmed"]()

        # Synthetic event with source='confirm' MUST be recorded
        sources = self.db.get_input_events_with_sources(0, 2000000000)
        self.assertEqual(len(sources), 2)
        confirm_event = sources[1]
        self.assertEqual(confirm_event[2], "confirm")

        # Period [t0, t0 + 180] is now counted active!
        events = [s[0] for s in sources]
        d = self.env.get_dt().date()
        m = calculate_day_metrics(
            target_date=d,
            events=events,
            sessions=[],
            work_start=self.config.work_start_time,
            work_end=self.config.work_end_time,
            work_days=self.config.work_days,
            daily_target_hours=8.0,
            grace_seconds=180,
            is_live=False,
        )
        # Event 0 contributes min(180, 185) = 180s
        self.assertGreaterEqual(m.worked_seconds, 180)

        # Timer must be reset to confirm time
        self.assertFalse(self.tracker._active_check_idle_mode)
        self.assertAlmostEqual(self.tracker._active_check_cycle_start_ts, self.env.get_time(), delta=1.0)

        # Advance 60s -> elapsed (60s) < interval (180s) -> no new popup
        self.env.advance(60.0, generate_input=False)
        self.tracker._tick()
        self.assertEqual(len(self.popup_calls), 1)

    def test_no_popup_during_meeting_or_pause(self) -> None:
        """(c) no popup during meeting/pause."""
        self.tracker._tick()

        # Case 1: In Meeting
        self.tracker.toggle_meeting()
        self.assertTrue(self.tracker.is_meeting_active())

        # Advance 180s with no keyboard/mouse input
        self.env.advance(180.0, generate_input=False)
        self.tracker._tick()
        # Meeting suppresses popup!
        self.assertEqual(len(self.popup_calls), 0)

        # End meeting
        self.tracker.toggle_meeting()
        self.assertFalse(self.tracker.is_meeting_active())

        # Case 2: Paused
        self.tracker.toggle_pause(minutes=30)
        self.assertTrue(self.tracker.is_paused())

        # Advance another 180s with no input
        self.env.advance(180.0, generate_input=False)
        self.tracker._tick()
        # Pause suppresses popup!
        self.assertEqual(len(self.popup_calls), 0)

        # Resume tracking
        self.tracker.toggle_pause()
        self.assertFalse(self.tracker.is_paused())

    def test_interval_change_from_settings_applies_without_restart(self) -> None:
        """(d) interval change from Settings applies without restart."""
        # Start with interval = 10 minutes (600s)
        self.config.active_check_interval_min = 10
        self.tracker._tick()

        # Advance 4 minutes (240s) without input
        self.env.advance(240.0, generate_input=False)
        self.tracker._tick()
        # 240s < 600s -> no popup
        self.assertEqual(len(self.popup_calls), 0)

        # Settings dialog saves new config with interval = 3 minutes (180s)
        updated_config = Config(
            work_start="09:30",
            work_end="17:30",
            work_days=["Mon", "Tue", "Wed", "Thu", "Sun"],
            daily_target_hours=8.0,
            grace_period_seconds=180,
            active_check_enabled=True,
            active_check_interval_min=3,
        )
        self.tracker.config = updated_config

        # Next tick at 240s immediately recognizes elapsed (240s) >= 180s!
        self.tracker._tick()
        self.assertEqual(len(self.popup_calls), 1)

    def test_database_source_column_migration_and_upsert(self) -> None:
        """Test DB migration for source column and upsert behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            legacy_db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(legacy_db_path)
            conn.execute("""
                CREATE TABLE input_events (
                    ts INTEGER PRIMARY KEY,
                    app_name TEXT
                );
            """)
            conn.execute("INSERT INTO input_events (ts, app_name) VALUES (1000, 'notepad.exe')")
            conn.commit()
            conn.close()

            # Initialize Database manager -> runs automatic migration
            db = Database(legacy_db_path)
            conn2 = sqlite3.connect(legacy_db_path)
            cursor = conn2.cursor()
            cursor.execute("PRAGMA table_info(input_events)")
            cols = [col[1] for col in cursor.fetchall()]
            conn2.close()

            self.assertIn("source", cols)

            # Query existing row: should have default 'input'
            events_with_sources = db.get_input_events_with_sources(900, 1100)
            self.assertEqual(len(events_with_sources), 1)
            self.assertEqual(events_with_sources[0][2], "input")

            # Insert confirm event
            db.record_input_event(2000, "chrome.exe", source="confirm")
            res2 = db.get_input_events_with_sources(1900, 2100)
            self.assertEqual(res2[0][2], "confirm")

            # Trying to overwrite with 'input' on same ts should NOT overwrite 'confirm'
            db.record_input_event(2000, "chrome.exe", source="input")
            res3 = db.get_input_events_with_sources(1900, 2100)
            self.assertEqual(res3[0][2], "confirm")

            # Updating an 'input' event to 'confirm' DOES update
            db.record_input_event(3000, "excel.exe", source="input")
            self.assertEqual(db.get_input_events_with_sources(2900, 3100)[0][2], "input")
            db.record_input_event(3000, "excel.exe", source="confirm")
            self.assertEqual(db.get_input_events_with_sources(2900, 3100)[0][2], "confirm")


if __name__ == "__main__":
    unittest.main()
