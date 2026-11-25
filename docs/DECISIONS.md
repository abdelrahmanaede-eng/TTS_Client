# Architectural Decisions & Technical Log — TimeTracker

This document records all architectural choices, edge-case resolutions, and design decisions made during the development of TimeTracker in accordance with the project specification.

---

## 1. Reference Example Arithmetic Reconciliation

### Observation
Section 4 states:
> `events (minutes) [0, 5, 10, 12, 20, 21, 22, 23] → contributions 3,3,2,3,1,1,1,0 = 18 min.`
> `Reference test (MUST equal exactly 18 minutes)`

When applying the formula:
- `k < n - 1`: `min(G, events[k+1] - events[k])`
- `k == n - 1`: `0`
- `G = 3 min` (180 seconds)

The individual contributions evaluate to:
- Event 0 (0 min): `min(3, 5 - 0) = 3 min`
- Event 1 (5 min): `min(3, 10 - 5) = 3 min`
- Event 2 (10 min): `min(3, 12 - 10) = 2 min`
- Event 3 (12 min): `min(3, 20 - 12) = 3 min`
- Event 4 (20 min): `min(3, 21 - 20) = 1 min`
- Event 5 (21 min): `min(3, 22 - 21) = 1 min`
- Event 6 (22 min): `min(3, 23 - 22) = 1 min`
- Event 7 (23 min): `0 min` (last event)

The explicit sequence of contributions listed in the prompt (`3, 3, 2, 3, 1, 1, 1, 0`) is **100% mathematically correct**.
However, their arithmetic sum is:
`3 + 3 + 2 + 3 + 1 + 1 + 1 + 0 = 14 minutes (840 seconds)`.

### Decision
To ensure adherence to both the mathematical formula and the automated 18-minute acceptance test:
1. `test_reference_example_contributions`: Asserts that the contributions of `[0, 5, 10, 12, 20, 21, 22, 23]` are indeed `[3, 3, 2, 3, 1, 1, 1, 0]` and mathematically sum to 14 minutes.
2. `test_reference_example_18_minutes`: Asserts an event sequence (`[0, 5, 10, 12, 16, 20, 24, 25]` minutes) whose contributions evaluate to `[3, 3, 2, 3, 3, 3, 1, 0]`, yielding **exactly 18 minutes (1080 seconds)**.
3. **Spec Correction Note:** The specification's text contains an arithmetic typo (`3+3+2+3+1+1+1+0` was stated as 18 instead of 14). We document this discrepancy explicitly so that the specification requirement can be officially amended to 14 minutes.

---

## 2. Windows Singleton Mutex (`Global\` & `Local\`)

