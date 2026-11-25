# TimeTracker — Simple Windows Tray App for Employee Work Hours

TimeTracker is a lightweight, local-only Windows system tray application designed to answer one question at the end of the day:
**"How many of my working hours did I work today?"**

The daily target and the working window are defaults, not constants: both are configurable in Settings (a default 8 h day, 09:30-17:30). Current version: **1.3.8** (source of truth: `config.py` → `APP_VERSION`; the artifacts in `dist/` match it).

---

## Project layout — read this first if you are an AI assistant

This is a Windows desktop project. Everything needed to understand, run, test and build it is in this README; the other Markdown files are historical or specialised notes (see *Documentation map* below).

```
Bussma_EA/                      <- workspace root
├── README.md                  <- workspace overview (Arabic + English quick reference)
├── docs/
│   └── REMAINING_WORK.md      <- single source of truth for open work
├── scripts/
│   ├── build_installer.bat    <- wrapper: cd ..\timetracker then build
│   └── upload_installer.bat   <- wrapper: cd ..\timetracker then upload
└── timetracker/               <- the actual application (this folder)
    ├── README.md              <- this file: complete technical reference
    ├── main.py ... *.py       <- source modules (must stay at this level, see note)
    ├── assets/                <- icons, official logo, egypt_holidays.json
    ├── docs/                  <- DECISIONS.md, USER_GUIDE.md, test_checklist.md
    ├── scripts/               <- set_version.py, smoke_test.py, benchmark.py,
    │                             seed_fake_data.py, token_store.py, upload_release.py
    ├── tests/                 <- 27 test files / 332 tests (pytest, fully offline)
    ├── dist/                  <- built artifacts: TimeTracker.exe,
    │                             TimeTracker_Setup.exe, version.json
    ├── build_installer.bat    <- real build script (PyInstaller + Inno Setup 6)
    ├── upload_installer.bat   <- release upload script
    ├── run_dev.bat            <- dev launcher (pip install + python main.py)
    ├── TimeTracker.spec       <- PyInstaller config (main.py, assets/, assets/app.ico)
    ├── installer.iss          <- Inno Setup config (dist/, assets/, output dist/)
    └── requirements.txt       <- pystray, Pillow, openpyxl, pyinstaller (pinned)
```

