"""End-to-end smoke test for TimeTracker v1.3.2 (item 9).

Uses a temporary data folder and a fake clock to simulate one working week
(typing, lunch gap, meeting, break, pause, presence confirmation, overtime
accepted and declined, a weekend meeting), then a crash + restart, a migration
from the v1.3.0 schema, retention + aggregation, Excel/CSV export and a
dashboard build.

It prints a PASS/FAIL table of the four invariants and exits non-zero when any
invariant fails. No GUI is created and nothing is written outside the temporary
folder.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import csv
import sqlite3
import sys
import tempfile
import time as time_module
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database as database_module  # noqa: E402
from calculator import get_day_boundaries  # noqa: E402
from config import Config  # noqa: E402
from database import SCHEMA_VERSION, Database  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from reporter import export_csv_bundle, export_report  # noqa: E402
from retention import (  # noqa: E402
    aggregate_missing_days,
    get_day_metrics,
    purge_old_data,
)
from tracker import ActivityTracker  # noqa: E402
from visualizer import build_dashboard_data  # noqa: E402

MONDAY = date(2026, 11, 2)
SATURDAY = date(2026, 11, 7)
TODAY = date(2026, 11, 11)
WORK_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


class Clock:
    """Minimal stand-in for the Windows clock the tracker reads."""

    def __init__(self, start: datetime) -> None:
        self.dt = start
        self.wall_time = start.timestamp()
        self.sys_ticks = int(self.wall_time * 1000) & 0xFFFFFFFF
        self.last_input_tick = self.sys_ticks
        self.locked = False

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
        return self.locked


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, dt_time(hour, minute))


def make_config(**overrides: Any) -> Config:
    base = dict(
        work_start="09:30",
        work_end="17:30",
        work_days=WORK_DAYS,
        holidays=[],
        language="en",
        enable_app_tracking=False,
        active_check_enabled=False,
        overtime_prompt_timeout_seconds=90,
        enable_retention_cleanup=False,
    )
    base.update(overrides)
    return Config(**base)


def build_tracker(db: Database, config: Config, clock: Clock, prompts: list) -> ActivityTracker:
    def fake_prompt(message, on_response, lang="en", timeout_seconds=None):
        prompts.append({"message": message, "on_response": on_response, "timeout_seconds": timeout_seconds})

    tracker = ActivityTracker(
        db=db,
        config=config,
        show_active_dialog_fn=lambda **kwargs: None,
        get_last_input_tick_fn=clock.get_last_input_tick,
        get_system_tick_count_fn=clock.get_system_tick_count,
        is_locked_fn=clock.is_workstation_locked,
        time_fn=clock.get_time,
        now_dt_fn=clock.get_dt,
    )
    tracker.on_notification = lambda *args: None
    return tracker


def simulate_day(tracker: ActivityTracker, clock: Clock, day: date, plan: dict[str, Any]) -> None:
    """Drive the tracker through one working day with a 5-minute tick."""
    clock.dt = _at(day, 9, 30)
    clock.wall_time = clock.dt.timestamp()
    clock.sys_ticks = int(clock.wall_time * 1000) & 0xFFFFFFFF
    clock.last_input_tick = clock.sys_ticks

    steps = int((8 * 60) / 5)  # 09:30 -> 17:30
    for step in range(steps):
        clock.advance(300, generate_input=True)
        now = clock.dt
        minute_of_day = now.hour * 60 + now.minute
        in_lunch = (12 * 60) <= minute_of_day < (12 * 60 + 45)
        if in_lunch:
            # No keyboard/mouse activity during lunch: the tracker sees an idle gap.
            clock.last_input_tick = clock.sys_ticks - 45 * 60 * 1000
        else:
            clock.last_input_tick = clock.sys_ticks

        with mock.patch.object(time_module, "time", clock.get_time):
            if plan.get("meeting_start") == now.time():
                tracker.toggle_meeting()
            if plan.get("meeting_end") == now.time():
                tracker.toggle_meeting()
            if plan.get("break_start") == now.time():
                tracker.toggle_break()
            if plan.get("break_end") == now.time():
                tracker.toggle_break()
            if plan.get("pause_at") == now.time():
                tracker.db.start_session("break", "tray", int(clock.wall_time), is_pause=True)
            if plan.get("pause_end") == now.time():
                tracker.db.end_session("break", int(clock.wall_time))
            if plan.get("confirm_at") == now.time():
                tracker.record_active_confirmation(int(clock.wall_time))
            tracker._tick(clock.get_time(), clock.get_dt())

    # Graceful shutdown of the simulated week: nothing may stay open.
    tracker.db.close_all_open_sessions(int(_at(day, 17, 30).timestamp()))


def simulate_week(db: Database, config: Config, prompts: list) -> Clock:
    clock = Clock(_at(MONDAY, 9, 30))
    tracker = build_tracker(db, config, clock, prompts)
    plans = {
        date(2026, 11, 2): {},
        date(2026, 11, 3): {  # meeting 13:00-14:00 + presence confirmation
            "meeting_start": dt_time(13, 0),
            "meeting_end": dt_time(14, 0),
            "confirm_at": dt_time(10, 30),
        },
        date(2026, 11, 4): {"break_start": dt_time(15, 0), "break_end": dt_time(15, 30)},
        date(2026, 11, 5): {"pause_at": dt_time(10, 0), "pause_end": dt_time(10, 15)},
        date(2026, 11, 6): {},
    }
    for day, plan in plans.items():
        simulate_day(tracker, clock, day, plan)
    return clock


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_week(tmp: Path) -> tuple[Database, Config, dict[str, Any]]:
    db = Database(tmp / "smoke.db")
    config = make_config()

    accepted: list[bool] = []

    def fake_prompt(message, on_response, lang="en", timeout_seconds=None):
        accepted.append(True)
        on_response(True, "Project Work")  # accept immediately

    with mock.patch("tracker.show_overtime_prompt_async", side_effect=fake_prompt), \
         mock.patch("tracker.show_meeting_end_note_dialog_async", lambda *a, **k: None):
        simulate_week(db, config, [])

    # Saturday meeting, accepted, then a second one declined.
    clock = Clock(_at(SATURDAY, 9, 30))
    tracker = build_tracker(db, config, clock, [])
    answers = [True, False]

    def sat_prompt(message, on_response, lang="en", timeout_seconds=None):
        on_response(answers.pop(0), "Project Work" if answers else "")

    with mock.patch("tracker.show_overtime_prompt_async", side_effect=sat_prompt), \
         mock.patch("tracker.show_meeting_end_note_dialog_async", lambda *a, **k: None):
        with mock.patch.object(time_module, "time", lambda: clock.wall_time):
            tracker.toggle_meeting()          # Saturday 09:30, accepted
        clock.dt = _at(SATURDAY, 13, 0)
        clock.wall_time = clock.dt.timestamp()
        with mock.patch.object(time_module, "time", lambda: clock.wall_time):
            tracker.toggle_meeting()          # closes it
            tracker.toggle_meeting()          # 13:00, declined
        # The declined meeting runs 13:00-13:30 and is closed without an overtime session.
        tracker.db.end_session("meeting", int(_at(SATURDAY, 13, 30).timestamp()))

    # End of the simulated week: leave nothing open (a clean shutdown).
    db.close_all_open_sessions(int(_at(SATURDAY, 17, 30).timestamp()))

    return db, config, {"accepted": accepted}


def step_crash_restart(db: Database) -> None:
    """Simulate a kill: leave a session open, reopen the database, clean up."""
    open_ts = int(_at(date(2026, 11, 6), 16, 0).timestamp())
    db.start_session("overtime", "tray", open_ts, reason="Crash Test")
    before = db.get_all_open_sessions()
    check("crash: session left open before restart", len(before) == 1, f"{len(before)} open")

    reopened = Database(db.db_path)
    closed = reopened.cleanup_orphan_sessions(int(_at(date(2026, 11, 9), 9, 0).timestamp()))
    after = reopened.get_all_open_sessions()
    check("restart: orphan sessions closed", closed >= 1 and not after, f"closed={closed}, open={len(after)}")


def step_migration(tmp: Path) -> None:
    schema = """
    CREATE TABLE schema_version (version INTEGER NOT NULL);
    CREATE TABLE input_events (ts INTEGER PRIMARY KEY, app_name TEXT, source TEXT DEFAULT 'input', project_id INTEGER);
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts INTEGER NOT NULL, end_ts INTEGER,
        session_type TEXT NOT NULL CHECK(session_type IN ('meeting','overtime','break')),
        source TEXT NOT NULL CHECK(source IN ('auto','tray','manual')),
        note TEXT, reason TEXT, is_pause INTEGER NOT NULL DEFAULT 0, project_id INTEGER);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE daily_summary (
        date TEXT PRIMARY KEY, worked_sec INTEGER NOT NULL DEFAULT 0,
        overtime_sec INTEGER NOT NULL DEFAULT 0, meeting_sec INTEGER NOT NULL DEFAULT 0,
        break_sec INTEGER NOT NULL DEFAULT 0, target_sec INTEGER NOT NULL DEFAULT 0,
        app_secs TEXT NOT NULL DEFAULT '{}', project_secs TEXT NOT NULL DEFAULT '{}', updated_ts INTEGER);
    """
    old_path = tmp / "v130.db"
    conn = sqlite3.connect(old_path)
    try:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_version (version) VALUES (3)")
        conn.execute("INSERT INTO input_events (ts, app_name) VALUES (?, 'word.exe')", (1_700_000_000,))
        conn.commit()
    finally:
        conn.close()

    backups = tmp / "backups"
    with mock.patch.object(database_module, "get_backups_dir", return_value=backups):
        upgraded = Database(old_path)
    version = upgraded._execute_with_retry(
        lambda c: c.execute("SELECT version FROM schema_version").fetchone()[0]
    )
    files = sorted(backups.glob("pre_migration_v3_to_v*.db"))
    check("migration: schema upgraded to current", int(version) == SCHEMA_VERSION, f"v{version}")
    check("migration: pre-migration backup written", len(files) == 1, f"{len(files)} file(s)")


def step_aggregate_and_retention(db: Database, config: Config, tmp: Path) -> dict[str, Any]:
    result = aggregate_missing_days(db, config, today=TODAY)
    # The tracker already aggregates the previous day on its day-change tick, so the
    # final pass only has to fill the days it has not seen yet.
    missing_days = [
        d.isoformat()
        for d in [MONDAY + timedelta(days=i) for i in range(5)]
        if db.get_daily_summary(d) is None
    ]
    check("aggregation: every simulated work day has a summary", not missing_days, str(missing_days))
    check("aggregation: final pass ran", result["aggregated"] >= 1, str(result))

    mismatches = []
    for day_iso in result["days"]:
        d = date.fromisoformat(day_iso)
        summary = db.get_daily_summary(d) or {}
        raw = get_day_metrics(db, config, d, ignore_summary=True)
        if int(summary.get("worked_sec", -1)) != raw.worked_seconds:
            mismatches.append((day_iso, summary.get("worked_sec"), raw.worked_seconds))
    check("aggregation: daily_summary equals raw recalculation", not mismatches, str(mismatches[:3]))

    again = aggregate_missing_days(db, config, today=TODAY)
    check("aggregation: idempotent second run", again["aggregated"] == 0, str(again))

    # Retention on a copy so the export below still has the week.
    purge_db = Database(tmp / "retention.db")
    purge_db._execute_with_retry(lambda c: None)
    for offset in range(1, 41):
        d = TODAY - timedelta(days=offset)
        base = int(_at(d, 10, 0).timestamp())
        purge_db.record_input_events_batch([base + m * 60 for m in range(0, 120, 12)])

    purge_cfg = make_config(enable_retention_cleanup=True, retention_days=30)
    purged = purge_old_data(purge_db, purge_cfg, today=TODAY)
    check("retention: purge aggregated before deleting", purged["aggregated"] >= 10, str(purged))
    check("retention: nothing blocked", purged.get("blocked_by") is None, str(purged.get("blocked_by")))

    missing = [
        (TODAY - timedelta(days=offset)).isoformat()
        for offset in range(1, 41)
        if (TODAY - timedelta(days=offset)) < purged["cutoff"]
        and purge_db.get_daily_summary(TODAY - timedelta(days=offset)) is None
    ]
    check("retention: every purged day kept a summary", not missing, str(missing[:3]))

    leftover = 0
    for offset in range(31, 41):
        d = TODAY - timedelta(days=offset)
        start, end = get_day_boundaries(d)
        leftover += len(purge_db.get_input_events_between(start, end))
    check("retention: raw events of purged days deleted", leftover == 0, f"{leftover} events left")

    return {"cutoff": purged["cutoff"]}


def step_exports_and_dashboard(db: Database, config: Config, tmp: Path) -> None:
    out = tmp / "reports"
    out.mkdir(parents=True, exist_ok=True)
    xlsx = export_report(
        db, config, start_date=MONDAY, end_date=date(2026, 11, 13),
        open_folder=False, output_dir=out,
    )
    csv_files = export_csv_bundle(
        db, config, start_date=MONDAY, end_date=date(2026, 11, 13),
        open_folder=False, output_dir=out,
    )
    check("export: Excel file written", xlsx.exists(), xlsx.name)
    check("export: CSV bundle written", len(csv_files) >= 1, f"{len(csv_files)} file(s)")

    wb = load_workbook(xlsx)
    ws = wb["Daily Summary"]
    # Sum the per-day rows; compare with the CSV TOTALS row for the same range.
    xlsx_sums = [0.0, 0.0, 0.0, 0.0]
    day_rows = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not isinstance(row[0], str) or not row[0][:4].isdigit():
            continue
        day_rows += 1
        for i, col in enumerate((4, 5, 6, 7)):
            try:
                xlsx_sums[i] += float(row[col] or 0)
            except (TypeError, ValueError, IndexError):
                pass
    xlsx_totals = [round(v, 2) for v in xlsx_sums]
    check("export: Excel daily summary has day rows", day_rows >= 5, f"{day_rows} rows")

    csv_totals: list[Any] = []
    summary_csv = next(p for p in csv_files if "DailySummary" in p.name)
    with open(summary_csv, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if row and row[0].strip().upper().startswith("TOTAL"):
                csv_totals = row
                break

    def num(values: list[Any], idx: int) -> float:
        try:
            return round(float(values[idx]), 2)
        except (IndexError, TypeError, ValueError):
            return -999.0

    csv_sums = [num(csv_totals, i) for i in (4, 5, 6, 7)]
    same = all(abs(a - b) < 0.02 for a, b in zip(xlsx_totals, csv_sums))
    check("export: Excel and CSV totals match", same, f"xlsx={xlsx_totals} csv={csv_sums}")

    data = build_dashboard_data(db, config, date(2026, 11, 13))
    empty_days = []
    for iso, payload in data["days"].items():
        day = date.fromisoformat(iso)
        start, end = get_day_boundaries(day)
        if not db.get_input_events_between(start, end):
            continue
        totals = (payload.get("worked_hours") or 0) + (payload.get("meeting_hours") or 0) \
            + (payload.get("overtime_hours") or 0)
        if totals <= 0:
            empty_days.append(iso)
    check("dashboard: no day with activity is empty", not empty_days, str(empty_days[:3]))
    check("dashboard: days payload present", len(data["days"]) >= 5, f"{len(data['days'])} days")


def step_invariants(db: Database, config: Config) -> None:
    days = [MONDAY + timedelta(days=i) for i in range(5)] + [SATURDAY]
    disjoint_ok, disjoint_detail = True, []
    negative_ok, negative_detail = True, []

    for d in days:
        m = get_day_metrics(db, config, d)
        if min(m.worked_seconds, m.overtime_seconds, m.break_seconds, m.meeting_seconds) < 0:
            negative_ok = False
            negative_detail.append(d.isoformat())
        span = 24 * 3600
        if m.worked_seconds + m.overtime_seconds > span:
            disjoint_ok = False
            disjoint_detail.append(f"{d}: worked+ot > 24h")
        if m.meeting_seconds > m.worked_seconds + m.overtime_seconds + m.unclassified_meeting_seconds:
            disjoint_ok = False
            disjoint_detail.append(f"{d}: meeting not covered")

    check("no negative durations", negative_ok, str(negative_detail))
    check("worked/overtime/break pairwise disjoint (meeting covered)", disjoint_ok, str(disjoint_detail))

    sessions = db.get_sessions_between(0, int(_at(date(2026, 11, 30), 0, 0).timestamp()))
    open_sessions = [s for s in sessions if s.get("end_ts") is None]
    check("no session without end after restart", not open_sessions, f"{len(open_sessions)} open")


def main() -> int:
    started = datetime.now()
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        db, config, info = step_week(tmp)
        step_invariants(db, config)
        step_crash_restart(db)
        step_migration(tmp)
        step_aggregate_and_retention(db, config, tmp)
        step_exports_and_dashboard(db, config, tmp)

    width = max(len(name) for name, _, _ in RESULTS)
    print("")
    print("TimeTracker v1.3.2 smoke test")
    print("=" * (width + 12))
    failed = 0
    for name, ok, detail in RESULTS:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{status}  {name.ljust(width)}  {detail}")
    print("=" * (width + 12))
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} checks passed in "
          f"{(datetime.now() - started).total_seconds():.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
