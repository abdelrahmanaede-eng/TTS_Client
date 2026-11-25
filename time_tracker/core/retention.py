"""Retention aggregation and summary-aware day analytics.

``input_events`` grow quickly (one row per few seconds of activity).  To keep the
database small the app purges raw events older than the configured retention
window.  Before purging, every affected day is aggregated into ``daily_summary``
so reports, the dashboard and the Excel/CSV exports keep identical totals.

All functions here are local-only and pure Python/SQLite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from time_tracker.core.calculator import (
    AppUsageItem,
    DayMetrics,
    Interval,
    ProjectUsageItem,
    calculate_app_usage,
    calculate_day_metrics,
    calculate_project_usage,
    format_duration_hm,
    get_day_boundaries,
    get_work_window_for_day,
    half_day_target_hours,
    presence_credit_intervals,
)
from time_tracker.core.config import Config
from time_tracker.core.database import Database

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
UNASSIGNED_KEY = "unassigned"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def retention_days(config: Config) -> int:
    """Return the configured retention in days (0 disables purging)."""
    try:
        return max(0, int(getattr(config, "retention_days", DEFAULT_RETENTION_DAYS) or 0))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def retention_enabled(config: Config) -> bool:
    return bool(getattr(config, "enable_retention_cleanup", True)) and retention_days(config) > 0


def retention_cutoff_date(config: Config, today: date | None = None) -> date | None:
    """Return the first date that is still kept as raw data (None = keep forever)."""
    if not retention_enabled(config):
        return None
    return (today or date.today()) - timedelta(days=retention_days(config))


def active_check_interval_seconds(config: Config) -> int:
    """Presence-check interval in seconds (0 when the feature is disabled)."""
    if not getattr(config, "active_check_enabled", True):
        return 0
    try:
        minutes = max(1, min(60, int(getattr(config, "active_check_interval_min", 3))))
    except (TypeError, ValueError):
        minutes = 3
    return minutes * 60


# ---------------------------------------------------------------------------
# Day level access (raw events when available, daily_summary otherwise)
# ---------------------------------------------------------------------------

def day_inputs(
    db: Database,
    config: Config,
    d: date,
) -> tuple[list[int], list[Interval], list[tuple[int, str, str, int | None]]]:
    """Return ``(events, confirm_credit_intervals, records)`` for a single day."""
    day_start_ts, day_end_ts = get_day_boundaries(d)
    records = db.get_input_events_with_details(day_start_ts, day_end_ts)
    events = [rec[0] for rec in records]
    credits = presence_credit_intervals(records, active_check_interval_seconds(config))
    return events, credits, records


def has_summary(db: Database, d: date) -> bool:
    return db.get_daily_summary(d) is not None


def day_has_raw_data(db: Database, d: date) -> bool:
    """True when the day still has raw input events on disk.

    Sessions are deliberately not checked: the purge keeps meeting/break/overtime
    sessions forever, so their presence cannot tell us whether a day was already
    aggregated.
    """
    day_start_ts, day_end_ts = get_day_boundaries(d)
    return bool(db.get_input_events_between(day_start_ts, day_end_ts))


def metrics_from_summary(config: Config, d: date, summary: dict[str, Any]) -> DayMetrics:
    """Rebuild a :class:`DayMetrics` object from an aggregated summary row."""
    is_holiday = bool(config.holidays) and d.isoformat() in config.holidays
    is_work_day = (
        get_work_window_for_day(d, config.work_start_time, config.work_end_time, config.work_days, config.holidays)
        is not None
    )
    worked = int(summary.get("worked_sec") or 0)
    target = int(summary.get("target_sec") or 0)
    return DayMetrics(
        target_date=d,
        day_name=d.strftime("%a"),
        is_work_day=is_work_day,
        first_activity_ts=None,
        last_activity_ts=None,
        worked_seconds=worked,
        overtime_seconds=int(summary.get("overtime_sec") or 0),
        target_seconds=target,
        remaining_seconds=max(0, target - worked),
        meeting_seconds=int(summary.get("meeting_sec") or 0),
        break_seconds=int(summary.get("break_sec") or 0),
        unclassified_meeting_seconds=int(summary.get("unclassified_meeting_sec") or 0),
        is_holiday=is_holiday,
    )


def get_day_metrics(
    db: Database,
    config: Config,
    d: date,
    half_day_dates: set[date] | None = None,
    is_live: bool = False,
    now_ts: int | None = None,
    ignore_summary: bool = False,
) -> DayMetrics:
    """Day metrics for ``d``, reading ``daily_summary`` when the day was purged.

    ``ignore_summary`` forces a fresh calculation from raw data, which is what a
    stale-summary rebuild needs (item 1).
    """
    summary = db.get_daily_summary(d)
    # Raw data is authoritative while it exists (it carries first/last activity and
    # honours per-call overrides such as a half-day target); the summary is the
    # fallback for purged days.
    if summary is not None and not ignore_summary and not day_has_raw_data(db, d):
        return metrics_from_summary(config, d, summary)

    events, credits, _records = day_inputs(db, config, d)
    sessions = db.get_sessions_between(*get_day_boundaries(d))
    target_h = (
        half_day_target_hours(config.daily_target_hours)
        if (half_day_dates and d in half_day_dates)
        else config.daily_target_hours
    )
    return calculate_day_metrics(
        target_date=d,
        events=events,
        sessions=sessions,
        work_start=config.work_start_time,
        work_end=config.work_end_time,
        work_days=config.work_days,
        holidays=config.holidays,
        daily_target_hours=target_h,
        grace_seconds=config.grace_period_seconds,
        is_live=is_live,
        now_ts=now_ts,
        confirm_credits=credits,
    )


def app_usage_from_summary(summary: dict[str, Any]) -> list[AppUsageItem]:
    app_secs = summary.get("app_secs") or {}
    total = sum(int(v) for v in app_secs.values() if isinstance(v, (int, float))) or 0
    items: list[AppUsageItem] = []
    for app_name, sec in sorted(
        ((str(k), int(v)) for k, v in app_secs.items() if isinstance(v, (int, float))),
        key=lambda item: item[1],
        reverse=True,
    ):
        if sec <= 0:
            continue
        items.append(
            AppUsageItem(
                app_name=app_name,
                active_seconds=sec,
                active_hours=round(sec / 3600.0, 2),
                formatted=format_duration_hm(sec),
                percentage=round((sec / total) * 100.0, 1) if total > 0 else 0.0,
            )
        )
    return items


def get_day_app_usage(
    db: Database,
    config: Config,
    d: date,
    is_live: bool = False,
    ignore_summary: bool = False,
) -> list[AppUsageItem]:
    """Application usage for ``d`` (summary fallback for purged days)."""
    summary = db.get_daily_summary(d)
    if summary is not None and not ignore_summary and not day_has_raw_data(db, d):
        return app_usage_from_summary(summary)

    _events, _credits, records = day_inputs(db, config, d)
    return calculate_app_usage(
        records,
        grace_seconds=config.grace_period_seconds,
        is_live=is_live,
        active_check_interval_sec=active_check_interval_seconds(config),
    )


def project_usage_dict(
    db: Database,
    config: Config,
    d: date,
    is_live: bool = False,
    scale_to_worked: bool = True,
    ignore_summary: bool = False,
) -> dict[str, int]:
    """Per-project active seconds for ``d`` as ``{"<project_id>|unassigned": sec}``."""
    summary = db.get_daily_summary(d)
    if summary is not None and not ignore_summary and not day_has_raw_data(db, d):
        raw = summary.get("project_secs") or {}
        return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}

    _events, _credits, records = day_inputs(db, config, d)
    items = calculate_project_usage(
        records,
        grace_seconds=config.grace_period_seconds,
        is_live=is_live,
        active_check_interval_sec=active_check_interval_seconds(config),
    )
    total_active = sum(item.active_seconds for item in items)
    result: dict[str, int] = {}
    if not scale_to_worked or total_active <= 0:
        for item in items:
            key = UNASSIGNED_KEY if item.project_id is None else str(item.project_id)
            result[key] = result.get(key, 0) + item.active_seconds
        return result

    metrics = get_day_metrics(db, config, d, ignore_summary=ignore_summary)
    factor = (metrics.worked_seconds / total_active) if total_active > 0 else 0.0
    for item in items:
        key = UNASSIGNED_KEY if item.project_id is None else str(item.project_id)
        result[key] = result.get(key, 0) + int(round(item.active_seconds * factor))
    return result


# ---------------------------------------------------------------------------
# Aggregation & purge
# ---------------------------------------------------------------------------

def aggregate_day(db: Database, config: Config, d: date, recompute: bool = False) -> dict[str, Any]:
    """Compute and persist the ``daily_summary`` row for ``d`` (idempotent).

    ``recompute`` ignores an existing (stale) summary row and rebuilds it from the
    raw events and sessions.
    """
    metrics = get_day_metrics(db, config, d, is_live=False, ignore_summary=recompute)
    app_items = get_day_app_usage(db, config, d, is_live=False, ignore_summary=recompute)
    app_secs = {item.app_name: item.active_seconds for item in app_items}
    project_secs = project_usage_dict(
        db, config, d, is_live=False, scale_to_worked=True, ignore_summary=recompute
    )

    db.upsert_daily_summary(
        summary_date=d,
        worked_sec=metrics.worked_seconds,
        overtime_sec=metrics.overtime_seconds,
        meeting_sec=metrics.meeting_seconds,
        break_sec=metrics.break_seconds,
        target_sec=metrics.target_seconds,
        app_secs=app_secs,
        project_secs=project_secs,
        data_version=db.get_day_data_version(d.isoformat()),
    )
    # D3: keep the unclassified meeting seconds auditable after the purge.
    db.set_summary_unclassified(d, metrics.unclassified_meeting_seconds)
    return {
        "date": d,
        "worked_sec": metrics.worked_seconds,
        "overtime_sec": metrics.overtime_seconds,
        "meeting_sec": metrics.meeting_seconds,
        "break_sec": metrics.break_seconds,
        "unclassified_meeting_sec": metrics.unclassified_meeting_seconds,
        "target_sec": metrics.target_seconds,
        "apps": app_secs,
        "projects": project_secs,
    }


def aggregate_missing_days(
    db: Database,
    config: Config,
    today: date | None = None,
) -> dict[str, Any]:
    """Aggregate every completed day whose ``daily_summary`` row is missing or stale.

    A day is eligible when it is before ``today`` and has raw events or sessions.
    A row is stale when its ``updated_ts`` is older than the newest data timestamp
    of that day (events or sessions). Re-running is idempotent: up-to-date rows are
    skipped, and recomputing a stable day produces identical values.

    Returns ``{"aggregated": n, "days": [...]}``.
    """
    today = today or date.today()
    try:
        changed = db.get_last_data_change_by_day()
    except Exception as exc:
        logger.error("Could not inspect data changes for aggregation: %s", exc)
        return {"aggregated": 0, "days": []}

    days: list[date] = []
    for key in changed:
        try:
            d = date.fromisoformat(key)
        except (TypeError, ValueError):
            continue
        if d < today:
            days.append(d)
    if not days:
        return {"aggregated": 0, "days": []}

    try:
        summaries = {
            row["date"]: row for row in db.get_daily_summaries(min(days), max(days))
        }
    except Exception as exc:
        logger.error("Could not read existing summaries for aggregation: %s", exc)
        summaries = {}

    aggregated: list[str] = []
    for d in sorted(days):
        key = d.isoformat()
        last_change = int(changed.get(key) or 0)
        row = summaries.get(key)
        stored_version = int(row.get("data_version") or 0) if row is not None else 0
        current_version = db.get_day_data_version(key)
        # Re-aggregate when the row is missing, when raw data has changed since
        # the last aggregation (per-day data_version bump), or when the
        # underlying data timestamp moved forward. This covers retroactive
        # session edits, whose session timestamps live in the past and would
        # otherwise be skipped.
        if (
            row is not None
            and stored_version >= current_version
            and int(row.get("updated_ts") or 0) >= last_change
        ):
            continue
        try:
            aggregate_day(db, config, d, recompute=row is not None)
            aggregated.append(key)
        except Exception as exc:  # a single bad day must not stop the rest
            logger.error("Could not aggregate %s: %s", d, exc)

    if aggregated:
        logger.info("Aggregated %d day(s) into daily_summary.", len(aggregated))
    return {"aggregated": len(aggregated), "days": aggregated}


def purge_old_data(db: Database, config: Config, today: date | None = None) -> dict[str, Any]:
    """Aggregate then purge raw events older than the retention window.

    Returns ``{"cutoff": date|None, "aggregated": n, "deleted": n}``.
    """
    today = today or date.today()
    cutoff = retention_cutoff_date(config, today)
    if cutoff is None:
        return {"cutoff": None, "aggregated": 0, "deleted": 0}

    # Aggregate first (missing and stale rows), then purge. Idempotent.
    aggregated = aggregate_missing_days(db, config, today)["aggregated"]

    # Never delete raw data for a day that still has no summary row: find the
    # earliest blocked day before the cutoff and stop the deletion there.
    blocked: date | None = None
    for date_str in db.get_recorded_dates():
        try:
            d = date.fromisoformat(date_str)
        except (TypeError, ValueError):
            continue
        if d >= cutoff:
            continue
        if db.get_daily_summary(d) is None:
            blocked = d if blocked is None or d < blocked else blocked

    delete_before = get_day_boundaries(blocked or cutoff)[0]
    deleted = db.delete_input_events_before(delete_before)
    if blocked is not None:
        logger.warning(
            "Retention purge stopped at %s: that day still has no summary row.", blocked
        )
    return {"cutoff": cutoff, "aggregated": aggregated, "deleted": deleted, "blocked_by": blocked}


# ---------------------------------------------------------------------------
# Period summaries & comparison
# ---------------------------------------------------------------------------

@dataclass
class PeriodTotals:
    label: str
    start: date
    end: date
    worked_sec: int = 0
    overtime_sec: int = 0
    meeting_sec: int = 0
    break_sec: int = 0
    target_sec: int = 0
    days_with_data: int = 0
    app_secs: dict[str, int] = field(default_factory=dict)

    @property
    def top_apps(self) -> list[tuple[str, int]]:
        return sorted(self.app_secs.items(), key=lambda item: item[1], reverse=True)[:5]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "worked_sec": self.worked_sec,
            "worked_hours": round(self.worked_sec / 3600.0, 2),
            "worked_formatted": format_duration_hm(self.worked_sec),
            "overtime_sec": self.overtime_sec,
            "overtime_hours": round(self.overtime_sec / 3600.0, 2),
            "overtime_formatted": format_duration_hm(self.overtime_sec),
            "meeting_sec": self.meeting_sec,
            "meeting_hours": round(self.meeting_sec / 3600.0, 2),
            "meeting_formatted": format_duration_hm(self.meeting_sec),
            "break_sec": self.break_sec,
            "break_hours": round(self.break_sec / 3600.0, 2),
            "break_formatted": format_duration_hm(self.break_sec),
            "target_sec": self.target_sec,
            "target_hours": round(self.target_sec / 3600.0, 2),
            "days_with_data": self.days_with_data,
            "top_apps": [
                {
                    "name": name,
                    "seconds": sec,
                    "hours": round(sec / 3600.0, 2),
                    "formatted": format_duration_hm(sec),
                }
                for name, sec in self.top_apps
            ],
        }


def bucket_records_by_day(
    records: Sequence[tuple[int, str, str, int | None]],
) -> dict[date, list[tuple[int, str, str, int | None]]]:
    """QUAL-03: one bucket-by-day primitive shared by every range aggregator.

    Invalid timestamps are skipped so a corrupt row cannot break a whole range.
    """
    by_day: dict[date, list[tuple[int, str, str, int | None]]] = {}
    for rec in records:
        try:
            rec_day = date.fromtimestamp(rec[0])
        except (OSError, OverflowError, ValueError):
            continue
        by_day.setdefault(rec_day, []).append(rec)
    return by_day


def _iter_days(start: date, end: date) -> Sequence[date]:
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def period_totals(
    db: Database,
    config: Config,
    start: date,
    end: date,
    label: str = "",
    half_day_dates: set[date] | None = None,
    is_live: bool = False,
) -> PeriodTotals:
    """Aggregate a date range using raw data or ``daily_summary`` as appropriate.

    Raw events/sessions/summaries for the whole range are fetched once and then
    bucketed per day in memory, so a year-long range costs a handful of queries.
    """
    totals = PeriodTotals(label=label, start=start, end=end)
    if end < start:
        return totals

    start_ts = get_day_boundaries(start)[0]
    end_ts = get_day_boundaries(end)[1]
    summaries = {row["date"]: row for row in db.get_daily_summaries(start, end)}
    records = db.get_input_events_with_details(start_ts, end_ts)
    sessions = db.get_sessions_between(start_ts, end_ts)
    interval_sec = active_check_interval_seconds(config)
    credits = presence_credit_intervals(records, interval_sec)

    # Bucket the range's records by day once. Scanning the whole range for every
    # day made a year-long comparison quadratic and dominated dashboard builds.
    records_by_day = bucket_records_by_day(records)

    for d in _iter_days(start, end):
        key = d.isoformat()
        if key in summaries:
            summary = summaries[key]
            metrics = metrics_from_summary(config, d, summary)
            app_items = app_usage_from_summary(summary)
        else:
            d_st, d_et = get_day_boundaries(d)
            day_records = records_by_day.get(d, [])
            day_sessions = [
                s for s in sessions
                if int(s["start_ts"]) < d_et
                and (s.get("end_ts") is None or int(s["end_ts"]) >= d_st)
            ]
            target_h = (
                half_day_target_hours(config.daily_target_hours)
                if (half_day_dates and d in half_day_dates)
                else config.daily_target_hours
            )
            # Item 5: the current day must use live values so the dashboard,
            # tray and Excel agree with each other.
            day_is_live = bool(is_live and d == date.today())
            metrics = calculate_day_metrics(
                target_date=d,
                events=[rec[0] for rec in day_records],
                sessions=day_sessions,
                work_start=config.work_start_time,
                work_end=config.work_end_time,
                work_days=config.work_days,
                holidays=config.holidays,
                daily_target_hours=target_h,
                grace_seconds=config.grace_period_seconds,
                is_live=day_is_live,
                now_ts=int(datetime.now().timestamp()) if day_is_live else None,
                confirm_credits=credits,
            )
            app_items = calculate_app_usage(
                day_records,
                grace_seconds=config.grace_period_seconds,
                is_live=day_is_live,
                active_check_interval_sec=interval_sec,
            )

        totals.worked_sec += metrics.worked_seconds
        totals.overtime_sec += metrics.overtime_seconds
        totals.meeting_sec += metrics.meeting_seconds
        totals.break_sec += metrics.break_seconds
        totals.target_sec += metrics.target_seconds
        if metrics.worked_seconds or metrics.overtime_seconds or metrics.meeting_seconds:
            totals.days_with_data += 1

        for item in app_items:
            totals.app_secs[item.app_name] = totals.app_secs.get(item.app_name, 0) + item.active_seconds

    return totals


def percent_delta(current: int, previous: int) -> float | None:
    """Percentage change from ``previous`` to ``current`` (None if no baseline)."""
    if previous <= 0:
        return None
    return round(((current - previous) / previous) * 100.0, 1)


def _range_for_period(kind: str, today: date) -> tuple[date, date, date, date]:
    """Return (cur_start, cur_end, prev_start, prev_end) for a comparison period."""
    if kind == "week":
        cur_start = today - timedelta(days=6)
        prev_end = cur_start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=6)
        return cur_start, today, prev_start, prev_end

    if kind == "month":
        cur_start = today.replace(day=1)
        prev_end = cur_start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        # Compare the same number of days from the previous month.
        span = (today - cur_start).days
        prev_end = min(prev_end, prev_start + timedelta(days=span))
        return cur_start, today, prev_start, prev_end

    # year
    cur_start = today - timedelta(days=364)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=364)
    return cur_start, today, prev_start, prev_end


def build_comparison(
    db: Database,
    config: Config,
    today: date | None = None,
    half_day_dates: set[date] | None = None,
    is_live: bool = False,
) -> dict[str, Any]:
    """Week / month / year totals compared with the previous period."""
    today = today or date.today()
    result: dict[str, Any] = {}

    for kind in ("week", "month", "year"):
        cur_start, cur_end, prev_start, prev_end = _range_for_period(kind, today)
        current = period_totals(
            db, config, cur_start, cur_end, label=kind,
            half_day_dates=half_day_dates, is_live=is_live,
        )
        previous = period_totals(db, config, prev_start, prev_end, label=kind, half_day_dates=half_day_dates)

        result[kind] = {
            "current": current.to_dict(),
            "previous": previous.to_dict(),
            "delta": {
                "worked_pct": percent_delta(current.worked_sec, previous.worked_sec),
                "overtime_pct": percent_delta(current.overtime_sec, previous.overtime_sec),
                "meeting_pct": percent_delta(current.meeting_sec, previous.meeting_sec),
                "break_pct": percent_delta(current.break_sec, previous.break_sec),
            },
            "top_app": current.top_apps[0][0] if current.top_apps else "",
            "previous_top_app": previous.top_apps[0][0] if previous.top_apps else "",
        }

    return result


# ---------------------------------------------------------------------------
# Project breakdown
# ---------------------------------------------------------------------------

def project_breakdown(
    db: Database,
    config: Config,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Worked / meeting / overtime per project for a date range."""
    projects = {int(p["id"]): p for p in db.list_projects(include_archived=True)}
    worked: dict[str, int] = {}
    meeting: dict[str, int] = {}
    overtime: dict[str, int] = {}
    sessions_count: dict[str, int] = {}

    # PERF-03: fetch the range's raw data once and bucket by day in memory.
    start_ts = get_day_boundaries(start)[0]
    end_ts = get_day_boundaries(end)[1]
    summaries = {row["date"]: row for row in db.get_daily_summaries(start, end)}
    all_records = db.get_input_events_with_details(start_ts, end_ts)
    all_sessions = db.get_sessions_between(start_ts, end_ts)
    interval_sec = active_check_interval_seconds(config)

    records_by_day = bucket_records_by_day(all_records)

    for d in _iter_days(start, end):
        day_start_ts, day_end_ts = get_day_boundaries(d)
        day_records = records_by_day.get(d, [])
        has_raw = bool(day_records)
        summary = summaries.get(d.isoformat())
        if summary is not None and not has_raw:
            raw = summary.get("project_secs") or {}
            for k, v in raw.items():
                if isinstance(v, (int, float)):
                    worked[k] = worked.get(k, 0) + int(v)
        elif day_records:
            items = calculate_project_usage(
                day_records,
                grace_seconds=config.grace_period_seconds,
                is_live=False,
                active_check_interval_sec=interval_sec,
            )
            total_active = sum(i.active_seconds for i in items)
            day_sessions = [
                s for s in all_sessions
                if int(s["start_ts"]) < day_end_ts
                and (s.get("end_ts") is None or int(s["end_ts"]) >= day_start_ts)
            ]
            metrics = calculate_day_metrics(
                target_date=d,
                events=[r[0] for r in day_records],
                sessions=day_sessions,
                work_start=config.work_start_time,
                work_end=config.work_end_time,
                work_days=config.work_days,
                holidays=config.holidays,
                daily_target_hours=config.daily_target_hours,
                grace_seconds=config.grace_period_seconds,
                is_live=False,
                confirm_credits=presence_credit_intervals(day_records, interval_sec),
            )
            if total_active > 0:
                factor = metrics.worked_seconds / total_active
                for item in items:
                    key = UNASSIGNED_KEY if item.project_id is None else str(item.project_id)
                    worked[key] = worked.get(key, 0) + int(round(item.active_seconds * factor))
            else:
                for item in items:
                    key = UNASSIGNED_KEY if item.project_id is None else str(item.project_id)
                    worked[key] = worked.get(key, 0) + item.active_seconds

        for s in all_sessions:
            pid = s.get("project_id")
            st = int(s["start_ts"])
            et = int(s["end_ts"]) if s.get("end_ts") is not None else st
            if not (st < day_end_ts and (s.get("end_ts") is None or int(s["end_ts"]) >= day_start_ts)):
                continue
            key = UNASSIGNED_KEY if pid is None else str(pid)
            dur = max(0, min(et, day_end_ts) - max(st, day_start_ts))
            session_type = s.get("session_type")
            if session_type == "meeting":
                meeting[key] = meeting.get(key, 0) + dur
                sessions_count[key] = sessions_count.get(key, 0) + 1
            elif session_type == "overtime":
                overtime[key] = overtime.get(key, 0) + dur
                sessions_count[key] = sessions_count.get(key, 0) + 1

    rows: list[dict[str, Any]] = []
    for key in set(worked) | set(meeting) | set(overtime):
        if key == UNASSIGNED_KEY or key.strip() == "":
            project_id = None
            name = "Unassigned"
        else:
            try:
                project_id = int(key)
            except ValueError:
                project_id = None
            info = projects.get(project_id) if project_id is not None else None
            name = str(info["name"]) if info else (f"Project #{project_id}" if project_id is not None else "Unassigned")

        rows.append(
            {
                "project_id": project_id,
                "name": name,
                "worked_sec": worked.get(key, 0),
                "meeting_sec": meeting.get(key, 0),
                "overtime_sec": overtime.get(key, 0),
                "sessions": sessions_count.get(key, 0),
            }
        )

    total_worked = sum(int(r["worked_sec"]) for r in rows) or 0
    for row in rows:
        row["worked_hours"] = round(int(row["worked_sec"]) / 3600.0, 2)
        row["meeting_hours"] = round(int(row["meeting_sec"]) / 3600.0, 2)
        row["overtime_hours"] = round(int(row["overtime_sec"]) / 3600.0, 2)
        row["worked_formatted"] = format_duration_hm(int(row["worked_sec"]))
        row["percentage"] = round((int(row["worked_sec"]) / total_worked) * 100.0, 1) if total_worked else 0.0

    rows.sort(key=lambda r: int(r["worked_sec"]), reverse=True)
    return rows
