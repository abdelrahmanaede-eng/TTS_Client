"""Comprehensive integration tests for TimeTracker.

Covers all features across all development iterations:
  A. Calculation Scenarios (work day, holiday, overtime, meeting, break, half-day, midnight)
  B. Database Scenarios (past sessions, notes/reasons, backup, batch insert, schema migration)
  C. Config Scenarios (holidays, partial config, invalid times, round-trip save/load)
  D. Reporter Scenarios (meeting/break/overtime in Excel, sessions log, bar chart, totals)
  E. App Usage Scenarios (multiple apps, percentages, live vs strict)
  F. Interval Algebra Edge Cases (merge, clip, intersect, subtract)
  G. Grace Period Boundary Conditions
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

import openpyxl

from calculator import (
    calculate_active_seconds,
    calculate_app_usage,
    calculate_day_metrics,
    calculate_event_contributions,
    clip_intervals,
    events_to_active_intervals,
    get_day_boundaries,
    get_work_window_for_day,
    intersect_intervals,
    merge_intervals,
    split_session_by_days,
    subtract_intervals,
    total_duration_seconds,
)
from config import Config, load_config
from database import Database
from reporter import export_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _work_ts(d: date, hour: int, minute: int = 0) -> int:
    """Return a unix timestamp for a given date + hour:minute."""
    return int(datetime.combine(d, time(hour, minute)).timestamp())


WORKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu"]
WORK_START = time(9, 30)
WORK_END = time(17, 30)


# ===========================================================================
# A. Calculation Scenarios
# ===========================================================================

class TestCalculationScenarios(unittest.TestCase):
    """End-to-end calculation tests using calculate_day_metrics."""

    def _metrics(
        self,
        d: date,
        events: list[int],
        sessions: list[dict],
        daily_target: float = 8.0,
        holidays: list[str] | None = None,
        is_live: bool = False,
    ):
        return calculate_day_metrics(
            target_date=d,
            events=events,
            sessions=sessions,
            work_start=WORK_START,
            work_end=WORK_END,
            work_days=WORKDAYS,
            holidays=holidays or [],
            daily_target_hours=daily_target,
            grace_seconds=180,
            is_live=is_live,
        )

    def test_full_8h_workday(self):
        """8 hours of continuous events within work window → 8.0 worked, 0 remaining."""
        d = date(2026, 11, 9)  # Monday
        day_start, _ = get_day_boundaries(d)
        # Events every minute from 09:30 to 17:30 (480 intervals + 1 endpoint)
        start_offset = 9 * 3600 + 30 * 60
        events = [day_start + start_offset + i * 60 for i in range(481)]
        m = self._metrics(d, events, [])
        self.assertEqual(m.worked_hours, 8.0)
        self.assertEqual(m.remaining_hours, 0.0)
        self.assertEqual(m.overtime_hours, 0.0)

    def test_partial_5h_workday_3h_remaining(self):
        """5 hours of work inside the work window → 3h remaining."""
        d = date(2026, 11, 9)  # Monday
        day_start, _ = get_day_boundaries(d)
        # Events every minute from 10:00 to 15:00 (300 intervals)
        events = [day_start + 10 * 3600 + i * 60 for i in range(301)]
        m = self._metrics(d, events, [])
        # B12: the finished day keeps the 3-minute tail of its last event.
        self.assertEqual(m.worked_hours, 5.05)
        self.assertEqual(m.target_hours, 8.0)
        self.assertEqual(m.remaining_hours, 2.95)

    def test_holiday_zero_worked_zero_target(self):
        """Activity on a declared holiday → 0 worked, 0 target."""
        d = date(2026, 11, 9)  # Monday declared as holiday
        day_start, _ = get_day_boundaries(d)
        events = [day_start + 10 * 3600 + i * 60 for i in range(60)]
        m = self._metrics(d, events, [], holidays=["2026-11-09"])
        self.assertEqual(m.worked_hours, 0.0)
        self.assertEqual(m.target_hours, 0.0)
        self.assertFalse(m.is_work_day)

    def test_holiday_with_accepted_overtime(self):
        """Holiday + overtime session → 0 worked, N overtime."""
        d = date(2026, 11, 9)  # Monday declared as holiday
        day_start, _ = get_day_boundaries(d)
        # 2 hours of activity
        events = [day_start + 10 * 3600 + i * 60 for i in range(121)]
        ot_session = {
            "start_ts": day_start + 10 * 3600,
            "end_ts": day_start + 12 * 3600,
            "session_type": "overtime",
            "source": "auto",
        }
        m = self._metrics(d, events, [ot_session], holidays=["2026-11-09"])
        self.assertEqual(m.worked_hours, 0.0)
        self.assertEqual(m.overtime_hours, 2.0)

    def test_meeting_outside_hours_no_overtime_is_unclassified(self):
        """D3 (v1.3.2): a declined out-of-hours meeting is reported, not booked."""
        d = date(2026, 11, 9)  # Monday
        day_start, _ = get_day_boundaries(d)
        # Meeting 18:00 to 19:00 (after 17:30 work_end)
        meeting = {
            "start_ts": day_start + 18 * 3600,
            "end_ts": day_start + 19 * 3600,
            "session_type": "meeting",
            "source": "tray",
        }
        m = self._metrics(d, [], [meeting])
        # No accepted overtime: nothing is Worked, and the hour stays visible as
        # unclassified meeting time instead of disappearing.
        self.assertEqual(m.worked_hours, 0.0)
        self.assertEqual(m.overtime_hours, 0.0)
        self.assertEqual(m.meeting_hours, 1.0)
        self.assertEqual(m.unclassified_meeting_hours, 1.0)

    def test_break_session_duration_appears(self):
        """Manual break session duration shows up in break_seconds."""
        d = date(2026, 11, 9)  # Monday
        day_start, _ = get_day_boundaries(d)
        # 2 hours of activity 10:00–11:57; its 180 s tail ends exactly at 12:00.
        events = [day_start + 10 * 3600 + i * 60 for i in range(118)]
        # 30-min break 12:00–12:30, with no activity inside it
        brk = {
            "start_ts": day_start + 12 * 3600,
            "end_ts": day_start + 12 * 3600 + 30 * 60,
            "session_type": "break",
            "source": "tray",
        }
        m = self._metrics(d, events, [brk])
        # D2: the break is unworked time, so all 30 minutes are reported.
        self.assertEqual(m.break_seconds, 1800)
        self.assertEqual(m.worked_hours, 2.0)

    def test_half_day_target_4h(self):
        """Half-day mode: target is 4.0h, remaining computed correctly."""
        d = date(2026, 11, 9)
        day_start, _ = get_day_boundaries(d)
        # 3 hours of work
        events = [day_start + 10 * 3600 + i * 60 for i in range(181)]
        m = self._metrics(d, events, [], daily_target=4.0)
        # 3 h of activity plus the 3-minute day-close tail (B12).
        self.assertEqual(m.target_hours, 4.0)
        self.assertEqual(m.worked_hours, 3.05)
        self.assertEqual(m.remaining_hours, 0.95)

    def test_midnight_cross_session_splits_to_two_days(self):
        """Session crossing midnight (Mon 23:30 → Tue 01:30) splits into two day-segments."""
        mon = date(2026, 11, 9)
        mon_start, tue_start = get_day_boundaries(mon)
        s_start = mon_start + 23 * 3600 + 30 * 60  # Mon 23:30
        s_end = tue_start + 90 * 60                 # Tue 01:30

        splits = split_session_by_days(s_start, s_end)
        self.assertEqual(len(splits), 2)

        d1, (a1, b1) = splits[0]
        d2, (a2, b2) = splits[1]

        self.assertEqual(d1, mon)
        self.assertEqual(d2, date(2026, 11, 10))
        self.assertEqual(b1 - a1, 30 * 60)   # 30 min on Monday
        self.assertEqual(b2 - a2, 90 * 60)   # 90 min on Tuesday

    def test_overtime_on_work_day_outside_window(self):
        """Activity outside work hours on a work day counts as Overtime when session accepted."""
        d = date(2026, 11, 9)  # Monday
        day_start, _ = get_day_boundaries(d)
        # 1 hour of events from 18:00 to 19:00 (after 17:30 end)
        events = [day_start + 18 * 3600 + i * 60 for i in range(61)]
        ot_session = {
            "start_ts": day_start + 18 * 3600,
            "end_ts": day_start + 19 * 3600,
            "session_type": "overtime",
            "source": "auto",
        }
        m = self._metrics(d, events, [ot_session])
        self.assertEqual(m.worked_hours, 0.0)
        self.assertEqual(m.overtime_hours, 1.0)

    def test_no_double_counting_overlapping_active_and_meeting(self):
        """Active + overlapping meeting = union (no double count)."""
        d = date(2026, 11, 10)  # Tuesday
        day_start, _ = get_day_boundaries(d)
        # Active 10:00–10:30
        active_events = [day_start + 10 * 3600 + i * 60 for i in range(31)]
        # Meeting 10:15–11:00 (overlaps)
        meeting = {
            "start_ts": day_start + 10 * 3600 + 15 * 60,
            "end_ts": day_start + 11 * 3600,
            "session_type": "meeting",
            "source": "tray",
        }
        m = self._metrics(d, active_events, [meeting])
        # Union of [10:00-10:33] and [10:15-11:00] = [10:00-11:00] = 60 min
        self.assertEqual(m.worked_hours, 1.0)

    def test_friday_no_overtime_zero_worked(self):
        """Activity on Friday (non-work day) with no overtime session → 0 worked."""
        d = date(2026, 11, 13)  # Friday
        day_start, _ = get_day_boundaries(d)
        events = [day_start + 10 * 3600 + i * 60 for i in range(60)]
        m = self._metrics(d, events, [])
        self.assertEqual(m.worked_hours, 0.0)
        self.assertEqual(m.overtime_hours, 0.0)
        self.assertFalse(m.is_work_day)

    def test_remaining_cannot_go_negative(self):
        """Remaining hours must never be negative (clamped at 0)."""
        d = date(2026, 11, 9)
        day_start, _ = get_day_boundaries(d)
        # Full 8h + extra events (still inside work window, but target=4h)
        events = [day_start + 9 * 3600 + 30 * 60 + i * 60 for i in range(481)]
        m = self._metrics(d, events, [], daily_target=4.0)
        self.assertGreaterEqual(m.remaining_seconds, 0)
        self.assertEqual(m.remaining_hours, 0.0)


# ===========================================================================
# B. Grace Period Boundary Conditions
# ===========================================================================

class TestGracePeriodBoundaries(unittest.TestCase):
    """Detailed tests of the trailing grace period math."""

    def test_exact_grace_gap_contributes_grace(self):
        """Gap exactly equal to G → contribution is G."""
        # Events at 0 and G=180
        events = [0, 180]
        contribs = calculate_event_contributions(events, grace_seconds=180)
        self.assertEqual(contribs[0], 180)
        self.assertEqual(contribs[1], 0)

    def test_gap_larger_than_grace_capped(self):
        """Gap > G → contribution is capped at G."""
        events = [0, 400]  # gap = 400 > G=180
        contribs = calculate_event_contributions(events, grace_seconds=180)
        self.assertEqual(contribs[0], 180)  # capped at 180

    def test_gap_smaller_than_grace_uses_actual_gap(self):
        """Gap < G → contribution is the actual gap."""
        events = [0, 60]  # gap = 60 < G=180
        contribs = calculate_event_contributions(events, grace_seconds=180)
        self.assertEqual(contribs[0], 60)

    def test_single_event_strict_zero(self):
        """A single event with is_live=False contributes 0 (strict rule)."""
        self.assertEqual(calculate_active_seconds([1000], grace_seconds=180, is_live=False), 0)

    def test_single_event_live_adds_grace(self):
        """A single event with is_live=True contributes G (live display rule)."""
        self.assertEqual(calculate_active_seconds([1000], grace_seconds=180, is_live=True), 180)

    def test_empty_events_zero(self):
        """No events → 0 active seconds."""
        self.assertEqual(calculate_active_seconds([], grace_seconds=180), 0)

    def test_unsorted_events_sorted_internally(self):
        """Events passed in reverse order produce correct results after internal sort."""
        # Events at minute 5 and minute 0 (reversed)
        events = [5 * 60, 0]
        contribs = calculate_event_contributions(events, grace_seconds=180)
        # After sort: [0, 300] → gap=300 > 180 → contrib[0]=180, contrib[1]=0
        self.assertEqual(contribs[0], 180)
        self.assertEqual(contribs[1], 0)
        self.assertEqual(calculate_active_seconds(events, grace_seconds=180), 180)

    def test_reference_18min_exact(self):
        """The specific sequence [0,5,10,12,16,20,24,25] minutes = exactly 18 min active."""
        minutes = [0, 5, 10, 12, 16, 20, 24, 25]
        events_sec = [m * 60 for m in minutes]
        total = calculate_active_seconds(events_sec, grace_seconds=180, is_live=False)
        self.assertEqual(total, 18 * 60)


# ===========================================================================
# C. Interval Algebra Edge Cases
# ===========================================================================

class TestIntervalAlgebra(unittest.TestCase):
    """Tests for merge, clip, intersect, subtract operations."""

    def test_merge_disjoint(self):
        result = merge_intervals([(0, 10), (20, 30)])
        self.assertEqual(result, [(0, 10), (20, 30)])

    def test_merge_overlapping(self):
        result = merge_intervals([(0, 20), (10, 30)])
        self.assertEqual(result, [(0, 30)])

    def test_merge_adjacent(self):
        result = merge_intervals([(0, 10), (10, 20)])
        self.assertEqual(result, [(0, 20)])

    def test_merge_empty(self):
        self.assertEqual(merge_intervals([]), [])

    def test_clip_basic(self):
        result = clip_intervals([(0, 100)], 20, 80)
        self.assertEqual(result, [(20, 80)])

    def test_clip_outside(self):
        # Interval entirely outside clip window
        result = clip_intervals([(0, 10)], 20, 100)
        self.assertEqual(result, [])

    def test_intersect_basic(self):
        a = [(0, 50)]
        b = [(30, 80)]
        self.assertEqual(intersect_intervals(a, b), [(30, 50)])

    def test_intersect_no_overlap(self):
        self.assertEqual(intersect_intervals([(0, 10)], [(20, 30)]), [])

    def test_subtract_basic(self):
        result = subtract_intervals([(0, 100)], [(30, 60)])
        self.assertEqual(result, [(0, 30), (60, 100)])

    def test_subtract_complete_removal(self):
        result = subtract_intervals([(0, 50)], [(0, 50)])
        self.assertEqual(result, [])

    def test_total_duration(self):
        self.assertEqual(total_duration_seconds([(0, 60), (100, 160)]), 120)


# ===========================================================================
# D. Database Scenarios
# ===========================================================================

class TestDatabaseScenarios(unittest.TestCase):
    """Database functionality tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_past_session_retroactive(self):
        """add_past_session stores a completed session that appears in get_sessions_between."""
        past_id = self.db.add_past_session(
            session_type="meeting",
            start_ts=1000,
            end_ts=4600,
            source="manual",
            note="Retroactive client meeting",
            reason="",
        )
        self.assertGreater(past_id, 0)
        sessions = self.db.get_sessions_between(500, 5000)
        ids = [s["id"] for s in sessions]
        self.assertIn(past_id, ids)

    def test_session_note_and_reason_update(self):
        """update_session_note_or_reason correctly updates note and reason fields."""
        sid = self.db.add_past_session("meeting", 1000, 2000)
        self.db.update_session_note_or_reason(sid, note="Team standup", reason="schedule")
        sessions = self.db.get_sessions_between(500, 3000)
        s = next(s for s in sessions if s["id"] == sid)
        self.assertEqual(s["note"], "Team standup")
        self.assertEqual(s["reason"], "schedule")

    def test_database_backup_creates_readable_sqlite(self):
        """backup_database() produces a valid SQLite file that can be opened."""
        self.db.record_input_event(999)
        backup_path = self.db.backup_database(Path(self.tmp.name))
        self.assertTrue(backup_path.exists())
        self.assertGreater(backup_path.stat().st_size, 0)
        # Must be a valid SQLite3 database
        conn = sqlite3.connect(str(backup_path))
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        table_names = [t[0] for t in tables]
        self.assertIn("input_events", table_names)
        self.assertIn("sessions", table_names)

    def test_duplicate_input_events_ignored(self):
        """INSERT OR IGNORE ensures duplicate timestamps are silently dropped."""
        self.db.record_input_event(500)
        self.db.record_input_event(500)  # duplicate
        self.db.record_input_event(500)  # duplicate again
        events = self.db.get_input_events_between(0, 1000)
        self.assertEqual(events.count(500), 1)

    def test_break_session_stored_correctly(self):
        """session_type='break' is accepted by the current schema."""
        sid = self.db.start_session("break", "tray", 2000)
        self.assertGreater(sid, 0)
        open_s = self.db.get_open_session("break")
        self.assertIsNotNone(open_s)
        closed = self.db.end_session("break", end_ts=5000)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["session_type"], "break")

    def test_manual_source_accepted(self):
        """source='manual' is accepted by the current schema."""
        sid = self.db.add_past_session("overtime", 100, 200, source="manual")
        sessions = self.db.get_sessions_between(0, 300)
        s = next(s for s in sessions if s["id"] == sid)
        self.assertEqual(s["source"], "manual")

    def test_batch_insert_input_events(self):
        """record_input_events_batch inserts multiple events at once."""
        events_to_insert = [(1000, "chrome.exe"), (2000, "Code.exe"), (3000, "excel.exe")]
        inserted = self.db.record_input_events_batch(events_to_insert)
        result = self.db.get_input_events_between(500, 4000)
        self.assertEqual(sorted(result), [1000, 2000, 3000])

    def test_batch_insert_ignores_duplicates(self):
        """Batch insert with duplicate timestamps keeps only unique ones."""
        self.db.record_input_event(500)
        batch = [(500, "chrome.exe"), (600, "Code.exe")]
        self.db.record_input_events_batch(batch)
        events = self.db.get_input_events_between(0, 1000)
        self.assertEqual(events.count(500), 1)
        self.assertIn(600, events)

    def test_get_input_events_with_apps(self):
        """get_input_events_with_apps returns (ts, app_name) tuples."""
        self.db.record_input_events_batch([(100, "firefox.exe"), (200, "notepad.exe")])
        result = self.db.get_input_events_with_apps(0, 500)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], (100, "firefox.exe"))
        self.assertEqual(result[1], (200, "notepad.exe"))

    def test_close_all_open_sessions_on_quit(self):
        """close_all_open_sessions closes every open session at quit time."""
        self.db.start_session("meeting", "tray", 100)
        self.db.start_session("overtime", "auto", 200)
        count = self.db.close_all_open_sessions(end_ts=9999)
        self.assertEqual(count, 2)
        self.assertEqual(len(self.db.get_all_open_sessions()), 0)

    def test_settings_key_value_store(self):
        """get/set_setting correctly stores and retrieves values."""
        self.db.set_setting("last_export", "2026-11-09")
        val = self.db.get_setting("last_export")
        self.assertEqual(val, "2026-11-09")

    def test_settings_default_when_missing(self):
        """get_setting returns the default when key doesn't exist."""
        val = self.db.get_setting("nonexistent_key", default="fallback")
        self.assertEqual(val, "fallback")

    def test_schema_migration_adds_note_reason_to_old_db(self):
        """Opening a DB without note/reason columns triggers automatic migration."""
        old_db_path = Path(self.tmp.name) / "old.db"
        # Manually create old-style schema without note/reason
        conn = sqlite3.connect(str(old_db_path))
        conn.execute("""
            CREATE TABLE input_events (ts INTEGER PRIMARY KEY);
        """)
        conn.execute("""
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER,
                session_type TEXT NOT NULL,
                source TEXT NOT NULL
            );
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        conn.execute("INSERT INTO sessions (start_ts, session_type, source) VALUES (100, 'meeting', 'tray')")
        conn.commit()
        conn.close()

        # Opening with Database() should trigger migration silently
        db2 = Database(old_db_path)
        # Verify note column now exists
        conn2 = sqlite3.connect(str(old_db_path))
        cols = [row[1] for row in conn2.execute("PRAGMA table_info(sessions)").fetchall()]
        conn2.close()
        self.assertIn("note", cols)
        self.assertIn("reason", cols)
        # Verify existing data is preserved
        sessions = db2.get_sessions_between(0, 999)
        self.assertEqual(len(sessions), 1)


# ===========================================================================
# E. Config Scenarios
# ===========================================================================

class TestConfigScenarios(unittest.TestCase):
    """Configuration loading, validation, and round-trip tests."""

    def test_holidays_list_loaded_correctly(self):
        """Holidays list in config is preserved and loaded."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            data = {
                "work_start": "09:30",
                "work_end": "17:30",
                "work_days": ["Sun", "Mon", "Tue", "Wed", "Thu"],
                "daily_target_hours": 8.0,
                "holidays": ["2026-12-25", "2026-01-01"],
            }
            cfg_path.write_text(json.dumps(data), encoding="utf-8")
            cfg = load_config(cfg_path)
            self.assertIn("2026-12-25", cfg.holidays)
            self.assertIn("2026-01-01", cfg.holidays)

    def test_new_fields_filled_with_defaults_on_old_config(self):
        """Old config missing new fields gets defaults applied for them."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            # Minimal old-style config
            data = {
                "work_start": "09:30",
                "work_end": "17:30",
                "work_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                "daily_target_hours": 8.0,
            }
            cfg_path.write_text(json.dumps(data), encoding="utf-8")
            cfg = load_config(cfg_path)
            # New fields should be filled with defaults
            self.assertEqual(cfg.weekly_target_hours, 40.0)
            self.assertTrue(cfg.enable_end_of_day_notification)
            self.assertTrue(cfg.enable_auto_weekly_backup)
            self.assertEqual(cfg.grace_period_seconds, 180)

    def test_invalid_time_format_recreates_defaults(self):
        """Config with invalid work_start time (25:00) → defaults recreated."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            data = {
                "work_start": "25:00",  # Invalid!
                "work_end": "17:30",
                "work_days": ["Mon"],
                "daily_target_hours": 8.0,
            }
            cfg_path.write_text(json.dumps(data), encoding="utf-8")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.work_start, "09:30")  # reverted to default

    def test_round_trip_save_and_reload(self):
        """Config saved to disk and reloaded produces identical values."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            original = Config(
                work_start="08:00",
                work_end="16:00",
                work_days=["Mon", "Tue", "Wed"],
                daily_target_hours=7.5,
                weekly_target_hours=37.5,
                holidays=["2026-12-25"],
                enable_stretch_reminder=False,
                stretch_reminder_minutes=90,
            )
            original.save(cfg_path)
            reloaded = load_config(cfg_path)

            self.assertEqual(reloaded.work_start, "08:00")
            self.assertEqual(reloaded.work_end, "16:00")
            self.assertEqual(reloaded.work_days, ["Mon", "Tue", "Wed"])
            self.assertAlmostEqual(reloaded.daily_target_hours, 7.5)
            self.assertAlmostEqual(reloaded.weekly_target_hours, 37.5)
            self.assertIn("2026-12-25", reloaded.holidays)
            self.assertFalse(reloaded.enable_stretch_reminder)
            self.assertEqual(reloaded.stretch_reminder_minutes, 90)

    def test_empty_work_days_list_invalidates_config(self):
        """Config with empty work_days list → defaults recreated."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            data = {
                "work_start": "09:00",
                "work_end": "17:00",
                "work_days": [],  # empty!
                "daily_target_hours": 8.0,
            }
            cfg_path.write_text(json.dumps(data), encoding="utf-8")
            cfg = load_config(cfg_path)
            # Defaults should be restored
            self.assertNotEqual(cfg.work_days, [])

    def test_holiday_invalid_format_filtered_out(self):
        """Malformed holiday dates (bad format) are stripped during load."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            data = {
                "work_start": "09:30",
                "work_end": "17:30",
                "work_days": ["Mon"],
                "daily_target_hours": 8.0,
                "holidays": ["2026-12-25", "not-a-date", "25/12/2026"],
            }
            cfg_path.write_text(json.dumps(data), encoding="utf-8")
            cfg = load_config(cfg_path)
            self.assertIn("2026-12-25", cfg.holidays)
            self.assertNotIn("not-a-date", cfg.holidays)
            self.assertNotIn("25/12/2026", cfg.holidays)

    def test_work_start_time_property(self):
        """Config.work_start_time returns a correct time object."""
        cfg = Config(work_start="09:30")
        self.assertEqual(cfg.work_start_time, time(9, 30))

    def test_work_end_time_property(self):
        """Config.work_end_time returns a correct time object."""
        cfg = Config(work_end="17:30")
        self.assertEqual(cfg.work_end_time, time(17, 30))


# ===========================================================================
# F. Reporter Scenarios
# ===========================================================================

class TestReporterScenarios(unittest.TestCase):
    """Excel report generation tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "test.db"
        self.db = Database(self.db_path)
        self.config = Config()

    def tearDown(self):
        self.tmp.cleanup()

    def _export(self, d: date, end_date: date | None = None, half_day_dates=None) -> Path:
        return export_report(
            self.db,
            self.config,
            start_date=d,
            end_date=end_date or d,
            open_folder=False,
            output_dir=self.tmp_path,
            half_day_dates=half_day_dates,
        )

    def _base_ts(self, d: date, hour: int, minute: int = 0) -> int:
        return int(datetime.combine(d, time(hour, minute)).timestamp())

    def test_report_with_meeting_session(self):
        """Meeting session in DB → Meeting Hours column shows correct non-zero value."""
        d = date(2026, 11, 9)  # Monday
        base = self._base_ts(d, 10, 0)
        # 1 event so the day isn't empty
        self.db.record_input_event(base)
        # 30-min meeting 10:00–10:30
        self.db.add_past_session("meeting", base, base + 1800)
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        data_row = [cell.value for cell in ws[2]]
        # Meeting Hours = index 5
        self.assertEqual(data_row[5], 0.5)  # 30 min = 0.5h

    def test_report_with_break_session(self):
        """Break session in DB → Break Hours column shows non-zero value."""
        d = date(2026, 11, 9)  # Monday
        base = self._base_ts(d, 10, 0)
        self.db.record_input_event(base)
        # 1-hour break 11:00–12:00
        self.db.add_past_session("break", base + 3600, base + 7200)
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        data_row = [cell.value for cell in ws[2]]
        # Break Hours = index 6
        self.assertGreater(data_row[6], 0.0)

    def test_report_with_overtime_session(self):
        """Overtime session in DB → Overtime Hours column shows correct value."""
        d = date(2026, 11, 9)  # Monday (after-hours overtime)
        base = self._base_ts(d, 18, 0)  # 18:00
        events = [base + i * 60 for i in range(61)]  # 1 hour of events
        self.db.record_input_events_batch(events)
        # Overtime session 18:00–19:00
        self.db.add_past_session("overtime", base, base + 3600)
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        data_row = [cell.value for cell in ws[2]]
        # Overtime Hours = index 7
        self.assertEqual(data_row[7], 1.0)

    def test_sessions_log_sheet_has_note_and_reason(self):
        """Sessions with note and reason appear correctly in Sessions Log sheet."""
        d = date(2026, 11, 9)
        base = self._base_ts(d, 10, 0)
        self.db.record_input_event(base)
        sid = self.db.add_past_session("meeting", base, base + 3600,
                                        note="Weekly sync", reason="project")
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        ws_sess = wb["Sessions Log"]
        # Row 2 is first data row
        values = [cell.value for cell in ws_sess[2]]
        # Note = index 7, Reason = index 8
        self.assertEqual(values[7], "Weekly sync")
        self.assertEqual(values[8], "project")

    def test_report_skips_days_with_no_data(self):
        """B17: empty non-work days are skipped, like the CSV export does.

        An empty *work* day is still listed (it reports a full remaining target),
        so the row policy is identical for Excel and CSV.
        """
        d1 = date(2026, 11, 9)   # Monday — has data
        d2 = date(2026, 11, 13)  # Friday — empty and not a work day
        base = self._base_ts(d1, 10, 0)
        self.db.record_input_events_batch([base, base + 60, base + 120])
        out = export_report(
            self.db, self.config,
            start_date=d1, end_date=d2,
            open_folder=False, output_dir=self.tmp_path,
        )
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        # Monday has data; Tuesday–Thursday are work days and are listed with zero
        # hours; Friday is neither a work day nor has data, so it is skipped.
        dates = [ws.cell(row=r, column=1).value for r in range(2, 7)]
        self.assertEqual(
            dates, ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "Total"]
        )
        # The D1 note row keeps column A empty so the aggregator skips it.
        self.assertIsNone(ws.cell(row=8, column=1).value)
        self.assertIn("included in Worked", str(ws.cell(row=8, column=2).value))

    def test_half_day_in_report_shows_4h_target(self):
        """When half_day_dates is provided, Target in report shows 4.0h."""
        d = date(2026, 11, 9)
        base = self._base_ts(d, 10, 0)
        self.db.record_input_events_batch([base + i * 60 for i in range(60)])
        out = self._export(d, half_day_dates={d})
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        data_row = [cell.value for cell in ws[2]]
        self.assertEqual(data_row[8], 4.0)  # Target = 4.0h (half day)

    def test_barchart_added_to_summary_sheet(self):
        """BarChart object is embedded in the Daily Summary sheet."""
        d = date(2026, 11, 9)
        base = self._base_ts(d, 10, 0)
        self.db.record_input_events_batch([base + i * 60 for i in range(60)])
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        self.assertTrue(len(ws._charts) > 0, "Expected at least one chart in Daily Summary sheet")

    def test_multiple_days_total_row_sums_correctly(self):
        """Multi-day report total row correctly sums worked hours."""
        d1 = date(2026, 11, 9)   # Monday
        d2 = date(2026, 11, 10)  # Tuesday

        # 1h of events on each day
        for d in (d1, d2):
            base = self._base_ts(d, 10, 0)
            self.db.record_input_events_batch([base + i * 60 for i in range(61)])

        out = export_report(
            self.db, self.config,
            start_date=d1, end_date=d2,
            open_folder=False, output_dir=self.tmp_path,
        )
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        # 3 rows: header + d1 + d2 + totals = row 4
        total_row = [cell.value for cell in ws[4]]
        self.assertEqual(total_row[0], "Total")
        # 1h + 1h worked plus the 3-minute day-close tail of each day (B12)
        self.assertAlmostEqual(total_row[4], 2.1, places=1)

    def test_no_formula_strings_in_totals(self):
        """Totals row must contain static values, not Excel formula strings."""
        d = date(2026, 11, 9)
        base = self._base_ts(d, 10, 0)
        self.db.record_input_events_batch([base + i * 60 for i in range(60)])
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        totals_row_idx = ws.max_row
        for cell in ws[totals_row_idx]:
            if cell.value is not None:
                self.assertFalse(str(cell.value).startswith("="),
                                 f"Formula found in totals: {cell.value}")

    def test_report_three_sheets_always_present(self):
        """Report always includes 3 sheets: Daily Summary, App Usage, Sessions Log."""
        d = date(2026, 11, 9)
        base = self._base_ts(d, 10, 0)
        self.db.record_input_event(base)
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        self.assertIn("Daily Summary", wb.sheetnames)
        self.assertIn("App Usage", wb.sheetnames)
        self.assertIn("Sessions Log", wb.sheetnames)

    def test_first_last_activity_in_report(self):
        """First and Last Activity columns in report show correct times."""
        d = date(2026, 11, 9)
        base_start = self._base_ts(d, 10, 0)
        base_end = self._base_ts(d, 12, 0)
        # Events at exactly 10:00 and 12:00
        self.db.record_input_events_batch([base_start, base_end])
        out = self._export(d)
        wb = openpyxl.load_workbook(out)
        ws = wb["Daily Summary"]
        data_row = [cell.value for cell in ws[2]]
        self.assertEqual(data_row[2], "10:00:00")  # First Activity
        self.assertEqual(data_row[3], "12:00:00")  # Last Activity


