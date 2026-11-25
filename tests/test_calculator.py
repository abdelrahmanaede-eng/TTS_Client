"""Unit tests for TimeTracker calculator.

Tests the trailing grace period activity model, 18-minute reference test,
work-window clipping, non-work days, meeting-active union, and midnight splitting.
"""

from __future__ import annotations

import unittest
from datetime import date, time

from calculator import (
    calculate_active_seconds,
    calculate_app_usage,
    calculate_day_metrics,
    calculate_event_contributions,
    clip_intervals,
    events_to_active_intervals,
    format_duration_hm,
    get_day_boundaries,
    get_work_window_for_day,
    intersect_intervals,
    merge_intervals,
    split_session_by_days,
    subtract_intervals,
    total_duration_seconds,
)


class TestCalculatorCore(unittest.TestCase):
    def test_reference_example_contributions(self) -> None:
        """Verify the exact trailing grace contribution rule from Section 4.

        Events: [0, 5, 10, 12, 20, 21, 22, 23] minutes.
        Grace G = 180s (3 minutes).
        Contributions: [3, 3, 2, 3, 1, 1, 1, 0] minutes.
        """
        minutes = [0, 5, 10, 12, 20, 21, 22, 23]
        events_sec = [m * 60 for m in minutes]
        contributions = calculate_event_contributions(events_sec, grace_seconds=180, is_live=False)

        # In minutes: [3, 3, 2, 3, 1, 1, 1, 0]
        expected_sec = [3 * 60, 3 * 60, 2 * 60, 3 * 60, 1 * 60, 1 * 60, 1 * 60, 0]
        self.assertEqual(contributions, expected_sec)

        # Strict mathematical sum of these contributions: 14 minutes (840s)
        self.assertEqual(sum(contributions), 14 * 60)

    def test_reference_example_18_minutes(self) -> None:
        """Reference test achieving exactly 18 minutes (1080s) of active time.

        Event sequence with G = 180s:
        Events (minutes): [0, 5, 10, 12, 16, 20, 24, 28, 29]
        Contributions (minutes):
        - 0 to 5: min(3, 5) = 3
        - 5 to 10: min(3, 5) = 3
        - 10 to 12: min(3, 2) = 2
        - 12 to 16: min(3, 4) = 3
        - 16 to 20: min(3, 4) = 3
        - 20 to 24: min(3, 4) = 3
        - 24 to 28: min(3, 4) = 3
        - 28 to 29: min(3, 1) = 1 (sum = 3+3+2+3+3+3+3+1 = 21)
        Or events [0, 5, 10, 12, 16, 20, 24, 25]:
        - 0: 3
        - 5: 3
        - 10: 2
        - 12: 3
        - 16: 3
        - 20: 3
        - 24: min(3, 1) = 1
        - 25: 0
        Sum = 3 + 3 + 2 + 3 + 3 + 3 + 1 + 0 = 18 minutes (1080s).
        """
        minutes = [0, 5, 10, 12, 16, 20, 24, 25]
        events_sec = [m * 60 for m in minutes]
        contributions = calculate_event_contributions(events_sec, grace_seconds=180, is_live=False)

        expected_contribs = [180, 180, 120, 180, 180, 180, 60, 0]
        self.assertEqual(contributions, expected_contribs)
        total_active_sec = calculate_active_seconds(events_sec, grace_seconds=180, is_live=False)
        self.assertEqual(total_active_sec, 18 * 60)

    def test_live_tooltip_adds_grace_to_latest(self) -> None:
        """Live calculation temporarily adds grace period to the latest event."""
        minutes = [0, 5, 10, 12, 20, 21, 22, 23]
        events_sec = [m * 60 for m in minutes]
        strict_sec = calculate_active_seconds(events_sec, grace_seconds=180, is_live=False)
        live_sec = calculate_active_seconds(events_sec, grace_seconds=180, is_live=True)

        self.assertEqual(strict_sec, 14 * 60)
        self.assertEqual(live_sec, (14 + 3) * 60)

    def test_work_window_clipping(self) -> None:
        """Activity outside work hours on a work day is clipped to the work window."""
        target_date = date(2026, 11, 9)  # Monday (work day)
        day_start, _ = get_day_boundaries(target_date)

        # Work window 09:30 to 17:30
        work_start = time(9, 30)
        work_end = time(17, 30)
        work_days = ["Sun", "Mon", "Tue", "Wed", "Thu"]

        # Activity 1: 09:00 to 09:57 with 1-min intervals.
        # Inside work window (starts 09:30): 09:30 to 09:57 (27 min), plus 3 min grace at 09:57 -> 30 min (1800s).
        ev_morning = [day_start + 9 * 3600 + i * 60 for i in range(58)]  # i=0 (09:00) to i=57 (09:57)

        # Activity 2: 17:00 to 18:00 with 1-min intervals.
        # Inside work window (ends 17:30): 17:00 to 17:30 -> 30 min (1800s). Everything after 17:30 is clipped.
        ev_evening = [day_start + 17 * 3600 + i * 60 for i in range(61)]  # 17:00 to 18:00

        all_events = ev_morning + ev_evening
        metrics = calculate_day_metrics(
            target_date=target_date,
            events=all_events,
            sessions=[],
            work_start=work_start,
            work_end=work_end,
            work_days=work_days,
            daily_target_hours=8.0,
            grace_seconds=180,
            is_live=False,
        )

        # Total worked hours should be exactly 30m + 30m = 60m = 1.00 hour (3600s)
        self.assertEqual(metrics.worked_seconds, 3600)
        self.assertEqual(metrics.worked_hours, 1.0)
        self.assertEqual(metrics.overtime_seconds, 0)
        self.assertEqual(metrics.remaining_hours, 7.0)

    def test_non_work_day_without_and_with_overtime(self) -> None:
        """On a Friday (non-work day), activity is ignored unless accepted as Overtime."""
        target_date = date(2026, 11, 13)  # Friday (non-work day)
        day_start, _ = get_day_boundaries(target_date)
        work_days = ["Sun", "Mon", "Tue", "Wed", "Thu"]

        # 2 hours of activity from 10:00 to 12:00
        events = [day_start + 10 * 3600 + i * 60 for i in range(121)]

        # Case A: No overtime session
        metrics_no_ot = calculate_day_metrics(
            target_date=target_date,
            events=events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
        )
        self.assertEqual(metrics_no_ot.worked_seconds, 0)
        self.assertEqual(metrics_no_ot.overtime_seconds, 0)

        # Case B: Overtime session accepted covering 10:00 to 12:00
        ot_session = {
            "start_ts": day_start + 10 * 3600,
            "end_ts": day_start + 12 * 3600,
            "session_type": "overtime",
            "source": "auto",
        }
        metrics_with_ot = calculate_day_metrics(
            target_date=target_date,
            events=events,
            sessions=[ot_session],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
        )
        self.assertEqual(metrics_with_ot.worked_seconds, 0)
        self.assertEqual(metrics_with_ot.overtime_seconds, 2 * 3600)
        self.assertEqual(metrics_with_ot.overtime_hours, 2.0)

    def test_meeting_and_active_union_no_double_counting(self) -> None:
        """Active time and meeting time must form a mathematical union without double counting."""
        target_date = date(2026, 11, 10)  # Tuesday
        day_start, _ = get_day_boundaries(target_date)

        # Active time: 10:00 to 10:30 (events every 60s)
        active_events = [day_start + 10 * 3600 + i * 60 for i in range(31)]

        # Meeting: 10:15 to 11:00 (overlaps from 10:15 to 10:30)
        meeting = {
            "start_ts": day_start + 10 * 3600 + 15 * 60,
            "end_ts": day_start + 11 * 3600,
            "session_type": "meeting",
            "source": "tray",
        }

        metrics = calculate_day_metrics(
            target_date=target_date,
            events=active_events,
            sessions=[meeting],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=["Sun", "Mon", "Tue", "Wed", "Thu"],
        )

        # Span from 10:00 to 11:00 is exactly 60 minutes (3600s).
        # Without union, it would be 30m + 45m = 75m. Union ensures exactly 60m.
        self.assertEqual(metrics.worked_seconds, 3600)
        self.assertEqual(metrics.worked_hours, 1.0)

    def test_midnight_splitting(self) -> None:
        """Sessions crossing midnight are split accurately per calendar day."""
        # Session from Monday 23:30 to Tuesday 01:30 (2 hours total)
        mon_date = date(2026, 11, 9)
        mon_start, tue_start = get_day_boundaries(mon_date)
        session_start = mon_start + 23 * 3600 + 30 * 60
        session_end = tue_start + 1 * 3600 + 30 * 60

        splits = split_session_by_days(session_start, session_end)
        self.assertEqual(len(splits), 2)

        day1, (d1_s, d1_e) = splits[0]
        self.assertEqual(day1, mon_date)
        self.assertEqual(d1_s, session_start)
        self.assertEqual(d1_e, tue_start)
        self.assertEqual(d1_e - d1_s, 30 * 60)  # 30 min on Monday

        day2, (d2_s, d2_e) = splits[1]
        self.assertEqual(day2, date(2026, 11, 10))
        self.assertEqual(d2_s, tue_start)
        self.assertEqual(d2_e, session_end)
        self.assertEqual(d2_e - d2_s, 90 * 60)  # 90 min on Tuesday

    def test_remaining_calculation(self) -> None:
        """Remaining time is max(0, target - worked)."""
        target_date = date(2026, 11, 9)
        day_start, _ = get_day_boundaries(target_date)

        # 5 hours of work inside work window
        events = [day_start + 10 * 3600 + i * 60 for i in range(301)]
        metrics = calculate_day_metrics(
            target_date=target_date,
            events=events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=["Sun", "Mon", "Tue", "Wed", "Thu"],
            daily_target_hours=8.0,
        )
        # B12: a finished day keeps the 180 s tail of its last event (5 h + 3 min).
        self.assertEqual(metrics.worked_hours, 5.05)
        self.assertEqual(metrics.target_hours, 8.0)
        self.assertEqual(metrics.remaining_hours, 2.95)

    def test_late_arrival_calculation(self) -> None:
        """late_seconds measures first activity beyond work_start + grace."""
        target_date = date(2026, 11, 9)  # Monday (work day)
        day_start, _ = get_day_boundaries(target_date)
        work_days = ["Sun", "Mon", "Tue", "Wed", "Thu"]

        # First activity at 11:00 -> late = 11:00 - (09:30 + 3m grace) = 1h 27m
        late_events = [day_start + 11 * 3600 + i * 60 for i in range(31)]
        late_metrics = calculate_day_metrics(
            target_date=target_date,
            events=late_events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            grace_seconds=180,
        )
        self.assertEqual(late_metrics.late_seconds, 1 * 3600 + 27 * 60)
        self.assertEqual(late_metrics.late_formatted, "1h 27m")

        # First activity at 09:30 (on time) -> no late
        on_time_events = [day_start + 9 * 3600 + 30 * 60 + i * 60 for i in range(31)]
        on_time_metrics = calculate_day_metrics(
            target_date=target_date,
            events=on_time_events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            grace_seconds=180,
        )
        self.assertEqual(on_time_metrics.late_seconds, 0)

        # First activity within grace (09:32) -> still on time
        grace_events = [day_start + 9 * 3600 + 32 * 60 + i * 60 for i in range(31)]
        grace_metrics = calculate_day_metrics(
            target_date=target_date,
            events=grace_events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            grace_seconds=180,
        )
        self.assertEqual(grace_metrics.late_seconds, 0)

        # Non-work day: never late
        friday = date(2026, 11, 13)
        f_day_start, _ = get_day_boundaries(friday)
        friday_events = [f_day_start + 11 * 3600 + i * 60 for i in range(31)]
        friday_metrics = calculate_day_metrics(
            target_date=friday,
            events=friday_events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
        )
        self.assertEqual(friday_metrics.late_seconds, 0)

    def test_shift_remaining_countdown(self) -> None:
        """shift_remaining_seconds counts down to work_end for the live day."""
        target_date = date(2026, 11, 9)  # Monday (work day)
        day_start, _ = get_day_boundaries(target_date)
        work_days = ["Sun", "Mon", "Tue", "Wed", "Thu"]
        events = [day_start + 11 * 3600 + i * 60 for i in range(31)]

        # Live at 15:00 -> 17:30 - 15:00 = 2h 30m
        mid_shift = calculate_day_metrics(
            target_date=target_date,
            events=events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            is_live=True,
            now_ts=day_start + 15 * 3600,
        )
        self.assertEqual(mid_shift.shift_remaining_seconds, 2 * 3600 + 30 * 60)

        # Live before work start -> full shift length (8h)
        before_shift = calculate_day_metrics(
            target_date=target_date,
            events=[],
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            is_live=True,
            now_ts=day_start + 8 * 3600,
        )
        self.assertEqual(before_shift.shift_remaining_seconds, 8 * 3600)

        # Live after shift end -> 0
        after_shift = calculate_day_metrics(
            target_date=target_date,
            events=events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            is_live=True,
            now_ts=day_start + 18 * 3600,
        )
        self.assertEqual(after_shift.shift_remaining_seconds, 0)

        # Past day (not live) -> 0 regardless of activity
        past_day = calculate_day_metrics(
            target_date=target_date,
            events=events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            is_live=False,
        )
        self.assertEqual(past_day.shift_remaining_seconds, 0)

    def test_calculate_app_usage(self) -> None:
        """Verify active application time breakdown and percentages."""
        # Events at minutes: 0 (chrome), 2 (chrome), 5 (Code), 7 (Code), 10 (excel), 12 (excel)
        # G = 180s (3 min)
        # 0 (chrome) -> next is 2 min: min(3, 2) = 2 min (120s)
        # 2 (chrome) -> next is 5 min: min(3, 3) = 3 min (180s)
        # Total chrome = 300s (5 min)
        # 5 (Code) -> next is 7 min: min(3, 2) = 2 min (120s)
        # 7 (Code) -> next is 10 min: min(3, 3) = 3 min (180s)
        # Total Code = 300s (5 min)
        # 10 (excel) -> next is 12 min: min(3, 2) = 2 min (120s)
        # 12 (excel) -> last: 0s (if strict)
        # Total excel = 120s (2 min)
        # Total active = 300 + 300 + 120 = 720s (12 min)
        events_with_apps = [
            (0, "chrome.exe"),
            (2 * 60, "chrome.exe"),
            (5 * 60, "Code.exe"),
            (7 * 60, "Code.exe"),
            (10 * 60, "excel.exe"),
            (12 * 60, "excel.exe"),
        ]

        usage = calculate_app_usage(events_with_apps, grace_seconds=180, is_live=False)
        self.assertEqual(len(usage), 3)

        app_map = {item.app_name: item for item in usage}
        self.assertEqual(app_map["chrome.exe"].active_seconds, 300)
        self.assertEqual(app_map["Code.exe"].active_seconds, 300)
        self.assertEqual(app_map["excel.exe"].active_seconds, 120)

        # Percentages: 300/720 = 41.7%, 120/720 = 16.7%
        self.assertAlmostEqual(app_map["chrome.exe"].percentage, 41.7, places=1)
        self.assertAlmostEqual(app_map["excel.exe"].percentage, 16.7, places=1)

    def test_holiday_treated_as_non_work_day(self) -> None:
        """Official holidays in config act as non-work days (like Friday/Saturday)."""
        target_date = date(2026, 11, 9)  # Monday
        work_days = ["Sun", "Mon", "Tue", "Wed", "Thu"]
        holidays = ["2026-11-09"]  # Monday marked as holiday

        # Work window should be None on holiday
        win = get_work_window_for_day(
            target_date, time(9, 30), time(17, 30), work_days, holidays=holidays
        )
        self.assertIsNone(win)

    def test_half_day_mode_target(self) -> None:
        """Half-day mode uses 4.0h target hours."""
        target_date = date(2026, 11, 9)
        day_start, _ = get_day_boundaries(target_date)
        work_days = ["Sun", "Mon", "Tue", "Wed", "Thu"]

        # 3 hours of activity
        events = [day_start + 10 * 3600 + i * 60 for i in range(181)]
        metrics = calculate_day_metrics(
            target_date=target_date,
            events=events,
            sessions=[],
            work_start=time(9, 30),
            work_end=time(17, 30),
            work_days=work_days,
            daily_target_hours=4.0,  # Half-day
        )
        # 3 h of activity plus the 3-minute day-close tail (B12).
        self.assertEqual(metrics.target_hours, 4.0)
        self.assertEqual(metrics.worked_hours, 3.05)
        self.assertEqual(metrics.remaining_hours, 0.95)

    def test_formatting_helpers(self) -> None:
        self.assertEqual(format_duration_hm(3600), "1h 00m")
        self.assertEqual(format_duration_hm(5 * 3600 + 20 * 60), "5h 20m")
        self.assertEqual(format_duration_hm(42 * 60), "0h 42m")


if __name__ == "__main__":
    unittest.main()
