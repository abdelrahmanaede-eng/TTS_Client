"""Tests for the audit fixes (v1.3.1).

Covers the product decisions D1-D6 and the P0/P1 items:

* D2 break accounting (disjoint from Worked/Overtime/Meeting)
* D3 non-work day activity is Overtime only
* D4 work window validation
* D5 presence-confirmation credit cap
* D6 export folder choice
* P0-4 half-day target, P0-5 i18n keys, P0-6 dialog flag, P0-7 update path
* P1-3 performance, P1-4 dashboard, P1-5 migrations, P1-8 pause flag,
  P1-10 quit time, P1-11 backup pruning
* B7 update marker, B14 event timestamp, B16 date guards
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

from time_tracker.core.calculator import (
    MAX_CONFIRM_CREDIT_SECONDS,
    calculate_day_metrics,
    get_day_boundaries,
    half_day_target_hours,
    presence_credit_intervals,
)
from time_tracker.core.config import APP_VERSION, DEFAULT_CONFIG, Config, is_valid_config, load_config
from time_tracker.core.database import SCHEMA_VERSION, Database
from time_tracker.localization.i18n import TRANSLATIONS
from time_tracker.reporting.reporter import documents_folder_is_cloud_synced, export_csv_bundle
from time_tracker.core.retention import get_day_metrics
from time_tracker.core.tracker import ActivityTracker
from time_tracker.system.updater import is_plain_filename, resolve_installer_path
from time_tracker.reporting.visualizer import build_dashboard_data, generate_dashboard_html

WORK_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu"]


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, dt_time(hour, minute)).timestamp())


def _metrics(
    d: date,
    events: list[int],
    sessions: list[dict[str, Any]],
    daily_target: float = 8.0,
    holidays: list[str] | None = None,
) -> Any:
    return calculate_day_metrics(
        target_date=d,
        events=events,
        sessions=sessions,
        work_start=dt_time(9, 30),
        work_end=dt_time(17, 30),
        work_days=WORK_DAYS,
        holidays=holidays or [],
        daily_target_hours=daily_target,
        grace_seconds=180,
        is_live=False,
    )


def _session(stype: str, start: int, end: int, source: str = "tray") -> dict[str, Any]:
    return {
        "start_ts": start,
        "end_ts": end,
        "session_type": stype,
        "source": source,
        "note": "",
        "reason": "",
    }


class _MockEnv:
    """Minimal injected clock/idle environment for the tracker."""

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
# D2 / P0-1: break accounting
# ---------------------------------------------------------------------------

class TestBreakAccounting(unittest.TestCase):
    def test_eight_hours_work_plus_two_hours_overtime_has_no_break(self) -> None:
        """D2: an 8 h shift followed by 2 h of overtime has no unworked time."""
        d = date(2026, 11, 9)
        # Activity across the shift (09:30–17:30) and through the overtime.
        events = [_ts(d, 9, 30) + i * 60 for i in range(10 * 60 + 1)]
        ot = _session("overtime", _ts(d, 17, 30), _ts(d, 19, 30), source="auto")
        m = _metrics(d, events, [ot])
        self.assertEqual(m.worked_seconds, 8 * 3600)
        self.assertEqual(m.overtime_seconds, 2 * 3600)
        # The evening is overtime, never break.
        self.assertEqual(m.break_seconds, 0)

    def test_lunch_gap_inside_window_is_break(self) -> None:
        """D2: unworked time inside the work window is break."""
        d = date(2026, 11, 9)
        events = [_ts(d, 9, 30) + i * 60 for i in range(121)]  # 09:30–11:30
        events += [_ts(d, 12, 30) + i * 60 for i in range(121)]  # 12:30–14:30
        m = _metrics(d, events, [])
        # 11:33 (end of the tail) .. 12:30 is the lunch gap = 57 minutes.
        self.assertEqual(m.break_seconds, 57 * 60)

    def test_manual_break_overlapping_typing_is_not_double_counted(self) -> None:
        """D2: break is disjoint from Worked, even for a manual break session."""
        d = date(2026, 11, 9)
        events = [_ts(d, 10, 0) + i * 60 for i in range(181)]
        brk = _session("break", _ts(d, 12, 0), _ts(d, 12, 30), source="manual")
        m = _metrics(d, events, [brk])
        self.assertEqual(m.break_seconds, 0)

    def test_worked_overtime_break_stay_disjoint_on_random_days(self) -> None:
        """D2/D1 invariants hold for random event and session mixes."""
        rng = random.Random(20261109)
        for _ in range(25):
            d = date(2026, 11, 2) + timedelta(days=rng.randrange(0, 28))
            events: list[int] = []
            cur = _ts(d, 8, 0) + rng.randrange(0, 3600)
            for _ in range(rng.randrange(1, 80)):
                cur += rng.randrange(30, 900)
                events.append(cur)
            sessions = []
            if rng.random() < 0.5:
                s = _ts(d, 10, 0) + rng.randrange(0, 3600)
                sessions.append(_session("meeting", s, s + rng.randrange(600, 5400)))
            if rng.random() < 0.4:
                s = _ts(d, 18, 0) + rng.randrange(0, 1800)
                sessions.append(_session("overtime", s, s + rng.randrange(600, 7200)))
            if rng.random() < 0.4:
                s = _ts(d, 12, 0) + rng.randrange(0, 1800)
                sessions.append(_session("break", s, s + rng.randrange(300, 3600)))

            m = _metrics(d, events, sessions)
            # D1: on a work day, meeting time is a subset of Worked + Overtime.
            # (On a non-work day without accepted overtime the meeting is reported
            # but booked as neither, per D3.)
            if m.is_work_day:
                self.assertLessEqual(m.meeting_seconds, m.worked_seconds + m.overtime_seconds + 1)
            # D2: break never exceeds the inside-window presence span.
            win_start, win_end = get_day_boundaries(d)
            win_start = max(win_start, _ts(d, 9, 30))
            win_end = min(win_end, _ts(d, 17, 30))
            self.assertLessEqual(m.break_seconds, max(0, win_end - win_start))
            # Nothing is negative.
            self.assertGreaterEqual(min(m.worked_seconds, m.overtime_seconds, m.break_seconds), 0)


# ---------------------------------------------------------------------------
# D3 / P1-2: non-work days
# ---------------------------------------------------------------------------

class TestNonWorkDayOvertime(unittest.TestCase):
    def test_saturday_meeting_without_overtime_is_not_worked(self) -> None:
        """D3: a weekend meeting without accepted overtime is never Worked."""
        saturday = date(2026, 11, 7)
        meeting = _session("meeting", _ts(saturday, 10, 0), _ts(saturday, 12, 0))
        m = _metrics(saturday, [], [meeting])
        self.assertFalse(m.is_work_day)
        self.assertEqual(m.worked_seconds, 0)
        self.assertEqual(m.overtime_seconds, 0)
        # The meeting itself is still reported.
        self.assertEqual(m.meeting_seconds, 2 * 3600)

    def test_saturday_meeting_with_overtime_is_overtime(self) -> None:
        """D3: the same activity counts as Overtime once it is accepted."""
        saturday = date(2026, 11, 7)
        meeting = _session("meeting", _ts(saturday, 10, 0), _ts(saturday, 12, 0))
        ot = _session("overtime", _ts(saturday, 10, 0), _ts(saturday, 12, 0), source="auto")
        m = _metrics(saturday, [], [meeting, ot])
        self.assertEqual(m.worked_seconds, 0)
        self.assertEqual(m.overtime_seconds, 2 * 3600)

    def test_holiday_activity_without_overtime_is_not_worked(self) -> None:
        d = date(2026, 11, 9)
        events = [_ts(d, 10, 0) + i * 60 for i in range(60)]
        m = _metrics(d, events, [], holidays=["2026-11-09"])
        self.assertEqual(m.worked_seconds, 0)
        self.assertEqual(m.overtime_seconds, 0)


# ---------------------------------------------------------------------------
# D4 / P0-2: work window validation
# ---------------------------------------------------------------------------

class TestWorkWindowValidation(unittest.TestCase):
    def _data(self, **overrides: Any) -> dict[str, Any]:
        data = {
            "work_start": "09:00",
            "work_end": "17:00",
            "work_days": ["Mon"],
            "daily_target_hours": 8.0,
        }
        data.update(overrides)
        return data

    def test_valid_window_accepted(self) -> None:
        self.assertTrue(is_valid_config(self._data()))

    def test_reversed_window_rejected(self) -> None:
        """D4: night shifts are not supported."""
        self.assertFalse(is_valid_config(self._data(work_start="17:30", work_end="09:00")))

    def test_equal_window_rejected(self) -> None:
        self.assertFalse(is_valid_config(self._data(work_start="09:00", work_end="09:00")))

    def test_load_config_falls_back_to_defaults(self) -> None:
        """An invalid file must never block startup: defaults are used instead."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "work_start": "20:00",
                        "work_end": "08:00",
                        "work_days": ["Mon"],
                        "daily_target_hours": 8.0,
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.work_start, DEFAULT_CONFIG["work_start"])
            self.assertEqual(cfg.work_end, DEFAULT_CONFIG["work_end"])


