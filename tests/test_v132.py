"""Tests for the v1.3.2 hardening pass.

Covers automatic aggregation (item 1), the D3 overtime/meeting flow (item 2),
the pre-migration backup (item 3), the overtime prompt timeout (item 4) and the
live current-period comparison (item 5).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import time_tracker.core.database as database_module
from time_tracker.core.calculator import calculate_day_metrics, get_day_boundaries
from time_tracker.core.config import Config
from time_tracker.core.database import SCHEMA_VERSION, Database
from time_tracker.core.retention import (
    aggregate_missing_days,
    get_day_metrics,
    purge_old_data,
)
from time_tracker.core.tracker import ActivityTracker

WORK_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu"]


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, dt_time(hour, minute)).timestamp())


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


# ---------------------------------------------------------------------------
# Item 1: automatic aggregation
# ---------------------------------------------------------------------------

class TestAutomaticAggregation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "agg.db")
        self.config = Config(work_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.today = date.today()
        self.days = [self.today - timedelta(days=i) for i in range(1, 61)]
        for i, d in enumerate(self.days):
            # 2 h of work per day, 10 inserted events each.
            base = _ts(d, 10, 0)
            self.db.record_input_events_batch([base + minute * 60 for minute in range(0, 120, 12)])
            self.db.add_past_session("meeting", _ts(d, 11, 0), _ts(d, 11, 30), source="manual")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_every_day_gets_a_summary_and_matches_raw(self) -> None:
        raw = {
            d: get_day_metrics(self.db, self.config, d, ignore_summary=True)
            for d in self.days
        }
        result = aggregate_missing_days(self.db, self.config, today=self.today)
        self.assertEqual(result["aggregated"], len(self.days))

        for d in self.days:
            summary = self.db.get_daily_summary(d)
            self.assertIsNotNone(summary, d)
            assert summary is not None
            self.assertEqual(int(summary["worked_sec"]), raw[d].worked_seconds)
            self.assertEqual(int(summary["overtime_sec"]), raw[d].overtime_seconds)
            self.assertEqual(int(summary["meeting_sec"]), raw[d].meeting_seconds)
            self.assertEqual(int(summary["break_sec"]), raw[d].break_seconds)

    def test_second_run_changes_nothing(self) -> None:
        aggregate_missing_days(self.db, self.config, today=self.today)
        before = {d: self.db.get_daily_summary(d) for d in self.days}
        again = aggregate_missing_days(self.db, self.config, today=self.today)
        self.assertEqual(again["aggregated"], 0)
        after = {d: self.db.get_daily_summary(d) for d in self.days}
        for d in self.days:
            self.assertEqual(
                {k: v for k, v in before[d].items() if k != "updated_ts"},
                {k: v for k, v in after[d].items() if k != "updated_ts"},
            )

    def test_today_is_never_aggregated(self) -> None:
        self.db.record_input_event(_ts(self.today, 9, 0))
        aggregate_missing_days(self.db, self.config, today=self.today)
        self.assertIsNone(self.db.get_daily_summary(self.today))

    def test_stale_summary_is_refreshed(self) -> None:
        d = self.days[0]
        aggregate_missing_days(self.db, self.config, today=self.today)
        # Zero the row and backdate its updated_ts so it looks older than the data.
        self.db.upsert_daily_summary(
            summary_date=d, worked_sec=0, overtime_sec=0, meeting_sec=0,
            break_sec=0, target_sec=0,
        )
        self.db._execute_with_retry(
            lambda conn: conn.execute(
                "UPDATE daily_summary SET updated_ts = 1 WHERE date = ?", (d.isoformat(),)
            )
        )
        self.db.record_input_event(_ts(d, 16, 0))

        result = aggregate_missing_days(self.db, self.config, today=self.today)
        self.assertEqual(result["days"], [d.isoformat()])
        fresh = self.db.get_daily_summary(d) or {}
        self.assertGreater(int(fresh["worked_sec"]), 0)

    def test_purge_never_deletes_an_unaggregated_day(self) -> None:
        """If aggregation fails for a day, the purge stops before that day."""
        config = Config(retention_days=30, enable_retention_cleanup=True)
        victim = self.days[-1]  # oldest day
        with mock.patch(
            "time_tracker.core.retention.aggregate_day", side_effect=RuntimeError("boom")
        ):
            result = purge_old_data(self.db, config, today=self.today)

        self.assertIsNotNone(result["blocked_by"])
        # Raw events for the oldest day are still present.
        start, end = get_day_boundaries(victim)
        self.assertTrue(self.db.get_input_events_between(start, end))

    def test_dashboard_days_are_not_empty(self) -> None:
        from time_tracker.reporting.visualizer import build_dashboard_data

        aggregate_missing_days(self.db, self.config, today=self.today)
        data = build_dashboard_data(self.db, self.config, self.today - timedelta(days=20))
        day = data["days"][self.days[19].isoformat()]
        self.assertTrue(day.get("is_summarized"))
        # Work days (Sun-Thu by default) must not come back empty even though they
        # are older than the 14-day eager window and only exist as summaries now.
        working = [
            payload for key, payload in data["days"].items()
            if not payload.get("is_summarized") or (payload.get("worked_hours") or 0) > 0
        ]
        self.assertTrue(working)
        self.assertIn("worked_hours", day)


# ---------------------------------------------------------------------------
# Item 2: D3 meeting/overtime flow
# ---------------------------------------------------------------------------

class TestMeetingOvertimeFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "d3.db")
        self.prompts: list[dict[str, Any]] = []
        self.notifications: list[tuple[Any, ...]] = []

        def fake_prompt(message, on_response, lang="en", timeout_seconds=None):
            self.prompts.append(
                {"message": message, "on_response": on_response, "timeout_seconds": timeout_seconds}
            )

        patcher = mock.patch(
            "time_tracker.core.tracker.show_overtime_prompt_async", side_effect=fake_prompt
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.config = Config(
            work_start="09:30", work_end="17:30", work_days=WORK_DAYS,
            active_check_enabled=False, overtime_prompt_timeout_seconds=120,
        )
        # Saturday 10:00 — a non-work day.
        self.env = _MockEnv(datetime(2026, 11, 7, 10, 0, 0))
        self.tracker = self._make_tracker(self.env)

    def _make_tracker(self, env: _MockEnv) -> ActivityTracker:
        tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_active_dialog_fn=lambda **kwargs: None,
            get_last_input_tick_fn=env.get_last_input_tick,
            get_system_tick_count_fn=env.get_system_tick_count,
            is_locked_fn=env.is_workstation_locked,
            time_fn=env.get_time,
            now_dt_fn=env.get_dt,
        )
        tracker.on_notification = lambda *args: self.notifications.append(args)
        return tracker

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_weekend_meeting_prompts_for_overtime(self) -> None:
        self.tracker.toggle_meeting()
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.prompts[0]["timeout_seconds"], 120)

    def test_accepted_weekend_meeting_becomes_overtime(self) -> None:
        self.tracker.toggle_meeting()
        self.prompts[0]["on_response"](True, "Project Work")
        session = self.db.get_open_session("overtime")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session["reason"], "Project Work")

    def test_declined_weekend_meeting_is_reported(self) -> None:
        self.tracker.toggle_meeting()
        self.prompts[0]["on_response"](False, "")
        self.assertIsNone(self.db.get_open_session("overtime"))
        self.assertTrue(self.notifications)

    def test_work_day_meeting_inside_window_does_not_prompt(self) -> None:
        env = _MockEnv(datetime(2026, 11, 9, 10, 0, 0))
        tracker = self._make_tracker(env)
        tracker.toggle_meeting()
        self.assertEqual(self.prompts, [])

    def test_past_meeting_checkbox_adds_overtime(self) -> None:
        d = date(2026, 11, 7)
        self.tracker.add_past_meeting(
            d.isoformat(), "10:00", "12:00", "Weekend sync", True, "Urgent Emergency"
        )
        sessions = self.db.get_sessions_between(_ts(d, 0), _ts(d, 23, 59))
        types = {s["session_type"] for s in sessions}
        self.assertEqual(types, {"meeting", "overtime"})

    def test_past_meeting_without_checkbox_is_meeting_only(self) -> None:
        d = date(2026, 11, 7)
        self.tracker.add_past_meeting(d.isoformat(), "10:00", "12:00", "Weekend sync")
        sessions = self.db.get_sessions_between(_ts(d, 0), _ts(d, 23, 59))
        self.assertEqual([s["session_type"] for s in sessions], ["meeting"])


# ---------------------------------------------------------------------------
# Item 3: backup before migration
# ---------------------------------------------------------------------------

_V3_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE input_events (ts INTEGER PRIMARY KEY, app_name TEXT, source TEXT DEFAULT 'input', project_id INTEGER);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    session_type TEXT NOT NULL CHECK(session_type IN ('meeting', 'overtime', 'break')),
    source TEXT NOT NULL CHECK(source IN ('auto', 'tray', 'manual')),
    note TEXT, reason TEXT, is_pause INTEGER NOT NULL DEFAULT 0, project_id INTEGER
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE daily_summary (
    date TEXT PRIMARY KEY, worked_sec INTEGER NOT NULL DEFAULT 0,
    overtime_sec INTEGER NOT NULL DEFAULT 0, meeting_sec INTEGER NOT NULL DEFAULT 0,
    break_sec INTEGER NOT NULL DEFAULT 0, target_sec INTEGER NOT NULL DEFAULT 0,
    app_secs TEXT NOT NULL DEFAULT '{}', project_secs TEXT NOT NULL DEFAULT '{}',
    updated_ts INTEGER
);
"""


