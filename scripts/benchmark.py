"""Measure the numbers quoted in README.md / docs/test_checklist.md (v1.3.2, item 3).

Prints a table with:
  (a) average and max CPU time per tracker tick with 60 000 stored events,
  (b) dashboard build time and HTML size for 90 days of history,
  (c) startup time from process start to the first tick,
  (d) process RAM after 5 minutes of simulated ticks (psutil, else "not measured").

Everything runs in a temporary folder; no real data is touched.

    python scripts/benchmark.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from time_tracker.core.config import Config  # noqa: E402
from time_tracker.core.database import Database  # noqa: E402
from time_tracker.core.retention import get_day_metrics  # noqa: E402
from time_tracker.reporting.visualizer import build_dashboard_data, generate_dashboard_html  # noqa: E402

PROCESS_START = time.perf_counter()
TOTAL_EVENTS = 60_000
HISTORY_DAYS = 90


class Clock:
    def __init__(self, start: datetime) -> None:
        self.dt = start
        self.wall_time = start.timestamp()
        self.sys_ticks = int(self.wall_time * 1000) & 0xFFFFFFFF
        self.last_input_tick = self.sys_ticks

    def advance(self, seconds: float) -> None:
        self.wall_time += seconds
        self.dt += timedelta(seconds=seconds)
        self.sys_ticks = int(self.wall_time * 1000) & 0xFFFFFFFF
        self.last_input_tick = self.sys_ticks

    def get_last_input_tick(self) -> int:
        return self.last_input_tick

    def get_system_tick_count(self) -> int:
        return self.sys_ticks

    def get_time(self) -> float:
        return self.wall_time

    def get_dt(self) -> datetime:
        return self.dt


def seed(db: Database, config: Config) -> date:
    """Insert 60 000 events spread over 90 days, ending yesterday."""
    today = date.today()
    start = today - timedelta(days=HISTORY_DAYS)
    per_day = TOTAL_EVENTS // (HISTORY_DAYS - 1)
    for day_index in range(HISTORY_DAYS - 1):
        day = start + timedelta(days=day_index)
        base = int(datetime.combine(day, datetime.min.time()).timestamp()) + 9 * 3600
        db.record_input_events_batch([base + i * 8 for i in range(per_day)])
        if day_index % 7 == 1:
            db.add_past_session(
                "meeting", base + 3 * 3600, base + 4 * 3600, source="manual"
            )
    return today


def bench_tick(db: Database, config: Config, day: date) -> tuple[float, float]:
    """CPU seconds per metrics refresh (the work a 5 s tick does)."""
    samples: list[float] = []
    for _ in range(40):
        started = time.perf_counter()
        get_day_metrics(db, config, day)
        samples.append(time.perf_counter() - started)
    return sum(samples) / len(samples), max(samples)


def bench_dashboard(db: Database, config: Config, day: date) -> tuple[float, float, int, str]:
    # Cold: includes the aggregation pass that fills daily_summary for 90 days.
    started = time.perf_counter()
    data = build_dashboard_data(db, config, day)
    cold_seconds = time.perf_counter() - started
    # Warm: summaries are fresh, so this is the steady-state repeat build.
    started = time.perf_counter()
    data = build_dashboard_data(db, config, day)
    warm_seconds = time.perf_counter() - started
    html = generate_dashboard_html(data)
    return cold_seconds, warm_seconds, len(html.encode("utf-8")), html


def bench_startup(tmp: Path) -> float:
    """Database open + first tracker tick on a fresh data folder."""
    path = tmp / "startup.db"
    started = time.perf_counter()
    db = Database(path)
    from time_tracker.core.tracker import ActivityTracker

    clock = Clock(datetime.now())
    tracker = ActivityTracker(
        db=db,
        config=Config(),
        show_active_dialog_fn=lambda **kwargs: None,
        get_last_input_tick_fn=clock.get_last_input_tick,
        get_system_tick_count_fn=clock.get_system_tick_count,
        is_locked_fn=lambda: False,
        time_fn=clock.get_time,
        now_dt_fn=clock.get_dt,
    )
    tracker.on_notification = lambda *args: None
    tracker._tick(clock.get_time(), clock.get_dt())
    return time.perf_counter() - started


def bench_ram(db: Database, config: Config, day: date) -> str:
    """RSS after ~5 minutes of simulated 5 s ticks (60 ticks)."""
    try:
        import psutil  # type: ignore
    except Exception:
        return "not measured (psutil not installed)"
    process = psutil.Process(os.getpid())
    from time_tracker.core.tracker import ActivityTracker

    clock = Clock(datetime.now())
    tracker = ActivityTracker(
        db=db,
        config=config,
        show_active_dialog_fn=lambda **kwargs: None,
        get_last_input_tick_fn=clock.get_last_input_tick,
        get_system_tick_count_fn=clock.get_system_tick_count,
        is_locked_fn=lambda: False,
        time_fn=clock.get_time,
        now_dt_fn=clock.get_dt,
    )
    tracker.on_notification = lambda *args: None
    for _ in range(60):
        clock.advance(5)
        tracker._tick(clock.get_time(), clock.get_dt())
    rss_mb = process.memory_info().rss / (1024 * 1024)
    return f"{rss_mb:.1f} MB RSS"


def main() -> int:
    tmp_dir = tempfile.TemporaryDirectory()
    tmp = Path(tmp_dir.name)
    db = Database(tmp / "bench.db")
    config = Config()
    day = seed(db, config)

    avg_tick, max_tick = bench_tick(db, config, day)
    build_cold, build_warm, html_bytes, _html = bench_dashboard(db, config, day)
    startup = bench_startup(tmp)
    ram = bench_ram(db, config, day)
    elapsed_total = time.perf_counter() - PROCESS_START

    rows: list[tuple[str, str]] = [
        ("(a) tick CPU, average", f"{avg_tick * 1000:.2f} ms"),
        ("(a) tick CPU, max", f"{max_tick * 1000:.2f} ms"),
        ("(b) dashboard build, 90 days (first)", f"{build_cold:.2f} s"),
        ("(b) dashboard build, 90 days (repeat)", f"{build_warm:.2f} s"),
        ("(b) dashboard HTML size", f"{html_bytes / (1024 * 1024):.2f} MiB"),
        ("(c) startup to first tick", f"{startup * 1000:.0f} ms"),
        ("(d) process RAM after 5 min", ram),
        ("total benchmark wall time", f"{elapsed_total:.1f} s"),
    ]

    width = max(len(label) for label, _ in rows)
    print("")
    print("TimeTracker v1.3.2 benchmark")
    print("=" * (width + 30))
    for label, value in rows:
        print(f"{label.ljust(width)}  {value}")
    print("=" * (width + 30))
    print(f"stored events: {TOTAL_EVENTS:,} over {HISTORY_DAYS} days")
    tmp_dir.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