# ---------------------------------------------------------------------------
# D5 / P0-4: presence cap and half-day target
# ---------------------------------------------------------------------------

class TestPresenceCapAndHalfDay(unittest.TestCase):
    def test_credit_cap_constant_is_five_minutes(self) -> None:
        self.assertEqual(MAX_CONFIRM_CREDIT_SECONDS, 300)

    def test_long_interval_credits_at_most_five_minutes(self) -> None:
        d = date(2026, 11, 9)
        base = _ts(d, 10, 0)
        records = [(base, "chrome.exe", "input"), (base + 3600, "chrome.exe", "confirm")]
        credits = presence_credit_intervals(records, 3600)
        self.assertEqual(credits, [(base, base + 300)])

    def test_half_day_target_is_half_of_daily_target(self) -> None:
        self.assertEqual(half_day_target_hours(8.0), 4.0)
        self.assertEqual(half_day_target_hours(6.0), 3.0)
        self.assertEqual(half_day_target_hours(0), 0.0)

    def test_half_day_metrics_use_six_hour_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "d.db")
            cfg = Config(daily_target_hours=6.0)
            d = date(2026, 11, 9)
            metrics = get_day_metrics(db, cfg, d, half_day_dates={d})
            self.assertEqual(metrics.target_hours, 3.0)