class TestMigrationBackup(unittest.TestCase):
    def _make_v3_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(_V3_SCHEMA)
            conn.execute("INSERT INTO schema_version (version) VALUES (3)")
            conn.execute("INSERT INTO input_events (ts, app_name) VALUES (?, 'chrome.exe')", (1_700_000_000,))
            conn.commit()
        finally:
            conn.close()

    def test_backup_written_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            self._make_v3_db(path)
            backups = Path(tmp) / "backups"
            with mock.patch.object(database_module, "get_backups_dir", return_value=backups):
                db = Database(path)
            self.assertEqual(
                int(db._execute_with_retry(lambda c: c.execute("SELECT version FROM schema_version").fetchone()[0])),
                SCHEMA_VERSION,
            )
            files = list(backups.glob("pre_migration_v3_to_v*_*.db"))
            self.assertEqual(len(files), 1)
            # The copy still holds the original data and can be opened.
            conn = sqlite3.connect(files[0])
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM input_events").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], 3)
            finally:
                conn.close()

    def test_failed_backup_aborts_the_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            self._make_v3_db(path)
            with mock.patch.object(
                database_module, "get_backups_dir", side_effect=OSError("no share")
            ):
                with self.assertRaises(RuntimeError):
                    Database(path)
            # The database is untouched: still schema version 3.
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM input_events").fetchone()[0], 1)
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Item 5: live current period
# ---------------------------------------------------------------------------

class TestLiveComparison(unittest.TestCase):
    def test_current_period_uses_live_values(self) -> None:
        from time_tracker.core.retention import build_comparison

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "live.db")
            cfg = Config()
            today = date.today()
            # Events a few minutes ago: a live calculation adds the trailing grace.
            now_ts = int(datetime.now().timestamp())
            db.record_input_event(now_ts - 120)
            db.record_input_event(now_ts - 60)

            live = build_comparison(db, cfg, today=today, is_live=True)
            strict = build_comparison(db, cfg, today=today, is_live=False)
            self.assertGreaterEqual(
                live["week"]["current"]["worked_sec"], strict["week"]["current"]["worked_sec"]
            )


if __name__ == "__main__":
    unittest.main()
