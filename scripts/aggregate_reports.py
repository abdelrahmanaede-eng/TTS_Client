"""Team Report Aggregator for Managers (Standalone Utility).

Combines individual employee TimeTracker Excel reports (TimeReport_*.xlsx)
into a unified Team Dashboard without any server or network requirements.
Can be run via command line or double-clicked to pick a folder.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def aggregate_employee_reports(folder_path: Path, output_file: Path | None = None) -> Path | None:
    """Read all TimeReport_*.xlsx in folder_path and generate a consolidated report."""
    excel_files = sorted(folder_path.glob("TimeReport_*.xlsx"))
    if not excel_files:
        excel_files = sorted([f for f in folder_path.glob("*.xlsx") if not f.name.startswith("Team_Consolidated_")])

    if not excel_files:
        print(f"No Excel reports found in {folder_path}")
        return None

    wb_out = openpyxl.Workbook()

    # Styling
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
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

    # ---------------------------------------------------------
    # Sheet 1: Team Summary
    # ---------------------------------------------------------
    ws_summary = wb_out.active
    ws_summary.title = "Team Summary"
    sum_headers = [
        "Employee File",
        "Total Worked Hours",
        "Total Meeting Hours",
        "Total Break Hours",
        "Total Overtime Hours",
        "Target Hours",
        "Remaining Hours",
    ]
    ws_summary.append(sum_headers)
    for col_idx in range(1, len(sum_headers) + 1):
        cell = ws_summary.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_summary.freeze_panes = "A2"

    # ---------------------------------------------------------
    # Sheet 2: All Employees Daily Log
    # ---------------------------------------------------------
    ws_daily = wb_out.create_sheet(title="All Employees Daily Log")
    daily_headers = [
        "Employee File",
        "Date",
        "Day",
        "Worked Hours",
        "Meeting Hours",
        "Break Hours",
        "Overtime Hours",
        "Target",
        "Remaining",
    ]
    ws_daily.append(daily_headers)
    for col_idx in range(1, len(daily_headers) + 1):
        cell = ws_daily.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_daily.freeze_panes = "A2"

    team_worked = 0.0
    team_meeting = 0.0
    team_break = 0.0
    team_overtime = 0.0
    team_target = 0.0
    team_remaining = 0.0

    app_durations: dict[str, float] = {}

    for fpath in excel_files:
        emp_name = fpath.stem.replace("TimeReport_", "")
        try:
            wb_in = openpyxl.load_workbook(fpath, data_only=True)
            if "Daily Summary" in wb_in.sheetnames:
                ws_in = wb_in["Daily Summary"]
                emp_worked = 0.0
                emp_meeting = 0.0
                emp_break = 0.0
                emp_ot = 0.0
                emp_target = 0.0
                emp_rem = 0.0

                for row in ws_in.iter_rows(min_row=2, values_only=True):
                    if not row or not row[0]:
                        continue
                    if str(row[0]).strip().lower() == "total":
                        continue

                    # row: Date, Day, First, Last, Worked, Meeting, Break, Overtime, Target, Remaining
                    r_date = str(row[0])
                    r_day = str(row[1]) if len(row) > 1 else ""

                    # Parse numeric cells defensively: a single bad/shifted cell must
                    # not abort the whole employee file (BUG-11). Skip the row on
                    # failure rather than leaving a half-written report.
                    try:
                        r_worked = float(row[4] or 0.0) if len(row) > 4 else 0.0
                        r_meet = float(row[5] or 0.0) if len(row) > 5 else 0.0
                        r_break = float(row[6] or 0.0) if len(row) > 6 else 0.0
                        r_ot = float(row[7] or 0.0) if len(row) > 7 else 0.0
                        r_tar = float(row[8] or 0.0) if len(row) > 8 else 0.0
                        r_rem = float(row[9] or 0.0) if len(row) > 9 else 0.0
                    except (TypeError, ValueError):
                        print(f"  Skipping malformed row in {fpath.name}: {row!r}")
                        continue

                    emp_worked += r_worked
                    emp_meeting += r_meet
                    emp_break += r_break
                    emp_ot += r_ot
                    emp_target += r_tar
                    emp_rem += r_rem

                    ws_daily.append([
                        emp_name, r_date, r_day, r_worked, r_meet, r_break, r_ot, r_tar, r_rem
                    ])
                    cur_r = ws_daily.max_row
                    for c_idx in range(1, len(daily_headers) + 1):
                        c = ws_daily.cell(row=cur_r, column=c_idx)
                        c.font = data_font
                        c.border = thin_border
                        if c_idx in (1, 2, 3):
                            c.alignment = Alignment(horizontal="center")
                        else:
                            c.alignment = Alignment(horizontal="right")
                            c.number_format = "0.00"

                ws_summary.append([
                    emp_name,
                    round(emp_worked, 2),
                    round(emp_meeting, 2),
                    round(emp_break, 2),
                    round(emp_ot, 2),
                    round(emp_target, 2),
                    round(emp_rem, 2),
                ])
                cur_r = ws_summary.max_row
                for c_idx in range(1, len(sum_headers) + 1):
                    c = ws_summary.cell(row=cur_r, column=c_idx)
                    c.font = data_font
                    c.border = thin_border
                    if c_idx == 1:
                        c.alignment = Alignment(horizontal="left")
                    else:
                        c.alignment = Alignment(horizontal="right")
                        c.number_format = "0.00"

                team_worked += emp_worked
                team_meeting += emp_meeting
                team_break += emp_break
                team_overtime += emp_ot
                team_target += emp_target
                team_remaining += emp_rem

            if "App Usage" in wb_in.sheetnames:
                ws_app_in = wb_in["App Usage"]
                for a_row in ws_app_in.iter_rows(min_row=2, values_only=True):
                    if a_row and len(a_row) >= 3 and a_row[1] and a_row[2] is not None:
                        try:
                            app = str(a_row[1])
                            hrs = float(a_row[2])
                            app_durations[app] = app_durations.get(app, 0.0) + hrs
                        except ValueError:
                            pass

        except Exception as exc:
            print(f"Error processing {fpath.name}: {exc}")
        finally:
            try:
                wb_in.close()
            except Exception:
                pass

    # Add Totals Row to Summary
    ws_summary.append([
        "Team Total",
        round(team_worked, 2),
        round(team_meeting, 2),
        round(team_break, 2),
        round(team_overtime, 2),
        round(team_target, 2),
        round(team_remaining, 2),
    ])
    tot_row_idx = ws_summary.max_row
    for c_idx in range(1, len(sum_headers) + 1):
        c = ws_summary.cell(row=tot_row_idx, column=c_idx)
        c.font = total_font
        c.fill = total_fill
        c.border = total_border
        if c_idx == 1:
            c.alignment = Alignment(horizontal="left")
        else:
            c.alignment = Alignment(horizontal="right")
            c.number_format = "0.00"

    # Sheet 3: Team Top Applications
    if app_durations:
        ws_apps = wb_out.create_sheet(title="Team App Usage")
        app_headers = ["Application", "Total Hours across Team"]
        ws_apps.append(app_headers)
        for col_idx in range(1, 3):
            cell = ws_apps.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws_apps.freeze_panes = "A2"

        for app, hrs in sorted(app_durations.items(), key=lambda x: x[1], reverse=True):
            ws_apps.append([app, round(hrs, 2)])
            r_idx = ws_apps.max_row
            ws_apps.cell(row=r_idx, column=1).alignment = Alignment(horizontal="left")
            ws_apps.cell(row=r_idx, column=1).font = data_font
            ws_apps.cell(row=r_idx, column=2).alignment = Alignment(horizontal="right")
            ws_apps.cell(row=r_idx, column=2).font = data_font
            ws_apps.cell(row=r_idx, column=2).number_format = "0.00"

        for col in ws_apps.columns:
            ws_apps.column_dimensions[get_column_letter(col[0].column)].width = 24

    for sheet in (ws_summary, ws_daily):
        for col in sheet.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            sheet.column_dimensions[get_column_letter(col[0].column)].width = max(max_len + 4, 14)

    target_out = output_file or (folder_path / f"Team_Consolidated_Report_{date.today().isoformat()}.xlsx")
    wb_out.save(target_out)
    print(f"Team consolidated report successfully generated: {target_out}")
    return target_out


def main() -> None:
    if len(sys.argv) > 1:
        target_dir = Path(sys.argv[1])
    else:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askdirectory(title="Select Folder containing Employee TimeReports")
        if not selected:
            print("No folder selected.")
            return
        target_dir = Path(selected)

    out = aggregate_employee_reports(target_dir)
    if out and out.exists():
        try:
            os.startfile(out)
        except Exception:
            pass


if __name__ == "__main__":
    main()