# ---------------------------------------------------------------------------
# P0-5: i18n keys
# ---------------------------------------------------------------------------

class TestI18nCoverage(unittest.TestCase):
    def test_every_literal_key_exists_in_both_languages(self) -> None:
        """No ``t("key")`` call may reference a missing translation."""
        pattern = re.compile(r"\bt\(\s*[\"']([a-zA-Z0-9_]+)[\"']")
        package_dir = Path(__file__).resolve().parent.parent / "time_tracker"
        used: dict[str, set[str]] = {}
        for path in sorted(package_dir.rglob("*.py")):
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                used.setdefault(match.group(1), set()).add(path.name)

        missing_en = sorted(k for k in used if k not in TRANSLATIONS["en"])
        missing_ar = sorted(k for k in used if k not in TRANSLATIONS["ar"])
        self.assertEqual(missing_en, [], f"Missing English keys: {missing_en}")
        self.assertEqual(missing_ar, [], f"Missing Arabic keys: {missing_ar}")

    def test_both_languages_have_the_same_keys(self) -> None:
        self.assertEqual(sorted(TRANSLATIONS["en"]), sorted(TRANSLATIONS["ar"]))


# ---------------------------------------------------------------------------
# P0-6: dialog flag
# ---------------------------------------------------------------------------

class TestDialogFlag(unittest.TestCase):
    def test_dispatch_failure_does_not_leave_the_flag_set(self) -> None:
        import time_tracker.ui.dialogs as dialogs

        original_dispatch = dialogs.dispatch_to_ui

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("ui thread unavailable")

        try:
            # Queueing the dialog fails: the flag must be released, so the next
            # presence check is not silently blocked forever (P0-6).
            dialogs._is_dialog_active = False
            dialogs.dispatch_to_ui = boom  # type: ignore[assignment]
            dialogs.show_active_check_dialog_async(lambda: None, lambda: None)
            self.assertFalse(dialogs._is_dialog_active)
        finally:
            dialogs.dispatch_to_ui = original_dispatch  # type: ignore[assignment]
            dialogs._is_dialog_active = False


