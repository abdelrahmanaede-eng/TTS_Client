"""Interactive Visual Dashboard for TimeTracker.

Provides rich visual representations inspired by ActivityWatch:
  1. Horizontal Interactive Timeline View (Gantt-style activity & app streams across 24h)
  2. Summary Dashboard View with:
     - 14-day history volume bar chart
     - Top Applications horizontal progress bars with durations & percentages
     - Hourly stacked bar chart (hours 0..23: Work vs Meeting vs Break vs Overtime)
     - Donut distribution chart
     - KPI cards (Worked, Target, Overtime, Meeting, Break)
     - Detailed Sessions Log

Generates a standalone, 100% local, offline-capable HTML dashboard and opens it
in the default web browser. Zero network requests, zero CDN dependencies.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import secrets
import socketserver
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from calculator import (
    AppUsageItem,
    DayMetrics,
    calculate_app_usage,
    calculate_day_metrics,
    clip_intervals,
    events_to_active_intervals,
    format_duration_hm,
    get_day_boundaries,
    get_work_window_for_day,
    intersect_intervals,
    merge_intervals,
    subtract_intervals,
    total_duration_seconds,
)
from config import Config, get_app_dir, resolve_writable_dir
from database import Database
from retention import (
    build_comparison,
    get_day_app_usage,
    get_day_metrics,
    project_breakdown,
)

logger = logging.getLogger(__name__)


def get_dashboard_html_path() -> Path:
    """Return path for the local visual dashboard HTML file.

    Checks user Documents first, but falls back gracefully to AppData or Temp
    if Documents is restricted by enterprise Group Policy, offline network redirection,
    or insufficient write permissions.
    """
    candidates = [
        Path.home() / "Documents" / "TimeTracker",
        get_app_dir() / "Dashboard",
        Path(tempfile.gettempdir()) / "TimeTracker",
    ]
    fallback = Path(tempfile.gettempdir()) / "TimeTracker"
    directory = resolve_writable_dir(candidates, fallback)
    return directory / "Dashboard.html"


# ---------------------------------------------------------------------------
# Data Preparation
# ---------------------------------------------------------------------------

def _get_hourly_breakdown(
    target_date: date,
    events: list[int],
    sessions: list[dict[str, Any]],
    config: Config,
) -> list[dict[str, Any]]:
    """Compute per-hour breakdown (hours 0..23) of worked, meeting, break, and overtime seconds."""
    day_start_ts, day_end_ts = get_day_boundaries(target_date)
    work_win = get_work_window_for_day(
        target_date, config.work_start_time, config.work_end_time, config.work_days, config.holidays
    )
    is_work_day = work_win is not None

    # Base active intervals
    active_intervals = events_to_active_intervals(events, config.grace_period_seconds, is_live=False)
    active_intervals = clip_intervals(active_intervals, day_start_ts, day_end_ts)

    # Session intervals
    meeting_raw = []
    ot_raw = []
    break_raw = []
    for s in sessions:
        st = max(int(s["start_ts"]), day_start_ts)
        et = min(int(s["end_ts"]) if s.get("end_ts") is not None else day_end_ts, day_end_ts)
        if et > st:
            stype = s.get("session_type")
            if stype == "meeting":
                meeting_raw.append((st, et))
            elif stype == "overtime":
                ot_raw.append((st, et))
            elif stype == "break":
                break_raw.append((st, et))

    meeting_intervals = merge_intervals(meeting_raw)
    ot_intervals = merge_intervals(ot_raw)
    break_intervals = merge_intervals(break_raw)

    # Worked intervals
    active_or_meeting = merge_intervals(active_intervals + meeting_intervals)
    worked_intervals = []
    overtime_active = []

    if is_work_day and work_win:
        w_start, w_end = work_win
        in_win = clip_intervals(active_or_meeting, w_start, w_end)
        worked_intervals.extend(in_win)

        outside_win = subtract_intervals(active_or_meeting, [(w_start, w_end)])
        overtime_active.extend(intersect_intervals(outside_win, ot_intervals))

        # Meetings outside work hours not in OT count as worked
        out_meetings = intersect_intervals(meeting_intervals, outside_win)
        worked_intervals.extend(subtract_intervals(out_meetings, ot_intervals))
    else:
        overtime_active.extend(intersect_intervals(active_or_meeting, ot_intervals))
        worked_intervals.extend(subtract_intervals(meeting_intervals, ot_intervals))

    final_worked = subtract_intervals(merge_intervals(worked_intervals), ot_intervals)
    final_ot = merge_intervals(overtime_active)

    hourly = []
    for h in range(24):
        h_start = day_start_ts + h * 3600
        h_end = h_start + 3600

        w_sec = total_duration_seconds(clip_intervals(final_worked, h_start, h_end))
        m_sec = total_duration_seconds(clip_intervals(meeting_intervals, h_start, h_end))
        ot_sec = total_duration_seconds(clip_intervals(final_ot, h_start, h_end))
        b_sec = total_duration_seconds(clip_intervals(break_intervals, h_start, h_end))

        hourly.append({
            "hour": h,
            "label": f"{h:02d}:00",
            "worked_min": round(w_sec / 60.0, 1),
            "meeting_min": round(m_sec / 60.0, 1),
            "overtime_min": round(ot_sec / 60.0, 1),
            "break_min": round(b_sec / 60.0, 1),
            "total_min": round((w_sec + ot_sec) / 60.0, 1),
        })

    return hourly


def _timeline_window(
    d: date,
    events: list[int],
    sessions: list[dict[str, Any]],
    events_with_apps: list[Any],
    config: Config,
) -> tuple[int, int]:
    """Return the visible timeline span for a day.

    Instead of a fixed 00:00–24:00 ruler, use the configured working window plus
    the actual first/last activity (so overtime stays visible), with a small
    margin. This makes a normal 09:30–17:30 day fill the track instead of ~1/3
    of it. Falls back to 08:00–18:00 for a day with no data and no work window.
    """
    day_start, day_end = get_day_boundaries(d)
    margin = 30 * 60
    stamps: list[int] = [int(t) for t in events if day_start <= int(t) < day_end]
    for rec in events_with_apps:
        ts = rec.get("ts") if isinstance(rec, dict) else rec[0]
        if ts is not None and day_start <= int(ts) < day_end:
            stamps.append(int(ts))
    for s in sessions:
        for key in ("start_ts", "end_ts"):
            value = s.get(key)
            if value is not None and day_start <= int(value) < day_end:
                stamps.append(int(value))

    work_win = get_work_window_for_day(
        d, config.work_start_time, config.work_end_time, config.work_days, config.holidays
    )
    if work_win:
        start, end = work_win[0] - margin, work_win[1] + margin
    elif stamps:
        start, end = min(stamps) - margin, max(stamps) + margin
    else:
        start, end = day_start + 8 * 3600, day_start + 18 * 3600

    if stamps:
        start, end = min(start, min(stamps) - margin), max(end, max(stamps) + margin)
    start, end = max(day_start, start), min(day_end, end)
    if end - start < 3600:
        mid = (start + end) // 2
        start, end = max(day_start, mid - 1800), min(day_end, mid + 1800)
    return int(start), int(end)


def _pct_in_window(ts: int, start: int, end: int) -> float:
    """Clamp a timestamp into the visible window as a 0..100 percentage."""
    span = max(1, end - start)
    return round(max(0.0, min(100.0, ((int(ts) - start) / span) * 100.0)), 2)


def _get_app_timeline_blocks(
    events_with_apps: list[tuple[int, str]],
    day_start_ts: int,
    day_end_ts: int,
    grace_seconds: int = 180,
    window: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Convert input event stream with applications into continuous timeline blocks."""
    if not events_with_apps:
        return []

    win_start, win_end = window or (day_start_ts, day_end_ts)
    sorted_events = sorted(events_with_apps, key=lambda x: x[0])
    blocks: list[dict[str, Any]] = []

    cur_app = None
    cur_start = None
    cur_end = None

    for k in range(len(sorted_events)):
        ts, app = sorted_events[k]
        app_name = app or "Unknown"

        # End of this event's grace span
        if k < len(sorted_events) - 1:
            diff = sorted_events[k + 1][0] - ts
            span = min(grace_seconds, diff)
        else:
            span = grace_seconds

        ev_start = ts
        ev_end = ts + span

        if cur_app == app_name and cur_end is not None and ev_start <= cur_end + 30:
            # Extend existing block
            cur_end = max(cur_end, ev_end)
        else:
            if cur_app is not None and cur_start is not None and cur_end is not None and cur_end > cur_start:
                blocks.append({
                    "app": cur_app,
                    "start_ts": cur_start,
                    "end_ts": cur_end,
                    "duration_sec": cur_end - cur_start,
                    "start_time": datetime.fromtimestamp(cur_start).strftime("%H:%M:%S"),
                    "end_time": datetime.fromtimestamp(cur_end).strftime("%H:%M:%S"),
                    "start_pct": _pct_in_window(cur_start, win_start, win_end),
                    "width_pct": max(
                        0.2,
                        round(
                            (_pct_in_window(cur_end, win_start, win_end)
                             - _pct_in_window(cur_start, win_start, win_end)), 2
                        ),
                    ),
                })
            cur_app = app_name
            cur_start = ev_start
            cur_end = ev_end

    if cur_app is not None and cur_start is not None and cur_end is not None and cur_end > cur_start:
        blocks.append({
            "app": cur_app,
            "start_ts": cur_start,
            "end_ts": cur_end,
            "duration_sec": cur_end - cur_start,
            "start_time": datetime.fromtimestamp(cur_start).strftime("%H:%M:%S"),
            "end_time": datetime.fromtimestamp(cur_end).strftime("%H:%M:%S"),
            "start_pct": _pct_in_window(cur_start, win_start, win_end),
            "width_pct": max(
                0.2,
                round(
                    (_pct_in_window(cur_end, win_start, win_end)
                     - _pct_in_window(cur_start, win_start, win_end)), 2
                ),
            ),
        })

    return blocks


