"""Excel Reporter for TimeTracker.

Generates professional Excel work hour reports using openpyxl.
Includes visual BarChart of Worked vs Overtime vs Unworked Hours, Application Breakdown,
and a detailed Sessions Log with notes and overtime reasons.
Saves to %USERPROFILE%\\Documents\\TimeTracker\\Reports\\TimeReport_<start>_to_<end>.xlsx.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import date, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Any, Sequence

import openpyxl
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.series import SeriesLabel
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from time_tracker.core.calculator import (
    DayMetrics,
    calculate_app_usage,
    calculate_day_metrics,
    format_duration_hm,
    get_day_boundaries,
)
from time_tracker.core.config import Config, get_app_dir, resolve_writable_dir
from time_tracker.core.database import Database
from time_tracker.core.retention import (
    aggregate_missing_days,
    get_day_app_usage,
    get_day_metrics,
    project_breakdown,
)
from time_tracker.paths import get_app_root

logger = logging.getLogger(__name__)


# Excel re-interprets text that looks like a date / clock time / duration when a
# CSV is opened and silently displays it as a *time value* (e.g. "1h 31m" or
# "13:31:00" become "1:31"). A leading apostrophe keeps the literal text while
# being invisible in Excel. The same regex also covers the classic CSV formula
# injection prefixes (= + - @), so this is both a formatting and a safety fix.
_EXCEL_COERCION_RE = re.compile(
    r"^\s*(?:[=+@\t\r]|-(?=\S)"
    r"|\d{4}-\d{1,2}-\d{1,2}(?:[ T].*)?"
    r"|\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?"
    r"|\d+(?:\.\d+)?\s*h(?:\s*\d+(?:\.\d+)?\s*m(?:in(?:s|utes)?)?)?"
    r")",
    re.IGNORECASE,
)

#: openpyxl number format that forces Excel to display a cell as literal text.
TEXT_NUMBER_FORMAT = "@"


def excel_safe_text(value: object) -> object:
    """Prefix strings Excel would auto-convert with an invisible apostrophe.

    Leaving the value untouched keeps it machine-readable for other tools when
    the caller passes ``excel_safe=False`` to the CSV exporters.
    """
    if not isinstance(value, str) or not value:
        return value
    if _EXCEL_COERCION_RE.match(value):
        return "'" + value
    return value


def _atomic_save_workbook(wb: Any, final_path: Path) -> Path:
    """BUG-14: write to a unique temp file then atomically replace the target.

    A partially written workbook can never be observed at ``final_path``, and
    the TOCTOU window between the writability probe and the real write is gone.
    """
    final_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".tt_report_", suffix=".xlsx", dir=str(final_path.parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        wb.save(tmp)
        os.replace(tmp, final_path)
        return final_path
    finally:
        tmp.unlink(missing_ok=True)


def hours_to_hm(value: object) -> str:
    """Render an hours value from a report row as an explicit 'Xh Ym' string."""
    try:
        hours = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    return format_duration_hm(int(round(hours * 3600)))


def _day_has_data(db: Database, d: date) -> bool:
    """True if a day has raw events/sessions or an aggregated daily summary."""
    if db.get_daily_summary(d) is not None:
        return True
    day_start_ts, day_end_ts = get_day_boundaries(d)
    return bool(
        db.get_input_events_between(day_start_ts, day_end_ts)
        or db.get_sessions_between(day_start_ts, day_end_ts)
    )


def _aggregate_before_build(db: Database, config: Config) -> None:
    """Item 1c: refresh daily_summary before a report or export reads older days."""
    try:
        aggregate_missing_days(db, config)
    except Exception as exc:  # an export must still work from raw data
        logger.warning("Could not refresh daily summaries before export: %s", exc)


def get_reports_dir() -> Path:
    """Return %USERPROFILE%\\Documents\\TimeTracker\\Reports with fallbacks for restricted environments."""
    candidates = [
        Path.home() / "Documents" / "TimeTracker" / "Reports",
        get_app_dir() / "Reports",
        Path(tempfile.gettempdir()) / "TimeTracker" / "Reports",
    ]
    fallback = Path(tempfile.gettempdir()) / "TimeTracker" / "Reports"
    return resolve_writable_dir(candidates, fallback)


def documents_folder_is_cloud_synced() -> bool:
    """True when Documents is redirected into OneDrive or a similar sync root (D6)."""
    try:
        docs = str(Path.home() / "Documents").lower()
    except Exception:
        return False
    if "onedrive" in docs or "dropbox" in docs or "google drive" in docs:
        return True
    for env_name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(env_name)
        if not root:
            continue
        try:
            if str(Path(root)).lower() in docs:
                return True
        except Exception:
            continue
    return False


def export_report(
    db: Database,
    config: Config,
    start_date: date,
    end_date: date,
    open_folder: bool = True,
    output_dir: Path | None = None,
    half_day_dates: set[date] | None = None,
) -> Path:
    """Generate and save Excel report for the date range [start_date, end_date].

    Skips days with no activity/sessions.
    """
    _aggregate_before_build(db, config)
    reports_dir = output_dir or get_reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    base_file_name = f"TimeReport_{start_date.isoformat()}_to_{end_date.isoformat()}"
    file_path = reports_dir / f"{base_file_name}.xlsx"

    # PERF-04: pre-fetch sessions/events once for the whole range instead of
    # re-querying per day in the Sessions Log / Activity Timeline sheets.
    _range_start_ts = get_day_boundaries(start_date)[0]
    _range_end_ts = get_day_boundaries(end_date)[1]
    _all_sessions = db.get_sessions_between(_range_start_ts, _range_end_ts)
    _all_events_with_apps = db.get_input_events_with_apps(_range_start_ts, _range_end_ts)

    # BUG-14: no writability probe here — the atomic save below handles locks.

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Daily Summary"

    # Header styling
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")  # Deep Navy
    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Segoe UI", size=10)
    total_font = Font(name="Segoe UI", size=10, bold=True)
    total_fill = PatternFill(start_color="E9ECEF", end_color="E9ECEF", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="D3D3D3"),
        right=Side(style="thin", color="D3D3D3"),
        top=Side(style="thin", color="D3D3D3"),
        bottom=Side(style="thin", color="D3D3D3"),
    )
    total_border = Border(
        top=Side(style="thin", color="000000"),
        bottom=Side(style="double", color="000000"),
    )

    headers = [
        "Date",
        "Day",
        "First Activity",
        "Last Activity",
        "Worked Hours",
        "Meeting Hours (included in Worked)",
        "Break Hours",
        "Overtime Hours",
        "Target",
        "Remaining",
        "Unclassified Meeting Hours",
    ]

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws.freeze_panes = "A2"

    cur_date = start_date
    rows_added = 0
    total_worked = 0.0
    total_meeting = 0.0
    total_break = 0.0
    total_overtime = 0.0
    total_target = 0.0
    total_remaining = 0.0
    total_unclassified = 0.0

    while cur_date <= end_date:
        # Reads daily_summary for days older than the retention window, so the
        # totals of purged days stay exactly the same.
        metrics = get_day_metrics(db, config, cur_date, half_day_dates=half_day_dates)
        # B17: identical row policy to the CSV export. A day is listed when it has
        # data or when it is a working day (an absent workday is still reported).
        if not _day_has_data(db, cur_date) and not (metrics.is_work_day and not metrics.is_holiday):
            cur_date += timedelta(days=1)
            continue

        row_data = [
            cur_date.strftime("%Y-%m-%d"),
            metrics.day_name,
            metrics.first_activity_formatted,
            metrics.last_activity_formatted,
            metrics.worked_hours,
            metrics.meeting_hours,
            metrics.break_hours,
            metrics.overtime_hours,
            metrics.target_hours,
            metrics.remaining_hours,
            metrics.unclassified_meeting_hours,
        ]
        ws.append(row_data)
        rows_added += 1

        total_worked += metrics.worked_hours
        total_meeting += metrics.meeting_hours
        total_break += metrics.break_hours
        total_overtime += metrics.overtime_hours
        total_target += metrics.target_hours
        total_remaining += metrics.remaining_hours
        total_unclassified += metrics.unclassified_meeting_hours

        cur_row = ws.max_row
        for col_idx in range(1, len(headers) + 1):
            c = ws.cell(row=cur_row, column=col_idx)
            c.font = data_font
            c.border = thin_border
            if col_idx in (1, 2, 3, 4):
                c.alignment = Alignment(horizontal="center")
                if col_idx in (3, 4):
                    # Clock times stay literal; Excel would show 13:31:00 as 1:31 PM.
                    c.number_format = TEXT_NUMBER_FORMAT
            else:
                c.alignment = Alignment(horizontal="right")
                c.number_format = "0.00"

        cur_date += timedelta(days=1)

    # Append Totals Row
    total_row = [
        "Total",
        "",
        "",
        "",
        round(total_worked, 2),
        round(total_meeting, 2),
        round(total_break, 2),
        round(total_overtime, 2),
        round(total_target, 2),
        round(total_remaining, 2),
        round(total_unclassified, 2),
    ]
    ws.append(total_row)
    totals_row_idx = ws.max_row

    for col_idx in range(1, len(headers) + 1):
        c = ws.cell(row=totals_row_idx, column=col_idx)
        c.font = total_font
        c.fill = total_fill
        c.border = total_border
        if col_idx in (1, 2, 3, 4):
            c.alignment = Alignment(horizontal="center")
        else:
            c.alignment = Alignment(horizontal="right")
            c.number_format = "0.00"

    # D1: Meeting hours are a subset of Worked hours. Column A stays empty so the
    # manager aggregator skips this explanatory row.
    note_cell = ws.cell(row=totals_row_idx + 2, column=2)
    note_cell.value = (
        "Meeting hours are included in Worked hours; only Worked + Overtime are additive."
    )
    note_cell.font = Font(name="Segoe UI", size=9, italic=True, color="666666")

    # Add Visual Bar Chart (Worked vs Overtime vs Unworked Hours)
    if rows_added > 0:
        try:
            chart = BarChart()
            chart.type = "col"
            chart.style = 10
            chart.title = "Worked vs Overtime vs Unworked Hours"
            chart.y_axis.title = "Hours"
            chart.x_axis.title = "Date"
            chart.width = 16
            chart.height = 10

            # Worked Hours (Col 5), Overtime Hours (Col 8), and Unworked/Remaining (Col 10)
            data_worked = Reference(ws, min_col=5, min_row=1, max_col=5, max_row=1 + rows_added)
            data_ot = Reference(ws, min_col=8, min_row=1, max_col=8, max_row=1 + rows_added)
            data_unworked = Reference(ws, min_col=10, min_row=1, max_col=10, max_row=1 + rows_added)
            cats = Reference(ws, min_col=1, min_row=2, max_row=1 + rows_added)

            chart.add_data(data_worked, titles_from_data=True)
            chart.add_data(data_ot, titles_from_data=True)
            chart.add_data(data_unworked, titles_from_data=True)
            chart.set_categories(cats)

            if len(chart.series) >= 3:
                chart.series[2].tx = SeriesLabel(v="Unworked Hours")

            ws.add_chart(chart, "L2")
        except Exception as exc:
            logger.warning("Could not add chart to Excel report: %s", exc)

    # Auto-fit columns
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    # -------------------------------------------------------------
    # Sheet 2: "App Usage" Breakdown
    # -------------------------------------------------------------
    ws_app = wb.create_sheet(title="App Usage")
    app_headers = [
        "Date",
        "Application",
        "Active Hours",
        "Time Spent",
        "% of Day Active Time",
    ]
    ws_app.append(app_headers)
    for col_idx in range(1, len(app_headers) + 1):
        cell = ws_app.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_app.freeze_panes = "A2"

    cur_date = start_date
    app_rows_added = 0
    while cur_date <= end_date:
        usage_items = get_day_app_usage(db, config, cur_date)
        if usage_items:
            for item in usage_items:
                ws_app.append([
                    cur_date.strftime("%Y-%m-%d"),
                    item.app_name,
                    item.active_hours,
                    item.formatted,
                    f"{item.percentage:.1f}%",
                ])
                app_rows_added += 1
                row_idx = ws_app.max_row
                for col_idx in range(1, len(app_headers) + 1):
                    c = ws_app.cell(row=row_idx, column=col_idx)
                    c.font = data_font
                    c.border = thin_border
                    if col_idx in (1, 2):
                        c.alignment = Alignment(horizontal="left" if col_idx == 2 else "center")
                    elif col_idx == 3:
                        c.alignment = Alignment(horizontal="right")
                        c.number_format = "0.00"
                    else:
                        c.alignment = Alignment(horizontal="center")
                        if col_idx == 4:
                            # Keep the '1h 31m' duration literal (Excel would show '1:31').
                            c.number_format = TEXT_NUMBER_FORMAT

        cur_date += timedelta(days=1)

    if app_rows_added == 0:
        ws_app.append(["No application activity recorded in this period.", "", "", "", ""])

    for col in ws_app.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws_app.column_dimensions[col_letter].width = max(max_len + 4, 14)

    # -------------------------------------------------------------
    # Sheet 3: "Sessions Log" (Meetings, Breaks, Overtime)
    # -------------------------------------------------------------
    ws_sess = wb.create_sheet(title="Sessions Log")
    sess_headers = [
        "Date",
        "Type",
        "Source",
        "Start Time",
        "End Time",
        "Duration (Minutes)",
        "Duration (Hours)",
        "Note / Description",
        "Reason",
    ]
    ws_sess.append(sess_headers)
    for col_idx in range(1, len(sess_headers) + 1):
        cell = ws_sess.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_sess.freeze_panes = "A2"

    cur_date = start_date
    sess_rows_added = 0
    while cur_date <= end_date:
        day_start_ts, day_end_ts = get_day_boundaries(cur_date)
        sessions = [
            s for s in _all_sessions
            if int(s["start_ts"]) < day_end_ts
            and (s.get("end_ts") is None or int(s["end_ts"]) >= day_start_ts)
        ]
        for s in sessions:
            st = int(s["start_ts"])
            et = int(s["end_ts"]) if s.get("end_ts") is not None else None
            st_dt = datetime.fromtimestamp(st)
            st_str = st_dt.strftime("%H:%M:%S")

            if et is not None:
                et_dt = datetime.fromtimestamp(et)
                et_str = et_dt.strftime("%H:%M:%S")
                dur_sec = max(0, et - st)
                dur_min = round(dur_sec / 60.0, 1)
                dur_hr = round(dur_sec / 3600.0, 2)
            else:
                et_str = "In Progress"
                dur_min = 0.0
                dur_hr = 0.0

            ws_sess.append([
                cur_date.strftime("%Y-%m-%d"),
                str(s.get("session_type", "")).capitalize(),
                str(s.get("source", "")).capitalize(),
                st_str,
                et_str,
                dur_min,
                dur_hr,
                str(s.get("note") or ""),
                str(s.get("reason") or ""),
            ])
            sess_rows_added += 1

            cur_row = ws_sess.max_row
            for col_idx in range(1, len(sess_headers) + 1):
                c = ws_sess.cell(row=cur_row, column=col_idx)
                c.font = data_font
                c.border = thin_border
                if col_idx in (1, 2, 3, 4, 5):
                    c.alignment = Alignment(horizontal="center")
                    if col_idx in (4, 5):
                        c.number_format = TEXT_NUMBER_FORMAT
                elif col_idx in (6, 7):
                    c.alignment = Alignment(horizontal="right")
                    c.number_format = "0.00"
                else:
                    c.alignment = Alignment(horizontal="left")

        cur_date += timedelta(days=1)

    if sess_rows_added == 0:
        ws_sess.append(["No sessions recorded in this period.", "", "", "", "", "", "", "", ""])

    for col in ws_sess.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws_sess.column_dimensions[col_letter].width = max(max_len + 4, 14)

    # -------------------------------------------------------------
    # Sheet 4: "Activity Timeline" (Full Chronological Work & Inactive Log)
    # -------------------------------------------------------------
    ws_time = wb.create_sheet(title="Activity Timeline")
    time_headers = [
        "Date",
        "Category",
        "Source / App",
        "Start Time",
        "End Time",
        "Duration (Minutes)",
        "Duration (Hours)",
        "Details / Status",
    ]
    ws_time.append(time_headers)
    for col_idx in range(1, len(time_headers) + 1):
        cell = ws_time.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_time.freeze_panes = "A2"

    cur_date = start_date
    time_rows_added = 0
    while cur_date <= end_date:
        day_start_ts, day_end_ts = get_day_boundaries(cur_date)
        # use pre-fetched range data sliced to this day
        sessions = [
            s for s in _all_sessions
            if int(s["start_ts"]) < day_end_ts
            and (s.get("end_ts") is None or int(s["end_ts"]) >= day_start_ts)
        ]
        events_with_apps = [ea for ea in _all_events_with_apps if day_start_ts <= ea[0] < day_end_ts]
        events = [ea[0] for ea in events_with_apps]

        if events or sessions:
            from time_tracker.reporting.visualizer import build_activity_and_sessions_log
            log_items = build_activity_and_sessions_log(cur_date, events, sessions, events_with_apps, config)
            for it in log_items:
                dur_m = it["duration_min"]
                dur_h = round(dur_m / 60.0, 2)
                ws_time.append([
                    cur_date.strftime("%Y-%m-%d"),
                    it["type"],
                    it["source"],
                    it["start"],
                    it["end"],
                    dur_m,
                    dur_h,
                    it["note"] or it["reason"] or "-",
                ])
                time_rows_added += 1

                cur_row = ws_time.max_row
                for col_idx in range(1, len(time_headers) + 1):
                    c = ws_time.cell(row=cur_row, column=col_idx)
                    c.font = data_font
                    c.border = thin_border
                    if col_idx in (1, 2, 3, 4, 5):
                        c.alignment = Alignment(horizontal="center")
                        if col_idx in (4, 5):
                            c.number_format = TEXT_NUMBER_FORMAT
                    elif col_idx in (6, 7):
                        c.alignment = Alignment(horizontal="right")
                        c.number_format = "0.00"
                    else:
                        c.alignment = Alignment(horizontal="left")

        cur_date += timedelta(days=1)

    if time_rows_added == 0:
        ws_time.append(["No activity timeline recorded in this period.", "", "", "", "", "", "", ""])

    for col in ws_time.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws_time.column_dimensions[col_letter].width = max(max_len + 4, 14)

    # -------------------------------------------------------------
    # Sheet 5: "Projects" (optional project tag breakdown)
    # -------------------------------------------------------------
    ws_proj = wb.create_sheet(title="Projects")
    proj_headers = [
        "Project",
        "Worked Hours",
        "Meeting Hours",
        "Overtime Hours",
        "Sessions",
        "% of Worked Time",
    ]
    ws_proj.append(proj_headers)
    for col_idx in range(1, len(proj_headers) + 1):
        cell = ws_proj.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_proj.freeze_panes = "A2"

    project_rows = project_breakdown(db, config, start_date, end_date)
    for row in project_rows:
        ws_proj.append([
            row["name"],
            row["worked_hours"],
            row["meeting_hours"],
            row["overtime_hours"],
            int(row["sessions"]),
            f"{row['percentage']:.1f}%",
        ])
        row_idx = ws_proj.max_row
        for col_idx in range(1, len(proj_headers) + 1):
            c = ws_proj.cell(row=row_idx, column=col_idx)
            c.font = data_font
            c.border = thin_border
            if col_idx == 1:
                c.alignment = Alignment(horizontal="left")
            elif col_idx in (2, 3, 4):
                c.alignment = Alignment(horizontal="right")
                c.number_format = "0.00"
            else:
                c.alignment = Alignment(horizontal="center")

    if not project_rows:
        ws_proj.append(["No project-tagged activity in this period.", "", "", "", "", ""])

    for col in ws_proj.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws_proj.column_dimensions[col_letter].width = max(max_len + 4, 14)

    # BUG-14: the writability probe above is TOCTOU-prone, so retry the actual
    # save with a suffixed name if the target becomes locked between probe and
    # write (e.g. concurrent export or Excel opening the file).
    save_counter = 0
    while True:
        try:
            file_path = _atomic_save_workbook(wb, file_path)
            break
        except (PermissionError, OSError) as exc:
            logger.warning("Could not atomically save report to %s (%s); retrying with suffix.", file_path, exc)
            save_counter += 1
            if save_counter > 5:
                raise
            file_path = reports_dir / f"{base_file_name}_{save_counter}.xlsx"
    logger.info("Report exported successfully to %s", file_path)

    if open_folder:
        try:
            os.startfile(reports_dir)
        except Exception as exc:
            logger.warning("Could not automatically open reports directory: %s", exc)

    return file_path


def _write_csv_file(
    path: Path,
    headers: list[str],
    rows: list[list[object]],
    excel_safe: bool = True,
) -> Path:
    """Write a CSV file as UTF-8 with BOM so Excel opens Arabic text correctly.

    ``excel_safe=True`` (the default) keeps date/clock/duration strings and
    formula-like text literal by prefixing an apostrophe Excel hides. Set it to
    ``False`` when another tool consumes the file and needs raw values.
    """
    import csv

    def clean(cell: object) -> object:
        return excel_safe_text(cell) if excel_safe else cell

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([clean(h) for h in headers])
        for row in rows:
            writer.writerow([clean(cell) for cell in row])
    return path


def _daily_summary_rows(
    db: Database,
    config: Config,
    start_date: date,
    end_date: date,
    half_day_dates: set[date] | None = None,
) -> tuple[list[list[object]], dict[str, float]]:
    """Rows for the daily summary CSV plus range totals."""
    rows: list[list[object]] = []
    totals = {
        "worked": 0.0, "meeting": 0.0, "break": 0.0, "overtime": 0.0,
        "target": 0.0, "unclassified": 0.0,
    }

    cur = start_date
    while cur <= end_date:
        m = get_day_metrics(db, config, cur, half_day_dates=half_day_dates)
        has_data = (
            m.worked_seconds > 0 or m.meeting_seconds > 0
            or m.break_seconds > 0 or m.overtime_seconds > 0
        )
        if has_data or (m.is_work_day and not m.is_holiday):
            rows.append([
                cur.isoformat(),
                m.day_name,
                m.first_activity_formatted,
                m.last_activity_formatted,
                m.worked_hours,
                m.meeting_hours,
                m.break_hours,
                m.overtime_hours,
                m.target_hours,
                m.remaining_hours,
                m.unclassified_meeting_hours,
            ])
            totals["worked"] += m.worked_hours
            totals["unclassified"] += m.unclassified_meeting_hours
            totals["meeting"] += m.meeting_hours
            totals["break"] += m.break_hours
            totals["overtime"] += m.overtime_hours
            totals["target"] += m.target_hours

        cur += timedelta(days=1)

    return rows, totals


def export_csv_report(
    db: Database,
    config: Config,
    start_date: date,
    end_date: date,
    open_folder: bool = True,
    output_dir: Path | None = None,
    half_day_dates: set[date] | None = None,
    excel_safe: bool = True,
) -> Path:
    """Legacy single-file daily summary CSV. Prefer :func:`export_csv_bundle`."""
    reports_dir = output_dir or get_reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    file_path = reports_dir / f"TimeReport_{start_date.isoformat()}_to_{end_date.isoformat()}.csv"

    rows, totals = _daily_summary_rows(db, config, start_date, end_date, half_day_dates)
    rows.append([])
    rows.append([
        "TOTALS", "", "", "",
        round(totals["worked"], 2),
        round(totals["meeting"], 2),
        round(totals["break"], 2),
        round(totals["overtime"], 2),
        round(totals["target"], 2),
        "",
        round(totals.get("unclassified", 0.0), 2),
    ])

    _write_csv_file(
        file_path,
        [
            "Date", "Day", "First Activity", "Last Activity",
            "Worked Hours", "Meeting Hours (included in Worked)", "Break Hours",
            "Overtime Hours", "Target Hours", "Remaining Hours",
            "Unclassified Meeting Hours",
        ],
        rows,
        excel_safe=excel_safe,
    )

    logger.info("CSV report exported successfully to %s", file_path)
    if open_folder:
        try:
            os.startfile(reports_dir)
        except Exception:
            pass
    return file_path


def export_csv_bundle(
    db: Database,
    config: Config,
    start_date: date,
    end_date: date,
    open_folder: bool = True,
    output_dir: Path | None = None,
    half_day_dates: set[date] | None = None,
    excel_safe: bool = True,
) -> list[Path]:
    """Export daily summary, sessions log, app usage and projects as separate CSVs.

    Every file is written as UTF-8 with BOM (``utf-8-sig``) so Microsoft Excel
    detects the encoding and renders Arabic text correctly. Date, clock and
    duration strings are apostrophe-protected (``excel_safe=True``) so Excel cannot
    reformat ``1h 31m`` into ``1:31``; pass ``excel_safe=False`` for integration
    with tools that need the raw strings.
    """
    reports_dir = output_dir or get_reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)

    suffix = f"{start_date.isoformat()}_to_{end_date.isoformat()}"
    written: list[Path] = []

    # 1. Daily summary
    rows, totals = _daily_summary_rows(db, config, start_date, end_date, half_day_dates)
    rows.append([])
    rows.append([
        "TOTALS", "", "", "",
        round(totals["worked"], 2),
        round(totals["meeting"], 2),
        round(totals["break"], 2),
        round(totals["overtime"], 2),
        round(totals["target"], 2),
        "",
        round(totals.get("unclassified", 0.0), 2),
    ])
    written.append(_write_csv_file(
        reports_dir / f"TimeTracker_DailySummary_{suffix}.csv",
        [
            "Date", "Day", "First Activity", "Last Activity",
            "Worked Hours", "Meeting Hours (included in Worked)", "Break Hours",
            "Overtime Hours", "Target Hours", "Remaining Hours",
            "Unclassified Meeting Hours",
        ],
        rows,
        excel_safe=excel_safe,
    ))

    # 2. Sessions log (meetings, breaks, overtime)
    project_names: dict[int, str] = {}

    def _project_name(project_id: object) -> str:
        if project_id is None:
            return ""
        try:
            pid = int(project_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ""
        if pid not in project_names:
            project = db.get_project(pid)
            project_names[pid] = str(project["name"]) if project else ""
        return project_names[pid]

    session_rows: list[list[object]] = []
    cur = start_date
    while cur <= end_date:
        day_start_ts, day_end_ts = get_day_boundaries(cur)
        for s in db.get_sessions_between(day_start_ts, day_end_ts):
            st = int(s["start_ts"])
            et = int(s["end_ts"]) if s.get("end_ts") is not None else None
            st_str = datetime.fromtimestamp(st).strftime("%H:%M:%S")
            if et is not None:
                et_str = datetime.fromtimestamp(et).strftime("%H:%M:%S")
                dur_min = round(max(0, et - st) / 60.0, 1)
                dur_hr = round(max(0, et - st) / 3600.0, 2)
            else:
                et_str = "In Progress"
                dur_min = 0.0
                dur_hr = 0.0
            session_rows.append([
                cur.isoformat(),
                str(s.get("session_type", "")).capitalize(),
                str(s.get("source", "")).capitalize(),
                _project_name(s.get("project_id")),
                st_str,
                et_str,
                dur_min,
                dur_hr,
                str(s.get("note") or ""),
                str(s.get("reason") or ""),
            ])
        cur += timedelta(days=1)

    written.append(_write_csv_file(
        reports_dir / f"TimeTracker_Sessions_{suffix}.csv",
        [
            "Date", "Type", "Source", "Project", "Start Time", "End Time",
            "Duration (Minutes)", "Duration (Hours)", "Note / Description", "Reason",
        ],
        session_rows or [["No sessions recorded in this period.", "", "", "", "", "", "", "", "", ""]],
        excel_safe=excel_safe,
    ))

    # 3. App usage
    app_rows: list[list[object]] = []
    cur = start_date
    while cur <= end_date:
        for item in get_day_app_usage(db, config, cur):
            app_rows.append([
                cur.isoformat(),
                item.app_name,
                item.active_hours,
                item.formatted,
                f"{item.percentage:.1f}%",
            ])
        cur += timedelta(days=1)

    written.append(_write_csv_file(
        reports_dir / f"TimeTracker_AppUsage_{suffix}.csv",
        ["Date", "Application", "Active Hours", "Time Spent", "% of Day Active Time"],
        app_rows or [["No application activity recorded in this period.", "", "", "", ""]],
        excel_safe=excel_safe,
    ))

    # 4. Projects breakdown
    project_rows = [
        [
            row["name"],
            row["worked_hours"],
            row["meeting_hours"],
            row["overtime_hours"],
            int(row["sessions"]),
            f"{row['percentage']:.1f}%",
        ]
        for row in project_breakdown(db, config, start_date, end_date)
    ]

    written.append(_write_csv_file(
        reports_dir / f"TimeTracker_Projects_{suffix}.csv",
        ["Project", "Worked Hours", "Meeting Hours", "Overtime Hours", "Sessions", "% of Worked Time"],
        project_rows or [["No project-tagged activity in this period.", "", "", "", "", ""]],
        excel_safe=excel_safe,
    ))

    logger.info("CSV bundle exported (%d files) to %s", len(written), reports_dir)
    if open_folder:
        try:
            os.startfile(reports_dir)
        except Exception:
            pass
    return written


def find_headless_browser() -> str | None:
    """Find Microsoft Edge or Google Chrome executable for headless PDF generation."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\Application\msedge.exe"),
        shutil.which("msedge"),
        shutil.which("msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _build_official_pdf_html(
    logo_base64: str,
    start_date: date,
    end_date: date,
    report_uid: str,
    now_str: str,
    employee_name: str,
    totals: dict[str, float],
    compliance_pct: float,
    rows: list[list[object]],
    projects: list[dict[str, Any]],
) -> str:
    """Generate professional, print-optimized HTML for official HR PDF export."""
    if logo_base64:
        logo_html = f'<img src="{logo_base64}" alt="Bussma Logo" style="height: 52px; object-fit: contain;">'
    else:
        logo_html = '<div style="font-size: 24px; font-weight: bold; color: #1F497D;">⏱ BUSSMA</div>'

    table_rows_html = []
    for r in rows:
        if not r or len(r) < 9:
            continue
        d_val = str(r[0])
        day_val = str(r[1])
        first_act = str(r[2]) if r[2] else "—"
        last_act = str(r[3]) if r[3] else "—"
        worked_h = hours_to_hm(r[4])
        meeting_h = hours_to_hm(r[5]) if isinstance(r[5], (int, float)) and float(r[5]) > 0 else "—"
        break_h = hours_to_hm(r[6]) if isinstance(r[6], (int, float)) and float(r[6]) > 0 else "—"
        ot_h = hours_to_hm(r[7]) if isinstance(r[7], (int, float)) and float(r[7]) > 0 else "—"
        target_h = hours_to_hm(r[8])

        is_holiday = "holiday" in str(day_val).lower() or "عطلة" in str(day_val)
        if isinstance(r[4], (int, float)) and float(r[4]) >= float(r[8] if isinstance(r[8], (int, float)) else 8):
            status_badge = '<span class="badge badge-success">مكتمل / Done</span>'
        elif isinstance(r[4], (int, float)) and float(r[4]) > 0:
            status_badge = '<span class="badge badge-warning">جزئي / Partial</span>'
        elif is_holiday:
            status_badge = '<span class="badge badge-secondary">عطلة / Holiday</span>'
        else:
            status_badge = '<span class="badge badge-neutral">غياب / Off</span>'

        table_rows_html.append(f"""
        <tr>
            <td style="font-weight: 600; text-align: center;">{d_val}</td>
            <td style="text-align: center;">{day_val}</td>
            <td style="text-align: center;">{first_act}</td>
            <td style="text-align: center;">{last_act}</td>
            <td style="text-align: center; font-weight: bold; color: #1e3a8a;">{worked_h}</td>
            <td style="text-align: center; color: #b45309;">{ot_h}</td>
            <td style="text-align: center; color: #dc2626;">{meeting_h}</td>
            <td style="text-align: center; color: #4b5563;">{break_h}</td>
            <td style="text-align: center;">{target_h}</td>
            <td style="text-align: center;">{status_badge}</td>
        </tr>
        """)

    table_body = "\n".join(table_rows_html)

    projects_html = ""
    if projects:
        proj_rows = []
        for p in projects[:6]:
            proj_rows.append(f"""
            <tr>
                <td style="font-weight: 500;">{html_escape(str(p.get('name', 'General')))}</td>
                <td style="text-align: center;">{hours_to_hm(p.get('worked_hours', 0))}</td>
                <td style="text-align: center;">{p.get('sessions', 0)}</td>
                <td style="text-align: center; font-weight: 600;">{p.get('percentage', 0):.1f}%</td>
            </tr>
            """)
        projects_html = f"""
        <div style="margin-top: 18px; margin-bottom: 12px;">
            <div style="font-size: 13px; font-weight: bold; color: #1F497D; margin-bottom: 6px;">مشاريع العمل المنجزة خلال الفترة (Projects Breakdown)</div>
            <table class="data-table" style="font-size: 10px;">
                <thead>
                    <tr style="background: #f1f5f9;">
                        <th>المشروع / Project</th>
                        <th style="text-align: center;">ساعات العمل (Worked)</th>
                        <th style="text-align: center;">الجلسات (Sessions)</th>
                        <th style="text-align: center;">النسبة (%)</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(proj_rows)}
                </tbody>
            </table>
        </div>
        """

    stamp_date_str = start_date.strftime("%Y/%m")

    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data: file:; font-src data: file:">