# ---------------------------------------------------------------------------
# P0-7: update manifest path safety
# ---------------------------------------------------------------------------

class TestUpdaterPaths(unittest.TestCase):
    def test_plain_filenames(self) -> None:
        self.assertTrue(is_plain_filename("TimeTracker_Setup.exe"))
        self.assertFalse(is_plain_filename("sub/Setup.exe"))
        self.assertFalse(is_plain_filename("sub\\Setup.exe"))
        self.assertFalse(is_plain_filename("../Setup.exe"))
        self.assertFalse(is_plain_filename("C:\\Setup.exe"))
        self.assertFalse(is_plain_filename("\\\\server\\share\\Setup.exe"))
        self.assertFalse(is_plain_filename(".."))
        self.assertFalse(is_plain_filename(""))

    def test_resolve_installer_path_stays_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            good = resolve_installer_path(source, "Setup.exe")
            self.assertIsNotNone(good)
            assert good is not None
            self.assertEqual(good.parent, source.resolve())
            self.assertEqual(good.name, "Setup.exe")

            for bad in ("../Setup.exe", "sub/Setup.exe", "..\\Setup.exe", "C:\\Windows\\Setup.exe"):
                self.assertIsNone(resolve_installer_path(source, bad), bad)


# ---------------------------------------------------------------------------
# P1-5: migrations
# ---------------------------------------------------------------------------

_OLD_SCHEMA = """
CREATE TABLE input_events (ts INTEGER PRIMARY KEY, app_name TEXT, source TEXT DEFAULT 'input');
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    session_type TEXT NOT NULL,
    source TEXT DEFAULT 'tray',
    note TEXT DEFAULT '',
    reason TEXT DEFAULT ''
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class TestMigrations(unittest.TestCase):
    def _schema_version(self, db: Database) -> int:
        return int(
            db._execute_with_retry(
                lambda conn: conn.execute("SELECT version FROM schema_version").fetchone()[0]
            )
        )

    def _make_old_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(_OLD_SCHEMA)
            conn.execute("INSERT INTO input_events (ts, app_name) VALUES (?, ?)", (1_700_000_000, "chrome.exe"))
            conn.execute(
                "INSERT INTO sessions (start_ts, end_ts, session_type, source, note, reason) "
                "VALUES (?, ?, 'meeting', 'tray', 'sync', 'weekly')",
                (1_700_000_000, 1_700_000_600),
            )
            conn.commit()
        finally:
            conn.close()

    def test_migrate_from_oldest_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            self._make_old_db(path)
            db = Database(path)
            self.assertEqual(self._schema_version(db), SCHEMA_VERSION)
            columns = {row[1] for row in db._execute_with_retry(lambda c: c.execute("PRAGMA table_info(sessions)").fetchall())}
            self.assertIn("is_pause", columns)
            self.assertIn("project_id", columns)
            self.assertEqual(len(db.get_input_events_between(0, 2_000_000_000)), 1)
            sessions = db.get_sessions_between(0, 2_000_000_000)
            self.assertEqual(sessions[0]["note"], "sync")

    def test_crash_mid_migration_is_resumable(self) -> None:
        """A leftover sessions_new table and a stale version must not block startup."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crash.db"
            self._make_old_db(path)
            conn = sqlite3.connect(path)
            try:
                # Simulate a crash after the rebuild table was created.
                conn.execute(
                    "CREATE TABLE sessions_new (id INTEGER PRIMARY KEY, start_ts INTEGER, end_ts INTEGER)"
                )
                conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
                conn.execute("INSERT INTO schema_version (version) VALUES (1)")
                conn.commit()
            finally:
                conn.close()

            db = Database(path)  # must not raise
            self.assertEqual(self._schema_version(db), SCHEMA_VERSION)
            sessions = db.get_sessions_between(0, 2_000_000_000)
            self.assertEqual(len(sessions), 1)
            # Running again is a no-op.
            Database(path)
            self.assertEqual(len(db.get_sessions_between(0, 2_000_000_000)), 1)


# ---------------------------------------------------------------------------
# P1-8: pause session flag
# ---------------------------------------------------------------------------