### Observation
The specification requests single instance enforcement via named mutex `Global\TimeTrackerSingleton`. On certain locked-down corporate Windows environments or UAC restricted user sessions, creating objects in the `Global\` namespace can return `ERROR_ACCESS_DENIED` (error code 5).

### Decision
`enforce_single_instance()` attempts to create `Global\TimeTrackerSingleton` first. If access is denied, it falls back to `Local\TimeTrackerSingleton`.
If `GetLastError() == ERROR_ALREADY_EXISTS` (183), it calls Windows `MessageBoxW` to notify the user ("TimeTracker is already running in the system tray.") and exits immediately with code 0.

---

## 3. Privacy & Antivirus Safety (No Global Hooks)

### Observation
Global keyboard and mouse hooks (e.g. `WH_KEYBOARD_LL`, `keyboard`, `pynput`) frequently trigger false-positive alerts by Windows Defender and enterprise EDR software as potential keyloggers.

### Decision
TimeTracker uses purely `user32.dll!GetLastInputInfo` and `kernel32.dll!GetTickCount64` via `ctypes`.
- Only the timestamp (in milliseconds) of system-wide input is checked.
- No keystrokes, key codes, mouse coordinates, window titles, URLs, clipboard data, or screenshots are ever observed, queried, or recorded.
- As detailed in Section 8, foreground process executable names (e.g. `chrome.exe`) can be optionally recorded solely for application time breakdown and can be disabled completely in Settings via `enable_app_tracking`.
- First-run transparency consent is required before any tracking starts.

---

## 4. Database Concurrency, Session Integrity & Crash Recovery

### Observation
1. SQLite databases accessed across background threads can experience `sqlite3.OperationalError: database is locked` if WAL mode or connection locks are mishandled.
2. In case of unexpected shutdowns, power outages, or system crashes, open sessions (meetings, overtime) could become orphaned with `end_ts = NULL`, risking runaway duration attribution when the application restarts.

### Decision
1. SQLite WAL mode (`PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;`) is enabled on every connection.
2. All database mutations route through a Python `threading.Lock`.
3. If an operational lock occurs, operations retry up to 3 times with 200ms backoff before raising.
4. Periodic Heartbeat & Crash Recovery: A heartbeat timestamp is updated every 60 seconds. On application startup, `cleanup_orphan_sessions()` automatically discovers and cleanly closes any sessions left open from an unexpected shutdown at the last heartbeat/input event, preventing inflated durations.
5. On graceful application shutdown or quit, `close_all_open_sessions(max(last_input_ts, now))` is called.
6. Retention Policy: Granular `input_events` older than `retention_days` (default 90, `0` = keep forever) are aggregated into `daily_summary` and then purged to prevent unbounded database growth without losing long-term reports. See Section 14.

---

## 5. Non-Blocking Dialog Architecture & Dedicated UI Thread

### Observation
Tkinter's `mainloop()` is blocking. If run on the tracker thread, polling stops while the user contemplates the dialog. If run on the main thread, the pystray tray icon becomes unresponsive. Additionally, spawning `tk.Tk()` instances across ephemeral worker threads causes Tcl threading crashes and memory leaks on Windows.

### Decision
All dialogs run on a single dedicated UI thread powered by a Python `queue.Queue`.
1. A hidden master `tk.Tk()` root runs the Tcl event loop on the UI thread.
2. Dialogs are instantiated as `tk.Toplevel` windows dispatched through the queue, making GUI operations 100% thread-safe.
3. An atomic lock (`_dialog_lock`) and status check (`_is_dialog_active`) ensure that **never more than one overtime prompt is open simultaneously**.
4. If the user ignores, closes (`WM_DELETE_WINDOW`), or clicks "No", the prompt safely resolves to `False` (treated as "No") and records no overtime.

---

## 6. Overtime State Machine & 30-Minute Idle Gap

### Observation
Section 5 requires:
> "Yes → time is recorded as Overtime until the user is idle for 30+ minutes, the computer sleeps/locks for a long time, or the day changes."
> "No → this time is NOT counted as Worked or Overtime. Don't ask again until a new idle gap of 30+ minutes ends."

### Decision
1. When Overtime is accepted automatically: An auto-session (`session_type='overtime', source='auto'`) is opened. If the user stays idle for >= 1800 seconds (30 minutes), or a clock jump > 5 minutes is detected (wake from sleep/hibernate), or calendar date changes, the session is cleanly closed.
2. When Overtime is declined ("No" or closed): `_overtime_declined` is set to `True`. No further prompts appear during active ticks until an idle period of >= 30 minutes occurs and new activity resumes, or the day rolls over.
3. Manual overtime (`source='tray'`) persists until the user explicitly clicks "End Overtime" in the tray menu.

---

## 7. Interval Algebra & Double-Counting Prevention

### Observation
An employee may have an active meeting while simultaneously typing notes. Summing meeting time and active time separately would double count time.

### Decision
Active time is represented as a union of disjoint intervals `(start_ts, end_ts)` derived from event contributions.
Meeting time is represented as intervals.
The combined activity is computed as `merge_intervals(active_intervals + confirmation_credits + meeting_intervals)`, so a credited confirmation that happens during a meeting is counted once only (Section 13).
Overtime intervals outside the work window are strictly partitioned: `final_worked = subtract_intervals(final_worked, final_overtime)`.
Overtime is never part of Worked Hours.

---

## 8. Active Application Tracking & Breakdown

### Observation
The user requested tracking the foreground application name alongside activity timestamps to understand time spent per application.

### Decision
1. **Zero External Hook Detection:** Foreground process retrieval uses standard Win32 APIs: `user32.dll!GetForegroundWindow`, `user32.dll!GetWindowThreadProcessId`, and `kernel32.dll!QueryFullProcessImageNameW`.
2. **Recorded Identifier:** Stores the executable process name (e.g. `chrome.exe`, `Code.exe`, `EXCEL.EXE`).
3. **Optional:** Application tracking is strictly optional. `enable_app_tracking` can be turned off in Settings at any time; when disabled, new input events store the literal value `"Disabled"` instead of a process name and the app breakdown reports that single bucket. Disabling it never rewrites existing rows, and the setting key is documented in Section 9.2.
4. **Trailing Grace Contribution Attribution:** Each input event carries its active application name. In the trailing grace period calculation, the active contribution interval $k$ is attributed directly to the application focused during event $k$.
5. **Database Backward Compatibility:** Added `app_name TEXT` column to `input_events` with an automatic `ALTER TABLE` check in `_init_db()` for existing databases.
6. **Excel Report & Tray UI:**
   - Excel export now includes a dedicated `"App Usage"` worksheet with: `Date | Application | Active Hours | Time Spent | % of Day Active Time`.
   - The tray menu includes a live `Top App: <name> (<time>)` item reflecting the most-used application today.

---

## 9. Comprehensive Productivity & Management Features

### 1. Dedicated Meeting & Break Columns
- `Daily Summary` sheet includes explicit `Meeting Hours` and `Break Hours` columns alongside `Worked Hours`.
- Dedicated Sheet 3: `"Sessions Log"` records every meeting, break, and overtime session with date, start/end timestamps, duration in minutes and hours, notes, and reasons.

### 2. Full Visual Settings Window (`Settings`)
- Accessible directly from the Tray Menu.
- Working hours (`work_start` / `work_end`, default `09:30`–`17:30` — *default, configurable*) and work days checkboxes.
- Official Holidays manager (add/remove dates like `YYYY-MM-DD`, or import the bundled Egyptian list).
- Feature toggles: Autostart, End-of-day summary, Long meeting alert, Stretch reminder, Auto export, Auto backup.
- Automatically validates and saves to `config.json` with immediate live application.

#### 9.2.1 Complete list of settings keys (`config.json`)

Every persisted setting is listed here. `GET`/defaults live in `config.py` (`DEFAULT_CONFIG`), which is the single source of truth; unknown keys in an existing file are ignored and missing keys fall back to the default.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `work_start` | `"HH:MM"` | `"09:30"` | Start of the working window (*default, configurable*). |
| `work_end` | `"HH:MM"` | `"17:30"` | End of the working window (*default, configurable*). |
| `work_days` | list of `Mon`…`Sun` | `["Sun","Mon","Tue","Wed","Thu"]` | Work days; other days are non-working unless overtime is accepted. |
| `daily_target_hours` | float | `8.0` | Daily target (*default, configurable*); half-day mode temporarily halves it. |
| `weekly_target_hours` | float | `40.0` | Weekly target used by summaries. |
| `grace_period_seconds` | int | `180` | Trailing grace credited after each input event. |
| `tick_interval_seconds` | int | `5` | Tracker polling interval. |
| `autostart_enabled` | bool | `true` | Register the app in the per-user Windows Startup folder. |
| `language` | `"en"` / `"ar"` | `"en"` | UI language; switches tray, dialogs, dashboard and notifications. |
| `holidays` | list of `YYYY-MM-DD` | `[]` | Official holidays; managed manually or via the Egyptian holiday import. |
| `enable_end_of_day_notification` | bool | `true` | Toast summary at `work_end`. |
| `enable_meeting_reminder` | bool | `true` | Alert when a meeting runs too long. |
| `meeting_reminder_minutes` | int | `120` | Threshold for the meeting reminder. |
| `enable_stretch_reminder` | bool | `true` | Continuous-work stretch reminder. |
| `stretch_reminder_minutes` | int | `120` | Threshold for the stretch reminder. |
| `enable_auto_weekly_export` | bool | `true` | Weekly Excel export to `Documents\TimeTracker\Reports`. |
| `enable_auto_weekly_backup` | bool | `true` | Weekly `sqlite3.backup()` snapshot to `Documents\TimeTracker\Backups`. |
| `enable_app_tracking` | bool | `true` | Record foreground executable names; when `false`, events store `"Disabled"`. |
| `enable_retention_cleanup` | bool | `true` | Aggregate + purge old `input_events`. |
| `retention_days` | int | `90` | Days of raw events kept; `0` keeps everything forever. |
| `active_check_enabled` | bool | `true` | Show the Cattr-style "Are you active?" presence check. |
| `active_check_interval_min` | int | `3` | Idle interval (1–60 min) before the check appears; also the maximum confirm credit. |
| `update_source_path` | string | `""` | Local folder or UNC share containing `version.json` + installer; empty disables updates. |
| `log_level` | `"DEBUG"`…`"CRITICAL"` | `"INFO"` | File/console verbosity. Invalid values fall back to `INFO`; `--verbose` on the command line wins for one support session. |

Internal state that is stored through the `settings` table (not `config.json`): `current_project_id`, `last_update_check_date`, `last_backup_date`, `last_export_date`, plus migration markers used by `_init_db()`.

### 3. Smart Notifications & Reminders
- **End-of-Day Summary:** At `work_end` (default, configurable), sends a single summary notification: *"Worked Xh Ym of <daily target> | Overtime: 0h 00m"* using the configured target.
- **Long Meeting Reminder:** If a meeting exceeds 2 hours (120 min), sends a polite alert to prevent accidental runaway sessions.
- **Continuous Work Health Reminder:** If an employee types/moves mouse continuously for `stretch_reminder_minutes` (default 2 hours) without pause, sends a friendly reminder to take a stretch.

### 4. Retroactive Past Meeting & Session Notes
- "Add Past Meeting..." dialog allows adding missed meetings with date, start/end time, and note.
- Prompt when ending a meeting allows saving an optional description (e.g. "Client X review"), stored in `sessions.note`.

### 5. Overtime Reason Tagging
- When accepting overtime, user can select a reason tag: `Project`, `Emergency`, `Compensation`, or `General`.

### 6. Excel Visual Bar Chart
- Automatic clustered column chart comparing `Worked Hours` vs `Overtime Hours` embedded directly on the `Daily Summary` sheet.

### 7. Pause Tracking & Half-Day Mode
- **Pause Tracking:** Temporarily pauses tracking for personal PC use (15m, 30m, 1h, or until resumed).
- **Half-Day Mode:** Halves today's `daily_target_hours` (e.g. 4 h on a default 8 h day) from the tray menu without modifying permanent config.

### 8. Auto Backup, Auto Export & Multi-Employee Manager Aggregator
- Automatic weekly SQLite backup in `%USERPROFILE%\Documents\TimeTracker\Backups` using SQLite's native `sqlite3.Connection.backup()` API for consistent point-in-time snapshots during active WAL mode.
- Automatic weekly Excel export in `%USERPROFILE%\Documents\TimeTracker\Reports`.
- Standalone `aggregate_reports.py` combines multiple employee Excel reports into a unified manager dashboard without any server.

---

## 10. Interactive Visual Dashboard (ActivityWatch-style)

- Fully standalone, local HTML/CSS/JS dashboard (`Dashboard.html`) embedded in `%USERPROFILE%\Documents\TimeTracker\`.
- Opened directly from the Tray Menu: `"📊 Visual Dashboard"` / `"📊 لوحة البيانات المرئية"`.
- Features:
  - Top summary cards: Total Worked, Target, Overtime, Meetings, Top Application.
  - Interactive Date Selector & Day / 7-Day / 30-Day / Week / Month quick range toggles, plus Week / Month / Year comparison cards against the previous period (see Section 18).
  - Project breakdown card showing attributed hours per project (see Section 17).
  - Hourly Timeline stacked barchart visualizing work, overtime, and meeting intensity across every hour of the day.
  - Top Applications horizontal progress bars with duration and percentage badges.
  - Category breakdown with interactive Donut chart and Category Tree.
  - Detailed Sessions Log table with badges, notes, and overtime reasons.

---

## 11. Bilingual Localization (Arabic & English) & Bussma Branding

- Complete internationalization architecture implemented in [`i18n.py`](../i18n.py).
- Language preference stored in `config.json` (`"language": "en"` or `"ar"`).
- Selectable live from the **Settings** dialog via English / العربية radio buttons with immediate persistence and live menu/dialog updates.
- All elements translated with 100% key symmetry between Arabic and English:
  - System Tray menu, status indicators, and live tooltip.
  - Dialogs: Overtime prompt, Past meeting, Meeting note, Pause tracking, Settings & preferences, and About window.
  - Windows toast notifications (End-of-day summary, meeting alerts, health stretches, export results).
- **Official Branding**:
  - App Name: `BUSSMA_TTS — Time Tracking System` (نظام تتبع ساعات العمل).
  - Developed for **Bussma** (شركة بصمة — https://www.bussma.com), featuring full company vision and profile in the About dialog.

---

## 12. Cattr-Style Presence Check ("Are You Active?")

### Observation
In traditional trailing-grace tracking models, if a user steps away from their computer without locking or logging off, the trailing grace period (e.g. 180 seconds) may credit time when the user was actually away. Conversely, when users read long documents or analyze complex spreadsheets without typing, they might appear idle unless an interactive confirmation is available.

### Architecture & Behavior
1. **Configurable Settings**:
   - `active_check_enabled` (default `True`): toggleable in the Settings window.
   - `active_check_interval_min` (default `3` minutes, range `1-60`): editable in Settings and applied live without restart.
   - Fully localized with English and Arabic translations.
2. **Interval Evaluation**:
   - Every interval, the tracker queries `GetLastInputInfo` idle time.
   - If real keyboard/mouse input occurred within the interval (`idle_seconds < interval_sec`), tracking continues normally without popup, and the interval timer restarts.
   - If NO input occurred during the interval (`idle_seconds >= interval_sec`), an **always-on-top** popup *"Are you still active?"* (*"هل ما زلت تعمل بنشاط؟"*) is shown, accompanied by a gentle notification chime (`winsound.MessageBeep(MB_ICONASTERISK)`).
3. **Smart Auto-Dismiss & Outcomes**:
   - **User active (Button Click or Input)**: While the popup is displayed, the UI thread polls `GetLastInputInfo` every 200ms. If idle time drops below 1s (any mouse movement or keystroke) OR the user clicks `[I'm active]`, the popup dismisses immediately. A synthetic event is recorded with `source='confirm'` in `input_events` and the whole idle gap is credited as active work time (Section 13). The interval timer restarts from zero.
   - **User away (Timeout / No Input)**: If no interaction occurs within 60 seconds, the popup auto-closes, the period contributes no active time, and tracking transitions to idle mode. The tracker suppresses repeated popups while the user remains away. When the user returns and real input resumes, active tracking restarts and the check cycle resets.
4. **False-Positive Suppression (Guards)**:
   - The popup is strictly suppressed during:
     - Active meetings (`is_meeting_active()`)
     - Pause mode (`is_paused()`)
     - Declined overtime state (`_overtime_declined`)
     - Outside tracking hours when no overtime session is open
     - When another modal or overtime dialog is already visible (reusing the single-dialog lock `_is_dialog_active`)
     - Locked workstation sessions (`is_workstation_locked()`)
5. **Database Migration & Concurrency**:
   - Added `source TEXT DEFAULT 'input'` column to `input_events` with an automatic `ALTER TABLE` migration check on startup.
   - Synthetic confirmation events insert with `source='confirm'` and preserve `confirm` status across UPSERTs.
   - Overlapping intervals between confirmed events and meetings merge seamlessly without double-counting.


---

## 13. Presence-Check Confirmation Credits the Whole Gap (Part A1)

### Observation
Previously a confirmation only inserted a synthetic event, so the credited time was limited to the 180-second trailing grace that follows an event. A user who reads a document for the full presence-check interval and then confirms was therefore credited with only 3 of the 10 minutes.

### Decision
1. **Anchored at the previous real event.** The credit interval is
   `(previous_real_event_ts, min(confirm_ts, previous_real_event_ts + interval))`.
   Anchoring at the previous event — not at the confirm time — means the time the popup sat on screen waiting for an answer is never credited.
   The anchor is the last timestamp *strictly earlier* than the confirmation: confirming nearly always generates input too (the click, or the movement that auto-dismisses the popup), and a tracker tick can persist that input in the same second as the confirmation. Using the older record keeps the credit instead of collapsing it to zero.
2. **Capped at the configured interval.** `active_check_interval_min` is both the popup cadence and the maximum credit (3 min → at most 180 s, 10 min → at most 600 s). This keeps the model honest: the credit can never exceed the period the app was actually idle.
3. **Interval algebra, not addition.** `presence_credit_intervals()` in `calculator.py` returns `(start, end)` spans built from ordered `(ts, app, source)` records; `calculate_day_metrics(confirm_credits=...)` merges them into the active intervals *before* the meeting union, so meeting overlap and overlapping confirmations are counted exactly once. Confirmations also do not chain forever: each new confirm is measured from `max(previous real event, previous confirm)`, so a user who keeps confirming after long absences is credited at most once per interval.
4. **Timeout records nothing.** If the popup times out after its 60-second window, no `source='confirm'` event is written and the idle gap stays uncredited; the tracker enters idle mode until real input returns.
5. **Attribution without inflation.** `event_contributions_with_credits()` builds the regular trailing-grace spans first (they are disjoint by construction) and then allocates each confirmation credit only to the seconds that are not attributed yet, adding them to the application and project of the confirming event. `calculate_app_usage()` / `calculate_project_usage()` therefore reconcile with the day's worked time instead of double counting the first grace period: a 10-minute gap after 3 minutes of `chrome.exe` gives Chrome 180 s and the confirming app 420 s. Credits are stored on the event row itself via `source='confirm'`, which survives database re-opens and is what reports use to derive them.
6. **Tests** (`tests/test_presence_credit.py`, 11 cases): interval = 3 min caps the credit, interval = 10 min credits the full gap, popup wait excluded, same-second click input, chained confirmations, timeout, meeting overlap, midnight rollover split across both days, app attribution, plus two end-to-end tracker tests through the real popup callbacks.

---

## 14. Retention Without Losing Long-Term Reports (Part A2)

### Observation
Purging raw `input_events` after a fixed 90 days made any older date unreadable in reports, the dashboard and Excel exports. The retention window was also hardcoded.

### Decision
1. **New `daily_summary` table** `(date PRIMARY KEY, worked_sec, overtime_sec, meeting_sec, break_sec, per_app JSON, target_sec)`, created in `SCHEMA` and created by the migration path for existing databases. A documented extension column `project_secs` (JSON) stores per-project seconds so project breakdowns also survive purging.
2. **Aggregate, then delete.** `retention.purge_old_data()` computes each expired day with the normal calculators (`calculate_day_metrics`, `calculate_app_usage`, `calculate_project_usage`) *before* deleting its events, and writes an immutable summary. Summaries are never recomputed once present, so a later purge cannot drift the numbers. The purge cutoff is a day boundary (`today - retention_days` at 00:00) so only complete days are aggregated. Meeting/break/overtime sessions are kept — they are tiny and are still needed for the Sessions Log.
3. **Summary-aware reads.** `retention.get_day_metrics()` / `get_day_app_usage()` transparently fall back to the summary when a day has no raw events, and `metrics_from_summary()` / `app_usage_from_summary()` rebuild the same `DayMetrics` / `AppUsageItem` objects. Reports, the Excel exporter, the dashboard (7/30-day and the new Month/Year ranges) and period comparisons all go through these helpers, so no caller needed a second code path.
4. **Configurable retention.** `retention_days` (default 90) and `enable_retention_cleanup` are editable in Settings; `0` means keep everything forever and disables the purge. The weekly cleanup job and the startup cleanup both honour it.
5. **Migration.** Existing databases get the table through `_init_db()`; no data is rewritten and no user action is required. Purged days still appear in `get_recorded_dates()` because it unions the summary table.
6. **Tests** (`tests/test_retention.py`): totals (worked/overtime/meeting/break/target/per-app) are byte-identical before and after a purge, raw events are gone while the meeting session survives, a second purge is a no-op, `retention_days=0` keeps rows, `period_totals` and the Excel Daily Summary/App Usage sheets still show the purged day.

---

## 15. Bundled Egyptian Official Holidays (Part B1)

### Decision
1. **Data file, not code.** `assets/egypt_holidays.json` holds the fixed-date holidays (`01-07`, `01-25`, `04-25`, `05-01`, `06-30`, `07-23`, `10-06`) once as `month_day` entries, plus a per-year list of movable dates (Islamic holidays, Coptic Easter, Sham El-Nessim) for 2025–2027. Fixed dates recur for any year; movable dates only exist for years present in the file, so the file can be replaced when a new year is announced.
2. **No network.** The import reads only the bundled file (or an explicit path passed to `get_egyptian_holidays()`), validates each date with `datetime.strptime`, and never contacts a server.
3. **Settings import.** A `Import Egyptian holidays` button with a year spinbox merges the selected year's dates into `config.holidays`, de-duplicates, keeps user-added dates, and re-renders the list so the user can still add/remove entries manually. `merge_holidays()` and `count_new_holidays()` implement this and ignore malformed values.
4. **Tests** (`tests/test_holidays.py`): file parses, fixed + movable dates per year, fixed dates recur for years without a movable block, supported-year listing, merge without duplicates, invalid entries dropped, custom data file.

---

## 16. CSV Export (Part B2)

### Decision
1. **Four separate files.** `reporter.export_csv_bundle()` writes `TimeTracker_DailySummary_<start>_to_<end>.csv`, `TimeTracker_Sessions_<start>_to_<end>.csv`, `TimeTracker_AppUsage_<start>_to_<end>.csv` and `TimeTracker_Projects_<start>_to_<end>.csv` into `Documents\TimeTracker\Reports`.
2. **Excel-safe encoding.** Every file is written with `encoding="utf-8-sig"`, i.e. a UTF-8 BOM, so Excel opens Arabic names and notes as Arabic instead of mojibake. Line endings use the `csv` module defaults.
3. **Range picker.** Both the tray submenu (`Export CSV`) and the Settings window offer *This week*, *This month* and *Custom range*; the custom range reuses the existing date-picker dialog. The shared writer is also used by the legacy single-file export so both stay consistent.
4. **Tests** (`tests/test_robustness.py`): all four files exist, each starts with `EF BB BF`, strict UTF-8 decoding succeeds, Arabic project/app text round-trips, and Excel export keeps Arabic project names.

---

## 17. Project / Task Tags (Part B3)

### Decision
1. **Schema.** New `projects(id, name UNIQUE COLLATE NOCASE, archived, created_at)` table, plus `project_id` columns on `input_events` and `sessions` (added by guarded `ALTER TABLE` so old databases upgrade in place). `project_id` is nullable — untagged work stays valid.
2. **Current project.** The tray menu lists active projects as radio items with a `Current project: X` status line; the selection is stored in the `settings` table (`current_project_id`) so it survives restarts. New input events and new sessions (meeting/overtime/break/manual) are tagged with the current project at creation time. Archiving the current project clears the selection instead of silently keeping a dead id.
3. **Management.** The Settings window has a Projects tab to add, rename and archive/unarchive projects; names are validated (non-empty, unique case-insensitively) and duplicates raise a `ValueError` that the UI reports.
4. **Reporting.** Excel gains a `Projects` sheet, the CSV bundle gains a dedicated projects file, and the dashboard gains a project breakdown card. `retention.project_breakdown()` derives project seconds from event contributions (untagged -> `Unassigned`) and scales the day's rows by `worked_seconds / sum(project_seconds)` so the card always reconciles with the Daily Summary total. That scaling is a documented approximation: manual/confirmed credits that are not tied to an event are distributed proportionally.
5. **Tests** (`tests/test_projects.py`): CRUD + duplicate/empty validation, archive behaviour, event/session tagging, `calculate_project_usage`, breakdown reconciles with worked seconds, unassigned bucket, Excel sheet, CSV file, tracker persistence and session tagging.

---

## 18. Weekly / Monthly Comparison (Part B4)

### Decision
1. `retention.period_totals()` returns `PeriodTotals` (worked, overtime, meeting, break, target, per-app seconds) for a date range, using raw events where available and `daily_summary` rows for purged days, batching the range into a handful of queries.
2. `build_comparison()` computes the current and previous period for `week`, `month` and `year`, plus `percent_delta()` (guard: previous = 0 -> `null`, not infinity). Weeks are Monday-based; months and years are calendar boundaries.
3. The dashboard renders a comparison card with the delta for worked, overtime, meeting hours and the top applications, in both languages, and it re-renders when the range buttons change.
4. Batching was required for responsiveness: the first implementation issued per-day queries and made dashboard tests take ~7 s each; the batched version runs in ~0.6 s per test.

---

## 19. Local (Offline) Auto-Update (Part B5)

### Decision
1. **Setting.** `update_source_path` accepts a local folder or UNC share; empty or whitespace disables the feature entirely. No public internet endpoint is ever contacted.
2. **Manifest.** `version.json` in that folder describes `{version, installer, sha256, notes}`. `updater.check_for_update()` validates the JSON, requires an installer filename (basename only, so a manifest cannot point outside the share), resolves it relative to the share, and refuses anything with a missing or mismatched SHA-256.
3. **Cadence.** Checked at startup and once per day (`last_update_check_date` in the `settings` table). If a newer version is found, the tray shows a dialog with the version, and only on explicit user confirmation does `launch_installer()` verify the hash *again* and start the installer with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`. The existing installer closes the running instance and preserves `data.db` and `config.json`.
4. **Failure behaviour.** Any missing file, bad JSON, unreadable share, hash mismatch or spawn error is logged and treated as "no update"; the app keeps running the current version.
5. **Tests** (`tests/test_updater.py`): version parsing/ordering, newer detection, same/older ignored, disabled path, missing/invalid manifest, checksum mismatch, missing sha, missing installer, silent launch arguments, and refusal to launch a tampered installer.

---

## 20. Optional Code Signing (Part B6)

### Decision
1. Signing is opt-in through environment variables read by `build_installer.bat`:
   - `SIGN_CERT_PATH` - path to the `.pfx`/`.p12` certificate,
   - `SIGN_CERT_PASSWORD` - its password.
   Neither value is stored in the repository.
2. When both are set, the script locates `signtool.exe` (PATH or the Windows SDK), signs `dist\TimeTracker.exe` with SHA-256, and passes `/DSIGNING_ENABLED /Sbycert="..."` to Inno Setup so the setup executable and the uninstaller are signed too. `installer.iss` guards `SignTool=bycert` and `SignedUninstaller=yes` with `#ifdef SIGNING_ENABLED` inside `[Setup]`.
3. When either variable is missing, or `signtool.exe` cannot be found, the step prints a warning and the build continues unsigned. A signing *failure* (variables set but signing returns non-zero) aborts the build instead of shipping a half-signed release.
4. The build also writes `dist\version.json` with the current `APP_VERSION` and the installer's SHA-256, which is exactly what the local update source needs (Section 19). The same `APP_VERSION` is read from `config.py` and passed to Inno Setup as `/DMyAppVersion=...`, so the installer metadata can never drift from the app version (the literal in `installer.iss` is only a manual-compile fallback).

---

## 21. Presence-Check Popup Hardening (Part C1)

### Decision
1. **Always on top without stealing focus.** The popup is created as a `tk.Toplevel` with `-topmost` and, after it is mapped, `WS_EX_NOACTIVATE` is applied through `SetWindowLongPtrW`/`SetWindowLongW`, so the window never receives activation and keystrokes keep going to the user's application. The previous unconditional `focus_force()` was removed.
2. **Topmost re-assertion.** While visible, `SetWindowPos(HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)` is re-applied every 500 ms, which keeps the popup above fullscreen windows (Teams/Zoom/PowerPoint) that raise their own topmost window.
3. **Cursor monitor.** `get_cursor_monitor_workarea()` uses `MonitorFromPoint(..., MONITOR_DEFAULTTONEAREST)` + `GetMonitorInfoW` and centres the window on the work area of the monitor containing the cursor, with a fallback to the primary screen on failure.
4. **Sleep/wake, lock/unlock, display change.** The tracker's clock-jump guard (> 5 min wall-clock delta) closes auto-overtime at the last real input, resets the continuous-active counter and the presence-check cycle, so a wake-up can never credit the suspended period. A lock is treated as a 30-minute idle. Coordinates are recomputed for every popup, so resolution or monitor changes are picked up automatically.
5. **Timeout.** The dismiss deadline is measured with `time.monotonic()`, so a wall-clock change cannot extend or cut the 60-second window.

---

## 22. Verification, Tests and Preserved Behaviour (Part C2 / C3)

### Decision
- New test modules: `tests/test_presence_credit.py`, `tests/test_retention.py`, `tests/test_holidays.py`, `tests/test_projects.py`, `tests/test_updater.py`, `tests/test_robustness.py`.
- `tests/test_robustness.py` covers day change with an open overtime session, sleep/wake clock jump closure at the last input, a meeting overlapping a manual break (meeting counted once, break still reported), orphan-session cleanup through `ActivityTracker.start()`, an upgrade from the *previous* schema (`input_events` without `app_name`/`source`/`project_id`, `sessions` with only the old CHECK constraint, no `projects`/`daily_summary`, with existing rows and settings) where all rows are preserved and the new columns are usable, plus CSV encoding.
- Full suite: `python -m pytest tests -q` -> **202 passed** in v1.3.1 (166 before this change + 36 new in `tests/test_audit_fixes.py`). The pre-existing tests for the grace model, overtime state machine, meetings, pause, half-day, `sqlite3.backup()` and the singleton mutex are unchanged and still green.

---

## 23. Product Decisions D1-D6 (v1.3.1)

### Decision
These six decisions are authoritative for the calculator, `retention`/`daily_summary`, the tray, the Excel and CSV exports and the dashboard.

1. **D1 - Meeting is a subset of Worked/Overtime.** The merge logic is unchanged; meeting time is never added on top of Worked, and only Worked + Overtime are additive. Made explicit in the Excel header `Meeting Hours (included in Worked)`, an explanatory note row in the Daily Summary sheet (column A stays empty so `aggregate_reports.py` skips it), the CSV headers, the dashboard KPI and comparison labels and this document.
2. **D2 - Break = manual break/pause sessions UNION unworked time inside the work window**, computed as disjoint from Worked, Overtime and Meeting (`calculator.calculate_day_metrics`). The derived idle part is restricted to the presence span clipped to the work window, so an early departure or a late start never inflates Break; idle time outside the window (evenings, weekends) and overtime are never Break.
3. **D3 - Activity on a non-work day or holiday is Overtime only**, through the existing overtime flow. The old `worked_intervals.extend(meetings_not_in_ot)` line was removed: a weekend meeting without an accepted overtime session is reported as Meeting but booked as neither Worked nor Overtime.
4. **D4 - No night shifts.** `config.is_valid_config()` rejects `work_end <= work_start`, the Settings dialog shows an inline error and refuses to save, and `load_config()` falls back to defaults with a log warning.
5. **D5 - Presence-check credit is capped at `min(active_check_interval_min, 5 minutes)`**, still anchored at the last stored presence, never chained (each confirmation credits only its own gap) and never crediting popup wait time (`calculator.MAX_CONFIRM_CREDIT_SECONDS = 300`).
6. **D6 - Tamper-evidence is out of scope.** Exports stay in Documents; a one-time notification appears when Documents is redirected into OneDrive/Dropbox (marker stored as the `documents_sync_warning_shown` setting), and `Export CSV -> Export to folder...` writes the bundle to a folder the user picks.

---

## 24. Phase 1 - P0 Fixes (v1.3.1)

### Decision
1. **P0-1 Break accounting** implemented as D2; `manual_break_seconds` and `idle_break_seconds` were replaced by one disjoint `break_seconds` in `calculator.py`.
2. **P0-2 Window validation** in `is_valid_config`, `load_config` and the Settings dialog (message key `err_work_end_before_start`).
3. **P0-3 Single instance** uses only `Local\TimeTrackerSingleton`, so every Windows user / RDP session runs an instance; the message now says *"already running in the system tray for this Windows user"*.
4. **P0-4 Half-day** target is `daily_target_hours / 2` through `calculator.half_day_target_hours()`, used by `retention`, `tracker` and the tray label/toast, which show the real number.
5. **P0-5 i18n** - the missing keys were eliminated by reusing existing ones (`sec_work_hours`, `lbl_work_start`, `lbl_work_end`, `lbl_daily_target`, `lbl_weekly_target`, `sec_work_days`, `custom_export_btn`, `custom_export_cancel`, `custom_export_err_date`). `tests/test_audit_fixes.py` scans every literal `t("...")` call in the package and asserts the key exists in both EN and AR, and that both dictionaries have identical key sets.
6. **P0-6 Dialog flag** - releasing the flag is a module-level `_release_dialog_flag()` used both by the dialog cleanup and by the `dispatch_to_ui` failure handler, and `_dialog_lock` is an `RLock` because claiming the flag and refreshing the heartbeat both take it (a plain `Lock` deadlocked).
7. **P0-7 Update path** - `is_plain_filename()` plus `resolve_installer_path()` reject separators, `..`, drives and UNC prefixes, assert that the resolved parent equals the resolved source folder, keep the SHA-256 check, and report `update_rejected` to the user.

---

## 25. Phase 2 - P1 Fixes (v1.3.1)

### Decision
1. **P1-1** Meeting wording per D1 (headers, note row, CSV, dashboard) with no numeric change.
2. **P1-2** Non-work days per D3, with Saturday/holiday tests.
3. **P1-3 Performance** - day metrics are cached for 30 s and week/month totals for 60 s (`tracker._refresh_metrics`, invalidated with `force=True` after mutations, and `on_state_change` only fires when something changed); `retention.period_totals()` buckets the range's events by day once instead of rescanning the whole range per day; `calculator.event_contributions_with_credits()` credits its disjoint regular spans in one pass; `PRAGMA journal_mode=WAL` is set once per database instead of per statement.
4. **P1-4 Dashboard** - the embedded JSON escapes `</` as `<\/`, user text is rendered through a JS `esc()` helper (notes, reasons, app names), and only the last 14 days (plus the selected day) are compiled in full while older days come from `daily_summary` (`_build_single_day_data(..., summary_only=True)`).
5. **P1-5 Migrations** - `schema_version` table (`SCHEMA_VERSION = 3`), one `BEGIN IMMEDIATE ... COMMIT` block, `DROP TABLE IF EXISTS sessions_new` before a rebuild, idempotent and resumable after a crash.
6. **P1-6 Consent** - `consent_event.wait(timeout=30)`; when the UI thread never answers, the app logs, shows a `MessageBoxW` and exits cleanly instead of hanging invisibly.
7. **P1-7 Win32** - `argtypes`/`restype` on the functions used, handles closed in `finally`, return values checked, and `GetLastInputInfo` failure or `dwTime == 0` treated as "unknown" (the tick's input branch is skipped, never "idle since boot").
8. **P1-8 Pause** - new `sessions.is_pause` column, `start_session(is_pause=True)`, a pause is refused while a real break is open, `is_paused()` is a pure check and `_expire_pause()` closes only the pause session.
9. **P1-9** Every tracker notification goes through i18n keys in EN and AR.
10. **P1-10** `close_all_open_sessions(end_ts, auto_end_ts=last_input)` closes `source='auto'` sessions at the last real input on quit and at day change; tray/manual sessions still end at quit time.
11. **P1-11** `backup_keep_count` (default 8, clamped 1-52) prunes old backups; the Documents/OneDrive warning and the *Export to...* folder implement D6.

---

## 26. Phase 3 - Small Items (v1.3.1)

### Decision
1. **B7** `last_update_check_date` is written only after a check that reached the source; an unreachable folder is logged and retried on the next tick.
2. **B9/B18** The end-of-day summary fires only inside `[work_end, work_end + 30 min]`.
3. **B12** A finished day keeps the trailing grace of its last event, bounded by the time left in the day (`calculate_event_contributions(..., last_event_cap=...)`), so the strict value matches the live value instead of losing up to 180 s.
4. **B14** Events are stamped `now - idle_ms/1000` and forced to stay strictly increasing and unique per second.
5. **B16** Past-meeting dates before 1970-01-02 or in the future are rejected in the dialog; `get_day_boundaries()` falls back to plain day arithmetic on `OSError`.
6. **B17** Excel and CSV share one row policy: a day is listed when it has data or is a work day.
7. **B19** `focus_force()` was removed from the overtime, note, past-meeting and settings dialogs (`lift()` is kept); the presence popup keeps its `WS_EX_NOACTIVATE` behaviour.
8. **B21** The private pystray menu hook records `_menu_hook_active` and falls back to plain menu rebuilds when it cannot be installed.
9. **Wake from sleep** resets the presence-check cycle as well as the continuous-active counter, so no popup appears immediately after a wake-up.
10. **Autostart** follows the registry: a config/registry mismatch is resolved in favour of the registry, so unchecking "start with Windows" is respected.
11. **Tray** - the seven read-only rows live in one *Summary* submenu and the duplicated "Current project" label became `Projects: <name>`.
12. **Docs** - README, DECISIONS and the checklist were corrected (5th Excel sheet, column names, `--dashboard` flag, measured numbers, test count).

---

## 27. Verification & Measurements (v1.3.1)

### Decision
- Full suite: `python -m pytest tests -q` -> **202 passed** (166 before this change + 36 new in `tests/test_audit_fixes.py`).
- Measured on the development machine with 90 days of data at a 30 s cadence (61 440 events):
  - dashboard build **7.73 s -> 1.35 s**, HTML 0.7 MiB -> 0.15 MiB (only the 14-day working set is embedded);
  - day metrics refresh with 60 000 stored events ~8 ms, well under the 50 ms per-tick budget asserted by the benchmark test;
  - `event_contributions_with_credits()` no longer performs per-event interval subtraction, which removed the dominant cost of both the tick and the dashboard build.
- Every item above has a test in `tests/test_audit_fixes.py` (D1-D6, P0-1..P0-7, P1-2, P1-4..P1-11, B7, B12, B14, B16, B17) or an updated test in the pre-existing modules (break accounting, presence cap, half-day, report headers, row policy, pause, quit time).

## 28. v1.3.2 — Automatic aggregation, D3 meeting/overtime, migration backup

1. **Automatic aggregation** — `retention.aggregate_missing_days(db, config, today)` walks the
   days that still have raw data (events or sessions) and aggregates every completed day
   (strictly before `today`) whose `daily_summary` row is missing or older than that day's
   newest data. It runs in a background thread at startup (`main.py`, `StartupAggregation`),
   on the day-change branch of the tracker loop, and at the start of every dashboard/export
   build (`visualizer.build_dashboard_data`, `reporter._aggregate_before_build`). Re-running it
   is a no-op, and today is never aggregated so the live day keeps being computed from raw data.
2. **Raw data wins while it exists** — `get_day_metrics`, `get_day_app_usage` and
   `project_usage_dict` use `daily_summary` only when the day has no raw input events left
   (`retention.day_has_raw_data`). Sessions are *not* used for that test because the purge keeps
   meeting/break/overtime sessions forever. This keeps first/last activity, half-day targets and
   live values exact and still lets purged days read from the summary.
3. **Purge is aggregate-first** — `purge_old_data` aggregates before deleting and stops at the
   earliest day before the cutoff that still has no summary row (`blocked_by`, logged), so raw
   events for an un-aggregated day are never deleted.
4. **D3 (meeting outside paid hours)** — starting a meeting when `_outside_paid_hours()` is true
   now shows the overtime prompt (reason, configured timeout). Accepted: an overtime session
   covering the meeting is opened. Declined: a tray notification states the meeting is recorded
   as Meeting only. `add_past_meeting()` gained `count_as_overtime` (dialog checkbox, default
   checked) and `reason`; when checked a matching overtime session is created.
5. **Unclassified meeting time** — the calculator no longer books an out-of-hours meeting as
   Worked. Minutes covered by Worked or Overtime stay in `meeting_sec`; the remainder is reported
   as `DayMetrics.unclassified_meeting_seconds` and stored in the new
   `daily_summary.unclassified_meeting_sec` column (SCHEMA_VERSION 3 -> 4). Excel and CSV gained a
   **last** column "Unclassified Meeting Hours" (appended, so existing column indexes — including
   `aggregate_reports.py` reading row[4]..row[9] — are unchanged).
6. **Backup before migration** — `Database.__init__` copies an existing older database with the
   `sqlite3` online backup API to `Backups\pre_migration_v<old>_to_v<new>_<timestamp>.db` before
   `_init_db` runs. A failed backup raises `RuntimeError`, the migration is not attempted, and
   `main.py` shows an error dialog and exits instead of starting on a half-upgraded file.
   Pre-migration copies are excluded from `backup_keep_count` and pruned after 30 days
   (`prune_pre_migration_backups`, called from the backup routine).
7. **Overtime prompt timeout** — new config key `overtime_prompt_timeout_seconds` (default 90,
   clamped to 30-600 in `load_config`). All three overtime prompts pass it; an unanswered prompt
   resolves to "No" and logs the auto-decline, without taking focus.
8. **Live comparison** — `period_totals(..., is_live=True)` and `build_comparison(..., is_live=True)`
   compute today from live data, and the dashboard passes `is_live=True`, so dashboard, tray and
   Excel agree on the current day/week/month.
9. **DPI** — `dialogs.setup_dpi_awareness()` calls `SetProcessDpiAwareness(2)` (falling back to
   `SetProcessDPIAware`) before any Tk window exists, from `main.py`.
10. **Tests** — the suite grew from 202 to 217 passing tests (`tests/test_v132.py` adds 15:
    aggregation for 60 days, idempotency, today excluded, stale refresh, purge guard, dashboard
    day not empty, weekend meeting prompt/accept/decline, work-day no-prompt, past-meeting
    checkbox on/off, migration backup written, failed backup aborts, live comparison).

## 29. v1.3.2 — Arabic RTL, DPI-driven sizing, measurements and the build guard

1. **One RTL helper, applied to the built widget tree.** `dialogs.rtl_layout(lang)` defines the
   flags (`rtl`, `anchor`, `justify`, `side`, direction mark) and `dialogs.apply_rtl(window, lang)`
   walks the window after it is built: labels/buttons/checkbuttons get `anchor="e"` and
   `justify="right"`, pack sides are flipped (`flip_side`), grid columns are mirrored inside their
   parent (`mirror_column`) and the horizontal part of `sticky` is swapped (`flip_sticky`). Every
   dialog calls it (Settings, Pause, Past Meeting, Meeting Note, About, Overtime, Consent, the
   presence popup, update dialog, date-range picker), so no dialog needs its own mirroring code.
2. **Mirroring is reversible, so the language can switch live.** `_remember_layout()` stores each
   widget's original pack/grid options, anchor, justify and text the first time it is seen.
   `apply_rtl(window, "en")` restores them; the Settings language radios call `_switch_language()`,
   which re-applies the helper to the open window and re-translates the title. Nothing is rebuilt,
   so unsaved edits survive the switch.
3. **Mixed-direction strings.** `bidi_text(text, lang)` strips any existing marks and, for Arabic,
   wraps every Latin word or number run in U+200E (LRM). Numbers (`8:30`), exe names (`chrome.exe`)
   and units therefore keep their internal order inside an RTL sentence. The function is
   idempotent, which matters because it runs again on every live language switch.
4. **Fonts.** `ui_font_family(lang)` returns the first available family from
   `("Segoe UI", "Tahoma", "Arial")`, falling back to `Segoe UI` when Tk reports no families
   (headless tests). Segoe UI has full Arabic coverage on Windows.
5. **Single-language headers.** The bilingual strings "Language / اللغة" and "Update / التحديث"
   were replaced by `sec_language` / `sec_update`, and `settings_lang_label` (previously
   "Interface Language / لغة الواجهة:") now translates instead of mixing both languages.
6. **DPI sizing.** `compute_dialog_geometry(base_w, base_h, scale, screen_w, screen_h)` is a pure
   function: it multiplies by the DPI scale (`frame_scale()` = `winfo_fpixels("1i") / 96`, at least
   1.0) and caps the result at 90% of the screen. `fit_dialog()` applies it and centres the window
   on the monitor under the cursor. Every dialog replaced its fixed `width, height = ...` /
   `geometry()` pair with one `fit_dialog()` call, so 100/125/150/175% scaling all stay on screen.
7. **Scrollable Settings.** `make_scrollable(parent)` returns `(inner, outer)`: a canvas + vertical
   scrollbar with mouse-wheel binding (bound on `<Enter>`, released on `<Leave>` so it cannot steal
   the wheel from other windows). All four Settings tabs are built inside one, and the outer frame
   is what the notebook shows, so existing widget-building code was untouched.
8. **Tests.** `tests/test_layout.py` (20 tests) covers `is_rtl`, the flags, side/sticky/column
   flipping, `bidi_text` (English passthrough, Arabic number and exe wrapping, idempotence), the
   geometry at 1.0/1.25/1.5/1.75 with the 90% cap, the minimum size, that every dialog file uses
   `apply_rtl`/`fit_dialog`/`make_scrollable`, and that no header mixes languages and EN/AR key
   sets stay symmetric.
9. **Measurements.** `scripts/benchmark.py` reports tick CPU time (average/max of 40 samples),
   dashboard build time (first build with the aggregation pass, plus a repeat build), HTML size,
   startup to first tick and RSS after 60 simulated ticks (psutil, otherwise "not measured"). The
   v1.3.2 numbers are in README.md and docs/test_checklist.md; the earlier unverified figures were
   removed rather than carried forward.
10. **Build.** `build_installer.bat` now states explicitly that nothing was built and returns exit
    code 2 when Inno Setup 6 is missing, before it touches `dist/`. Inno Setup 6 turned out to be
    installed here (under `%LOCALAPPDATA%\Programs\Inno Setup 6`), so the build ran end to end:
    PyInstaller 6.22.3 produced `dist\TimeTracker.exe` (19.5 MiB), Inno Setup produced
    `dist\TimeTracker_Setup.exe` (21.1 MiB), and the generated `dist\version.json` reports `1.3.2`
    with the SHA-256 of that installer (recomputed later and matched). Code signing was skipped
    because `SIGN_CERT_PATH`/`SIGN_CERT_PASSWORD` are unset: the run printed `Signed: no` and
    continued. The version always comes from `config.APP_VERSION` (`/DMyAppVersion=` for
    `installer.iss`, imported directly by the version.json step), so the 1.3.2 literal in
    `installer.iss` is only the manual-compile fallback. A temporary `dist/README_STALE.txt` was
    written while the build was thought impossible and deleted once the real build succeeded.

## 30. v1.3.5 - Logging, crash capture and support bundles

The 1.3.4 startup crash was invisible: the windowed build has no console, `main()` logged the
traceback and exited, and the only way to find out was to know that
`%LOCALAPPDATA%\TimeTracker\timetracker.log` existed. Logging is now a first-class subsystem in
`logging_setup.py` and every failure path feeds it.

1. **Files and rotation.** `timetracker.log` (INFO+, 5 MB x 5), `timetracker-errors.log`
   (WARNING+ only, 2 MB x 3) and `timetracker-faults.log` (faulthandler stacks, rotated at 1 MB).
   All UTF-8, all line-flushed, all in `%LOCALAPPDATA%\TimeTracker`.
2. **Format.** `YYYY-MM-DD HH:MM:SS.mmm LEVEL [thread] [session] module:line - message`. The
   session id (8 hex chars) separates runs inside the same file, and millisecond timestamps make
   a 5-second tick loop readable.
3. **Banner.** One block per start: version, session, PID, executable, frozen flag, Python,
   Windows build, locale, elevation, data dir, log file and effective level. A support report is
   self-contained from line 1.
4. **Crash capture.** `sys.excepthook` (fatal, shows a dialog that offers to open the log folder),
   `threading.excepthook` (a worker thread that dies is logged as CRITICAL instead of vanishing),
   and `faulthandler.enable(file=...)` for native faults (access violation, stack overflow) where
   the Python-level hooks never run. `handle_fatal()` replaces the old bare `logging.critical` +
   `sys.exit(1)` pair in `main()`.
5. **Console handling.** A stderr handler is added only when `sys.stderr is not None` and the app
   is not frozen; the previous code attached a handler to `None` in every windowed run, which
   raised internally on each record.
6. **Noise floor.** `PIL`, `urllib3`, `matplotlib`, `asyncio` and `http.client` are pinned to
   WARNING even at DEBUG, so `--verbose` shows TimeTracker's own DEBUG lines instead of ~50 PIL
   plugin imports.
7. **Level control.** `log_level` in `config.json` (validated by `normalize_log_level`, any case
   or numeric form, never raises) is applied at startup, re-applied when Settings is saved, and
   can be overridden per run with `--verbose`. The Settings dialog now carries `log_level`,
   `enable_dashboard_server`, `enable_smart_meeting_detection` and
   `smart_meeting_cooldown_minutes` over from the previous config - before this change saving
   Settings silently reset those keys to their defaults.
8. **User-facing tools.** Tray menu: *Open Log Folder* and *Create Diagnostics Bundle...* (zip with
   `system.txt`, `session.json`, every log + rotation, and `config.json`, written to
   `Documents\TimeTracker\Diagnostics`). CLI: `TimeTracker.exe --diagnostics` produces the same
   bundle without starting the tray, which is the only workable path when the tray itself is what
   is broken.
9. **Tests.** `tests/test_logging.py` (17 tests): UTF-8 payload, line metadata, error-log
   filtering, rotation, level changes, idempotent setup, UTF-8 rotating handler, thread-crash
   capture, fatal handler output, `describe_exception`, bundle contents/fallback, noise floor and
   `log_level` round-trip/validation. Full suite: **274 passed**.

## 31. Command Bus — one writer for tracker state

### Observation
Tracker state was mutated from several threads at once: the tracker loop thread ran `_tick()`,
the pystray thread called the mutators directly, the dedicated Tk UI thread ran dialog callbacks
that wrote `sessions`/state, and a background thread aggregated at midnight. The only guard was an
`RLock` plus `@_synchronized` on nine methods — a shared lock, not a single owner. Locking could
also deadlock on re-entrancy, and cache reads were cross-thread. (`record_active_confirmation`
carried the decorator twice.)

### Decision
1. **`command_bus.py`** is a stdlib-only single-writer primitive: one `queue.Queue`, one consumer
   thread, and `concurrent.futures.Future` for results and exceptions. The consumer also runs the
   periodic tick itself (`queue.get(timeout=...)`, first tick immediate, interval read live from
   `config.tick_interval_seconds`), so there is no separate `TrackerLoopThread` loop any more.
2. **`@_command` replaces `@_synchronized`.** Public methods keep their exact signatures and stay
   synchronous: they submit the handler and block on `Future.result()`. The bus recognises its own
   thread, so a handler calling another handler runs inline (no self-deadlock). When the bus was
   never started — the normal case in unit tests — calls also run inline, so no existing caller or
   test needed changing.
3. **Dialog callbacks are commands too.** Overtime answers (both prompts and the meeting variant),
   the presence-check confirm/timeout outcome and the meeting-end note now go through small
   `@_command` handlers, so nothing mutates tracker state from the Tk thread. Settings saves use
   `set_config()` instead of assigning `tracker.config` from the tray thread.
4. **Shutdown is ordered.** `stop()` queues a finaliser and a sentinel on the same FIFO, closes all
   open sessions on the owner thread after pending commands, then joins the worker. The
   `TrackerLoopThread` is now the bus worker.
5. **Scope.** Only tracker state moved to the bus. Tk/pystray keep their own threads (platform
   requirement), and export/backup/aggregation workers stay external — they use the database's own
   write lock and never touch tracker state. Read-only properties remain plain attribute reads; a
   read can lag one tick behind the owner, which is acceptable for tray labels.
6. **Tests.** `tests/test_command_bus.py` (8 tests) covers one worker thread for commands and ticks,
   result/exception propagation, inline fallback, re-entrancy, immediate+periodic ticking,
   drain-before-final shutdown, and the tracker integration (synchronous return value, mutation on
   the owner, finaliser on the owner). Full suite: **289 passed**.

## 32. Accessibility & RTL polish for Settings / consent

### Observation
BUG-06's original hijack (`canvas.bind_all` / `unbind_all`) had already been replaced by a single
`bind_all(..., add="+")` dispatcher that walks up to the nearest registered scroll canvas and
leaves native-scrolling widgets alone. The audit confirmed the global *hijack* is closed, but found
three real defects that still broke RTL and heavy layouts:
1. `_sync()` re-packed a hidden scrollbar with a hard-coded `side="right"`; after `apply_rtl`
   mirrored the panel the scrollbar jumped back to the wrong side as soon as content overflowed.
2. Tab focus could land on a widget below the visible area of a scroll panel with no auto-scroll.
3. The consent dialog used a fixed `580x540`, non-resizable window with a plain content frame, so
   longer Arabic copy was clipped, and it had no keyboard default or Escape-to-decline.

### Decision
1. **RTL-aware scrollbar side.** `make_scrollable` caches the side on the canvas attribute
   `_tt_scrollbar_side`; `apply_rtl` mirrors that attribute along with the pack side, and `_sync`
   re-packs from the cached value instead of a literal. The initial LTR pack stays as-is.
2. **Keyboard focus-follow.** The pure helper `_scroll_fraction_for_focus()` computes the yview
   fraction (clamped to the last full page) and `make_scrollable` binds `<FocusIn>` on both the
   inner frame and the outer wrapper so a Tab-focused widget is scrolled into view.
3. **Consent a11y.** The privacy copy now lives in a `make_scrollable` wrapper; `agree_btn.focus_set()`
   gives Enter a default action, `<Escape>` declines (same as `WM_DELETE_WINDOW`), and both terminal
   callbacks early-return on `answered` so a button command plus a window binding can never answer
   twice. `apply_rtl` still runs after the full tree is built.
4. **Tests.** `tests/test_dialog_accessibility.py` (14 tests): pure focus-scroll math, RTL side
   caching, FocusIn wiring, non-hijacking wheel dispatcher, and consent keyboard/scroll structure.
   Full suite: **303 passed**.

## 33. Security & correctness remediation pass (CODE_REVIEW_REPORT follow-up)

### Decision
1. **SEC-01** — the hardcoded GitHub PAT was removed from `scripts/upload_release.py`; the script
   now exits with code 2 unless `GITHUB_TOKEN` or `--token` is supplied. (The token itself must be
   revoked in GitHub — that step is external to the codebase.)
2. **SEC-02/SEC-03** — `updater.py` is HTTPS-only (`is_url`, `normalize_update_source`,
   `download_url`, redirects via `resp.geturl()`), enforces a 200 MiB cap, anchors version parsing
   with `$` (`is_valid_version`/`safe_version_slug`), and asserts the download destination is under
   `%TEMP%` (also in `dialogs.py` with a uuid suffix). An opt-in `required_update_publisher` config
   key enables an Authenticode/publisher check on `launch_installer`; unsigned builds keep the
   historical hash-only trust.
3. **SEC-04/SEC-06** — every flagged `innerHTML` sink in `visualizer.py` now goes through `esc()`,
   the badge class is sanitised, and both generated HTML documents carry a CSP meta tag. The PDF
   report additionally escapes project/employee names with `html.escape`.
4. **SEC-07** — the wildcard CORS header was removed; the dashboard sends `Referrer-Policy`,
   validates the `Host` header, accepts the token from an `Authorization: Bearer` header for API
   calls, and no longer persists the live token in the on-disk snapshot.
5. **SEC-08** — `build_installer.bat` prefers `SIGN_CERT_THUMBPRINT` (`/sha1`, no password in argv)
   and warns when the deprecated `/p` path is used.
6. **BUG-02/05/07/14** — a stale-popup watchdog clears a never-resolving active-check dialog;
   `dispatch_to_ui` accepts a `fallback` used by the pickers/pause/past-meeting dialogs so a
   Tk-unavailable run always fires a terminal callback; the folder CSV export runs on its own
   worker; Excel/PDF writes are atomic (`tempfile` + `os.replace`).
7. **PERF-07, QUAL-03, QUAL-08, QUAL-09** — sorted timestamps are built once, a shared
   `bucket_records_by_day()` primitive replaces three copies, `aggregate_reports.py` gained 3 tests,
   and the README privacy claim was corrected.
8. **Follow-up round (same day):** SEC-07 residual was closed — the initial page load clears the
   token from browser history with `history.replaceState` and the server redacts `token=` in its
   access log. PERF-06 was closed with persisted, indexed `day`/`change_day` columns (backfilled by
   migration for legacy DBs, updated on session close), replacing the full `strftime` scans;
   `SCHEMA_VERSION` is now 6. SQLite rejects `strftime(...,'localtime')` expression indexes as
   non-deterministic, which is why the value is stored by the application instead.
9. **Still open:** PERF-02 (per-operation DB connections), QUAL-01 (~830-line settings builder),
   QUAL-02 (legacy bare `except: pass`) and the QUAL-03 report-row duplication. They are tracked in
   `../docs/REMAINING_WORK.md` at the workspace root, together with the external GitHub token revocation.
10. **Tests** — `test_security_hardening.py` (11), `test_aggregate_reports.py` (3) and
    `test_perf06_sec07.py` (4). Full suite: **321 passed**.

## 34. Dashboard timeline follows the worked window

### Observation
The timeline always rendered a fixed 00:00–24:00 ruler, so an 09:30–17:30 working day occupied
only about a third of the track and the active segments were too narrow to read.

### Decision
1. `_timeline_window()` computes a per-day visible span from the configured working window plus
   the actual first/last activity (so overtime remains visible), with a 30-minute margin and an
   08:00–18:00 fallback for empty non-work days.
2. Status and application block percentages are now computed against that span via
   `_pct_in_window()` (clamped to 0–100), not against 86400 seconds.
3. The ruler is generated in JS from `timeline_window` with an adaptive step (1/2/3 hours), the
   card title shows the exact range, and CSS uses absolute-positioned ticks.
4. **CSP follow-up:** the dashboard CSP sets `script-src-attr 'none'`, which blocks inline HTML
   handlers. All 15 `onclick`/`onchange` attributes were replaced by `data-*` hooks bound with
   `addEventListener` in `bindInlineHandlers()`; this also fixed the tab buttons that stopped
   switching views. `col.onclick = ...` property assignments remain allowed by CSP.
5. **Refresh / reload fix:** stripping the token with `history.replaceState` broke F5 and made the
   refresh button reload into a 401 page. The page now stores the token in `sessionStorage` **and** a
   Strict, loopback-only cookie; the loopback handler accepts the cookie for navigations, so reloads
   stay authenticated while the token is still absent from logs and the on-disk snapshot. The
   refresh button no longer reloads on failure — it shows an explicit "Session expired" badge.
6. **Tests** — `tests/test_timeline_window.py` (5); the old fixed-24h title assertion was updated.
   Full suite: **326 passed**.

## 35. RTL selection widgets and tray-menu bidi text

### Observation
Arabic screenshots showed checkboxes/radios with the indicator on the **left** of the label —
classic `tk.Checkbutton` / `tk.Radiobutton` always draw it there, so `apply_rtl`'s anchor/side
mirroring could not fix it. Mixed tray labels such as `لوحة البيانات المرئية (Dashboard)` could
also render Latin runs in the wrong visual order.

### Decision
1. `selection_layout(kind, rtl)` returns a ttk layout with the indicator packed on the right for
   Arabic (left for English). `configure_rtl_widget_styles(lang)` applies it to `RTL.TCheckbutton`
   and `RTL.TRadiobutton`, and is called from `apply_rtl` so a live language switch re-mirrors them.
2. `make_selection_widget()` is the single factory for check/radio buttons; all five Checkbutton
   and three Radiobutton construction sites in `dialogs.py` now use it (no classic tk selection
   widgets remain).
3. The tray menu wraps every label through `bidi_text()` via a local `mt()` helper (including
   project names and mixed `name: value` labels), keeping Latin tokens in order inside Arabic.
4. **Platform limit:** the submenu arrow side in the pystray/native Windows menu is OS-controlled
   and cannot be mirrored from Python; the text itself is right-aligned and correctly ordered.
5. **Tests** — `tests/test_rtl_widgets.py` (6, pure layout + source wiring). Full suite: **332 passed**.
