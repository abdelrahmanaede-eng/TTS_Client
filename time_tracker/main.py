"""Main entry point for TimeTracker.

Enforces single-instance execution via Windows named mutex, initializes logging,
database, configuration, autostart registry, background activity tracking,
and runs the system tray UI on the main thread.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from datetime import date

from time_tracker.system.autostart import is_autostart_enabled
from time_tracker.core.config import Config, load_config
from time_tracker.core.database import Database
from time_tracker.system.logging_setup import (
    create_diagnostics_bundle,
    handle_fatal,
    install_crash_handlers,
    is_frozen,
    log_startup_banner,
    notify_user,
    open_folder,
    set_level,
    setup_logging,
)
from time_tracker.core.tracker import ActivityTracker
from time_tracker.ui.tray import TrayApp

# Mutex constants
ERROR_ALREADY_EXISTS = 183
# P0-3: the guard is per logon session, so every Windows user (including each
# RDP / terminal-services session) runs their own instance.
MUTEX_LOCAL_NAME = r"Local\TimeTrackerSingleton"

# Global handle to prevent garbage collection
_MUTEX_HANDLE = None


def enforce_single_instance() -> None:
    """Ensure one instance per Windows logon session (P0-3).

    A session-scoped mutex means a second Windows user (RDP, fast user
    switching, terminal server) still gets their own tracked instance.
    """
    global _MUTEX_HANDLE

    handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_LOCAL_NAME)
    last_error = ctypes.windll.kernel32.GetLastError()

    if handle and last_error == ERROR_ALREADY_EXISTS:
        logging.getLogger(__name__).info(
            "Another TimeTracker instance already owns the session mutex; exiting."
        )
        ctypes.windll.user32.MessageBoxW(
            0,
            "TimeTracker is already running in the system tray for this Windows user.",
            "TimeTracker",
            0x40,  # MB_ICONINFORMATION
        )
        sys.exit(0)
    elif handle:
        _MUTEX_HANDLE = handle
        return

    # If mutex creation failed completely, log and continue
    logging.warning("Could not acquire named singleton mutex.")



def check_config_integrity(db: Database, config: Config) -> None:
    """Detect if work_end or work_days changed and confirm on startup to prevent accidental edits."""
    prev_end = db.get_setting("known_work_end")
    prev_days = db.get_setting("known_work_days")
    curr_days_str = ",".join(sorted(config.work_days))

    if prev_end and prev_days:
        if prev_end != config.work_end or prev_days != curr_days_str:
            res = ctypes.windll.user32.MessageBoxW(
                0,
                f"TimeTracker configuration change detected:\n\n"
                f"• Work End: {prev_end} → {config.work_end}\n"
                f"• Work Days: {prev_days} → {curr_days_str}\n\n"
                f"Do you want to confirm and apply these changes?",
                "TimeTracker — Config Change Confirmation",
                0x24,  # MB_YESNO | MB_ICONQUESTION
            )
            if res != 6:  # IDYES is 6
                # Revert to previous
                config.work_end = prev_end
                config.work_days = prev_days.split(",")
                config.save()
                logging.info("Config changes reverted based on user prompt.")
                return

    db.set_setting("known_work_end", config.work_end)
    db.set_setting("known_work_days", curr_days_str)


def main() -> None:
    # Logging first: everything below can then be diagnosed from the file, and
    # the banner captures the environment before any subsystem can fail.
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    setup_logging(level="DEBUG" if verbose else "INFO")
    install_crash_handlers()
    log_startup_banner()

    if "--diagnostics" in sys.argv:
        bundle = create_diagnostics_bundle()
        logging.getLogger(__name__).info("Diagnostics bundle written to %s", bundle)
        print(f"Diagnostics bundle written to: {bundle}")
        if is_frozen():
            # No console in the installed build: tell the user where the zip is.
            notify_user(
                "TimeTracker — Diagnostics",
                f"Diagnostics bundle created:\n\n{bundle}\n\nThe folder will open now.",
            )
            open_folder(bundle.parent)
        sys.exit(0)

    if "--dashboard" in sys.argv or "-d" in sys.argv:
        from time_tracker.reporting.visualizer import open_dashboard
        cfg = load_config()
        database = Database()
        p = open_dashboard(database, cfg)
        print(f"Visual Dashboard opened: {p}")
        sys.exit(0)

    if "--export-pdf" in sys.argv:
        from time_tracker.reporting.reporter import export_pdf_report
        cfg = load_config()
        database = Database()
        today = date.today()
        p = export_pdf_report(database, cfg, today.replace(day=1), today, open_folder=False)
        print(f"Official PDF Timesheet exported: {p}")
        sys.exit(0)

    enforce_single_instance()

    # Item 7: per-monitor DPI awareness must be set before Tk creates a window.
    from time_tracker.ui.dialogs import setup_dpi_awareness

    setup_dpi_awareness()

    try:
        config = load_config()
        # ``--verbose`` always wins so a support session can force DEBUG without
        # editing config.json.
        set_level("DEBUG" if verbose else config.log_level)
        logging.info("Configuration loaded: %s", config)

        try:
            db = Database()
        except Exception as exc:
            # Item 3: a migration without a good backup is never attempted, and a
            # failure is reported instead of leaving a half-upgraded database.
            logging.critical("Database migration failed: %s", exc, exc_info=True)
            ctypes.windll.user32.MessageBoxW(
                0,
                "TimeTracker could not upgrade its database and will close.\n\n"
                f"{exc}\n\nSee timetracker.log for details.",
                "TimeTracker",
                0x10,  # MB_ICONERROR
            )
            sys.exit(1)
        logging.info("Database initialized at %s", db.db_path)

        # Check First-Run Privacy Consent
        if not db.get_setting("privacy_consent_accepted"):
            logging.info("First run detected: prompting user for privacy & activity consent.")
            from time_tracker.ui.dialogs import show_consent_dialog_async, stop_ui
            consent_granted = [False]
            consent_event = threading.Event()

            def on_consent_accept() -> None:
                consent_granted[0] = True
                consent_event.set()

            def on_consent_decline() -> None:
                consent_granted[0] = False
                consent_event.set()

            show_consent_dialog_async(on_consent_accept, on_consent_decline, lang=config.language)
            # P1-6: never hang invisibly if the UI thread cannot start.
            if not consent_event.wait(timeout=30):
                logging.error("Consent dialog did not respond; the UI thread is unavailable.")
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "TimeTracker could not show the first-run privacy notice and will close.\n"
                    "Please start the application again.",
                    "TimeTracker",
                    0x10,  # MB_ICONERROR
                )
                stop_ui()
                sys.exit(1)

            if not consent_granted[0]:
                logging.info("User declined privacy consent. Exiting.")
                stop_ui()
                sys.exit(0)

            db.set_setting("privacy_consent_accepted", "1")
            logging.info("Privacy consent accepted and recorded.")

        check_config_integrity(db, config)

        # Respect the autostart state actually configured on this machine: the
        # installer task and the Settings toggle are the sources of truth, so a
        # user who unchecked "start with Windows" is never silently overridden.
        registry_autostart = is_autostart_enabled()
        if config.autostart_enabled != registry_autostart:
            logging.info(
                "Autostart differs from config (registry=%s, config=%s); following the registry.",
                registry_autostart,
                config.autostart_enabled,
            )
            config.autostart_enabled = registry_autostart
            config.save()

        tracker = ActivityTracker(db, config)
        # Callbacks must be attached before the tracker loop starts so the very
        # first tick can already notify about a local update / reminder.
        tray_app = TrayApp(db, config, tracker)
        tracker.start()

        # Item 1a: backfill/refresh daily_summary in the background so the tray
        # is never blocked by a large first run.
        def _aggregate_on_startup() -> None:
            try:
                from time_tracker.core.retention import aggregate_missing_days

                result = aggregate_missing_days(db, config)
                if result["aggregated"]:
                    logging.info("Startup aggregation completed: %s day(s).", result["aggregated"])
            except Exception as exc:
                logging.error("Startup aggregation failed: %s", exc)

        threading.Thread(
            target=_aggregate_on_startup, name="StartupAggregation", daemon=True
        ).start()
        try:
            tray_app.run()
        finally:
            tracker.stop()
            from time_tracker.ui.dialogs import stop_ui
            stop_ui()
            logging.info("TimeTracker exited cleanly.")

    except Exception as exc:
        # ``handle_fatal`` writes the full traceback and offers to open the log
        # folder, so a silent windowed crash can never happen again.
        handle_fatal(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