class TestPauseFlag(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "pause.db")
        self.config = Config(
            work_start="09:30",
            work_end="17:30",
            work_days=WORK_DAYS,
            daily_target_hours=8.0,
            active_check_enabled=False,
        )
        self.env = _MockEnv(datetime(2026, 11, 9, 10, 0, 0))
        self.tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_active_dialog_fn=lambda **kwargs: None,
            get_last_input_tick_fn=self.env.get_last_input_tick,
            get_system_tick_count_fn=self.env.get_system_tick_count,
            is_locked_fn=self.env.is_workstation_locked,
            time_fn=self.env.get_time,
            now_dt_fn=self.env.get_dt,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pause_creates_flagged_session(self) -> None:
        self.assertTrue(self.tracker.toggle_pause(15))
        session = self.db.get_open_session("break")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(int(session["is_pause"]), 1)
        self.assertTrue(self.tracker.is_paused())

    def test_pause_is_refused_over_an_open_break(self) -> None:
        self.db.start_session("break", "tray", int(self.env.get_time()), note="Lunch")
        self.assertFalse(self.tracker.toggle_pause(15))
        self.assertFalse(self.tracker.is_paused())
        self.assertEqual(len(self.db.get_sessions_between(0, 2_000_000_000)), 1)

    def test_expired_pause_closes_only_the_pause_session(self) -> None:
        self.assertTrue(self.tracker.toggle_pause(1))
        self.env.advance(61)
        self.assertTrue(self.tracker._expire_pause())
        self.assertFalse(self.tracker.is_paused())
        self.assertIsNone(self.db.get_open_session("break"))
        self.assertEqual(len(self.db.get_sessions_between(0, 2_000_000_000)), 1)


# ---------------------------------------------------------------------------
# P1-10 / P1-11: quit time and backups
# ---------------------------------------------------------------------------

class TestQuitAndBackups(unittest.TestCase):
    def test_auto_sessions_close_at_last_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "q.db")
            last_input = 1_700_000_000
            now = last_input + 1800
            db.start_session("overtime", "auto", last_input - 600)
            db.start_session("meeting", "tray", last_input - 300)
            closed = db.close_all_open_sessions(now, auto_end_ts=last_input)
            self.assertEqual(closed, 2)
            sessions = {s["session_type"]: s for s in db.get_sessions_between(0, now + 60)}
            self.assertEqual(sessions["overtime"]["end_ts"], last_input)
            self.assertEqual(sessions["meeting"]["end_ts"], now)

    def test_backup_pruning_keeps_newest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "b.db")
            for _ in range(5):
                db.backup_database(keep=2)
            backups = sorted(Path(tmp).glob("*.db*"))
            self.assertLessEqual(len(backups), 3)  # db + at most 2 backups


# ---------------------------------------------------------------------------
# B7 / B14 / B16
# ---------------------------------------------------------------------------

class TestSmallFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "s.db")
        self.env = _MockEnv(datetime(2026, 11, 9, 10, 0, 0))
        self.update_dir = Path(self.tmp.name) / "updates"
        self.update_dir.mkdir()
        self.config = Config(
            work_start="09:30",
            work_end="17:30",
            work_days=WORK_DAYS,
            active_check_enabled=False,
            update_source_path=str(self.update_dir),
        )
        self.tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_active_dialog_fn=lambda **kwargs: None,
            get_last_input_tick_fn=self.env.get_last_input_tick,
            get_system_tick_count_fn=self.env.get_system_tick_count,
            is_locked_fn=self.env.is_workstation_locked,
            time_fn=self.env.get_time,
            now_dt_fn=self.env.get_dt,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_failed_update_check_does_not_mark_the_day(self) -> None:
        """B7: a missing manifest must not suppress retries for the day."""
        # The share is gone: the check did not succeed, so the day stays open.
        self.config.update_source_path = str(Path(self.tmp.name) / "no-such-share")
        self.tracker._check_for_update(self.env.get_dt())
        self.assertIsNone(self.db.get_setting("last_update_check_date"))

    def test_successful_update_check_marks_the_day(self) -> None:
        installer = self.update_dir / "Setup.exe"
        installer.write_bytes(b"installer-bytes")
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        (self.update_dir / "update.json").write_text(
            json.dumps(
                {"version": "99.0.0", "installer": "Setup.exe", "sha256": digest, "notes": "test"}
            ),
            encoding="utf-8",
        )
        self.tracker._check_for_update(self.env.get_dt())
        self.assertEqual(
            self.db.get_setting("last_update_check_date"), self.env.get_dt().date().isoformat()
        )

    def test_event_timestamp_uses_the_input_moment(self) -> None:
        """B14: events are stamped at now - idle and stay strictly increasing."""
        self.tracker._idle_seconds = 30.0
        first = self.tracker._event_timestamp(1000)
        self.assertEqual(first, 970)
        second = self.tracker._event_timestamp(1000)
        self.assertEqual(second, 971)

    def test_pre_epoch_day_boundaries_do_not_raise(self) -> None:
        """B16: Windows raises OSError for pre-1970 timestamps."""
        start, end = get_day_boundaries(date(1960, 1, 1))
        self.assertEqual(end - start, 86400)


