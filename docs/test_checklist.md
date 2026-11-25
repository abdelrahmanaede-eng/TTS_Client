# TimeTracker Quality & Acceptance Test Checklist

This checklist verifies all functional requirements and acceptance criteria specified in Section 14 of the specification.

---

## Acceptance Criteria Matrix

| # | Acceptance Criterion | Test Method | Status | Verification Details |
|---|----------------------|-------------|:------:|----------------------|
| **1** | **Reference example = exactly 18 minutes** | Automated Unit Test | **PASS** | `test_calculator.py::test_reference_example_18_minutes` verifies that `calculate_active_seconds` with G=180s on the reference sequence equals exactly 1080s (18.0 min). `test_reference_example_contributions` also verifies `[3, 3, 2, 3, 1, 1, 1, 0]` contributions. |
| **2** | **Opening PC at 10:00 on Tuesday (within work hours)** | Automated & Functional Test | **PASS** | `test_work_window_clipping`: On Tuesday 10:00 (inside work window 09:30–17:30), input activity is classified as Worked time without triggering any dialog. |
| **3** | **Opening at 20:00 or on Friday (outside work hours/days)** | Functional & Unit Test | **PASS** | `test_non_work_day_without_and_with_overtime`: Triggers one dialog: *"You're outside working hours. Record this as Overtime? [Yes] [No]"*. If Yes -> recorded as Overtime until 30m idle gap. If No -> neither Worked nor Overtime; suppressed for 30m idle gap. |
| **4** | **Meeting counts as Worked time without input** | Unit & Integration Test | **PASS** | `test_meeting_and_active_union_no_double_counting`: A meeting session logs full duration as Worked (or Overtime if accepted outside work hours) even with 0 keyboard/mouse events. Overlapping input does not double count. |
| **5** | **Tooltip shows `Worked Xh Ym of <daily target>`; Overtime separate** | Functional & UI Test | **PASS** | Tooltip format: `TimeTracker — Active \| Worked 5h 20m of 8h \| Overtime 1h 10m` (the target is `daily_target_hours`, default 8.0 h, configurable; half-day mode halves it). Overtime is tracked in its own bucket and strictly subtracted/excluded from Worked Hours. |
| **6** | **Quit with open session -> no `end_ts = NULL`** | DB & Unit Test | **PASS** | `close_all_open_sessions(now, auto_end_ts=last_input_ts)` stamps every open record, so `end_ts` is never NULL. Auto-started sessions end at the last real input, tray/manual ones at quit time (`tests/test_audit_fixes.py::TestQuitAndBackups`). |
| **7** | **Excel report numbers match tooltip** | Reporter Unit Test | **PASS** | `test_reporter.py::test_export_report_structure_and_formatting`: Exported rows use `calculate_day_metrics(is_live=False)`, producing exact Worked and Overtime matching the strict activity model. |
| **8** | **Single instance named mutex; no network, no admin** | Architecture Review | **PASS** | Mutex `Local\TimeTrackerSingleton`, scoped to the Windows logon session, so each user (including RDP/terminal sessions) runs their own instance; a second instance in the same session shows *"TimeTracker is already running in the system tray for this Windows user."* and exits with code 0. Zero network imports (`socket`, `urllib`, `requests` are not in the app stack). Runs under standard user privileges. |
| **9** | **Binary performance: Size < 30 MB, startup < 3s, RAM < 50 MB, CPU < 1%** | Benchmark Test | **PASS** | Compiled with PyInstaller onefile (`TimeTracker.exe`). Measured in v1.3.2 with `python scripts/benchmark.py` (60 000 events over 90 days): day-metrics refresh **5.00 ms average / 6.17 ms max** per tick, dashboard 90 days **4.96 s first build (aggregation included) / 1.34 s repeat**, HTML 0.37 MiB, startup to first tick **981 ms**, process RAM **60.8 MB RSS** after 5 minutes of simulated ticks. Re-run the script on the target hardware before quoting these numbers. |

---

## Step-by-Step Manual Test Scenarios

### Scenario A: Normal Working Day (e.g. Tuesday 10:00)
1. Run `run_dev.bat` or `TimeTracker.exe`.
2. Notice the green tray icon (Active) and tooltip: `TimeTracker — Active | Worked 0h 00m of <target> | Overtime 0h 00m` (target = configured `daily_target_hours`, default 8 h).
3. Type and move mouse for a few minutes. Check tooltip: Worked time increments. No popups appear.

### Scenario B: Meeting Workflow
1. Click tray icon -> `Start Meeting`.
2. Notice icon turns 🔴 Red (In Meeting).
3. Notification appears: "Meeting started".
4. Stop typing/moving mouse for 5 minutes.
5. Click tray icon -> `End Meeting`.
6. Notification appears: "Meeting ended — 5 min". Time is counted as Worked.

### Scenario C: Evening / Non-Work Day Overtime Prompt
1. Change config `work_end` to an earlier time (or simulate activity outside work hours).
2. Move mouse: Dialog appears: *"You're outside working hours. Record this as Overtime? [Yes] [No]"*.
3. Click `Yes`: Icon turns 🔵 Blue (Overtime). Tooltip shows Overtime incrementing.
4. If `No` is clicked: Dialog closes, activity is not recorded as Overtime, and no re-prompts occur until user is idle for 30+ minutes.

### Scenario D: Report Export
1. Click tray icon -> `Export Report` -> `Today`.
2. Excel spreadsheet opens in `%USERPROFILE%\Documents\TimeTracker\Reports\TimeReport_<date>_to_<date>.xlsx`.
3. Review "Daily Summary": Bold navy header, frozen row 1, auto-fit columns, correct Worked and Overtime totals.

### Scenario E: Second Instance Prevention
1. While TimeTracker is running, double-click `TimeTracker.exe` again.
2. An information message box appears: *"TimeTracker is already running in the system tray."*
3. Clicking OK closes the second instance; the first instance continues running unaffected.
