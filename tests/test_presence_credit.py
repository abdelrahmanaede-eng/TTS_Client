"""Tests for the presence-check confirmation credit (Part A1).

A confirmation must credit the whole idle gap that preceded it, capped at the
configured presence-check interval, merged with meetings without double counting,
and never crediting the time spent with the popup on screen.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from time_tracker.core.calculator import (
    calculate_app_usage,
    calculate_day_metrics,
    clip_intervals,
    get_day_boundaries,
    presence_credit_intervals,
    total_duration_seconds,
)
from time_tracker.core.config import Config
from time_tracker.core.database import Database
from time_tracker.core.tracker import ActivityTracker

WORK_DAYS = ["Mon", "Tue", "Wed", "Thu", "Sun"]


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute)).timestamp())


def _metrics(d: date, events: list[int], sessions: list[dict[str, Any]], credits: list[tuple[int, int]]):
    return calculate_day_metrics(
        target_date=d,
        events=events,
        sessions=sessions,
        work_start=time(9, 30),
        work_end=time(17, 30),
        work_days=WORK_DAYS,
        holidays=[],
        daily_target_hours=8.0,
        grace_seconds=180,
        is_live=False,
        confirm_credits=credits,
    )


class TestPresenceCreditIntervals(unittest.TestCase):
    def test_interval_three_minutes_caps_credit(self) -> None:
        """interval = 3 min, 15 idle minutes -> only 3 minutes are credited."""
        d = date(2026, 11, 9)  # Monday
        base = _ts(d, 10, 0)
        records = [(base, "chrome.exe", "input"), (base + 900, "chrome.exe", "confirm")]

        credits = presence_credit_intervals(records, 180)
        self.assertEqual(credits, [(base, base + 180)])

        m = _metrics(d, [r[0] for r in records], [], credits)
        # The trailing grace of the first event covers the same span: merged, not
        # doubled. B12 adds the day-close tail of the last event (the confirmation).
        self.assertEqual(m.worked_seconds, 360)

    def test_interval_ten_minutes_credits_full_gap(self) -> None:
        """interval = 10 min, user reading for 10 minutes, confirms -> 10 min active."""
        d = date(2026, 11, 9)
        base = _ts(d, 10, 0)
        records = [(base, "excel.exe", "input"), (base + 600, "excel.exe", "confirm")]

        # D5: a 10-minute interval still never credits more than 5 minutes.
        credits = presence_credit_intervals(records, 600)
        self.assertEqual(credits, [(base, base + 300)])

        m = _metrics(d, [r[0] for r in records], [], credits)
        # 300 s credited gap + the 180 s day-close tail of the last event.
        self.assertEqual(m.worked_seconds, 480)

        # Without the credit both events only contribute their trailing grace.
        baseline = _metrics(d, [r[0] for r in records], [], [])
        self.assertEqual(baseline.worked_seconds, 360)

    def test_popup_wait_is_not_credited(self) -> None:
        """The interval is anchored at the last real event, so the popup wait is excluded."""
        d = date(2026, 11, 9)
        base = _ts(d, 10, 0)
        # 3 min interval + 40 s of popup wait before the user confirms.
        confirm_ts = base + 180 + 40
        records = [(base, "chrome.exe", "input"), (confirm_ts, "chrome.exe", "confirm")]

        credits = presence_credit_intervals(records, 180)
        self.assertEqual(credits, [(base, base + 180)])
        self.assertEqual(total_duration_seconds(credits), 180)

    def test_chained_confirmations_cover_each_gap(self) -> None:
        d = date(2026, 11, 9)
        base = _ts(d, 10, 0)
        records = [
            (base, "chrome.exe", "input"),
            (base + 600, "chrome.exe", "confirm"),
            (base + 1200, "chrome.exe", "confirm"),
        ]
        credits = presence_credit_intervals(records, 600)
        # D5: each confirmation credits at most 5 minutes from its own anchor, so
        # chained confirmations never bridge the gaps in between.
        self.assertEqual(credits, [(base, base + 300), (base + 600, base + 900)])
        m = _metrics(d, [r[0] for r in records], [], credits)
        # 600 s credited + the 180 s day-close tail of the last confirmation.
        self.assertEqual(m.worked_seconds, 780)

    def test_click_input_recorded_in_same_second_still_credits_gap(self) -> None:
        """Confirming by clicking can be persisted as input in the same second."""
        d = date(2026, 11, 9)
        base = _ts(d, 10, 0)
        confirm_ts = base + 600
        records = [
            (base, "chrome.exe", "input"),
            (confirm_ts, "chrome.exe", "input"),  # the click, written by a tick
            (confirm_ts, "chrome.exe", "confirm"),
        ]
        credits = presence_credit_intervals(records, 600)
        self.assertEqual(credits, [(base, base + 300)])

        m = _metrics(d, [r[0] for r in records], [], credits)
        # 300 s credit + the 180 s day-close tail of the last event.
        self.assertEqual(m.worked_seconds, 480)

    def test_timeout_produces_no_credit(self) -> None:
        """A timed-out popup records nothing, so the idle gap stays uncredited."""
        # Only the real input event exists (the user never confirmed).
        records: list[tuple[int, str, str]] = []
        self.assertEqual(presence_credit_intervals(records, 180), [])

        d = date(2026, 11, 9)
        base = _ts(d, 10, 0)
        single = [(base, "chrome.exe", "input")]
        self.assertEqual(presence_credit_intervals(single, 600), [])

        m = _metrics(d, [base], [], presence_credit_intervals(single, 600))
        # Only the day-close tail of the single real event remains (B12).
        self.assertEqual(m.worked_seconds, 180)

    def test_overlap_with_meeting_is_not_double_counted(self) -> None:
        d = date(2026, 11, 9)
        meeting_start = _ts(d, 10, 0)
        meeting_end = _ts(d, 10, 20)
        sessions = [
            {
                "start_ts": meeting_start,
                "end_ts": meeting_end,
                "session_type": "meeting",
                "source": "tray",
                "note": "",
                "reason": "",
            }
        ]
        records = [
            (meeting_start + 300, "teams.exe", "input"),
            (meeting_start + 900, "teams.exe", "confirm"),
        ]
        credits = presence_credit_intervals(records, 600)
        self.assertEqual(credits, [(meeting_start + 300, meeting_start + 600)])

        m = _metrics(d, [r[0] for r in records], sessions, credits)
        # 20 minutes of meeting, no extra minutes added on top of it.
        self.assertEqual(m.meeting_seconds, 1200)
        self.assertEqual(m.worked_seconds, 1200)
        self.assertEqual(m.overtime_seconds, 0)

    def test_day_rollover_splits_credit(self) -> None:
        d1 = date(2026, 11, 9)
        d2 = date(2026, 11, 10)
        before_midnight = _ts(d1, 23, 55)
        after_midnight = _ts(d2, 0, 5)
        records = [
            (before_midnight, "chrome.exe", "input"),
            (after_midnight, "chrome.exe", "confirm"),
        ]
        credits = presence_credit_intervals(records, 600)
        # D5 caps the credit at 300 s, so it stays entirely on the first day.
        self.assertEqual(total_duration_seconds(credits), 300)

        d1_start, d1_end = get_day_boundaries(d1)
        d2_start, d2_end = get_day_boundaries(d2)
        self.assertEqual(total_duration_seconds(clip_intervals(credits, d1_start, d1_end)), 300)
        self.assertEqual(total_duration_seconds(clip_intervals(credits, d2_start, d2_end)), 0)

        # Both days accept an overtime session at 00:00 so the credit is countable.
        sessions = [
            {
                "start_ts": d1_start,
                "end_ts": d2_start,
                "session_type": "overtime",
                "source": "tray",
                "note": "",
                "reason": "",
            },
            {
                "start_ts": d2_start,
                "end_ts": d2_start + 600,
                "session_type": "overtime",
                "source": "tray",
                "note": "",
                "reason": "",
            },
        ]
        m1 = _metrics(d1, [before_midnight], sessions, credits)
        m2 = _metrics(d2, [after_midnight], sessions, credits)
        self.assertEqual(m1.overtime_seconds, 300)
        # Day 2 keeps only the day-close tail of its own event inside the accepted
        # overtime session (180 s), because no credit crosses midnight.
        self.assertEqual(m2.overtime_seconds, 180)

    def test_app_usage_attributes_credit_to_confirming_app(self) -> None:
        d = date(2026, 11, 9)
        base = _ts(d, 10, 0)
        records = [
            (base, "chrome.exe", "input"),
            (base + 600, "word.exe", "confirm"),
        ]
        usage = calculate_app_usage(records, grace_seconds=180, active_check_interval_sec=600)
        by_app = {item.app_name: item.active_seconds for item in usage}
        # Chrome keeps its 3-minute grace; the confirming app only receives the
        # remaining part of the credited gap, so the totals reconcile exactly.
        # D5: the 10-minute interval credits 300 s; chrome keeps 180 s of grace and
        # the confirming app receives the remaining 120 s.
        self.assertEqual(by_app["chrome.exe"], 180)
        self.assertEqual(by_app["word.exe"], 120)
        self.assertEqual(sum(by_app.values()), 300)


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


class TestTrackerPresenceCredit(unittest.TestCase):
    """End-to-end: a confirmation through the tracker credits the whole gap."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp_dir.name) / "test.db")
        self.config = Config(
            work_start="09:30",
            work_end="17:30",
            work_days=WORK_DAYS,
            daily_target_hours=8.0,
            grace_period_seconds=180,
            active_check_enabled=True,
            active_check_interval_min=10,
        )
        self.env = _MockEnv(datetime(2026, 11, 9, 10, 0, 0))
        self.popup_calls: list[dict[str, Any]] = []

        def mock_show_dialog(on_confirmed, on_timeout=None, timeout_seconds=60, lang="en"):
            self.popup_calls.append({"on_confirmed": on_confirmed, "on_timeout": on_timeout})

        self.tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_active_dialog_fn=mock_show_dialog,
            get_last_input_tick_fn=self.env.get_last_input_tick,
            get_system_tick_count_fn=self.env.get_system_tick_count,
            is_locked_fn=self.env.is_workstation_locked,
            time_fn=self.env.get_time,
            now_dt_fn=self.env.get_dt,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_confirmation_applies_interval_ten_minutes(self) -> None:
        self.tracker._tick()  # real input at 10:00
        # Idle for ten minutes, ticking every 5 s like the real loop. One single
        # 600 s jump would be a clock jump / wake and correctly resets the cycle.
        for _ in range(120):
            self.env.advance(5.0)
            self.tracker._tick()
        self.assertEqual(len(self.popup_calls), 1)

        self.env.advance(20.0, generate_input=True)
        self.popup_calls[0]["on_confirmed"]()

        # Live metrics add the trailing grace of the newest event on top of the
        # credited gap, which D5 caps at 5 minutes: 300 s + 180 s live grace.
        metrics = self.tracker.today_metrics
        assert metrics is not None
        self.assertEqual(metrics.worked_seconds, 300 + 180)

        # A completed (strict) calculation of the day credits exactly the gap.
        records = self.db.get_input_events_with_sources(0, 2_000_000_000)
        credits = presence_credit_intervals(records, 600)
        strict = _metrics(self.env.get_dt().date(), [r[0] for r in records], [], credits)
        self.assertEqual(strict.worked_seconds, 300 + 180)

    def test_timeout_does_not_credit_gap(self) -> None:
        self.tracker._tick()
        for _ in range(120):
            self.env.advance(5.0)
            self.tracker._tick()
        self.assertEqual(len(self.popup_calls), 1)

        self.popup_calls[0]["on_timeout"]()
        sources = self.db.get_input_events_with_sources(0, 2_000_000_000)
        self.assertEqual([s[2] for s in sources], ["input"])
        self.assertTrue(self.tracker._active_check_idle_mode)

        # The idle gap is not credited. Live metrics only count the trailing grace
        # of the single real event; a completed calculation counts nothing at all.
        records = self.db.get_input_events_with_sources(0, 2_000_000_000)
        credits = presence_credit_intervals(records, 600)
        self.assertEqual(credits, [])
        strict = _metrics(self.env.get_dt().date(), [r[0] for r in records], [], credits)
        # B12: only the day-close tail of the single real event is counted.
        self.assertEqual(strict.worked_seconds, 180)


if __name__ == "__main__":
    unittest.main()