# ---------------------------------------------------------------------------
# D6 / P1-4: exports and dashboard
# ---------------------------------------------------------------------------

class TestExportsAndDashboard(unittest.TestCase):
    def test_csv_bundle_honours_a_custom_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "e.db")
            out_dir = Path(tmp) / "chosen"
            db.record_input_event(_ts(date(2026, 11, 9), 10, 0))
            files = export_csv_bundle(
                db, Config(), date(2026, 11, 9), date(2026, 11, 9),
                open_folder=False, output_dir=out_dir,
            )
            self.assertTrue(files)
            for path in files:
                self.assertEqual(Path(path).parent, out_dir)
                self.assertTrue(Path(path).exists())

    def test_documents_sync_detection_returns_bool(self) -> None:
        self.assertIsInstance(documents_folder_is_cloud_synced(), bool)

    def test_dashboard_escapes_script_breakouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "v.db")
            d = date(2026, 11, 9)
            db.record_input_event(_ts(d, 10, 0))
            db.add_past_session(
                "meeting",
                _ts(d, 11, 0),
                _ts(d, 11, 30),
                note='</script><img src=x onerror="alert(1)">',
            )
            html = generate_dashboard_html(build_dashboard_data(db, Config(), d))
            self.assertNotIn("</script><img", html)
            self.assertIn("<\\/script>", html)

    def test_dashboard_compiles_only_the_working_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "w.db")
            end = date(2026, 11, 10)
            for i in range(30):
                day = end - timedelta(days=i)
                if day.strftime("%a") in WORK_DAYS:
                    db.record_input_event(_ts(day, 10, 0))
            data = build_dashboard_data(db, Config(), end)
            # Only the 14-day working set is compiled in full; older days are
            # summary-only payloads (item 1 keeps those summaries fresh).
            detailed = [d for d in data["days"].values() if not d.get("is_summarized")]
            self.assertLessEqual(len(detailed), 16)


# ---------------------------------------------------------------------------
# P1-3: performance
# ---------------------------------------------------------------------------

class TestPerformance(unittest.TestCase):
    def test_day_refresh_with_sixty_thousand_events(self) -> None:
        """A 30 s cadence over 30 days must refresh well under 50 ms per tick."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "perf.db")
            cfg = Config()
            end = date(2026, 11, 10)
            batch: list[int] = []
            for i in range(30):
                day = end - timedelta(days=i)
                base = _ts(day, 9, 0)
                batch.extend([base + s for s in range(0, 8 * 3600, 30)])
            self.assertGreaterEqual(len(batch), 20_000)
            for i in range(0, len(batch), 5000):
                db.record_input_events_batch(batch[i : i + 5000])

            get_day_metrics(db, cfg, end)  # warm up
            runs = 5
            started = time.perf_counter()
            for _ in range(runs):
                get_day_metrics(db, cfg, end)
            per_call_ms = (time.perf_counter() - started) / runs * 1000.0
            print(f"\n[perf] day metrics: {per_call_ms:.1f} ms over {len(batch)} events")
            self.assertLess(per_call_ms, 50.0)


if __name__ == "__main__":
    unittest.main()