**Why the loose files stay at this level — do not "tidy" them into a subfolder:**
`TimeTracker.spec` names `main.py`, `assets/` and `assets/app.ico` relative to this folder; `installer.iss` resolves `dist\` and `assets\` relative to itself; `tray.py`/`egypt_holidays.py` resolve assets via `Path(__file__).parent / "assets"`; `autostart.py` resolves `main.py` via `Path(__file__).parent`; `reporter.py` resolves the logo from `Path(__file__).resolve().parent`; `scripts/*.py` set `ROOT = Path(__file__).resolve().parent.parent`; and pytest imports modules by bare name from this folder. Moving them silently breaks the build or the tests. Generated caches (`__pycache__`, `.pytest_cache`, `build/`) are intentionally absent — they are regenerated automatically.

### What each source module does
| File | Responsibility |
|---|---|
| `main.py` | Entry point: tray loop + CLI flags (`--dashboard`/`-d`, `--verbose`, `--diagnostics`) |
| `config.py` | `APP_VERSION`, settings persistence in `%LOCALAPPDATA%\TimeTracker\config.json` |
| `database.py` | SQLite `data.db`, schema version 6, migrations, atomic backups |
| `calculator.py` | Pure work-time math: trailing grace period, worked/overtime/break classification |
| `command_bus.py` | Single-writer command bus for mutable tracker state |
| `tracker.py` | Core monitoring engine: input events, sessions, heartbeats, idle/presence |
| `dialogs.py` | Tkinter dialogs (Settings, Pause, Overtime, Meeting, Consent, About...) |
| `tray.py` | `pystray` icon, tray menus, icons loaded from `assets/` |
| `reporter.py` | Excel/CSV/PDF reports (Daily Summary, App Usage, Sessions, Timeline, Projects) |
| `visualizer.py` | HTML dashboard + optional loopback-only HTTP server |
| `retention.py` | Aggregation into `daily_summary`, retention purge, day analytics |
| `egypt_holidays.py` | Offline loader for `assets/egypt_holidays.json` |
| `i18n.py` | English/Arabic strings, RTL and bidi text helpers |
| `logging_setup.py` | Rotating logs, crash capture (`faulthandler`), diagnostics bundle |
| `updater.py` | Offline/HTTPS auto-update, SHA-256 verification, silent install |
| `autostart.py` | Windows Startup registration |
| `aggregate_reports.py` | Manager tool: combines employee Excel reports |

### Documentation map
- **This README** — complete product/feature/architecture/build/test reference. Start here.
- `../docs/REMAINING_WORK.md` — authoritative list of open code work and the external GitHub-token revocation.
- `docs/DECISIONS.md` — chronological decisions log (§9.2.1 lists every persisted config key; §30 logging; §31–33 security/performance review; §34 dashboard timeline).
- `docs/USER_GUIDE.md` — Arabic end-user guide (installation, tray menu, settings, reports).
- `docs/test_checklist.md` — manual/benchmark checklist and historical measurements.

### Current status (verified 2026-11-18)
- Version **1.3.8**; full suite **332 passed** (`python -m pytest tests -q`, Python 3.11 / Windows).
- `dist/TimeTracker_Setup.exe` (~27 MB) + `dist/TimeTracker.exe` (~25 MB) + `dist/version.json` are present and kept.
- Open items: PERF-02, QUAL-01, QUAL-02, QUAL-03 (see `../docs/REMAINING_WORK.md`) and SEC-01 (revoke the old GitHub token — outside the code).
- `MANUAL_QA.md` is referenced by older release notes but is no longer present; GUI acceptance checks are not currently documented.

---

## Key Features

- **Privacy-First & Local-First:** No telemetry and no administrator privileges required. Optional, user-controlled network features exist (online auto-update from a configured source and a loopback-only dashboard HTTP server) and can be disabled.
- **Privacy-First Activity Monitoring:** Uses standard Win32 `GetLastInputInfo` through `ctypes`. **Never records keystrokes, active window titles, URLs, clipboard content, or screenshots.** Safe from antivirus false-positives.
- **First-Run Privacy Consent:** Displays an upfront transparency screen on first launch explaining exactly what is and is not monitored.
- **Trailing Grace Period Model:** Intelligently attributes work time using a 180-second trailing grace period following input events.
- **Automatic Classification:**
  - Active during working hours on work days → Automatically counted as **Worked**.
  - Active outside working hours or on non-work days (e.g. Fri/Sat/Holidays) → Prompts with a simple dialog: *"You're outside working hours. Record this as Overtime? [Yes] [No]"* with reason selection (Project, Emergency, Compensation).
  - Active when working hours end → Prompts to continue as Overtime.
- **Dedicated Meeting & Break Tracking:**
  - One-click Start/End Meeting (counts even when listening on calls without typing). **Meeting is a subset of Worked/Overtime:** the Excel column is `Meeting Hours (included in Worked)` and only Worked + Overtime are additive.
  - One-click Start/End Break. **Break** is manual break/pause time union the unworked time inside the work window, so it is always disjoint from Worked, Overtime and Meeting; idle evenings and overtime are never Break.
  - Optional note prompt on meeting end (e.g. "Client X review").
  - "Add Past Meeting..." dialog to record missed meetings retroactively.
- **Projects / Task Tags (Optional):**
  - Pick a current project from the tray menu (*Current project: X*); every event and session created from then on is attributed to it until you change it.
  - Manage projects (add, rename, archive) in Settings; the selection persists across restarts and is cleared automatically if the project is archived.
  - Project hours appear in the Excel `Projects` sheet, a dedicated CSV file, and the Dashboard project breakdown card.
- **CSV Export:**
  - Export the Daily Summary, Sessions Log, App Usage and Projects as four separate CSV files from the tray menu or Settings.
  - Pick *This week*, *This month* or a custom date range; files are written as UTF-8 **with BOM**, so Excel opens Arabic text correctly.
  - `Export CSV -> Export to folder...` writes the same bundle to a folder you choose. If Documents is redirected into OneDrive/Dropbox, a one-time notification points this out (tamper-evidence is explicitly out of scope).
- **Egyptian Official Holidays (Offline):**
  - Bundled `assets/egypt_holidays.json` with the fixed national holidays and per-year movable dates (Islamic holidays, Coptic Easter, Sham El-Nessim).
  - Settings -> *Import Egyptian holidays* with a year selector merges dates into your list without duplicates; manual add/remove still works. No network access.
- **Active Application Tracking (Optional):**
  - Records only foreground process executable names (e.g. `chrome.exe`, `EXCEL.EXE`) during active input to compute application breakdown.
  - Can be toggled on/off at any time from Settings (`enable_app_tracking`). When disabled, events are stored with the literal app name `"Disabled"` and the app breakdown shows that single bucket.
  - Full breakdown of hours and percentages per application in Excel report, Visual Dashboard, and live tray menu.
- **Cattr-Style Active Presence Check ("Are You Active?"):**
  - Displays a small, always-on-top prompt *"Are you still active?"* with a soft alert chime when no keyboard or mouse input is detected across the configured interval (default 3 minutes, customizable 1-60 min).
  - **Full-gap credit:** Confirming (by moving the mouse, pressing any key, or clicking `[I'm active]`) credits the **whole idle gap since your last real input**, capped at `min(active_check_interval_min, 5 minutes)` — so a 10-minute interval still credits at most 5 minutes, anchored at the last real input, never chained and never crediting the time the popup waited on screen.
  - **Focus-safe popup:** Always on top (re-asserted every 500 ms, survives fullscreen Teams/Zoom/PowerPoint), created with `WS_EX_NOACTIVATE` so it never steals keyboard focus, and positioned on the monitor containing the mouse cursor.
  - **Automatic idle timeout:** If no response is detected within 60 seconds, the window auto-closes and tracking transitions to idle without crediting the period, automatically resuming when input returns.
  - **Intelligent suppression:** Automatically suppressed during active meetings, pause mode, declined overtime, locked workstation sessions, outside tracking hours, or when other dialogs are already open.
- **Robust Crash Recovery & Heartbeat:**
  - Records periodic heartbeats and automatically recovers and closes orphan sessions after sudden power cuts or abrupt reboots without runaway hours.
  - Configurable retention (`retention_days`, default 90, `0` = keep forever). Before old raw events are purged they are aggregated into a `daily_summary` table, so reports, Excel exports and the dashboard still show those days.
- **Smart Reminders & Automation:**
  - **End of Day Summary:** Notification at work end (default 17:30, customizable) with worked hours vs target and overtime.
  - **Long Meeting Reminder:** Alerts if a meeting exceeds 2 hours.
  - **Health Stretch Reminder:** Friendly alert if working continuously for 2 hours.
  - **Automated Weekly Backup:** Safeguards `data.db` using SQLite's atomic `backup()` API to `%USERPROFILE%\Documents\TimeTracker\Backups`, keeping the newest `backup_keep_count` copies (default 8).
  - **Automated Weekly Export:** Automatically saves weekly Excel timesheet.
- **Full Settings Window:**
  - Access "Settings" from the tray menu to configure work hours, target hours, work days, official holidays (including the Egyptian import), the Projects tab, retention (`retention_days`), the local update source (`update_source_path`), CSV export, app tracking (`enable_app_tracking`), the active presence check (`active_check_enabled`, `active_check_interval_min`), and any reminders. Live updates apply immediately without restarting.
  - Every persisted key is documented in [`DECISIONS.md`](docs/DECISIONS.md) section 9.2.1.
- **Manager Multi-Employee Aggregator:**
  - Standalone script `aggregate_reports.py` combines employee Excel reports into a unified manager dashboard.
- **Pause & Half-Day Modes:**
  - **Pause Tracking:** Temporarily suspend tracking for personal device use (15m, 30m, 1h, or until resumed).
  - **Half-Day Mode:** Sets today's target to half of the configured daily target (e.g. 3 h for a 6 h day) with one click; the tray label and toast show the real number.
- **Dashboard Ranges & Comparisons:**
  - Day / 7-day / 30-day ranges plus **Week**, **Month** and **Year** views. The page embeds the last 14 days in full detail; older days are built from `daily_summary`.
  - Each view compares worked, overtime, meeting hours and top apps against the previous period with +/- percentages (old days come from `daily_summary`).
- **Offline Auto-Update (Optional):**
  - Point `update_source_path` at a local folder or UNC share; on startup and once a day the app reads `version.json` from it, verifies the installer's SHA-256, and offers to install silently. No internet access.
- **Visual Excel Timesheets:**
  - Sheet 1: **Daily Summary** (with visual Worked vs Overtime BarChart)
  - Sheet 2: **App Usage** (application breakdown)
  - Sheet 3: **Sessions Log** (complete meeting/break/overtime session timestamps and notes)
  - Sheet 4: **Activity Timeline** (per-day status timeline)
  - Sheet 5: **Projects** (hours per project, unassigned work included)

---

## Quick Start (Development Mode)

Run `python main.py --dashboard` (or `-d`) to build and open the visual dashboard immediately, without starting the tray loop.

1. Open a terminal in the `timetracker` folder.
2. Run the development batch script:
   ```cmd
   run_dev.bat
   ```
   Or run directly with Python:
   ```cmd
   pip install -r requirements.txt
   python main.py
   ```

---

## Standalone Executable & Employee Setup Installer

### 1-Click Complete Build (Recommended):
Double-click `build_installer.bat` (or run `build_installer.bat` from terminal).
It automatically:
1. Compiles the latest code into `TimeTracker.exe`.
   - The PyInstaller build excludes Java JDK `bin` folders from dependency resolution so unrelated Windows API-set DLL shims are not bundled.
2. Optionally signs the executable (see [Code Signing](#code-signing-optional)).
3. Packages it into a single-file wizard installer `TimeTracker_Setup.exe`.
4. Writes `dist\version.json` (version + SHA-256) for the local update share.

### Generated Files in `dist\`:
- `dist\TimeTracker_Setup.exe` (~27 MB): **The file to send to employees.**
  - Standard "Next -> Next -> Finish" Windows wizard.
  - No Administrator permissions needed (installs in user space).
  - Automatically registers in Windows Startup (runs silently on boot, tray only, no cmd window).
  - Seamless update support: closes any running instance, updates files, and preserves user database & settings.
- `dist\TimeTracker.exe` (~25 MB): Standalone portable binary.
- `dist\version.json`: `{ "version", "installer", "sha256", "notes" }` used by the local auto-update source.

---

## Code Signing (Optional)

The build is unsigned by default. To Authenticode-sign the executable, the setup and the uninstaller, set two environment variables before running `build_installer.bat`:

```cmd
set SIGN_CERT_PATH=C:\path\to\certificate.pfx
set SIGN_CERT_PASSWORD=your-certificate-password
build_installer.bat
```

- `signtool.exe` is located automatically (PATH or the Windows SDK). If it is missing, signing is skipped with a warning.
- If both variables are set but signing fails, the build stops with an error instead of shipping a partially signed release.
- Never commit the certificate or its password; the variables are read at build time only.
- Preferred since 1.3.5: import the certificate into the CurrentUser store and set `SIGN_CERT_THUMBPRINT` instead — no password appears in argv or build logs. `SIGN_CERT_PATH` + `SIGN_CERT_PASSWORD` still work but are deprecated.

---

## Local Auto-Update (Optional, No Internet)

1. Build a new release (this produces `TimeTracker_Setup.exe` and `version.json`).
2. Copy both files into a shared folder or UNC path, e.g. `\\fileserver\apps\TimeTracker`.
3. Set that path in Settings -> *Update source folder* (config key `update_source_path`). Empty disables updates.

The app checks at startup and once a day. If `version.json` advertises a newer version, the tray notifies the user; on confirmation the installer's SHA-256 is verified again and the setup runs silently. Silent updates leave the app closed; reopen it from the Start menu or tray shortcut after setup finishes. The database and settings are preserved. Nothing is downloaded from the internet.

---

## Local Data, Privacy & Retention

- Database: `%LOCALAPPDATA%\TimeTracker\data.db`; settings: `%LOCALAPPDATA%\TimeTracker\config.json`.
- Backups: `%USERPROFILE%\Documents\TimeTracker\Backups`; reports: `%USERPROFILE%\Documents\TimeTracker\Reports`.
- Raw input events older than `retention_days` (default 90, `0` = forever) are aggregated into `daily_summary` and then deleted. Aggregated days remain visible in every report, and no data ever leaves the machine.
- No telemetry and no data upload: tracking data never leaves the machine. The dashboard is an optional loopback-only HTTP server (`127.0.0.1`, 128-bit token, disable with `enable_dashboard_server=false`), and the optional auto-update can use either a user-configured local/UNC share or an HTTPS update URL; both are strictly opt-in.

---

## Logging, Crash Capture & Diagnostics

Every run writes to `%LOCALAPPDATA%\TimeTracker`:

| File | Content | Rotation |
| --- | --- | --- |
| `timetracker.log` | INFO and above (DEBUG with `--verbose` or `log_level`) | 5 MB x 5 |
| `timetracker-errors.log` | WARNING/ERROR/CRITICAL only, for quick triage | 2 MB x 3 |
| `timetracker-faults.log` | Native crash stacks (`faulthandler`) | 1 MB x 1 |

Each line carries milliseconds, the thread name, an 8-character session id, the module and the
line number:

```
2026-11-17 11:15:01.475 INFO  [MainThread] [8b631a55] root:159 - Database initialized at ...
```

Every start writes a banner with the version, PID, executable, frozen flag, Python and Windows
build, locale, elevation, data dir and effective level, so one log excerpt is enough to reproduce
a report.

- **Verbosity:** `log_level` in `config.json` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`;
  default `INFO`) or `TimeTracker.exe --verbose` for a single session. Changing it in Settings
  applies live.
- **Crash capture:** unhandled exceptions (main thread *and* worker threads) are logged as CRITICAL
  with the full traceback, native faults are captured by `faulthandler`, and a fatal error shows a
  dialog that offers to open the log folder - the windowed build has no console, so nothing is
  silent any more.
- **Support bundle:** tray menu -> *Create Diagnostics Bundle...* (or
  `TimeTracker.exe --diagnostics`, which works even when the tray is broken) writes a zip with the
  logs, `system.txt`, `session.json` and `config.json` to
  `Documents\TimeTracker\Diagnostics`. Nothing is uploaded; send the zip yourself.
- **Log level is a persisted setting:** see `DECISIONS.md` section 9.2.1.

---

## Tests

```cmd
python -m pytest tests -q
```

The suite has **332 tests** and is fully offline: grace model, interval algebra, presence-credit cap, overtime state machine, meeting/break accounting (D1/D2), non-work-day overtime (D3), migrations (including a simulated crash), retention aggregation, holiday import, CSV encoding, projects, updater path safety, pause sessions, i18n key coverage, logging/rotation/crash capture/diagnostics bundles and the dashboard (including `</script>` escaping).

A single instance per Windows session is enforced with the `Local\TimeTrackerSingleton` mutex, so a second Windows user (RDP / fast user switching) gets their own instance.

---

## Multi-Employee Report Aggregator (for Managers)

Run the aggregator tool:
```cmd
python aggregate_reports.py
```
Presents a folder picker to select the directory containing employee `TimeReport_*.xlsx` files, then generates `Team_Consolidated_Report_<date>.xlsx` with team totals, daily breakdowns, and top applications across all team members.

The `What's changed` sections below are **historical release notes**: their test counts and build sizes describe those specific releases, not the current state (see *Current status* above).

## What's changed in 1.3.5

- **Startup crash fixed in the shipped build.** The 1.3.4 binary built on 2026-11-15 aborted
  immediately after launch with `ValueError: <function TrayApp._build_menu.<locals>.<lambda>>`
  from `pystray._base._assert_action`, so "Launch TimeTracker" appeared to do nothing. The cause
  was a project menu entry whose action took three arguments (`icon`, `item`, a defaulted
  `pid`); pystray only accepts zero, one or two. The source was corrected to build per-project
  callbacks with a factory, and this release rebuilds the binary from that source.
  `tests/test_projects.py::test_tray_menu_builds_with_projects` now covers it.
- Version bumped so installs of the broken 1.3.4 are offered this build by the updater
  (`is_newer` requires a strictly greater version).
- **New: a real logging and crash-reporting subsystem** (`logging_setup.py`). Three rotating UTF-8
  files (`timetracker.log`, `timetracker-errors.log`, `timetracker-faults.log`), millisecond
  timestamps, thread names, per-run session ids, a startup banner, `sys`/`threading` excepthooks,
  `faulthandler` for native crashes, a fatal-error dialog that offers to open the log folder,
  tray entries *Open Log Folder* / *Create Diagnostics Bundle...*, and the `--verbose` and
  `--diagnostics` command-line flags. Verbosity is a persisted setting (`log_level`, default
  `INFO`). Details in *Logging, Crash Capture & Diagnostics* above and `DECISIONS.md` §30.
- Saving the Settings dialog no longer resets `enable_dashboard_server`,
  `enable_smart_meeting_detection`, `smart_meeting_cooldown_minutes` and `log_level` to their
  defaults.

## What's changed in 1.3.2

- **Older dashboard days are filled automatically.** A new aggregation pass runs at startup
  (background thread), after midnight, and before every dashboard/export build, so days 15-90 can
  no longer appear empty. It is idempotent, never touches today, and the retention purge refuses
  to delete raw events for a day that has not been aggregated yet.
- **Weekend / out-of-hours meetings are no longer silently dropped.** Starting such a meeting asks
  once whether it is Overtime; declining is reported in a notification and the time shows up in the
  report as **Unclassified Meeting Hours** (new last column in the Excel/CSV daily summary).
  "Add Past Meeting" gained a "Count as overtime" checkbox (default checked) and a reason selector.
- **The database is backed up before any schema upgrade** to
  `Backups\pre_migration_v<old>_to_v<new>_<timestamp>.db` (sqlite3 backup API). If the copy fails the
  upgrade is aborted with an error dialog. These copies are kept for 30 days and are not affected
  by `backup_keep_count`.
- **Overtime prompts answer themselves after 90 s** (configurable: `overtime_prompt_timeout_seconds`,
  30-600, in `config.json`). The original `MANUAL_QA.md` acceptance steps are no longer in the workspace.
- **Per-monitor DPI awareness** is enabled before Tk starts, dialogs are sized from the screen
  DPI and capped to 90% of the screen, and every Settings tab scrolls (canvas + mouse wheel).
- **The Arabic UI is properly mirrored.** Settings, Pause, Past Meeting, Meeting Note, About,
  Overtime, Consent, the presence popup and the update/date-range dialogs right-align labels,
  mirror grid columns, flip pack sides and move checkboxes/radios to the right. Latin words and
  numbers are wrapped in direction marks so they cannot scramble. Switching the language in
  Settings mirrors the window live, and the bilingual headers are gone: the section titles show
  only "Language" / "اللغة" and "Update" / "التحديث" for the current language.
- **Tests:** 237 passing (`python -m pytest tests -q`). New `scripts/smoke_test.py` simulates a full
  work week in a temporary folder and prints a PASS/FAIL invariant table.

### Verified in this pass / not verified

- Verified: the 237 automated tests and `scripts/smoke_test.py` (both run on Windows, Python 3.11).
- **Build:** `build_installer.bat` completed (PyInstaller 6.22.3 + Inno Setup 6), producing
  `dist\TimeTracker.exe` (19.5 MiB) and `dist\TimeTracker_Setup.exe` (21.1 MiB). `dist\version.json`
  reports **1.3.2** with the SHA-256 of the new installer (recomputed afterwards and matched), and
  `installer.iss` carries the same version. Code signing was skipped gracefully — the run printed
  `Signed: no` because no certificate variables are set.
- **Not run in this pass:** GUI checks (`MANUAL_QA.md` is no longer present in the workspace).
### Measurements (v1.3.2, `python scripts/benchmark.py`)

Measured on the Windows development machine, 60 000 stored events over 90 days, Python 3.11:

| Metric | Value |
|---|---|
| CPU time per tick (day-metrics refresh), average | **5.00 ms** |
| CPU time per tick, max of 40 samples | **6.17 ms** |
| Dashboard build, 90 days, first build (includes the aggregation pass) | **4.96 s** |
| Dashboard build, 90 days, repeat build | **1.34 s** |
| Dashboard HTML size | **0.37 MiB** |
| Startup: process folder → database open → first tick | **981 ms** |
| Process RAM after 5 minutes of simulated 5 s ticks | **60.8 MB RSS** |

These replace the unverified figures from earlier releases. Re-run `scripts/benchmark.py` after
any change that touches the calculator, retention or the dashboard.
