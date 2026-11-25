"""Pure calculation functions for TimeTracker.

Implements the trailing grace period model, interval algebra,
work-window clipping, overtime/meeting classification, holidays,
half-day target overrides, and day/range summary metrics.
No database or UI dependencies so it is completely unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence

# Interval representation: tuple[int, int] where start < end (seconds epoch or offsets)
Interval = tuple[int, int]

# A presence-check confirmation never credits more than this, even when the
# configured interval is longer (D5).
MAX_CONFIRM_CREDIT_SECONDS = 300


# =====================================================================
# 1. Trailing Grace Period Model
# =====================================================================

def calculate_event_contributions(
    events: Sequence[int],
    grace_seconds: int = 180,
    is_live: bool = False,
    last_event_cap: int | None = None,
) -> list[int]:
    """Calculate the contribution of each sorted input event.

    Rules (Section 4):
    For events i_1 .. i_n:
    - If k < n - 1: min(G, i_{k+1} - i_k)
    - If k == n - 1 (last event): ``G`` when ``is_live``, otherwise
      ``last_event_cap`` when the caller knows how much of the day is left
      (day-close tail, B12), otherwise 0 (strict raw-event rule).
    """
    if not events:
        return []

    sorted_events = sorted(events)
    n = len(sorted_events)
    contributions: list[int] = []

    for k in range(n):
        if k < n - 1:
            diff = sorted_events[k + 1] - sorted_events[k]
            contributions.append(max(0, min(grace_seconds, diff)))
        elif is_live:
            contributions.append(grace_seconds)
        elif last_event_cap is not None:
            contributions.append(max(0, min(grace_seconds, int(last_event_cap))))
        else:
            contributions.append(0)

    return contributions


def events_to_active_intervals(
    events: Sequence[int],
    grace_seconds: int = 180,
    is_live: bool = False,
    last_event_cap: int | None = None,
) -> list[Interval]:
    """Convert input event timestamps into disjoint active intervals [start, end]."""
    if not events:
        return []

    sorted_events = sorted(events)
    contributions = calculate_event_contributions(
        sorted_events, grace_seconds, is_live=is_live, last_event_cap=last_event_cap
    )
    raw_intervals: list[Interval] = []

    for ev, contrib in zip(sorted_events, contributions):
        if contrib > 0:
            raw_intervals.append((ev, ev + contrib))

    return merge_intervals(raw_intervals)


def calculate_active_seconds(
    events: Sequence[int],
    grace_seconds: int = 180,
    is_live: bool = False,
) -> int:
    """Sum of contributions for input events."""
    return sum(calculate_event_contributions(events, grace_seconds, is_live=is_live))


def presence_credit_intervals(
    events_with_sources: Sequence[Sequence[Any]],
    interval_seconds: int,
) -> list[Interval]:
    """Return the intervals credited by confirmed presence checks.

    A presence-check confirmation (``source='confirm'``) proves that the user was
    present during the whole idle gap that preceded it.  The credited interval
    therefore starts at the previous recorded presence (real input event or a
    previous confirmation) and is capped at ``interval_seconds`` (the configured
    presence-check interval).

    The anchor is the last timestamp *strictly earlier* than the confirmation.
    Confirming usually produces input as well (a mouse click, or the movement that
    auto-dismisses the popup), and the tracker's 5-second tick may persist that
    input in the same second as the confirmation; anchoring to the older record
    keeps the intended credit instead of silently collapsing it to zero.

    Because the cap is applied from the *start* of the gap, any time spent with
    the popup on screen (the popup only appears once the interval has already
    elapsed) is never credited.  A confirmation after a timeout produces no
    ``confirm`` record at all, so nothing is credited for that period.

    Records are ``(ts, app)``, ``(ts, app, source)`` or
    ``(ts, app, source, project_id)`` sequences.
    """
    # D5: a confirmation never credits more than MAX_CONFIRM_CREDIT_SECONDS.
    interval_seconds = min(int(interval_seconds), MAX_CONFIRM_CREDIT_SECONDS)
    if interval_seconds <= 0 or not events_with_sources:
        return []

    normalized: list[tuple[int, str]] = []
    for rec in events_with_sources:
        try:
            ts = int(rec[0])
        except (TypeError, ValueError, IndexError):
            continue
        source = str(rec[2]) if len(rec) > 2 and rec[2] else "input"
        normalized.append((ts, source))

    if not normalized:
        return []

    normalized.sort(key=lambda item: item[0])
    credits: list[Interval] = []
    last_presence_ts: int | None = None
    previous_distinct_ts: int | None = None

    for ts, source in normalized:
        if last_presence_ts is not None and ts != last_presence_ts:
            previous_distinct_ts = last_presence_ts
        if source == "confirm" and previous_distinct_ts is not None and ts > previous_distinct_ts:
            end = min(ts, previous_distinct_ts + interval_seconds)
            if end > previous_distinct_ts:
                credits.append((previous_distinct_ts, end))
        last_presence_ts = ts

    return merge_intervals(credits)


def normalize_event_records(
    records: Sequence[Sequence[Any]],
) -> list[tuple[int, str, str, int | None]]:
    """Normalize event records to ``(ts, app, source, project_id)`` sorted by time.

    Accepts bare timestamps, ``(ts, app)``, ``(ts, app, source)`` or
    ``(ts, app, source, project_id)`` records.
    """
    normalized: list[tuple[int, str, str, int | None]] = []
    for rec in records:
        project_id: int | None = None
        if isinstance(rec, (tuple, list)):
            if not rec:
                continue
            try:
                ts = int(rec[0])
            except (TypeError, ValueError):
                continue
            app = str(rec[1]) if len(rec) > 1 and rec[1] else "Unknown"
            source = str(rec[2]) if len(rec) > 2 and rec[2] else "input"
            if len(rec) > 3 and rec[3] is not None:
                try:
                    project_id = int(rec[3])
                except (TypeError, ValueError):
                    project_id = None
        else:
            try:
                ts = int(rec)
            except (TypeError, ValueError):
                continue
            app = "Unknown"
            source = "input"
        normalized.append((ts, app, source, project_id))

    normalized.sort(key=lambda item: item[0])
    return normalized


def event_contributions_with_credits(
    records: Sequence[Sequence[Any]],
    grace_seconds: int = 180,
    is_live: bool = False,
    active_check_interval_sec: int = 0,
) -> list[tuple[int, str, int | None, str]]:
    """Return ``(seconds, app, project_id, source)`` contributions per event.

    Confirmed presence checks additionally credit the idle gap that precedes them
    (capped at ``active_check_interval_sec``); the credited span is attributed to
    the application and project that were recorded with the confirmation.

    Spans are allocated like interval algebra rather than simply summed: the
    trailing grace of each event is claimed first (those spans are disjoint by
    construction), then each confirmation credit only receives the seconds that
    are not already attributed.  Per-application totals therefore never exceed
    the day's worked time, which is what the dashboard and Excel percentages
    assume.
    """
    norm = normalize_event_records(records)
    n = len(norm)
    if n == 0:
        return []

    results: dict[int, int] = {k: 0 for k in range(n)}

    # Regular trailing-grace spans (disjoint by construction).
    regular_spans: list[tuple[int, int, int]] = []
    for k, (ts, app, source, project_id) in enumerate(norm):
        if k < n - 1:
            end = ts + max(0, min(grace_seconds, norm[k + 1][0] - ts))
        else:
            end = ts + (grace_seconds if is_live else 0)
        if end > ts:
            regular_spans.append((ts, end, k))

    # Confirmation credits, anchored at the last timestamp strictly earlier than
    # the confirmation (a same-second click may already be persisted as input).
    credit_spans: list[tuple[int, int, int]] = []
    credit_cap = min(int(active_check_interval_sec), MAX_CONFIRM_CREDIT_SECONDS)
    if credit_cap > 0:
        for k, (ts, app, source, project_id) in enumerate(norm):
            if source != "confirm":
                continue
            anchor: int | None = None
            for j in range(k - 1, -1, -1):
                if norm[j][0] < ts:
                    anchor = norm[j][0]
                    break
            if anchor is None:
                continue
            end = min(ts, anchor + credit_cap)
            if end > anchor:
                credit_spans.append((anchor, end, k))

    # Regular spans never overlap each other (each is capped at the gap to the next
    # event), so they can be merged once and credited in full. Only confirmation
    # credits need subtracting, which keeps this linear for busy days.
    merged_regular = merge_intervals([(start, end) for start, end, _k in regular_spans])
    for start, end, k in regular_spans:
        results[k] += end - start

    covered_credits: list[Interval] = []
    for start, end, k in credit_spans:
        for free_start, free_end in subtract_intervals(
            [(start, end)], merge_intervals(merged_regular + covered_credits)
        ):
            results[k] += free_end - free_start
        covered_credits = merge_intervals(covered_credits + [(start, end)])

    out: list[tuple[int, str, int | None, str]] = []
    for k, (ts, app, source, project_id) in enumerate(norm):
        out.append((results[k], app, project_id, source))
    return out


# =====================================================================
# 2. Interval Algebra
# =====================================================================

def merge_intervals(intervals: Sequence[Interval]) -> list[Interval]:
    """Sort and merge overlapping or adjacent intervals."""
    if not intervals:
        return []

    valid = sorted((start, end) for start, end in intervals if end > start)
    if not valid:
        return []

    merged: list[Interval] = [valid[0]]
    for cur_start, cur_end in valid[1:]:
        last_start, last_end = merged[-1]
        if cur_start <= last_end:
            merged[-1] = (last_start, max(last_end, cur_end))
        else:
            merged.append((cur_start, cur_end))

    return merged


def clip_intervals(intervals: Sequence[Interval], clip_start: int, clip_end: int) -> list[Interval]:
    """Restrict intervals to the window [clip_start, clip_end]."""
    if clip_end <= clip_start:
        return []

    clipped: list[Interval] = []
    for s, e in intervals:
        start = max(s, clip_start)
        end = min(e, clip_end)
        if end > start:
            clipped.append((start, end))
    return merge_intervals(clipped)


def intersect_intervals(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    """Return intersection of two sets of intervals (A ∩ B)."""
    merged_a = merge_intervals(a)
    merged_b = merge_intervals(b)
    result: list[Interval] = []

    i = 0
    j = 0
    while i < len(merged_a) and j < len(merged_b):
        a_s, a_e = merged_a[i]
        b_s, b_e = merged_b[j]

        start = max(a_s, b_s)
        end = min(a_e, b_e)
        if end > start:
            result.append((start, end))

        if a_e < b_e:
            i += 1
        else:
            j += 1

    return merge_intervals(result)


def subtract_intervals(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    """Return intervals in A with intervals in B removed (A - B)."""
    merged_a = merge_intervals(a)
    merged_b = merge_intervals(b)
    if not merged_b:
        return merged_a

    result: list[Interval] = []
    for a_start, a_end in merged_a:
        cur_start = a_start
        for b_start, b_end in merged_b:
            if b_end <= cur_start:
                continue
            if b_start >= a_end:
                break
            if b_start > cur_start:
                result.append((cur_start, min(b_start, a_end)))
            cur_start = max(cur_start, b_end)
            if cur_start >= a_end:
                break
        if cur_start < a_end:
            result.append((cur_start, a_end))

    return merge_intervals(result)


def total_duration_seconds(intervals: Sequence[Interval]) -> int:
    """Return the total duration of merged intervals in seconds."""
    return sum(end - start for start, end in merge_intervals(intervals))


# =====================================================================
# 3. Session Splitting and Day Time Windows
# =====================================================================

def split_session_by_days(start_ts: int, end_ts: int) -> list[tuple[date, Interval]]:
    """Split a session that may cross midnight into per-day intervals [start, end]."""
    if end_ts <= start_ts:
        return []

    results: list[tuple[date, Interval]] = []
    cur_start_dt = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts)

    cur_dt = cur_start_dt
    while cur_dt < end_dt:
        cur_date = cur_dt.date()
        next_day_dt = datetime.combine(cur_date + timedelta(days=1), time(0, 0, 0))
        segment_end_dt = min(end_dt, next_day_dt)

        seg_start_ts = int(cur_dt.timestamp())
        seg_end_ts = int(segment_end_dt.timestamp())
        if seg_end_ts > seg_start_ts:
            results.append((cur_date, (seg_start_ts, seg_end_ts)))

        cur_dt = segment_end_dt

    return results


def get_day_boundaries(d: date) -> tuple[int, int]:
    """Return (start_ts, end_ts) for day d (00:00:00 to 23:59:59.999...).

    ``datetime.timestamp()`` raises OSError on Windows for dates before the
    epoch (B16), so extreme values fall back to plain day arithmetic.
    """
    start_dt = datetime.combine(d, time(0, 0, 0))
    next_day_dt = datetime.combine(d + timedelta(days=1), time(0, 0, 0))
    try:
        return int(start_dt.timestamp()), int(next_day_dt.timestamp())
    except (OSError, OverflowError, ValueError):
        start_ts = (d - date(1970, 1, 1)).days * 86400
        return start_ts, start_ts + 86400


def half_day_target_hours(daily_target_hours: float) -> float:
    """Return half of the configured daily target (P0-4)."""
    try:
        return max(0.0, round(float(daily_target_hours) / 2.0, 2))
    except (TypeError, ValueError):
        return 4.0


def get_work_window_for_day(
    d: date,
    work_start: time,
    work_end: time,
    work_days: Sequence[str],
    holidays: Sequence[str] = (),
) -> Interval | None:
    """Return (work_start_ts, work_end_ts) if d is a work day and not a holiday, else None."""
    # Check holidays list (e.g. "YYYY-MM-DD")
    if holidays and d.isoformat() in holidays:
        return None

    day_name = d.strftime("%a")
    if day_name not in work_days:
        return None

    dt_start = datetime.combine(d, work_start)
    dt_end = datetime.combine(d, work_end)
    return int(dt_start.timestamp()), int(dt_end.timestamp())


# =====================================================================
# 4. Active Applications Usage
# =====================================================================

@dataclass
class AppUsageItem:
    app_name: str
    active_seconds: int
    active_hours: float
    formatted: str
    percentage: float


def calculate_app_usage(
    events_with_apps: Sequence[Sequence[Any]],
    grace_seconds: int = 180,
    is_live: bool = False,
    active_check_interval_sec: int = 0,
) -> list[AppUsageItem]:
    """Calculate the active duration spent on each foreground application.

    Accepts ``(ts, app)``, ``(ts, app, source)`` or
    ``(ts, app, source, project_id)`` records.  Confirmed presence checks credit
    the preceding idle gap to the application recorded with the confirmation
    (see :func:`presence_credit_intervals`).
    """
    if not events_with_apps:
        return []

    app_seconds: dict[str, int] = {}
    for contrib, app_name, _project_id, _source in event_contributions_with_credits(
        events_with_apps,
        grace_seconds=grace_seconds,
        is_live=is_live,
        active_check_interval_sec=active_check_interval_sec,
    ):
        app_seconds[app_name] = app_seconds.get(app_name, 0) + contrib

    total_active = sum(app_seconds.values())
    results: list[AppUsageItem] = []

    for app_name, sec in sorted(app_seconds.items(), key=lambda x: x[1], reverse=True):
        if sec <= 0:
            continue
        pct = round((sec / total_active) * 100.0, 1) if total_active > 0 else 0.0
        results.append(
            AppUsageItem(
                app_name=app_name,
                active_seconds=sec,
                active_hours=round(sec / 3600.0, 2),
                formatted=format_duration_hm(sec),
                percentage=pct,
            )
        )

    return results


@dataclass
class ProjectUsageItem:
    project_id: int | None
    active_seconds: int
    active_hours: float
    formatted: str
    percentage: float


def calculate_project_usage(
    records: Sequence[Sequence[Any]],
    grace_seconds: int = 180,
    is_live: bool = False,
    active_check_interval_sec: int = 0,
) -> list[ProjectUsageItem]:
    """Calculate active duration attributed to each project (``project_id``).

    Records are ``(ts, app, source, project_id)`` sequences; records without a
    project are grouped under ``None`` (unassigned time).
    """
    if not records:
        return []

    project_seconds: dict[int | None, int] = {}
    for contrib, _app, project_id, _source in event_contributions_with_credits(
        records,
        grace_seconds=grace_seconds,
        is_live=is_live,
        active_check_interval_sec=active_check_interval_sec,
    ):
        project_seconds[project_id] = project_seconds.get(project_id, 0) + contrib

    total_active = sum(project_seconds.values())
    results: list[ProjectUsageItem] = []
    for project_id, sec in sorted(
        project_seconds.items(), key=lambda item: item[1], reverse=True
    ):
        if sec <= 0:
            continue
        pct = round((sec / total_active) * 100.0, 1) if total_active > 0 else 0.0
        results.append(
            ProjectUsageItem(
                project_id=project_id,
                active_seconds=sec,
                active_hours=round(sec / 3600.0, 2),
                formatted=format_duration_hm(sec),
                percentage=pct,
            )
        )

    return results


# =====================================================================
# 5. Metrics and Classification
# =====================================================================

@dataclass
class DayMetrics:
    target_date: date
    day_name: str
    is_work_day: bool
    first_activity_ts: int | None
    last_activity_ts: int | None
    worked_seconds: int
    overtime_seconds: int
    target_seconds: int
    remaining_seconds: int
    meeting_seconds: int = 0
    break_seconds: int = 0
    # Meeting time that no Worked/Overtime interval covers (a weekend or
    # out-of-hours meeting whose overtime was not accepted). Reported separately
    # so the numbers can be audited instead of vanishing (D3, item 2).
    unclassified_meeting_seconds: int = 0
    is_holiday: bool = False
    # Time left until the official shift end (work_end), independent of the
    # daily target; 0 once the shift is over or for past days.
    shift_remaining_seconds: int = 0
    # Arrival after work_start (beyond the grace period); 0 when on time.
    late_seconds: int = 0

    @property
    def worked_hours(self) -> float:
        return round(self.worked_seconds / 3600.0, 2)

    @property
    def overtime_hours(self) -> float:
        return round(self.overtime_seconds / 3600.0, 2)

    @property
    def meeting_hours(self) -> float:
        return round(self.meeting_seconds / 3600.0, 2)

    @property
    def break_hours(self) -> float:
        return round(self.break_seconds / 3600.0, 2)

    @property
    def target_hours(self) -> float:
        return round(self.target_seconds / 3600.0, 2)

    @property
    def remaining_hours(self) -> float:
        return round(self.remaining_seconds / 3600.0, 2)

    @property
    def worked_formatted(self) -> str:
        return format_duration_hm(self.worked_seconds)

    @property
    def overtime_formatted(self) -> str:
        return format_duration_hm(self.overtime_seconds)

    @property
    def meeting_formatted(self) -> str:
        return format_duration_hm(self.meeting_seconds)

    @property
    def break_formatted(self) -> str:
        return format_duration_hm(self.break_seconds)

    @property
    def unclassified_meeting_hours(self) -> float:
        return round(self.unclassified_meeting_seconds / 3600.0, 2)

    @property
    def unclassified_meeting_formatted(self) -> str:
        return format_duration_hm(self.unclassified_meeting_seconds)

    @property
    def remaining_formatted(self) -> str:
        return format_duration_hm(self.remaining_seconds)

    @property
    def shift_remaining_formatted(self) -> str:
        return format_duration_hm(self.shift_remaining_seconds)

    @property
    def late_formatted(self) -> str:
        return format_duration_hm(self.late_seconds)

    @property
    def first_activity_formatted(self) -> str:
        if self.first_activity_ts is None:
            return "-"
        return datetime.fromtimestamp(self.first_activity_ts).strftime("%H:%M:%S")

    @property
    def last_activity_formatted(self) -> str:
        if self.last_activity_ts is None:
            return "-"
        return datetime.fromtimestamp(self.last_activity_ts).strftime("%H:%M:%S")


def format_duration_hm(seconds: int) -> str:
    """Format seconds into e.g. '5h 20m' or '0h 42m'."""
    total_minutes = max(0, int(round(seconds / 60.0)))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}h {minutes:02d}m"


def calculate_day_metrics(
    target_date: date,
    events: Sequence[int],
    sessions: Sequence[dict[str, Any]],
    work_start: time,
    work_end: time,
    work_days: Sequence[str],
    holidays: Sequence[str] = (),
    daily_target_hours: float = 8.0,
    grace_seconds: int = 180,
    is_live: bool = False,
    now_ts: int | None = None,
    confirm_credits: Sequence[Interval] = (),
) -> DayMetrics:
    """Calculate Worked, Overtime, Meeting, Break, and Remaining for target_date.

    ``confirm_credits`` holds intervals already credited by confirmed presence
    checks (see :func:`presence_credit_intervals`).  They are merged with the
    normal active intervals and with meetings, so no time is counted twice.
    """
    day_start_ts, day_end_ts = get_day_boundaries(target_date)
    day_name = target_date.strftime("%a")

    work_win = get_work_window_for_day(
        target_date, work_start, work_end, work_days, holidays=holidays
    )
    is_work_day = (work_win is not None)
    is_holiday = bool(holidays) and target_date.isoformat() in holidays

    # 1. Filter events on this day
    day_events = sorted([e for e in events if day_start_ts <= e < day_end_ts])
    first_activity_ts = day_events[0] if day_events else None
    last_activity_ts = day_events[-1] if day_events else None

    # Active intervals (trailing grace + confirmed presence credits).
    # A finished day still credits the last event's grace, bounded by the time
    # left in the day, so a completed day never loses the tail the live view
    # already showed (B12).
    last_event_cap: int | None = None
    if not is_live and day_events:
        last_event_cap = max(0, min(grace_seconds, day_end_ts - day_events[-1]))
    active_intervals = events_to_active_intervals(
        day_events, grace_seconds, is_live=is_live, last_event_cap=last_event_cap
    )
    if confirm_credits:
        day_credits = clip_intervals(list(confirm_credits), day_start_ts, day_end_ts)
        active_intervals = merge_intervals(active_intervals + day_credits)
    active_intervals = clip_intervals(active_intervals, day_start_ts, day_end_ts)

    # 2. Extract and clip meetings, overtime, and break sessions
    current_ts = now_ts or int(datetime.now().timestamp())
    meeting_raw: list[Interval] = []
    overtime_raw: list[Interval] = []
    break_raw: list[Interval] = []

    for s in sessions:
        st = int(s["start_ts"])
        et = int(s["end_ts"]) if s.get("end_ts") is not None else current_ts
        st_clipped = max(st, day_start_ts)
        et_clipped = min(et, day_end_ts)
        if et_clipped > st_clipped:
            stype = s.get("session_type")
            if stype == "meeting":
                meeting_raw.append((st_clipped, et_clipped))
            elif stype == "overtime":
                overtime_raw.append((st_clipped, et_clipped))
            elif stype == "break":
                break_raw.append((st_clipped, et_clipped))

    meeting_intervals = merge_intervals(meeting_raw)
    overtime_intervals = merge_intervals(overtime_raw)
    break_intervals = merge_intervals(break_raw)

    meeting_seconds = total_duration_seconds(meeting_intervals)

    # Combined active + meetings
    active_or_meeting = merge_intervals(active_intervals + meeting_intervals)

    worked_intervals: list[Interval] = []
    overtime_active_intervals: list[Interval] = []

    if is_work_day and work_win is not None:
        win_start, win_end = work_win
        # Activity inside work window -> Worked
        in_window = clip_intervals(active_or_meeting, win_start, win_end)
        worked_intervals.extend(in_window)

        # Outside work window on work day:
        outside_window = subtract_intervals(active_or_meeting, [(win_start, win_end)])

        # Outside + in accepted overtime -> Overtime
        ot_part = intersect_intervals(outside_window, overtime_intervals)
        overtime_active_intervals.extend(ot_part)

        # Meetings outside the work window are booked only when an accepted
        # overtime session covers them. Anything left over is reported as
        # unclassified meeting time below, never silently counted as Worked (D3).
        outside_meetings = intersect_intervals(meeting_intervals, outside_window)
        if outside_meetings and overtime_intervals:
            overtime_active_intervals.extend(
                intersect_intervals(outside_meetings, overtime_intervals)
            )
    else:
        # Non-work day or Holiday (D3): time counts as Overtime only when an
        # accepted overtime session covers it. It is never booked as Worked.
        ot_part = intersect_intervals(active_or_meeting, overtime_intervals)
        overtime_active_intervals.extend(ot_part)

    # Final merged sets (ensuring disjointness)
    final_worked = merge_intervals(worked_intervals)
    final_overtime = merge_intervals(overtime_active_intervals)
    final_worked = subtract_intervals(final_worked, final_overtime)

    worked_seconds = total_duration_seconds(final_worked)
    overtime_seconds = total_duration_seconds(final_overtime)
    # D3: meeting time no Worked/Overtime interval covers stays visible instead
    # of disappearing (weekend meeting with declined overtime, for example).
    unclassified_meeting_seconds = total_duration_seconds(
        subtract_intervals(meeting_intervals, merge_intervals(final_worked + final_overtime))
    )
    target_seconds = int(daily_target_hours * 3600) if is_work_day else 0
    remaining_seconds = max(0, target_seconds - worked_seconds)

    # Shift-end countdown and late-arrival are about the official schedule,
    # not the daily target, so they are tracked separately.
    shift_remaining_seconds = 0
    if is_work_day and work_win is not None:
        win_start, win_end = work_win
        if is_live:
            if current_ts < win_start:
                shift_remaining_seconds = win_end - win_start
            else:
                shift_remaining_seconds = max(0, win_end - current_ts)

    # Update first/last activity if meetings or breaks happened
    extra_intervals = meeting_intervals + break_intervals
    if extra_intervals:
        sorted_extra = sorted(extra_intervals, key=lambda x: x[0])
        e_first = sorted_extra[0][0]
        e_last = max(x[1] for x in sorted_extra)
        first_activity_ts = min(first_activity_ts, e_first) if first_activity_ts is not None else e_first
        last_activity_ts = max(last_activity_ts, e_last) if last_activity_ts is not None else e_last

    # Late arrival: first activity beyond work_start + grace (schedule-based,
    # independent of the daily target).
    late_seconds = 0
    if is_work_day and work_win is not None:
        win_start, _win_end = work_win
        if first_activity_ts is not None and first_activity_ts > win_start + grace_seconds:
            late_seconds = first_activity_ts - (win_start + grace_seconds)

    # Break (D2): manual break/pause sessions UNION the unworked time inside the
    # work window, always disjoint from Worked, Overtime and Meeting. Idle time
    # outside the work window (evenings, weekends) is never reported as Break.
    busy_intervals = merge_intervals(final_worked + final_overtime + meeting_intervals)
    break_spans: list[Interval] = list(break_intervals)
    if (
        is_work_day
        and work_win is not None
        and first_activity_ts is not None
        and last_activity_ts is not None
    ):
        win_start, win_end = work_win
        span_start = max(first_activity_ts, win_start)
        span_end = min(last_activity_ts, win_end)
        if span_end > span_start:
            break_spans.extend(subtract_intervals([(span_start, span_end)], busy_intervals))

    break_seconds = total_duration_seconds(
        subtract_intervals(merge_intervals(break_spans), busy_intervals)
    )

    return DayMetrics(
        target_date=target_date,
        day_name=day_name,
        is_work_day=is_work_day,
        first_activity_ts=first_activity_ts,
        last_activity_ts=last_activity_ts,
        worked_seconds=worked_seconds,
        overtime_seconds=overtime_seconds,
        target_seconds=target_seconds,
        remaining_seconds=remaining_seconds,
        meeting_seconds=meeting_seconds,
        break_seconds=break_seconds,
        unclassified_meeting_seconds=unclassified_meeting_seconds,
        shift_remaining_seconds=shift_remaining_seconds,
        late_seconds=late_seconds,
        is_holiday=is_holiday,
    )