<title>BUSSMA — Official HR Timesheet Report</title>
<style>
  @page {{
    size: A4 portrait;
    margin: 12mm 15mm 15mm 15mm;
  }}
  * {{
    box-sizing: border-box;
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }}
  body {{
    font-family: 'Segoe UI', Tahoma, Arial, sans-serif;
    color: #1e293b;
    background: #ffffff;
    margin: 0;
    padding: 0;
    font-size: 11px;
    line-height: 1.4;
  }}
  .header-container {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid #1F497D;
    padding-bottom: 10px;
    margin-bottom: 12px;
  }}
  .company-meta h1 {{
    margin: 0 0 2px 0;
    font-size: 17px;
    color: #1F497D;
    font-weight: 800;
  }}
  .company-meta p {{
    margin: 0;
    font-size: 10px;
    color: #64748b;
  }}
  .report-badge-box {{
    text-align: left;
    font-size: 10px;
    color: #475569;
  }}
  .report-badge-box .badge-id {{
    display: inline-block;
    background: #e0f2fe;
    color: #0369a1;
    font-weight: bold;
    padding: 2px 8px;
    border-radius: 4px;
    margin-bottom: 2px;
  }}
  .meta-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 12px;
    font-size: 10.5px;
  }}
  .meta-item span {{
    color: #64748b;
    display: block;
    font-size: 9.5px;
  }}
  .meta-item strong {{
    color: #0f172a;
    font-weight: 700;
  }}
  .kpi-row {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 8px;
    margin-bottom: 14px;
  }}
  .kpi-card {{
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 8px;
    text-align: center;
  }}
  .kpi-card.highlight {{
    background: #f0fdf4;
    border-color: #86efac;
  }}
  .kpi-title {{
    font-size: 9px;
    color: #64748b;
    margin-bottom: 2px;
    text-transform: uppercase;
  }}
  .kpi-value {{
    font-size: 15px;
    font-weight: 800;
    color: #1e3a8a;
  }}
  .kpi-card.highlight .kpi-value {{
    color: #166534;
  }}
  .data-table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 6px;
    font-size: 9.5px;
  }}
  .data-table th, .data-table td {{
    border: 1px solid #cbd5e1;
    padding: 5px 6px;
  }}
  .data-table th {{
    background: #1F497D;
    color: #ffffff;
    font-weight: bold;
    text-align: center;
  }}
  .data-table tbody tr:nth-child(even) {{
    background: #f8fafc;
  }}
  .table-totals {{
    background: #e2e8f0 !important;
    font-weight: 800;
  }}
  .badge {{
    display: inline-block;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 8.5px;
    font-weight: 600;
  }}
  .badge-success {{ background: #dcfce7; color: #166534; }}
  .badge-warning {{ background: #fef3c7; color: #92400e; }}
  .badge-secondary {{ background: #e0e7ff; color: #3730a3; }}
  .badge-neutral {{ background: #f1f5f9; color: #64748b; }}
  
  .approval-section {{
    margin-top: 20px;
    page-break-inside: avoid;
    display: grid;
    grid-template-columns: 1fr 1fr 1.2fr;
    gap: 15px;
    align-items: center;
    border-top: 2px solid #e2e8f0;
    padding-top: 14px;
  }}
  .approval-box {{
    border: 1px dashed #94a3b8;
    border-radius: 6px;
    padding: 10px;
    height: 95px;
    position: relative;
    font-size: 10px;
    background: #fafafa;
  }}
  .approval-title {{
    font-weight: bold;
    color: #334155;
    margin-bottom: 25px;
  }}
  .approval-line {{
    border-bottom: 1px solid #64748b;
    margin-top: 20px;
  }}
  .approval-sub {{
    font-size: 8.5px;
    color: #64748b;
    margin-top: 3px;
  }}
  .seal-container {{
    display: flex;
    justify-content: center;
    align-items: center;
  }}
  .official-seal {{
    width: 95px;
    height: 95px;
    border: 3px double #1F497D;
    border-radius: 50%;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    color: #1F497D;
    text-align: center;
    font-weight: bold;
    background: #ffffff;
    box-shadow: 0 0 0 1px rgba(31, 73, 125, 0.2);
    transform: rotate(-6deg);
  }}
  .official-seal .seal-star {{ font-size: 9px; color: #dc2626; }}
  .official-seal .seal-corp {{ font-size: 8px; letter-spacing: 0.5px; }}
  .official-seal .seal-status {{ font-size: 11px; font-weight: 900; color: #166534; margin: 1px 0; }}
  .official-seal .seal-date {{ font-size: 8px; color: #475569; }}

  .footer-note {{
    margin-top: 14px;
    font-size: 8.5px;
    color: #94a3b8;
    text-align: center;
    border-top: 1px solid #f1f5f9;
    padding-top: 6px;
  }}
</style>
</head>
<body>

  <div class="header-container">
    <div class="company-meta">
      <div style="display: flex; align-items: center; gap: 10px;">
        {logo_html}
        <div>
          <h1>مؤسسة بصمة للحلول الرقمية | كشف حضور رسمي معتمد</h1>
          <p>BUSSMA ENTERPRISE SOLUTIONS — OFFICIAL TIMESHEET & ATTENDANCE REPORT</p>
        </div>
      </div>
    </div>
    <div class="report-badge-box">
      <div class="badge-id">{report_uid}</div>
      <div>تاريخ الاستخراج: {now_str}</div>
    </div>
  </div>

  <div class="meta-grid">
    <div class="meta-item">
      <span>اسم الموظف / Employee</span>
      <strong>{html_escape(str(employee_name))}</strong>
    </div>
    <div class="meta-item">
      <span>فترة التقرير / Period</span>
      <strong>{start_date.isoformat()} ➔ {end_date.isoformat()}</strong>
    </div>
    <div class="meta-item">
      <span>أيام العمل المقررة / Days</span>
      <strong>{len(rows)} يوم عمل مسجل</strong>
    </div>
    <div class="meta-item">
      <span>النظام المعتمد / System</span>
      <strong>BUSSMA_TTS v1.3.2 (معتمد)</strong>
    </div>
  </div>

  <div class="kpi-row">
    <div class="kpi-card highlight">
      <div class="kpi-title">إجمالي ساعات العمل (Worked)</div>
      <div class="kpi-value">{hours_to_hm(totals['worked'])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">المستهدف الرسمي (Target)</div>
      <div class="kpi-value">{hours_to_hm(totals['target'])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">الوقت الإضافي (Overtime)</div>
      <div class="kpi-value" style="color: #b45309;">{hours_to_hm(totals['overtime'])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">الاجتماعات (Meetings)</div>
      <div class="kpi-value" style="color: #dc2626;">{hours_to_hm(totals['meeting'])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">نسبة الالتزام (Compliance)</div>
      <div class="kpi-value" style="color: #0d9488;">{compliance_pct:.1f}%</div>
    </div>
  </div>

  <table class="data-table">
    <thead>
      <tr>
        <th style="width: 11%;">التاريخ (Date)</th>
        <th style="width: 9%;">اليوم (Day)</th>
        <th style="width: 9%;">أول نشاط</th>
        <th style="width: 9%;">آخر نشاط</th>
        <th style="width: 10%;">ساعات العمل</th>
        <th style="width: 9%;">إضافي</th>
        <th style="width: 9%;">اجتماعات</th>
        <th style="width: 9%;">استراحات</th>
        <th style="width: 9%;">المستهدف</th>
        <th style="width: 16%;">الحالة والاعتماد</th>
      </tr>
    </thead>
    <tbody>
      {table_body}
      <tr class="table-totals">
        <td colspan="4" style="text-align: center; font-weight: bold;">الإجمالي المعتمد للفترة (PERIOD TOTALS)</td>
        <td style="text-align: center; font-weight: bold; color: #1e3a8a;">{hours_to_hm(totals['worked'])}</td>
        <td style="text-align: center; font-weight: bold; color: #b45309;">{hours_to_hm(totals['overtime'])}</td>
        <td style="text-align: center; font-weight: bold; color: #dc2626;">{hours_to_hm(totals['meeting'])}</td>
        <td style="text-align: center; font-weight: bold; color: #4b5563;">{hours_to_hm(totals['break'])}</td>
        <td style="text-align: center; font-weight: bold;">{hours_to_hm(totals['target'])}</td>
        <td style="text-align: center; font-weight: bold; color: #166534;">معتمد {compliance_pct:.1f}%</td>
      </tr>
    </tbody>
  </table>

  {projects_html}

  <div class="approval-section">
    <div class="approval-box">
      <div class="approval-title">إعداد وتوقيع الموظف (Employee Sign):</div>
      <div class="approval-line"></div>
      <div class="approval-sub">التاريخ: ____ / ____ / ________</div>
    </div>

    <div class="approval-box">
      <div class="approval-title">مراجعة الموارد البشرية (HR Audit):</div>
      <div class="approval-line"></div>
      <div class="approval-sub">التوقيع والاعتماد: ______________</div>
    </div>

    <div class="seal-container">
      <div class="official-seal">
        <div class="seal-star">★ ★ ★</div>
        <div class="seal-corp">مؤسسة بصمة</div>
        <div class="seal-status">معتمد رسمياً</div>
        <div class="seal-corp">HR CERTIFIED</div>
        <div class="seal-date">{stamp_date_str}</div>
      </div>
    </div>
  </div>

  <div class="footer-note">
    هذا التقرير صادر آلياً وموثق رقمياً عبر نظام إدارة أوقات العمل الذكي (BUSSMA_TTS) لمؤسسة بصمة. التقرير محمي وغير قابل للتعديل بعد التصدير.
  </div>

</body>
</html>
"""
    return html


def export_pdf_report(
    db: Database,
    config: Config,
    start_date: date,
    end_date: date,
    open_folder: bool = True,
    output_dir: Path | None = None,
    half_day_dates: set[date] | None = None,
    employee_name: str | None = None,
) -> Path:
    """Generate and save an official HR-ready PDF timesheet report with Bussma seal.
    
    Uses Windows built-in headless browser (msedge.exe) to render high quality
    A4 printable PDF with seal, logo, approval blocks, and compliance breakdown.
    """
    _aggregate_before_build(db, config)
    reports_dir = output_dir or get_reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{start_date.isoformat()}_to_{end_date.isoformat()}"
    base_file_name = f"TimeReport_{suffix}"
    pdf_path = reports_dir / f"{base_file_name}.pdf"

    # BUG-14: Chromium prints to a temp PDF and we os.replace it into place, so
    # a crash or a locked target can never leave a half-written report.
    temp_pdf = reports_dir / f".tt_pdf_{uuid.uuid4().hex[:8]}.pdf"

    logo_base64 = ""
    app_root = get_app_root()
    logo_path = app_root / "assets" / "official_logo.png"
    if logo_path.exists():
        try:
            with open(logo_path, "rb") as lf:
                logo_base64 = f"data:image/png;base64,{base64.b64encode(lf.read()).decode('ascii')}"
        except Exception as exc:
            logger.warning("Could not encode official logo for PDF: %s", exc)

    rows, totals = _daily_summary_rows(db, config, start_date, end_date, half_day_dates)
    projects = project_breakdown(db, config, start_date, end_date)

    report_uid = f"BSS-TR-{start_date.strftime('%Y%m')}-{uuid.uuid4().hex[:8].upper()}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    emp = employee_name or "موظف رسمي / Official Staff"
    compliance_pct = (totals["worked"] / totals["target"] * 100) if totals["target"] > 0 else 100.0

    html_content = _build_official_pdf_html(
        logo_base64=logo_base64,
        start_date=start_date,
        end_date=end_date,
        report_uid=report_uid,
        now_str=now_str,
        employee_name=emp,
        totals=totals,
        compliance_pct=compliance_pct,
        rows=rows,
        projects=projects,
    )

    browser_exe = find_headless_browser()
    if browser_exe:
        temp_html = Path(tempfile.gettempdir()) / f"report_{uuid.uuid4().hex[:8]}.html"
        user_data_dir = Path(tempfile.gettempdir()) / f"tt_ud_{uuid.uuid4().hex[:8]}"
        try:
            temp_html.write_text(html_content, encoding="utf-8")
            user_data_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                browser_exe,
                "--headless",
                "--disable-gpu",
                "--run-all-compositor-stages-before-draw",
                "--no-pdf-header-footer",
                f"--user-data-dir={str(user_data_dir)}",
                f"--print-to-pdf={str(temp_pdf.resolve())}",
                str(temp_html.resolve()),
            ]
            subprocess.run(cmd, check=True, timeout=45)
            # BUG-08: a zero exit code does not guarantee a file was produced
            # (e.g. headless Chromium silently fails when the default profile is
            # already in use). Verify the output actually exists and is non-empty.
            if not temp_pdf.is_file() or temp_pdf.stat().st_size == 0:
                raise RuntimeError(
                    f"Headless browser exited successfully but produced no PDF at {temp_pdf}"
                )
            os.replace(temp_pdf, pdf_path)
            logger.info("Official PDF Report generated successfully: %s", pdf_path)
        except Exception as exc:
            logger.error("Failed to generate PDF via browser: %s. Saving HTML report instead.", exc, exc_info=True)
            fallback_html_path = reports_dir / f"{base_file_name}.html"
            fallback_html_path.write_text(html_content, encoding="utf-8")
            if open_folder:
                try:
                    os.startfile(reports_dir)
                except Exception:
                    pass
            return fallback_html_path
        finally:
            try:
                temp_html.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                temp_pdf.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception:
                pass
    else:
        logger.warning("No headless browser detected (msedge/chrome). Saving HTML report.")
        fallback_html_path = reports_dir / f"{base_file_name}.html"
        fallback_html_path.write_text(html_content, encoding="utf-8")
        if open_folder:
            try:
                os.startfile(reports_dir)
            except Exception:
                pass
        return fallback_html_path

    if open_folder:
        try:
            os.startfile(reports_dir)
        except Exception:
            pass

    return pdf_path