def _get_status_timeline_blocks(
    target_date: date,
    events: list[int],
    sessions: list[dict[str, Any]],
    config: Config,
    window: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Convert status (Worked, Meeting, Break, Overtime) into horizontal timeline blocks."""
    day_start_ts, day_end_ts = get_day_boundaries(target_date)
    win_start, win_end = window or (day_start_ts, day_end_ts)
    work_win = get_work_window_for_day(
        target_date, config.work_start_time, config.work_end_time, config.work_days, config.holidays
    )
    is_work_day = work_win is not None

    blocks: list[dict[str, Any]] = []

    # 1. Sessions first
    for s in sessions:
        st = max(int(s["start_ts"]), day_start_ts)
        et = min(int(s["end_ts"]) if s.get("end_ts") is not None else day_end_ts, day_end_ts)
        if et > st:
            stype = s.get("session_type", "meeting")
            blocks.append({
                "type": stype,
                "label": stype.capitalize(),
                "start_ts": st,
                "end_ts": et,
                "duration_sec": et - st,
                "start_time": datetime.fromtimestamp(st).strftime("%H:%M:%S"),
                "end_time": datetime.fromtimestamp(et).strftime("%H:%M:%S"),
                "start_pct": _pct_in_window(st, win_start, win_end),
                "width_pct": max(
                    0.2,
                    round(_pct_in_window(et, win_start, win_end)
                          - _pct_in_window(st, win_start, win_end), 2),
                ),
                "note": s.get("note") or "",
                "reason": s.get("reason") or "",
            })

    # 2. Active input intervals outside existing sessions
    active_intervals = events_to_active_intervals(events, config.grace_period_seconds, is_live=False)
    active_intervals = clip_intervals(active_intervals, day_start_ts, day_end_ts)

    session_intervals = merge_intervals([(b["start_ts"], b["end_ts"]) for b in blocks])
    pure_active = subtract_intervals(active_intervals, session_intervals)

    for st, et in pure_active:
        if et > st:
            # Determine if this was inside or outside work hours
            in_work = False
            if is_work_day and work_win:
                win_s, win_e = work_win
                if st >= win_s and et <= win_e:
                    in_work = True

            btype = "worked" if in_work else "idle_activity"
            blabel = "Active (Worked)" if in_work else "Active (Off-hours)"

            blocks.append({
                "type": btype,
                "label": blabel,
                "start_ts": st,
                "end_ts": et,
                "duration_sec": et - st,
                "start_time": datetime.fromtimestamp(st).strftime("%H:%M:%S"),
                "end_time": datetime.fromtimestamp(et).strftime("%H:%M:%S"),
                "start_pct": _pct_in_window(st, win_start, win_end),
                "width_pct": max(
                    0.2,
                    round(_pct_in_window(et, win_start, win_end)
                          - _pct_in_window(st, win_start, win_end), 2),
                ),
                "note": "",
                "reason": "",
            })

    blocks.sort(key=lambda x: x["start_ts"])
    return blocks


def _get_past_14_days_summary(db: Database, config: Config, target_date: date) -> list[dict[str, Any]]:
    """Return summary for the past 14 days for the mini history bar chart."""
    days = []
    for i in range(13, -1, -1):
        cur_d = target_date - timedelta(days=i)
        d_start, d_end = get_day_boundaries(cur_d)
        evs = db.get_input_events_between(d_start, d_end)
        sess = db.get_sessions_between(d_start, d_end)
        summary = db.get_daily_summary(cur_d)

        # Reads daily_summary for days older than the retention window.
        m = get_day_metrics(db, config, cur_d)
        days.append({
            "date": cur_d.strftime("%Y-%m-%d"),
            "day_name": cur_d.strftime("%a"),
            "is_target_day": cur_d == target_date,
            "worked_hours": m.worked_hours,
            "overtime_hours": m.overtime_hours,
            "target_hours": m.target_hours,
            "has_data": (len(evs) > 0 or len(sess) > 0 or summary is not None),
        })
    return days


def build_activity_and_sessions_log(*args: Any, **kwargs: Any) -> Any:
    """Public alias for :func:`_build_activity_and_sessions_log` (QUAL-05)."""
    return _build_activity_and_sessions_log(*args, **kwargs)


def _build_activity_and_sessions_log(
    target_date: date,
    events: list[int],
    sessions: list[dict[str, Any]],
    events_with_apps: list[dict[str, Any]],
    config: Config,
) -> list[dict[str, Any]]:
    """Build a comprehensive chronological activity log including:
    - Work intervals (active computer work with top application)
    - Inactive / Idle intervals (away from PC)
    - Meetings (recorded meeting sessions)
    - Breaks & Pauses (recorded break sessions)
    - Overtime sessions
    """
    day_start_ts, day_end_ts = get_day_boundaries(target_date)
    is_today = (target_date == date.today())
    now_ts = int(datetime.now().timestamp())
    limit_ts = now_ts if is_today else day_end_ts

    items: list[dict[str, Any]] = []
    session_intervals: list[tuple[int, int]] = []

    # 1. DB Sessions (Meeting, Break, Overtime)
    for s in sessions:
        st = max(int(s["start_ts"]), day_start_ts)
        et = min(int(s["end_ts"]) if s.get("end_ts") is not None else limit_ts, day_end_ts)
        if et > st:
            dur_sec = et - st
            dur_min = round(dur_sec / 60.0, 1)
            stype = str(s.get("session_type", "")).capitalize()
            session_intervals.append((st, et))
            items.append({
                "type": stype,
                "category": stype.lower(),
                "source": str(s.get("source", "Tray")).capitalize(),
                "start_ts": st,
                "end_ts": et,
                "start": datetime.fromtimestamp(st).strftime("%H:%M:%S"),
                "end": datetime.fromtimestamp(et).strftime("%H:%M:%S") if s.get("end_ts") is not None else "In Progress",
                "duration_min": dur_min,
                "duration_formatted": format_duration_hm(dur_sec),
                "note": str(s.get("note") or ""),
                "reason": str(s.get("reason") or ""),
            })

    # 2. Pure Active Work intervals outside sessions
    active_intervals = events_to_active_intervals(events, config.grace_period_seconds, is_live=is_today)
    active_intervals = clip_intervals(active_intervals, day_start_ts, day_end_ts)
    merged_sess = merge_intervals(session_intervals)
    pure_active = subtract_intervals(active_intervals, merged_sess)

    # PERF-07: build the sorted timestamp list once, not per interval.
    import bisect

    ts_list = [ea[0] for ea in events_with_apps] if events_with_apps else []

    for st, et in pure_active:
        dur_sec = et - st
        if dur_sec < 10:
            continue
        dur_min = round(dur_sec / 60.0, 1)

        # PERF-07: bisect the pre-built sorted timestamp list to the window.
        if ts_list:
            lo = bisect.bisect_left(ts_list, st)
            hi = bisect.bisect_right(ts_list, et)
            interval_apps = [ea[1] for ea in events_with_apps[lo:hi] if ea[1]]
        else:
            interval_apps = []
        top_app = "Computer"
        if interval_apps:
            from collections import Counter
            top_app = Counter(interval_apps).most_common(1)[0][0]

        items.append({
            "type": "Work",
            "category": "work",
            "source": top_app,
            "start_ts": st,
            "end_ts": et,
            "start": datetime.fromtimestamp(st).strftime("%H:%M:%S"),
            "end": datetime.fromtimestamp(et).strftime("%H:%M:%S"),
            "duration_min": dur_min,
            "duration_formatted": format_duration_hm(dur_sec),
            "note": f"Active work ({top_app})" if top_app != "Computer" else "Active computer work",
            "reason": "Standard Work",
        })

    # 3. Inactive / Idle intervals between activities
    all_starts = [it["start_ts"] for it in items]
    all_ends = [it["end_ts"] for it in items]
    if all_starts and all_ends:
        day_first = min(all_starts)
        day_last = max(all_ends)
        busy_intervals = merge_intervals(active_intervals + merged_sess)
        idle_intervals = subtract_intervals([(day_first, day_last)], busy_intervals)

        for st, et in idle_intervals:
            dur_sec = et - st
            if dur_sec >= 60:  # Noticeable gaps >= 1 minute
                dur_min = round(dur_sec / 60.0, 1)
                items.append({
                    "type": "Inactive",
                    "category": "inactive",
                    "source": "System",
                    "start_ts": st,
                    "end_ts": et,
                    "start": datetime.fromtimestamp(st).strftime("%H:%M:%S"),
                    "end": datetime.fromtimestamp(et).strftime("%H:%M:%S"),
                    "duration_min": dur_min,
                    "duration_formatted": format_duration_hm(dur_sec),
                    "note": "Away from PC / Idle (خمول)",
                    "reason": "No Activity Detected",
                })

    items.sort(key=lambda x: x["start_ts"])
    return items


# ---------------------------------------------------------------------------
# HTML Dashboard Template Generator
# ---------------------------------------------------------------------------

def _build_single_day_data(
    db: Database,
    config: Config,
    d: date,
    history_14: list[dict[str, Any]] | None = None,
    summary_only: bool = False,
) -> dict[str, Any] | None:
    """Compile metrics, sessions, apps, and timeline for a single day.

    ``summary_only`` builds the cheap payload used for days outside the eager
    window (P1-4): exact totals from ``daily_summary`` but no timeline, app or
    project detail. It returns ``None`` when such a day has no summary row.
    """
    day_start_ts, day_end_ts = get_day_boundaries(d)
    timeline_window: dict[str, Any] = {}

    if summary_only:
        summary = db.get_daily_summary(d)
        if summary is None:
            return None
        from retention import metrics_from_summary

        events: list[int] = []
        events_with_apps: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        is_summarized = True
        metrics = metrics_from_summary(config, d, summary)
        app_usage: list[Any] = []
    else:
        events = db.get_input_events_between(day_start_ts, day_end_ts)
        events_with_apps = db.get_input_events_with_apps(day_start_ts, day_end_ts)
        sessions = db.get_sessions_between(day_start_ts, day_end_ts)
        summary = db.get_daily_summary(d)
        is_summarized = summary is not None

        metrics = get_day_metrics(db, config, d, is_live=(d == date.today()))
        app_usage = get_day_app_usage(db, config, d)

    if is_summarized and not events:
        # Raw events were purged by the retention policy: totals are still exact
        # (daily_summary), but minute-by-minute timelines no longer exist.
        hourly: list[dict[str, Any]] = []
        status_blocks: list[dict[str, Any]] = []
        app_blocks: list[dict[str, Any]] = []
        activity_log: list[dict[str, Any]] = []
    else:
        win_start, win_end = _timeline_window(d, events, sessions, events_with_apps, config)
        timeline_window = {
            "start_ts": win_start,
            "end_ts": win_end,
            "start_label": datetime.fromtimestamp(win_start).strftime("%H:%M"),
            "end_label": datetime.fromtimestamp(win_end).strftime("%H:%M"),
        }
        hourly = _get_hourly_breakdown(d, events, sessions, config)
        status_blocks = _get_status_timeline_blocks(
            d, events, sessions, config, window=(win_start, win_end)
        )
        app_blocks = _get_app_timeline_blocks(
            events_with_apps, day_start_ts, day_end_ts, config.grace_period_seconds,
            window=(win_start, win_end),
        )
        activity_log = _build_activity_and_sessions_log(d, events, sessions, events_with_apps, config)

    # Clean sessions for table
    formatted_sessions = []
    for s in sessions:
        st = int(s["start_ts"])
        et = int(s["end_ts"]) if s.get("end_ts") is not None else None
        st_dt = datetime.fromtimestamp(st).strftime("%H:%M:%S")
        et_dt = datetime.fromtimestamp(et).strftime("%H:%M:%S") if et else "In Progress"
        dur = round((et - st) / 60.0, 1) if et else 0.0
        formatted_sessions.append({
            "type": str(s.get("session_type", "")).capitalize(),
            "source": str(s.get("source", "")).capitalize(),
            "start": st_dt,
            "end": et_dt,
            "duration_min": dur,
            "note": str(s.get("note") or ""),
            "reason": str(s.get("reason") or ""),
        })

    # Color mapping for applications
    app_palette = [
        "#00bcd4", "#4caf50", "#ff9800", "#9c27b0", "#e91e63",
        "#3f51b5", "#009688", "#ff5722", "#795548", "#607d8b"
    ]
    app_colors: dict[str, str] = {}
    for idx, item in enumerate(app_usage):
        app_colors[item.app_name] = app_palette[idx % len(app_palette)]

    for b in app_blocks:
        b["color"] = app_colors.get(b["app"], "#78909c")

    target_h = metrics.target_hours
    progress_pct = min(100.0, round((metrics.worked_hours / target_h) * 100.0, 1)) if target_h > 0 else 0.0

    return {
        "date_str": d.strftime("%Y-%m-%d"),
        "date_display": d.strftime("%A, %B %d, %Y"),
        "day_name": metrics.day_name,
        "is_work_day": metrics.is_work_day,
        "worked_hours": metrics.worked_hours,
        "worked_formatted": metrics.worked_formatted,
        "target_hours": metrics.target_hours,
        "progress_pct": progress_pct,
        "overtime_hours": metrics.overtime_hours,
        "overtime_formatted": metrics.overtime_formatted,
        "meeting_hours": metrics.meeting_hours,
        "meeting_formatted": metrics.meeting_formatted,
        "unclassified_meeting_hours": metrics.unclassified_meeting_hours,
        "unclassified_meeting_formatted": metrics.unclassified_meeting_formatted,
        "break_hours": metrics.break_hours,
        "break_formatted": metrics.break_formatted,
        "remaining_hours": metrics.remaining_hours,
        "remaining_formatted": metrics.remaining_formatted,
        "shift_remaining_formatted": metrics.shift_remaining_formatted,
        "late_seconds": metrics.late_seconds,
        "late_formatted": metrics.late_formatted,
        "first_activity": metrics.first_activity_formatted,
        "last_activity": metrics.last_activity_formatted,
        "total_events": len(events),
        "app_usage": [asdict(item) for item in app_usage],
        "app_colors": app_colors,
        "hourly": hourly,
        "status_blocks": status_blocks,
        "app_blocks": app_blocks,
        "timeline_window": timeline_window,
        "history_14": history_14 or [],
        "sessions": formatted_sessions,
        "activity_log": activity_log,
        "is_summarized": is_summarized,
        "projects": [] if summary_only else project_breakdown(db, config, d, d),
        "config": {
            "work_start": config.work_start,
            "work_end": config.work_end,
            "work_days": config.work_days,
            "daily_target": config.daily_target_hours,
        }
    }


def build_dashboard_data(
    db: Database,
    config: Config,
    target_date: date | None = None,
) -> dict[str, Any]:
    """Compile all necessary data for the interactive visualization dashboard."""
    d = target_date or date.today()
    # Item 1c: older days are read from daily_summary, so make sure their rows
    # exist (and are fresh) before the page is compiled.
    try:
        from retention import aggregate_missing_days

        aggregate_missing_days(db, config)
    except Exception as exc:
        logger.warning("Could not refresh daily summaries for the dashboard: %s", exc)
    history_14 = _get_past_14_days_summary(db, config, d)

    # Collect distinct dates to make available in day filter
    dates_to_compile: set[date] = {
        datetime.strptime(item["date"], "%Y-%m-%d").date() for item in history_14
    }
    dates_to_compile.add(d)
    dates_to_compile.add(date.today())

    if hasattr(db, "get_recorded_dates"):
        try:
            # PERF-08: cap the number of historical days embedded in the payload;
            # older days remain queryable via the API instead of bloating the HTML.
            cutoff = date.today() - timedelta(days=90)
            for d_str in db.get_recorded_dates():
                try:
                    dd = datetime.strptime(d_str, "%Y-%m-%d").date()
                except (TypeError, ValueError):
                    continue
                if dd >= cutoff:
                    dates_to_compile.add(dd)
        except Exception as exc:
            logger.warning("Could not retrieve recorded dates: %s", exc)

    # P1-4: compile the last 14 days (plus the selected day) in full detail; older
    # days come from daily_summary only, which keeps large histories fast.
    eager_start = d - timedelta(days=13)
    days_map: dict[str, Any] = {}
    for item_d in sorted(dates_to_compile, reverse=True):
        d_key = item_d.strftime("%Y-%m-%d")
        if item_d >= eager_start:
            days_map[d_key] = _build_single_day_data(db, config, item_d, history_14=history_14)
            continue
        summary_day = _build_single_day_data(
            db, config, item_d, history_14=history_14, summary_only=True
        )
        if summary_day is not None:
            days_map[d_key] = summary_day

    target_key = d.strftime("%Y-%m-%d")
    current_day = days_map.get(target_key) or _build_single_day_data(db, config, d, history_14=history_14)

    payload = dict(current_day)
    payload["selected_date"] = target_key
    payload["available_dates"] = sorted(days_map.keys(), reverse=True)
    payload["days"] = days_map
    # Week / month / year totals compared with the previous period (uses
    # daily_summary for dates older than the retention window).
    payload["comparison"] = build_comparison(db, config, today=d, is_live=True)
    return payload


def generate_dashboard_html(
    data: dict[str, Any],
    server_port: int | None = None,
    server_token: str | None = None,
) -> str:
    """Generate the complete self-contained HTML page string."""
    # Escape "</" so a note or project name containing "</script>" cannot close
    # the payload script block (B22). User text is rendered with textContent.
    data_json = json.dumps(data).replace("</", "<\\/")
    server_port_js = server_port if server_port is not None else "null"
    server_token_js = server_token or ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Referrer-Policy" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; script-src-elem 'unsafe-inline'; script-src-attr 'none'; style-src 'unsafe-inline'; img-src data:; connect-src http://127.0.0.1:* http://localhost:*; font-src data:">
<title>BUSSMA_TTS — Time Tracking System | Bussma</title>
<style>
  :root {{
    --bg-primary: #0f172a;
    --bg-secondary: #1e293b;
    --bg-card: #1e293b;
    --bg-card-hover: #334155;
    --border-color: #334155;
    --text-primary: #f8fafc;
    --text-secondary: #94a3b8;
    --accent-work: #10b981;
    --accent-meeting: #ef4444;
    --accent-overtime: #3b82f6;
    --accent-break: #f59e0b;
    --accent-idle: #64748b;
    --accent-blue: #0284c7;
    --radius: 10px;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background-color: var(--bg-primary);
    color: var(--text-primary);
    font-family: var(--font);
    line-height: 1.5;
    padding: 24px;
  }}

  .container {{
    max-width: 1400px;
    margin: 0 auto;
  }}

  /* Top Navigation Bar */
  header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--bg-secondary);
    padding: 16px 24px;
    border-radius: var(--radius);
    border: 1px solid var(--border-color);
    margin-bottom: 20px;
    flex-wrap: wrap;
    gap: 16px;
  }}

  .logo-title {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .logo-badge {{
    width: 38px;
    height: 38px;
    background: linear-gradient(135deg, #10b981, #3b82f6);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    font-weight: bold;
    color: white;
  }}
  .title-group h1 {{
    font-size: 20px;
    font-weight: 700;
  }}
  .title-group p {{
    font-size: 13px;
    color: var(--text-secondary);
  }}

  .nav-controls {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .btn {{
    background: #334155;
    color: var(--text-primary);
    border: 1px solid #475569;
    padding: 8px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: all 0.2s;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }}
  .btn:hover {{
    background: #475569;
    color: white;
  }}
  .btn-primary {{
    background: var(--accent-blue);
    border-color: var(--accent-blue);
  }}
  .btn-primary:hover {{
    background: #0369a1;
  }}
  .current-date-badge {{
    background: #0f172a;
    padding: 8px 16px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 14px;
    border: 1px solid var(--border-color);
  }}

  /* Day Filter Controls */
  .day-filter-bar {{
    display: inline-flex;
    align-items: center;
    background: #0f172a;
    padding: 4px 8px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    gap: 8px;
    flex-wrap: wrap;
  }}
  .day-filter-label {{
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 4px;
    padding-left: 4px;
  }}
  .filter-arrow-btn {{
    background: #334155;
    color: #f8fafc;
    border: 1px solid #475569;
    border-radius: 4px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 12px;
    line-height: 1;
    transition: all 0.2s;
  }}
  .filter-arrow-btn:hover {{
    background: var(--accent-blue);
    border-color: var(--accent-blue);
  }}
  .date-picker-input {{
    background: #1e293b;
    border: 1px solid #475569;
    color: #f8fafc;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
    font-family: inherit;
    outline: none;
    cursor: pointer;
  }}
  .date-picker-input:focus {{
    border-color: var(--accent-blue);
  }}
  .date-dropdown {{
    background: #1e293b;
    border: 1px solid #475569;
    color: #f8fafc;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
    font-family: inherit;
    outline: none;
    cursor: pointer;
    max-width: 240px;
  }}
  .date-dropdown:focus {{
    border-color: var(--accent-blue);
  }}
  .btn-today {{
    background: #1e293b;
    border: 1px solid #475569;
    color: #38bdf8;
    font-weight: 600;
  }}
  .btn-today:hover {{
    background: #0284c7;
    color: #ffffff;
  }}
  .history-col {{
    cursor: pointer;
  }}
  .history-col:hover .bar-fill {{
    filter: brightness(1.3);
  }}

  /* View Tabs */
  .view-tabs {{
    display: flex;
    gap: 8px;
    margin-bottom: 20px;
  }}
  .tab-btn {{
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    padding: 10px 20px;
    border-radius: var(--radius);
    cursor: pointer;
    font-size: 14px;
    font-weight: 600;
    transition: all 0.2s;
  }}
  .tab-btn.active {{
    background: var(--accent-blue);
    color: white;
    border-color: var(--accent-blue);
  }}

  /* 14-day history bar */
  .history-card {{
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius);
    padding: 16px 20px;
    margin-bottom: 20px;
  }}
  .history-header {{
    display: flex;
    justify-content: space-between;
    margin-bottom: 12px;
    font-size: 13px;
    color: var(--text-secondary);
  }}
  .history-bars {{
    display: flex;
    align-items: flex-end;
    gap: 8px;
    height: 70px;
  }}
  .history-col {{
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    height: 100%;
    justify-content: flex-end;
    position: relative;
  }}
  .bar-fill {{
    width: 100%;
    min-height: 4px;
    border-radius: 4px 4px 0 0;
    background: #475569;
    transition: height 0.3s ease;
  }}
  .history-col.active-day .bar-fill {{
    background: linear-gradient(180deg, #38bdf8, #0284c7);
  }}
  .history-col:hover .bar-fill {{
    background: #94a3b8;
  }}
  .day-label {{
    font-size: 11px;
    color: var(--text-secondary);
  }}

  /* Metric KPI Cards */
  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }}
  .kpi-card {{
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius);
    padding: 18px;
    position: relative;
    overflow: hidden;
  }}
  .kpi-card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 4px;
  }}
  .kpi-worked::before {{ background: var(--accent-work); }}
  .kpi-ot::before {{ background: var(--accent-overtime); }}
  .kpi-meeting::before {{ background: var(--accent-meeting); }}
  .kpi-break::before {{ background: var(--accent-break); }}
  .kpi-late::before {{ background: #ef4444; }}
  .kpi-rem::before {{ background: #8b5cf6; }}

  .kpi-title {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }}
  .kpi-val {{
    font-size: 26px;
    font-weight: 700;
    color: var(--text-primary);
  }}
  .kpi-sub {{
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 4px;
  }}

  /* Main Grid */
  .content-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 24px;
  }}
  @media (max-width: 1024px) {{
    .content-grid {{ grid-template-columns: 1fr; }}
  }}

  .card {{
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius);
    padding: 20px;
  }}
  .card-title {{
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}

  /* Applications List (like ActivityWatch) */
  .app-item {{
    display: flex;
    flex-direction: column;
    margin-bottom: 14px;
  }}
  .app-header {{
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    margin-bottom: 6px;
  }}
  .app-name {{
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .app-dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
  }}
  .app-bar-bg {{
    background: #334155;
    height: 10px;
    border-radius: 5px;
    overflow: hidden;
  }}
  .app-bar-fill {{
    height: 100%;
    border-radius: 5px;
    transition: width 0.5s ease;
  }}

  /* Hourly Stacked Chart */
  .hourly-chart-container {{
    display: flex;
    align-items: flex-end;
    gap: 6px;
    height: 220px;
    padding-top: 20px;
  }}
  .hour-col {{
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    height: 100%;
    justify-content: flex-end;
    position: relative;
  }}
  .hour-stack {{
    width: 100%;
    display: flex;
    flex-direction: column-reverse;
    border-radius: 4px 4px 0 0;
    overflow: hidden;
  }}
  .segment-work {{ background: var(--accent-work); }}
  .segment-meeting {{ background: var(--accent-meeting); }}
  .segment-ot {{ background: var(--accent-overtime); }}
  .segment-break {{ background: var(--accent-break); }}
  .hour-label {{
    font-size: 10px;
    color: var(--text-secondary);
    margin-top: 6px;
    text-align: center;
  }}

  /* Timeline View (ActivityWatch style) */
  .timeline-container {{
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius);
    padding: 24px;
    overflow-x: auto;
  }}
  .timeline-ruler {{
    display: flex;
    height: 26px;
    border-bottom: 1px solid #475569;
    padding-bottom: 8px;
    margin-bottom: 14px;
    position: relative;
    font-size: 11px;
    color: var(--text-secondary);
  }}
  .timeline-ruler-hour {{
    position: absolute;
    top: 0;
    white-space: nowrap;
  }}
  .timeline-ruler-hour::after {{
    content: '';
    position: absolute;
    left: 0;
    bottom: -8px;
    width: 1px;
    height: 6px;
    background: #475569;
  }}

  .timeline-track-title {{
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 8px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .timeline-track {{
    position: relative;
    height: 38px;
    background: #0f172a;
    border-radius: 6px;
    border: 1px solid #334155;
    margin-bottom: 20px;
    overflow: hidden;
  }}
  .timeline-block {{
    position: absolute;
    height: 100%;
    border-radius: 4px;
    cursor: pointer;
    transition: opacity 0.2s;
    display: flex;
    align-items: center;
    padding: 0 6px;
    font-size: 11px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .timeline-block:hover {{
    opacity: 0.85;
    z-index: 10;
    outline: 2px solid white;
  }}
  .block-worked {{ background: var(--accent-work); color: #022c22; }}
  .block-meeting {{ background: var(--accent-meeting); color: white; }}
  .block-overtime {{ background: var(--accent-overtime); color: white; }}
  .block-break {{ background: var(--accent-break); color: #451a03; }}
  .block-idle_activity {{ background: var(--accent-idle); color: white; }}

  /* Sessions Table */
  .sessions-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  .sessions-table th {{
    text-align: left;
    padding: 10px;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-weight: 600;
  }}
  .sessions-table td {{
    padding: 10px;
    border-bottom: 1px solid #334155;
  }}
  .badge {{
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    display: inline-block;
  }}
  .badge-work {{ background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.4); }}
  .badge-meeting {{ background: rgba(139, 92, 246, 0.2); color: #c4b5fd; border: 1px solid rgba(139, 92, 246, 0.4); }}
  .badge-overtime {{ background: rgba(59, 130, 246, 0.2); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.4); }}
  .badge-break {{ background: rgba(245, 158, 11, 0.2); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.4); }}
  .badge-inactive {{ background: rgba(148, 163, 184, 0.2); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.3); }}

  .log-filters {{
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .log-filter-btn {{
    background: #0f172a;
    border: 1px solid #334155;
    color: #94a3b8;
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease-in-out;
  }}
  .log-filter-btn:hover {{
    border-color: #64748b;
    color: #f8fafc;
  }}
  .log-filter-btn.active {{
    background: #0284c7;
    border-color: #38bdf8;
    color: #ffffff;
    font-weight: 600;
  }}

  /* Tooltip */
  #timeline-tooltip {{
    position: fixed;
    background: #020617;
    color: white;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 12px;
    pointer-events: none;
    display: none;
    z-index: 9999;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    border: 1px solid #475569;
  }}
</style>
</head>
<body>
<script>
// SEC-07: keep the token out of browser history / Referer, but retain it in
// sessionStorage so an F5 / manual reload can re-authenticate the loopback page.
(function () {{
  var params = new URLSearchParams(location.search);
  var qToken = params.get('token');
  if (qToken) {{
    try {{ sessionStorage.setItem('tt_dash_token', qToken); }} catch (e) {{}}
    // The navigation reload sends cookies but cannot send an Authorization
    // header, so keep a loopback-only, Strict cookie for page reloads.
    try {{ document.cookie = 'tt_dash_token=' + encodeURIComponent(qToken) + '; Path=/; SameSite=Strict'; }} catch (e) {{}}
  }}
  if (location.search && location.search.indexOf('token=') !== -1) {{
    try {{ history.replaceState(null, '', location.pathname); }} catch (e) {{}}
  }}
}})();
</script>

<div id="timeline-tooltip"></div>

<div class="container">
  <!-- Top Navigation Header -->
  <header>
    <div class="logo-title">
      <div class="logo-badge">⏱</div>
      <div class="title-group">
        <h1>BUSSMA_TTS — Time Tracking System</h1>
        <p>by Bussma • <a href="https://www.bussma.com" target="_blank" style="color: #38bdf8; text-decoration: none;">www.bussma.com</a> • Local Telemetry & Analytics</p>
      </div>
    </div>

    <div class="nav-controls">
      <div class="day-filter-bar">
        <span class="day-filter-label">📅 Filter Day / تصفية باليوم:</span>
        <button class="filter-arrow-btn" data-step="-1" title="Previous Day (اليوم السابق)">◀</button>
        <input type="date" id="date-picker-input" class="date-picker-input" title="Choose date / اختر اليوم">
        <select id="date-dropdown" class="date-dropdown" title="Select day / اختر اليوم"></select>
        <button class="filter-arrow-btn" data-step="1" title="Next Day (اليوم التالي)">▶</button>
        <button class="btn btn-today" id="today-btn" title="Jump to Today">Today / اليوم</button>
      </div>
      <div class="current-date-badge" id="date-display">{data['date_display']}</div>
      <button id="live-refresh-btn" class="btn btn-primary" title="تحديث لحظي بدون إعادة تحميل الصفحة / Live Instant Refresh">🔄 Refresh</button>
      <span id="live-status-badge" style="font-size: 11px; color: #4ade80; background: rgba(34, 197, 94, 0.15); border: 1px solid rgba(34, 197, 94, 0.4); padding: 5px 10px; border-radius: 12px; display: inline-flex; align-items: center; gap: 4px;">● Live Sync</span>
    </div>
  </header>

  <!-- View Switcher Tabs -->
  <div class="view-tabs">
    <button class="tab-btn active" id="tab-summary-btn" data-view="summary">📊 Activity Summary</button>
    <button class="tab-btn" id="tab-timeline-btn" data-view="timeline">⏱ Work-Window Timeline</button>
  </div>

  <!-- 14-Day History Volume Bar -->
  <div class="history-card">
    <div class="history-header">
      <span>14-Day Activity Trend</span>
      <span id="history-target-sub">Daily Target: {data['target_hours']}h</span>
    </div>
    <div class="history-bars" id="history-bars-container"></div>
  </div>

  <!-- Metric KPI Cards -->
  <div class="kpi-grid">
    <div class="kpi-card kpi-worked">
      <div class="kpi-title">Worked Time</div>
      <div class="kpi-val" id="kpi-val-worked">{data['worked_formatted']}</div>
      <div class="kpi-sub" id="kpi-sub-worked">{data['progress_pct']}% of {data['target_hours']}h target</div>
    </div>
    <div class="kpi-card kpi-ot">
      <div class="kpi-title">Overtime</div>
      <div class="kpi-val" id="kpi-val-ot">{data['overtime_formatted']}</div>
      <div class="kpi-sub">Outside normal hours</div>
    </div>
    <div class="kpi-card kpi-meeting">
      <div class="kpi-title">Meetings (in Worked)</div>
      <div class="kpi-val" id="kpi-val-meeting">{data['meeting_formatted']}</div>
      <div class="kpi-sub">Included in Worked / ضمن العمل</div>
    </div>
    <div class="kpi-card kpi-break">
      <div class="kpi-title">Break / Idle</div>
      <div class="kpi-val" id="kpi-val-break">{data['break_formatted']}</div>
      <div class="kpi-sub">Gaps & recorded breaks</div>
    </div>
    <div class="kpi-card kpi-late">
      <div class="kpi-title">Late Arrival</div>
      <div class="kpi-val" id="kpi-val-late">{data['late_formatted']}</div>
      <div class="kpi-sub" id="kpi-sub-late">First: {data['first_activity']} | Last: {data['last_activity']}</div>
    </div>
    <div class="kpi-card kpi-rem">
      <div class="kpi-title">Remaining</div>
      <div class="kpi-val" id="kpi-val-rem">{data['shift_remaining_formatted']}</div>
      <div class="kpi-sub" id="kpi-sub-rem">Until shift end at {data['config']['work_end']}</div>
    </div>
  </div>

  <!-- VIEW 1: SUMMARY DASHBOARD -->
  <div id="view-summary">
    <!-- Period Comparison (Week / Month / Year vs the previous period) -->
    <div class="card" style="margin-bottom: 20px;">
      <div class="card-title">
        <span>Period Comparison <span style="font-size: 12px; color: var(--text-secondary);">(مقارنة الفترات)</span></span>
        <div class="log-filters">
          <button class="log-filter-btn active" data-period="week">Week / أسبوع</button>
          <button class="log-filter-btn" data-period="month">Month / شهر</button>
          <button class="log-filter-btn" data-period="year">Year / سنة</button>
        </div>
      </div>
      <div id="comparison-container"></div>
    </div>

    <div class="content-grid">
      <!-- Top Applications Card -->
      <div class="card">
        <div class="card-title">
          <span>Top Applications</span>
          <span id="apps-active-count" style="font-size: 12px; color: var(--text-secondary);">{len(data['app_usage'])} apps active</span>
        </div>
        <div id="apps-container"></div>
      </div>

      <!-- Hourly Intensity Barchart -->
      <div class="card">
        <div class="card-title">
          <span>Hourly Activity Breakdown</span>
          <span style="font-size: 12px; color: var(--text-secondary);">Minutes per hour (00..23)</span>
        </div>
        <div class="hourly-chart-container" id="hourly-container"></div>
      </div>

      <!-- Project Breakdown Card -->
      <div class="card">
        <div class="card-title">
          <span>Projects <span style="font-size: 12px; color: var(--text-secondary);">(المشاريع)</span></span>
          <span style="font-size: 12px; color: var(--text-secondary);">Worked time attributed per project</span>
        </div>
        <div id="projects-container"></div>
      </div>
    </div>

    <!-- Activity & Sessions Log Table -->
    <div class="card">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
        <div class="card-title" style="margin-bottom: 0;">
          <span>Recorded Sessions Log & Activity Timeline</span>
          <span style="font-size: 12px; color: var(--text-secondary); margin-left: 8px;">(سجل النشاط والفترات الزمنيّة)</span>
        </div>
        <div class="log-filters">
          <button class="log-filter-btn active" data-filter="all">All / الكل</button>
          <button class="log-filter-btn" data-filter="work">💻 Work / عمل فعلي</button>
          <button class="log-filter-btn" data-filter="meeting">👥 Meetings / اجتماعات</button>
          <button class="log-filter-btn" data-filter="break">☕ Breaks & Pauses / استراحة</button>
          <button class="log-filter-btn" data-filter="overtime">⚡ Overtime / إضافي</button>
          <button class="log-filter-btn" data-filter="inactive">💤 Inactive / خمول</button>
        </div>
      </div>
      <table class="sessions-table">
        <thead>
          <tr>
            <th>Type (النوع)</th>
            <th>Source / App (المصدر أو التطبيق)</th>
            <th>Start Time (البدء)</th>
            <th>End Time (الانتهاء)</th>
            <th>Duration (المدة)</th>
            <th>Note / Details (التفاصيل والملاحظات)</th>
            <th>Category / Status (الحالة)</th>
          </tr>
        </thead>
        <tbody id="sessions-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- VIEW 2: TIMELINE VIEW -->
  <div id="view-timeline" style="display: none;">
    <div class="timeline-container">
      <div class="card-title" style="margin-bottom: 20px;">
        <span id="timeline-title">Interactive Work-Window Timeline</span>
        <span style="font-size: 12px; color: var(--text-secondary);">Hover over any segment to inspect duration and details</span>
      </div>

      <!-- 24h Ruler -->
      <div class="timeline-ruler" id="ruler-container"></div>

      <!-- Track 1: Status & Sessions -->
      <div class="timeline-track-title">Activity Status (Worked / Meeting / Overtime / Break)</div>
      <div class="timeline-track" id="track-status"></div>

      <!-- Track 2: Applications -->
      <div class="timeline-track-title">Active Applications Stream</div>
      <div class="timeline-track" id="track-apps"></div>
    </div>
  </div>
</div>

<script>
const ROOT_DATA = {data_json};
const SERVER_PORT = {server_port_js};
const SERVER_TOKEN = "{server_token_js}";
const DAYS = ROOT_DATA.days || {{}};
let ACTIVE_DATE = ROOT_DATA.selected_date || ROOT_DATA.date_str;
let CURRENT_DATA = DAYS[ACTIVE_DATE] || ROOT_DATA;

function getEmptyDay(dateStr) {{
  const dt = new Date(dateStr + "T00:00:00");
  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const dName = isNaN(dt.getTime()) ? "" : dayNames[dt.getDay()];
  const display = isNaN(dt.getTime()) ? dateStr : dt.toLocaleDateString("en-US", {{ weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' }});
  const targetH = (ROOT_DATA.config && ROOT_DATA.config.daily_target) ? ROOT_DATA.config.daily_target : 8.0;

  return {{
    date_str: dateStr,
    date_display: display,
    day_name: dName,
    is_work_day: true,
    worked_hours: 0.0,
    worked_formatted: "00h 00m",
    target_hours: targetH,
    progress_pct: 0.0,
    overtime_hours: 0.0,
    overtime_formatted: "00h 00m",
    meeting_hours: 0.0,
    meeting_formatted: "00h 00m",
    break_hours: 0.0,
    break_formatted: "00h 00m",
    remaining_hours: targetH,
    remaining_formatted: `${{String(Math.floor(targetH)).padStart(2, '0')}}h 00m`,
    shift_remaining_formatted: "00h 00m",
    late_seconds: 0,
    late_formatted: "00h 00m",
    first_activity: "—",
    last_activity: "—",
    total_events: 0,
    app_usage: [],
    app_colors: {{}},
    hourly: Array.from({{length: 24}}, (_, i) => ({{
      hour: i,
      label: (i < 10 ? '0' + i : '' + i) + ':00',
      worked_min: 0, meeting_min: 0, overtime_min: 0, break_min: 0, total_active_min: 0
    }})),
    status_blocks: [],
    app_blocks: [],
    history_14: ROOT_DATA.history_14 || [],
    sessions: [],
    activity_log: [],
    config: ROOT_DATA.config || {{}}
  }};
}}

// Switch between Summary and Timeline views
function switchView(viewName) {{
  const summaryView = document.getElementById("view-summary");
  const timelineView = document.getElementById("view-timeline");
  const summaryBtn = document.getElementById("tab-summary-btn");
  const timelineBtn = document.getElementById("tab-timeline-btn");

  if (viewName === 'summary') {{
    summaryView.style.display = 'block';
    timelineView.style.display = 'none';
    summaryBtn.classList.add('active');
    timelineBtn.classList.remove('active');
  }} else {{
    summaryView.style.display = 'none';
    timelineView.style.display = 'block';
    summaryBtn.classList.remove('active');
    timelineBtn.classList.add('active');
  }}
}}

// Escape user-controlled text before it reaches innerHTML (B22).
function esc(value) {{
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}}

// Tooltip helpers
const tooltip = document.getElementById("timeline-tooltip");
function showTooltip(e, text) {{
  tooltip.innerHTML = text;
  tooltip.style.display = 'block';
  tooltip.style.left = (e.clientX + 14) + 'px';
  tooltip.style.top = (e.clientY + 14) + 'px';
}}
function hideTooltip() {{
  tooltip.style.display = 'none';
}}

// 1. Render 14-day history volume bars
function renderHistory() {{
  const container = document.getElementById("history-bars-container");
  if (!container) return;
  container.innerHTML = "";
  const historyList = CURRENT_DATA.history_14 || ROOT_DATA.history_14 || [];
  const maxHours = Math.max(8.0, ...historyList.map(d => (d.worked_hours || 0) + (d.overtime_hours || 0)));

  historyList.forEach(d => {{
    const isSelected = (d.date === ACTIVE_DATE);
    const col = document.createElement("div");
    col.className = "history-col" + (isSelected ? " active-day" : "");
    col.style.cursor = "pointer";
    col.title = `Click to view data for ${{d.date}} (${{d.day_name}})`;
    col.onclick = () => selectDay(d.date);

    const totalH = (d.worked_hours || 0) + (d.overtime_hours || 0);
    const heightPct = Math.min(100, Math.round((totalH / maxHours) * 100));

    col.innerHTML = `
      <div class="bar-fill" style="height: ${{Math.max(4, heightPct)}}%;"></div>
      <div class="day-label">${{d.day_name}}</div>
    `;
    col.onmouseenter = (e) => showTooltip(e, `<strong>${{d.date}} (${{d.day_name}})</strong><br>Worked: ${{d.worked_hours}}h<br>Overtime: ${{d.overtime_hours}}h<br><span style="color: #38bdf8; font-size: 11px;">👉 Click to filter this day</span>`);
    col.onmouseleave = hideTooltip;
    container.appendChild(col);
  }});
}}

// 2. Render Applications List
function renderApps() {{
  const container = document.getElementById("apps-container");
  if (!container) return;
  container.innerHTML = "";

  const appUsage = CURRENT_DATA.app_usage || [];
  const appColors = CURRENT_DATA.app_colors || {{}};

  if (appUsage.length === 0) {{
    container.innerHTML = `<div style="color: var(--text-secondary); padding: 20px 0; text-align: center;">No active application input recorded for this day (${{ACTIVE_DATE}}).</div>`;
    return;
  }}

  appUsage.slice(0, 8).forEach(item => {{
    const color = appColors[item.app_name] || "#00bcd4";
    const div = document.createElement("div");
    div.className = "app-item";
    div.innerHTML = `
      <div class="app-header">
        <span class="app-name">
          <span class="app-dot" style="background: ${{color}};"></span>
          ${{esc(item.app_name)}}
        </span>
        <span style="color: var(--text-secondary);">${{item.formatted}} (${{item.percentage}}%)</span>
      </div>
      <div class="app-bar-bg">
        <div class="app-bar-fill" style="width: ${{item.percentage}}%; background: ${{color}};"></div>
      </div>
    `;
    container.appendChild(div);
  }});
}}

// 3. Render Hourly Chart
function renderHourly() {{
  const container = document.getElementById("hourly-container");
  if (!container) return;
  container.innerHTML = "";

  const hourly = CURRENT_DATA.hourly || [];
  hourly.forEach(h => {{
    const col = document.createElement("div");
    col.className = "hour-col";

    const totalMin = Math.min(60, (h.worked_min || 0) + (h.overtime_min || 0) + (h.meeting_min || 0) + (h.break_min || 0));
    const heightPct = Math.round((totalMin / 60) * 100);

    const wH = Math.round(((h.worked_min || 0) / 60) * 100);
    const mH = Math.round(((h.meeting_min || 0) / 60) * 100);
    const otH = Math.round(((h.overtime_min || 0) / 60) * 100);
    const bH = Math.round(((h.break_min || 0) / 60) * 100);

    col.innerHTML = `
      <div class="hour-stack" style="height: ${{heightPct}}%;">
        ${{wH > 0 ? `<div class="segment-work" style="height: ${{wH}}%;"></div>` : ''}}
        ${{mH > 0 ? `<div class="segment-meeting" style="height: ${{mH}}%;"></div>` : ''}}
        ${{otH > 0 ? `<div class="segment-ot" style="height: ${{otH}}%;"></div>` : ''}}
        ${{bH > 0 ? `<div class="segment-break" style="height: ${{bH}}%;"></div>` : ''}}
      </div>
      <div class="hour-label">${{h.hour % 2 === 0 ? h.label : ''}}</div>
    `;

    col.onmouseenter = (e) => showTooltip(e, `
      <strong>Hour ${{h.label}}</strong><br>
      Worked: ${{h.worked_min}}m<br>
      Meeting: ${{h.meeting_min}}m<br>
      Overtime: ${{h.overtime_min}}m<br>
      Break: ${{h.break_min}}m
    `);
    col.onmouseleave = hideTooltip;

    container.appendChild(col);
  }});
}}

let currentLogFilter = 'all';

function setLogFilter(filterType) {{
  currentLogFilter = filterType;
  document.querySelectorAll('.log-filter-btn').forEach(btn => {{
    btn.classList.toggle('active', btn.getAttribute('data-filter') === filterType);
  }});
  renderSessions();
}}

// 4. Render Activity & Sessions Table
function renderSessions() {{
  const tbody = document.getElementById("sessions-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const fullList = (CURRENT_DATA.activity_log && CURRENT_DATA.activity_log.length > 0)
    ? CURRENT_DATA.activity_log
    : (CURRENT_DATA.sessions || []);

  let filtered = fullList;
  if (currentLogFilter !== 'all') {{
    filtered = fullList.filter(item => {{
      const cat = (item.category || item.type || '').toLowerCase();
      return cat.includes(currentLogFilter);
    }});
  }}

  if (filtered.length === 0) {{
    tbody.innerHTML = `<tr><td colspan="7" style="color: var(--text-secondary); text-align: center; padding: 25px;">No recorded activities matching this filter for ${{ACTIVE_DATE}}.</td></tr>`;
    return;
  }}

  filtered.forEach(s => {{
    const cat = (s.category || s.type || 'work').toLowerCase().replace(/[^a-z-]/g, '');
    const badgeClass = "badge-" + cat;
    const durDisplay = s.duration_formatted ? s.duration_formatted : (s.duration_min + ' min');
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="badge ${{badgeClass}}">${{esc(s.type)}}</span></td>
      <td><strong>${{esc(s.source)}}</strong></td>
      <td>${{s.start}}</td>
      <td>${{s.end}}</td>
      <td>${{durDisplay}}</td>
      <td>${{esc(s.note) || '-'}}</td>
      <td>${{esc(s.reason) || '-'}}</td>
    `;
    tbody.appendChild(tr);
  }});
}}

// 5. Render Timeline Ruler & Streams
function renderTimeline() {{
  const ruler = document.getElementById("ruler-container");
  const win = CURRENT_DATA.timeline_window || {{}};
  const title = document.getElementById("timeline-title");
  if (title) {{
    title.innerText = win.start_label
      ? `Interactive Work-Window Timeline  ·  ${{win.start_label}} – ${{win.end_label}}`
      : 'Interactive Work-Window Timeline';
  }}
  if (ruler) {{
    ruler.innerHTML = "";
    const startTs = Number(win.start_ts || 0);
    const endTs = Number(win.end_ts || 0);
    if (startTs && endTs && endTs > startTs) {{
      const spanH = (endTs - startTs) / 3600000;
      const stepH = spanH <= 8 ? 1 : (spanH <= 14 ? 2 : 3);
      const first = new Date(startTs * 1000);
      first.setMinutes(0, 0, 0);
      let t = first.getTime();
      for (; t / 1000 <= endTs; t += stepH * 3600000) {{
        const sec = Math.max(startTs, Math.round(t / 1000));
        const pct = ((sec - startTs) / (endTs - startTs)) * 100;
        const d = document.createElement("div");
        d.className = "timeline-ruler-hour";
        d.style.left = Math.min(99, pct) + "%";
        if (pct > 88) d.style.transform = "translateX(-100%)";
        const hh = new Date(t).getHours();
        d.innerText = `${{hh < 10 ? '0' + hh : hh}}:00`;
        ruler.appendChild(d);
      }}
      const last = document.createElement("div");
      last.className = "timeline-ruler-hour";
      last.style.left = "100%";
      last.style.transform = "translateX(-100%)";
      last.innerText = win.end_label || "";
      ruler.appendChild(last);
    }} else {{
      for (let i = 0; i <= 24; i += 2) {{
        const d = document.createElement("div");
        d.className = "timeline-ruler-hour";
        d.style.left = (i / 24) * 100 + "%";
        d.innerText = `${{i < 10 ? '0' + i : i}}:00`;
        ruler.appendChild(d);
      }}
    }}
  }}

  // Status Track
  const statusTrack = document.getElementById("track-status");
  if (statusTrack) {{
    statusTrack.innerHTML = "";
    const blocks = CURRENT_DATA.status_blocks || [];
    if (blocks.length === 0) {{
      statusTrack.innerHTML = `<div style="color: var(--text-secondary); padding: 8px 12px; font-size: 12px;">No activity blocks recorded for this day.</div>`;
    }} else {{
      blocks.forEach(b => {{
        const el = document.createElement("div");
        el.className = `timeline-block block-${{b.type}}`;
        el.style.left = b.start_pct + "%";
        el.style.width = b.width_pct + "%";
        el.innerText = b.width_pct > 3.0 ? b.label : '';

        el.onmouseenter = (e) => showTooltip(e, `
          <strong>${{b.label}}</strong><br>
          ${{b.start_time}} → ${{b.end_time}}<br>
          Duration: ${{Math.round(b.duration_sec / 60)}} min
          ${{b.note ? `<br>Note: ${{esc(b.note)}}` : ''}}
          ${{b.reason ? `<br>Reason: ${{esc(b.reason)}}` : ''}}
        `);
        el.onmouseleave = hideTooltip;

        statusTrack.appendChild(el);
      }});
    }}
  }}

  // Apps Track
  const appsTrack = document.getElementById("track-apps");
  if (appsTrack) {{
    appsTrack.innerHTML = "";
    const appBlocks = CURRENT_DATA.app_blocks || [];
    if (appBlocks.length === 0) {{
      appsTrack.innerHTML = `<div style="color: var(--text-secondary); padding: 8px 12px; font-size: 12px;">No application activity for this day.</div>`;
    }} else {{
      appBlocks.forEach(b => {{
        const el = document.createElement("div");
        el.className = "timeline-block";
        el.style.left = b.start_pct + "%";
        el.style.width = b.width_pct + "%";
        el.style.background = b.color;
        el.style.color = '#ffffff';
        el.innerText = b.width_pct > 3.5 ? b.app : '';

        el.onmouseenter = (e) => showTooltip(e, `
          <strong>${{esc(b.app)}}</strong><br>
          ${{b.start_time}} → ${{b.end_time}}<br>
          Duration: ${{Math.round(b.duration_sec / 60)}} min
        `);
        el.onmouseleave = hideTooltip;

        appsTrack.appendChild(el);
      }});
    }}
  }}
}}

// Day Filter Navigation & Selection
function initDateControls() {{
  const picker = document.getElementById("date-picker-input");
  const dropdown = document.getElementById("date-dropdown");
  if (!picker || !dropdown) return;

  picker.value = ACTIVE_DATE;
  dropdown.innerHTML = "";

  const dates = (ROOT_DATA.available_dates && ROOT_DATA.available_dates.length > 0)
    ? [...ROOT_DATA.available_dates]
    : Object.keys(DAYS).sort().reverse();

  if (!dates.includes(ACTIVE_DATE)) {{
    dates.unshift(ACTIVE_DATE);
  }}

  const todayStr = new Date().toISOString().slice(0, 10);

  dates.forEach(dStr => {{
    const opt = document.createElement("option");
    opt.value = dStr;
    const dObj = DAYS[dStr];
    let label = dStr;
    if (dStr === todayStr) {{
      label += " (Today / اليوم)";
    }} else if (dObj && dObj.day_name) {{
      label += ` (${{dObj.day_name}})`;
    }}
    opt.innerText = label;
    dropdown.appendChild(opt);
  }});

  dropdown.value = ACTIVE_DATE;
}}

function selectDay(dateStr) {{
  if (!dateStr) return;
  ACTIVE_DATE = dateStr;
  CURRENT_DATA = DAYS[dateStr] || getEmptyDay(dateStr);

  const picker = document.getElementById("date-picker-input");
  if (picker) picker.value = dateStr;

  const dropdown = document.getElementById("date-dropdown");
  if (dropdown) {{
    if (![...dropdown.options].some(o => o.value === dateStr)) {{
      const opt = document.createElement("option");
      opt.value = dateStr;
      opt.innerText = dateStr;
      dropdown.prepend(opt);
    }}
    dropdown.value = dateStr;
  }}

  const dateDisp = document.getElementById("date-display");
  if (dateDisp) dateDisp.innerText = CURRENT_DATA.date_display;

  const kWorked = document.getElementById("kpi-val-worked");
  const kWorkedSub = document.getElementById("kpi-sub-worked");
  if (kWorked) kWorked.innerText = CURRENT_DATA.worked_formatted;
  if (kWorkedSub) kWorkedSub.innerText = `${{CURRENT_DATA.progress_pct}}% of ${{CURRENT_DATA.target_hours}}h target`;

  const kOt = document.getElementById("kpi-val-ot");
  if (kOt) kOt.innerText = CURRENT_DATA.overtime_formatted;

  const kMeeting = document.getElementById("kpi-val-meeting");
  if (kMeeting) kMeeting.innerText = CURRENT_DATA.meeting_formatted;

  const kBreak = document.getElementById("kpi-val-break");
  if (kBreak) kBreak.innerText = CURRENT_DATA.break_formatted;

  const kLate = document.getElementById("kpi-val-late");
  const kLateSub = document.getElementById("kpi-sub-late");
  if (kLate) kLate.innerText = CURRENT_DATA.late_formatted || "00h 00m";
  if (kLateSub) kLateSub.innerText = `First: ${{CURRENT_DATA.first_activity}} | Last: ${{CURRENT_DATA.last_activity}}`;

  const kRem = document.getElementById("kpi-val-rem");
  const kRemSub = document.getElementById("kpi-sub-rem");
  if (kRem) kRem.innerText = CURRENT_DATA.shift_remaining_formatted || CURRENT_DATA.remaining_formatted;
  if (kRemSub) {{
    const cfgEnd = (CURRENT_DATA.config && CURRENT_DATA.config.work_end) || (ROOT_DATA.config && ROOT_DATA.config.work_end) || "";
    kRemSub.innerText = cfgEnd ? `Until shift end at ${{cfgEnd}}` : "Until shift end";
  }}
  updateLiveRemaining();

  const appsCount = document.getElementById("apps-active-count");
  if (appsCount) appsCount.innerText = `${{(CURRENT_DATA.app_usage || []).length}} apps active`;

  renderHistory();
  renderApps();
  renderHourly();
  renderSessions();
  renderTimeline();
  renderProjects();
}}

function onDateInputChange(val) {{
  if (val) selectDay(val);
}}

function onDateDropdownChange(val) {{
  if (val) selectDay(val);
}}

function stepDay(offset) {{
  const cur = new Date(ACTIVE_DATE + "T00:00:00");
  if (isNaN(cur.getTime())) return;
  cur.setDate(cur.getDate() + offset);
  const nextStr = cur.toISOString().slice(0, 10);
  selectDay(nextStr);
}}

function selectToday() {{
  const todayStr = new Date().toISOString().slice(0, 10);
  selectDay(todayStr);
}}

// ---------------------------------------------------------------------------
// 6. Period comparison (week / month / year vs the previous period)
// ---------------------------------------------------------------------------
let ACTIVE_PERIOD = 'week';

function formatDelta(value) {{
  if (value === null || value === undefined) {{
    return `<span style="color: var(--text-secondary);">—</span>`;
  }}
  const sign = value > 0 ? '+' : '';
  const color = value > 0 ? '#34d399' : (value < 0 ? '#f87171' : '#94a3b8');
  return `<span style="color: ${{color}}; font-weight: 600;">${{sign}}${{value}}%</span>`;
}}

function setPeriod(kind) {{
  ACTIVE_PERIOD = kind;
  document.querySelectorAll('[data-period]').forEach(btn => {{
    btn.classList.toggle('active', btn.getAttribute('data-period') === kind);
  }});
  renderComparison();
}}

function renderComparison() {{
  const container = document.getElementById('comparison-container');
  if (!container) return;

  const data = ROOT_DATA.comparison || {{}};
  const period = data[ACTIVE_PERIOD];
  if (!period) {{
    container.innerHTML = `<div style="color: var(--text-secondary); padding: 12px 0;">No comparison data available.</div>`;
    return;
  }}

  const cur = period.current || {{}};
  const prev = period.previous || {{}};
  const delta = period.delta || {{}};

  const rows = [
    ['Worked / ساعات العمل', cur.worked_formatted || '0h 00m', prev.worked_formatted || '0h 00m', delta.worked_pct],
    ['Overtime / وقت إضافي', cur.overtime_formatted || '0h 00m', prev.overtime_formatted || '0h 00m', delta.overtime_pct],
    ['Meetings (in Worked) / اجتماعات (ضمن العمل)', cur.meeting_formatted || '0h 00m', prev.meeting_formatted || '0h 00m', delta.meeting_pct],
    ['Break / استراحة', cur.break_formatted || '0h 00m', prev.break_formatted || '0h 00m', delta.break_pct],
  ];

  const html = `
    <table class="sessions-table">
      <thead>
        <tr>
          <th>Metric</th>
          <th>Current: ${{cur.start || ''}} → ${{cur.end || ''}}</th>
          <th>Previous: ${{prev.start || ''}} → ${{prev.end || ''}}</th>
          <th>Change</th>
        </tr>
      </thead>
      <tbody>
        ${{rows.map(r => `<tr><td>${{r[0]}}</td><td>${{r[1]}}</td><td>${{r[2]}}</td><td>${{formatDelta(r[3])}}</td></tr>`).join('')}}
        <tr>
          <td>Top App / أكثر برنامج</td>
          <td>${{esc(period.top_app || '—')}}</td>
          <td>${{esc(period.previous_top_app || '—')}}</td>
          <td><span style="color: var(--text-secondary);">—</span></td>
        </tr>
      </tbody>
    </table>
  `;
  container.innerHTML = html;
}}

// ---------------------------------------------------------------------------
// 7. Project breakdown card
// ---------------------------------------------------------------------------
function renderProjects() {{
  const container = document.getElementById('projects-container');
  if (!container) return;

  const rows = (CURRENT_DATA && CURRENT_DATA.projects) ? CURRENT_DATA.projects : [];
  if (rows.length === 0) {{
    container.innerHTML = `<div style="color: var(--text-secondary); padding: 12px 0; text-align: center;">No project-tagged activity for this day.</div>`;
    return;
  }}

  container.innerHTML = rows.map(row => `
    <div class="app-item">
      <div class="app-header">
        <span class="app-name">${{esc(row.name)}}</span>
        <span style="color: var(--text-secondary);">${{row.worked_formatted}} (${{row.percentage}}%)</span>
      </div>
      <div class="app-bar-bg">
        <div class="app-bar-fill" style="width: ${{Math.min(100, row.percentage)}}%; background: #8b5cf6;"></div>
      </div>
    </div>
  `).join('');
}}

// ---------------------------------------------------------------------------
// Live countdown: Remaining until shift end (today only)
// ---------------------------------------------------------------------------
function localDateStr(d) {{
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}}

function parseHm(str) {{
  if (!str) return null;
  const parts = str.split(":");
  const h = parseInt(parts[0], 10);
  const m = parseInt(parts[1] || "0", 10);
  if (isNaN(h) || isNaN(m)) return null;
  return {{ h, m }};
}}

function updateLiveRemaining() {{
  const el = document.getElementById("kpi-val-rem");
  if (!el) return;
  const cfg = (CURRENT_DATA && CURRENT_DATA.config) || ROOT_DATA.config || {{}};
  const endT = parseHm(cfg.work_end);
  const startT = parseHm(cfg.work_start);
  const now = new Date();
  // Countdown ticks live only for today; other days keep their stored value.
  if (!endT || !startT || ACTIVE_DATE !== localDateStr(now)) return;
  const nowMin = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60.0;
  const startMin = startT.h * 60 + startT.m;
  const endMin = endT.h * 60 + endT.m;
  let remMin = 0;
  if (nowMin < startMin) remMin = endMin - startMin;
  else if (nowMin < endMin) remMin = endMin - nowMin;
  const hours = Math.floor(remMin / 60);
  const mins = Math.floor(remMin % 60);
  el.innerText = `${{String(hours).padStart(2, "0")}}h ${{String(mins).padStart(2, "0")}}m`;
}}
setInterval(updateLiveRemaining, 1000);

// Live Dashboard Asynchronous Synchronization (Proposal 2)
async function fetchLiveDashboardData(userTriggered = false) {{
  const btn = document.getElementById("live-refresh-btn");
  const badge = document.getElementById("live-status-badge");
  const port = SERVER_PORT || location.port;
  let storedToken = "";
  try {{ storedToken = sessionStorage.getItem('tt_dash_token') || ''; }} catch (e) {{}}
  const token = SERVER_TOKEN || storedToken || (new URLSearchParams(location.search).get("token")) || "";
  if (!port || !token) {{
    if (badge) {{
      badge.textContent = "○ Live sync unavailable — reopen the dashboard from the tray";
      badge.style.color = "#f59e0b";
    }}
    return;
  }}
  if (btn) btn.textContent = "⏳ Syncing...";
  let lastStatus = 0;
  try {{
    const host = location.hostname || "127.0.0.1";
    const proto = location.protocol.startsWith("http") ? location.protocol : "http:";
    const url = `${{proto}}//${{host}}:${{port}}/api/data?date=${{encodeURIComponent(ACTIVE_DATE)}}`;
    const resp = await fetch(url, {{ headers: {{ "Authorization": `Bearer ${{token}}` }} }});
    lastStatus = resp.status;
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const json = await resp.json();
    if (json && json.success && json.data) {{
      const freshRoot = json.data;
      Object.assign(ROOT_DATA, freshRoot);
      const freshDays = freshRoot.days || {{}};
      for (const k in freshDays) {{
        DAYS[k] = freshDays[k];
      }}
      if (DAYS[ACTIVE_DATE]) {{
        CURRENT_DATA = DAYS[ACTIVE_DATE];
      }} else if (ACTIVE_DATE === freshRoot.selected_date) {{
        CURRENT_DATA = freshRoot;
      }}
      renderDay();
      renderComparison();
      if (badge) {{
        const timeStr = new Date().toLocaleTimeString();
        badge.textContent = `● Live (${{timeStr}})`;
        badge.style.color = "#4ade80";
      }}
    }}
  }} catch (err) {{
    console.warn("Live sync error:", err);
    if (badge) {{
      badge.textContent = lastStatus === 401
        ? "○ Session expired — reopen the dashboard from the tray"
        : "○ Offline";
      badge.style.color = "#f59e0b";
    }}
  }} finally {{
    if (btn) btn.textContent = "🔄 Refresh";
  }}
}}

if (SERVER_PORT || location.port) {{
  // PERF-01: poll less aggressively to avoid rebuilding the whole dataset every 15s.
  setInterval(() => {{
    if (!document.hidden) {{
      fetchLiveDashboardData(false);
    }}
  }}, 60000);
}}

// Bind controls without inline HTML handlers so script-src-attr 'none' (CSP)
// is respected. Property/event-listener bindings are not inline attributes.
function bindInlineHandlers() {{
  document.querySelectorAll('[data-step]').forEach(btn => {{
    btn.addEventListener('click', () => stepDay(Number(btn.dataset.step)));
  }});
  const picker = document.getElementById('date-picker-input');
  if (picker) picker.addEventListener('change', () => onDateInputChange(picker.value));
  const dropdown = document.getElementById('date-dropdown');
  if (dropdown) dropdown.addEventListener('change', () => onDateDropdownChange(dropdown.value));
  const todayBtn = document.getElementById('today-btn');
  if (todayBtn) todayBtn.addEventListener('click', () => selectToday());
  const refresh = document.getElementById('live-refresh-btn');
  if (refresh) refresh.addEventListener('click', () => fetchLiveDashboardData(true));
  document.querySelectorAll('[data-view]').forEach(btn => {{
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  }});
  document.querySelectorAll('[data-period]').forEach(btn => {{
    btn.addEventListener('click', () => setPeriod(btn.dataset.period));
  }});
  document.querySelectorAll('[data-filter]').forEach(btn => {{
    btn.addEventListener('click', () => setLogFilter(btn.dataset.filter));
  }});
}}

// Initialize all controls and views
bindInlineHandlers();
initDateControls();
selectDay(ACTIVE_DATE);
renderComparison();
</script>
</body>
</html>
"""
    return html


class _DashboardHttpHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # SEC-07: never write the bearer token to the dashboard access log.
        safe = tuple(
            str(a).split("?")[0] if isinstance(a, str) and "token=" in a.lower() else a
            for a in args
        )
        logger.debug("DashboardServer: %s - " + format, self.client_address[0], *safe)

    def do_GET(self) -> None:
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "localhost", "::1"):
            self.send_error(403, "Forbidden - Local Access Only")
            return

        # SEC-07: blunt DNS rebinding by rejecting any Host that is not the
        # loopback address this server is bound to.
        srv: DashboardServer = getattr(self.server, "dashboard_instance", None)  # type: ignore
        host_header = (self.headers.get("Host") or "").strip().lower()
        host_name = host_header.rsplit(":", 1)[0] if host_header else ""
        if host_name not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            self.send_error(403, "Forbidden - Invalid Host")
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        # Prefer the Authorization header; query tokens leak into history/logs.
        auth = (self.headers.get("Authorization") or "").strip()
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            token = params.get("token", [""])[0]
        if not token:
            # Page reloads are navigations: they carry cookies, not auth headers.
            cookie_header = self.headers.get("Cookie") or ""
            for part in cookie_header.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name == "tt_dash_token":
                    token = urllib.parse.unquote(value).strip()
                    break

        expected_token = getattr(srv, "token", "")
        if not token or token != expected_token:
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized: invalid or missing token"}')
            return

        if parsed.path in ("/", "/dashboard"):
            try:
                date_str = params.get("date", [None])[0]
                target_d = date.fromisoformat(date_str) if date_str else date.today()
                data = build_dashboard_data(srv.db, srv.config, target_date=target_d)
                html_str = generate_dashboard_html(data, server_port=srv.port, server_token=srv.token)
                html_bytes = html_str.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(html_bytes)
            except Exception as exc:
                logger.error("Error generating live dashboard HTML: %s", exc, exc_info=True)
                self.send_error(500, f"Error: {exc}")
            return

        if parsed.path == "/api/data":
            try:
                date_str = params.get("date", [None])[0]
                target_d = date.fromisoformat(date_str) if date_str else date.today()
                cache_key = target_d.isoformat()
                now = datetime.now().timestamp()
                # PERF-01: serve a short-lived cached payload to avoid rebuilding
                # the whole dataset on every poll.
                cache = getattr(srv, "_data_cache", None)
                if cache is not None and cache.get("key") == cache_key and (now - cache.get("ts", 0)) < 15.0:
                    body = cache["body"]
                else:
                    data = build_dashboard_data(srv.db, srv.config, target_date=target_d)
                    body = json.dumps({"success": True, "data": data}).encode("utf-8")
                    srv._data_cache = {"key": cache_key, "ts": now, "body": body}
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                logger.error("Error retrieving live dashboard data: %s", exc, exc_info=True)
                body = json.dumps({"success": False, "error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        if parsed.path == "/api/ping":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404, "Not Found")


class DashboardServer:
    def __init__(self, db: Database, config: Config, port: int = 0) -> None:
        self.db = db
        self.config = config
        self.token = secrets.token_hex(16)
        self._httpd = socketserver.TCPServer(("127.0.0.1", port), _DashboardHttpHandler)
        self._httpd.dashboard_instance = self  # type: ignore
        self.port: int = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="DashboardServerThread", daemon=True)
        self._thread.start()
        logger.info("DashboardServer listening on http://127.0.0.1:%d", self.port)

    def get_url(self, target_date: date | None = None) -> str:
        d_param = f"&date={target_date.isoformat()}" if target_date else ""
        return f"http://127.0.0.1:{self.port}/?token={self.token}{d_param}"

    def shutdown(self) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
            logger.info("DashboardServer stopped.")
        except Exception as exc:
            logger.warning("Error stopping DashboardServer: %s", exc)


_running_dashboard_server: DashboardServer | None = None
_dashboard_server_lock = threading.Lock()


def get_or_start_dashboard_server(db: Database, config: Config) -> DashboardServer:
    """Return active DashboardServer or spin up a new instance."""
    global _running_dashboard_server
    with _dashboard_server_lock:
        if _running_dashboard_server is None:
            _running_dashboard_server = DashboardServer(db, config)
        else:
            _running_dashboard_server.db = db
            _running_dashboard_server.config = config
        return _running_dashboard_server


def stop_dashboard_server() -> None:
    """Stop the running DashboardServer instance."""
    global _running_dashboard_server
    with _dashboard_server_lock:
        if _running_dashboard_server is not None:
            _running_dashboard_server.shutdown()
            _running_dashboard_server = None


def open_dashboard(
    db: Database,
    config: Config,
    target_date: date | None = None,
    open_browser: bool = True,
) -> Path:
    """Compile and open the visual dashboard in the default web browser."""
    data = build_dashboard_data(db, config, target_date=target_date)

    server: DashboardServer | None = None
    if getattr(config, "enable_dashboard_server", True):
        try:
            server = get_or_start_dashboard_server(db, config)
        except Exception as exc:
            logger.warning("Could not start DashboardServer (%s); fallback to static file.", exc)

    server_port = server.port if server else None
    server_token = server.token if server else None
    html_content = generate_dashboard_html(data, server_port=server_port, server_token=server_token)

    # SEC-07: the on-disk dashboard is a static snapshot; never persist the
    # live 128-bit server token there. The live copy is served by DashboardServer.
    static_content = html_content
    if server_token:
        static_content = html_content.replace(server_token, "")

    out_path = get_dashboard_html_path()
    try:
        out_path.write_text(static_content, encoding="utf-8")
        logger.info("Visual Dashboard written to %s", out_path)
    except (OSError, PermissionError) as exc:
        logger.warning(
            "Failed writing dashboard to %s (%s). Attempting temp file fallback...",
            out_path,
            exc,
        )
        temp_out = Path(tempfile.gettempdir()) / "TimeTracker_Dashboard.html"
        try:
            temp_out.write_text(static_content, encoding="utf-8")
            out_path = temp_out
            logger.info("Visual Dashboard written to fallback: %s", out_path)
        except Exception as inner_exc:
            error_msg = (
                f"Could not write Dashboard to primary path '{out_path}' nor fallback '{temp_out}': "
                f"{inner_exc}. Please verify folder write permissions or Group Policy restrictions."
            )
            logger.error(error_msg)
            raise OSError(error_msg) from exc

    if open_browser:
        try:
            if server:
                webbrowser.open(server.get_url(target_date))
            else:
                webbrowser.open(f"file://{out_path.resolve()}")
        except Exception as exc:
            logger.warning("Could not automatically launch browser for dashboard: %s", exc)

    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from config import load_config
    cfg = load_config()
    db = Database()
    p = open_dashboard(db, cfg)
    print(f"Dashboard opened: {p}")