# ===========================================================================
# G. App Usage Scenarios
# ===========================================================================

class TestAppUsageScenarios(unittest.TestCase):
    """Application usage tracking breakdown tests."""

    def test_multiple_apps_sorted_by_time_desc(self):
        """Three apps produce three usage items sorted by active time descending."""
        events = [
            (0 * 60, "chrome.exe"),
            (2 * 60, "chrome.exe"),
            (5 * 60, "Code.exe"),
            (7 * 60, "Code.exe"),
            (10 * 60, "excel.exe"),
            (12 * 60, "excel.exe"),
        ]
        usage = calculate_app_usage(events, grace_seconds=180, is_live=False)
        self.assertEqual(len(usage), 3)
        # First item should be highest time (chrome or Code, both 300s)
        self.assertGreaterEqual(usage[0].active_seconds, usage[1].active_seconds)
        self.assertGreaterEqual(usage[1].active_seconds, usage[2].active_seconds)

    def test_percentages_sum_to_100(self):
        """Sum of all app percentages is approximately 100%."""
        events = [
            (0 * 60, "app1"),
            (2 * 60, "app1"),
            (5 * 60, "app2"),
            (7 * 60, "app2"),
            (10 * 60, "app3"),
            (12 * 60, "app3"),
        ]
        usage = calculate_app_usage(events, grace_seconds=180, is_live=False)
        total_pct = sum(item.percentage for item in usage)
        self.assertAlmostEqual(total_pct, 100.0, delta=1.0)

    def test_live_vs_strict_last_event(self):
        """is_live=True adds grace to last app's time; strict adds 0 (excludes it)."""
        events = [(0, "chrome.exe"), (5 * 60, "Code.exe")]
        usage_strict = calculate_app_usage(events, grace_seconds=180, is_live=False)
        usage_live = calculate_app_usage(events, grace_seconds=180, is_live=True)

        # Strict: Code.exe gets 0 seconds (last event, no grace) → excluded from results
        strict_names = [u.app_name for u in usage_strict]
        self.assertNotIn("Code.exe", strict_names,
                         "Code.exe should be excluded in strict mode (0 seconds)")

        # Live: Code.exe gets grace (180s) → included in results
        live_code = next((u for u in usage_live if u.app_name == "Code.exe"), None)
        self.assertIsNotNone(live_code, "Code.exe should appear in live mode")
        self.assertEqual(live_code.active_seconds, 180)  # grace added

    def test_single_app_one_event_strict_zero(self):
        """Single event with one app and is_live=False → 0 app usage."""
        events = [(1000, "notepad.exe")]
        usage = calculate_app_usage(events, grace_seconds=180, is_live=False)
        self.assertEqual(len(usage), 0)

    def test_unknown_app_name_on_null(self):
        """None app_name falls back to 'Unknown'."""
        events = [(0, None), (60, None)]
        usage = calculate_app_usage(events, grace_seconds=180, is_live=False)
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].app_name, "Unknown")

    def test_app_usage_in_database(self):
        """App usage stored in database is correctly retrieved with get_input_events_with_apps."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            db.record_input_events_batch([
                (1000, "word.exe"),
                (2000, "excel.exe"),
                (3000, "word.exe"),
            ])
            result = db.get_input_events_with_apps(0, 5000)
            app_names = [r[1] for r in result]
            self.assertIn("word.exe", app_names)
            self.assertIn("excel.exe", app_names)

    def test_app_usage_zero_seconds_excluded(self):
        """Apps with 0 active seconds are excluded from results."""
        # Only one event per app, all at the end (strict=0 for last event)
        events = [(0, "app_a"), (200, "app_b")]
        usage = calculate_app_usage(events, grace_seconds=180, is_live=False)
        # app_a gets 180 (gap to app_b is 200 > 180 → capped at 180)
        # app_b gets 0 (last event, strict)
        app_map = {u.app_name: u for u in usage}
        self.assertIn("app_a", app_map)
        self.assertNotIn("app_b", app_map)  # excluded because 0 seconds


# ===========================================================================
# H. Work Window Detection
# ===========================================================================

class TestWorkWindowDetection(unittest.TestCase):
    """get_work_window_for_day tests for work days and holidays."""

    def test_work_day_returns_window(self):
        d = date(2026, 11, 9)  # Monday
        win = get_work_window_for_day(d, WORK_START, WORK_END, WORKDAYS)
        self.assertIsNotNone(win)

    def test_non_work_day_returns_none(self):
        d = date(2026, 11, 13)  # Friday
        win = get_work_window_for_day(d, WORK_START, WORK_END, WORKDAYS)
        self.assertIsNone(win)

    def test_holiday_on_work_day_returns_none(self):
        d = date(2026, 11, 9)  # Monday declared holiday
        win = get_work_window_for_day(d, WORK_START, WORK_END, WORKDAYS,
                                       holidays=["2026-11-09"])
        self.assertIsNone(win)

    def test_window_start_end_timestamps_correct(self):
        d = date(2026, 11, 9)
        win = get_work_window_for_day(d, WORK_START, WORK_END, WORKDAYS)
        self.assertIsNotNone(win)
        win_start, win_end = win
        expected_start = int(datetime.combine(d, WORK_START).timestamp())
        expected_end = int(datetime.combine(d, WORK_END).timestamp())
        self.assertEqual(win_start, expected_start)
        self.assertEqual(win_end, expected_end)


if __name__ == "__main__":
    unittest.main()
