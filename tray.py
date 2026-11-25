"""System Tray interface for TimeTracker using pystray.

Implements the comprehensive menu layout with daily/weekly/monthly progress,
session toggles, past meeting entry, pause tracking, half-day mode,
full visual settings dialog, dynamic status icons, tooltips, and notifications.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw
import pystray
from pystray import Menu, MenuItem

from autostart import is_autostart_enabled, set_autostart
from config import APP_VERSION, Config, get_app_dir
from database import Database
from dialogs import (
    bidi_text,
    show_about_dialog_async,
    show_date_range_picker_dialog_async,
    show_directory_picker_async,
    show_download_progress_dialog,
    show_info_dialog_async,
    show_past_meeting_dialog_async,
    show_pause_dialog_async,
    show_settings_dialog_async,
    show_update_dialog_async,
)
from i18n import t
from logging_setup import describe_exception, get_log_dir, open_log_folder, set_level
from reporter import export_csv_bundle, export_pdf_report, export_report
from tracker import ActivityTracker
from updater import UpdateInfo, check_for_update, launch_installer
from visualizer import open_dashboard

logger = logging.getLogger(__name__)

ICON_COLOR_MAP = {
    "Active": ("icon_active.png", "#28a745"),
    "Idle": ("icon_idle.png", "#ffc107"),
    "In Meeting": ("icon_meeting.png", "#dc3545"),
    "In Break": ("icon_idle.png", "#ffc107"),
    "Paused": ("icon_paused.png", "#dc3545"),
    "Overtime": ("icon_overtime.png", "#007bff"),
    "Error": ("icon_error.png", "#343a40"),
}


def get_asset_path(filename: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base_dir = Path(sys._MEIPASS) / "assets"  # type: ignore
    else:
        base_dir = Path(__file__).parent / "assets"
    return base_dir / filename


def create_fallback_icon(hex_color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=hex_color)
    return img


def is_popup_menu_open() -> bool:
    """Check if any Win32 popup menu is currently active on screen.

    Uses ``FindWindowExW`` with ``GetCurrentThreadId`` to scope the search to
    menus owned by this process's thread, avoiding false positives when other
    applications open their own Win32 context menus simultaneously.

    Falls back to the broader ``FindWindowW`` if the per-thread approach is
    unavailable, so the guard still works in degraded environments.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # Try to check only windows on the current thread's message queue.
        tid = kernel32.GetCurrentThreadId()
        # EnumThreadWindows is unavailable via windll easily; check if any
        # "#32768" (popup menu) window belongs to this process.
        # GetWindowThreadProcessId lets us filter by PID.
        pid = os.getpid()
        hwnd = user32.FindWindowW("#32768", None)
        while hwnd:
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if win_pid.value == pid:
                return True
            hwnd = user32.FindWindowExW(0, hwnd, "#32768", None)
        return False
    except Exception:
        # Broad fallback: any popup menu on the desktop
        try:
            return ctypes.windll.user32.FindWindowW("#32768", None) != 0
        except Exception:
            return False


class TrayApp:
    def __init__(self, db: Database, config: Config, tracker: ActivityTracker) -> None:
        self.db = db
        self.config = config
        self.tracker = tracker

        self._icons: dict[str, Image.Image] = {}
        self._load_icons()

        self._icon: pystray.Icon | None = None
        self._current_icon_state: str = "Active"
        self._is_menu_open: bool = False
        # True only when the private pystray menu hook was installed; otherwise
        # the state-change path falls back to a plain menu rebuild (B21).
        self._menu_hook_active: bool = False

        self.tracker.on_state_change = self._on_tracker_state_change
        self.tracker.on_notification = self._on_notification
        self.tracker.on_update_available = self._on_update_available

    def _load_icons(self) -> None:
        for state, (filename, fallback_hex) in ICON_COLOR_MAP.items():
            path = get_asset_path(filename)
            if path.exists():
                try:
                    self._icons[state] = Image.open(path)
                except Exception as exc:
                    logger.warning("Failed to open %s: %s", path, exc)
                    self._icons[state] = create_fallback_icon(fallback_hex)
            else:
                self._icons[state] = create_fallback_icon(fallback_hex)

    def _get_icon_image(self, state: str) -> Image.Image:
        return self._icons.get(state, self._icons["Active"])

    def _format_tooltip(self) -> str:
        metrics = self.tracker.today_metrics
        status = self.tracker.current_status
        target_h = int(metrics.target_hours) if metrics else int(self.config.daily_target_hours)
        lang = self.config.language

        if metrics:
            worked = metrics.worked_formatted
            overtime = metrics.overtime_formatted
        else:
            worked = "0h 00m"
            overtime = "0h 00m"

        status_key = f"status_{status.lower().replace(' ', '_')}"
        localized_status = t(status_key, lang)

        return t(
            "tooltip_format",
            lang,
            status=localized_status,
            worked=worked,
            target=target_h,
            overtime=overtime,
        )

    def _build_menu(self) -> Menu:
        metrics = self.tracker.today_metrics
        target_h = int(metrics.target_hours) if metrics else int(self.config.daily_target_hours)
        worked_text = metrics.worked_formatted if metrics else "0h 00m"
        overtime_text = metrics.overtime_formatted if metrics else "0h 00m"
        lang = self.config.language

        # RTL: wrap Latin/number runs with LRM so mixed labels such as
        # "لوحة البيانات المرئية (Dashboard)" keep their order in native menus.
        def mt(key: str, lang_arg: str | None = None, **kwargs: object) -> str:
            effective_lang = lang_arg or lang
            return bidi_text(t(key, effective_lang, **kwargs), effective_lang)

        # Information labels
        label_today = mt("menu_today_worked", worked=worked_text, target=target_h)
        label_week = mt("menu_this_week", summary=self.tracker.week_summary_formatted)
        label_month = mt("menu_this_month", summary=self.tracker.month_summary_formatted)
        label_meetings = mt("menu_meetings", time=metrics.meeting_formatted if metrics else "0h 00m")
        label_breaks = mt("menu_breaks", time=metrics.break_formatted if metrics else "0h 00m")
        label_overtime = mt("menu_overtime", time=overtime_text)
        label_top_app = mt("menu_top_app", app=self.tracker.top_app_formatted)

        meeting_action_text = mt("action_end_meeting") if self.tracker.is_meeting_active() else mt("action_start_meeting")
        break_action_text = mt("action_end_break") if self.tracker.is_break_active() else mt("action_start_break")
        overtime_action_text = mt("action_end_overtime") if self.tracker.is_overtime_active() else mt("action_start_overtime")
        pause_action_text = mt("action_resume_tracking") if self.tracker.is_paused() else mt("action_pause_tracking")
        half_day_hours = f"{self.tracker.half_day_target_hours:g}"
        half_day_action_text = (
            mt("action_half_day_on", hours=half_day_hours)
            if self.tracker.is_half_day_today
            else mt("action_half_day_off", hours=half_day_hours)
        )

        # Submenu: Export Report
        export_submenu = Menu(
            MenuItem(mt("action_export_pdf_report", lang), lambda icon, item: self._export_official_pdf_report()),
            MenuItem(mt("export_today", lang), lambda icon, item: self._export_range("today")),
            MenuItem(mt("export_7days", lang), lambda icon, item: self._export_range("7days")),
            MenuItem(mt("export_month", lang), lambda icon, item: self._export_range("month")),
            MenuItem(mt("export_custom", lang), lambda icon, item: self._export_custom_range()),
        )

        # Submenu: Export CSV (daily summary + sessions + app usage + projects)
        csv_submenu = Menu(
            MenuItem(mt("export_this_week", lang), lambda icon, item: self._export_csv_range("week")),
            MenuItem(mt("export_month", lang), lambda icon, item: self._export_csv_range("month")),
            MenuItem(mt("export_custom", lang), lambda icon, item: self._export_csv_custom_range()),
            MenuItem(mt("action_export_to_folder", lang), lambda icon, item: self._export_csv_to_folder()),
        )

        # Submenu: current project (attribution of tracked time)
        try:
            projects = self.db.list_projects()
        except Exception:
            projects = []
        current_project = self.tracker.current_project_id
        project_items = [
            MenuItem(
                mt("menu_project_none"),
                lambda icon, item: self._select_project(None),
                checked=lambda item: self.tracker.current_project_id is None,
                radio=True,
            )
        ]
        def _make_project_action(p_id: int):
            return lambda icon, item: self._select_project(p_id)

        def _make_project_checked(p_id: int):
            return lambda item: self.tracker.current_project_id == p_id

        for project in projects:
            pid = int(project["id"])
            project_items.append(
                MenuItem(
                    bidi_text(str(project["name"]), lang),
                    _make_project_action(pid),
                    checked=_make_project_checked(pid),
                    radio=True,
                )
            )

        current_label = self.tracker.current_project_name or mt("menu_project_none")
        project_submenu = Menu(
            MenuItem(mt("menu_current_project", lang, project=current_label), None, enabled=False),
            Menu.SEPARATOR,
            *project_items,
        )

        # The seven read-only progress rows live in one submenu so the actions
        # stay visible on small screens (audit UX item).
        summary_submenu = Menu(
            MenuItem(label_today, None, enabled=False),
            MenuItem(label_week, None, enabled=False),
            MenuItem(label_month, None, enabled=False),
            MenuItem(label_meetings, None, enabled=False),
            MenuItem(label_breaks, None, enabled=False),
            MenuItem(label_overtime, None, enabled=False),
            MenuItem(label_top_app, None, enabled=False),
        )

        menu_items = [
            # 1. Progress summaries (single submenu)
            MenuItem(mt("menu_summary_group", lang), summary_submenu),
            # Separator
            Menu.SEPARATOR,
            # 2. Main Live Actions
            MenuItem(meeting_action_text, lambda icon, item: self._toggle_meeting()),
            MenuItem(break_action_text, lambda icon, item: self._toggle_break()),
            MenuItem(overtime_action_text, lambda icon, item: self._toggle_overtime()),
            MenuItem(mt("action_add_past_meeting", lang), lambda icon, item: self._add_past_meeting()),
            MenuItem(pause_action_text, lambda icon, item: self._toggle_pause()),
            MenuItem(half_day_action_text, lambda icon, item: self._toggle_half_day()),
            # Separator
            Menu.SEPARATOR,
            # 3. Reports & Utilities
            MenuItem(bidi_text(f"{t('tab_projects', lang)}: {current_label}", lang), project_submenu),
            MenuItem(mt("action_dashboard", lang), lambda icon, item: self._open_dashboard()),
            MenuItem(mt("action_export_report", lang), export_submenu),
            MenuItem(mt("export_csv_menu", lang), csv_submenu),
            MenuItem(mt("action_open_data_folder", lang), lambda icon, item: self._open_data_folder()),
            MenuItem(mt("action_open_logs", lang), lambda icon, item: self._open_log_folder()),
            MenuItem(mt("action_diagnostics", lang), lambda icon, item: self._create_diagnostics()),
            MenuItem(mt("action_check_update", lang), lambda icon, item: self._check_update_manual()),
            MenuItem(mt("action_settings", lang), lambda icon, item: self._open_settings()),
            MenuItem(mt("action_about", lang), lambda icon, item: self._show_about()),
            MenuItem(mt("action_quit", lang), lambda icon, item: self._quit()),
        ]

        return Menu(*menu_items)

    def _on_tracker_state_change(self) -> None:
        if not self._icon:
            return

        status = self.tracker.current_status
        tooltip = self._format_tooltip()

        try:
            self._icon.title = tooltip
        except Exception:
            pass

        if status != self._current_icon_state:
            self._current_icon_state = status
            try:
                self._icon.icon = self._get_icon_image(status)
            except Exception:
                pass

        # If a popup context menu is currently displayed, do NOT rebuild or destroy
        # the native Win32 menu handle! Destroying hmenu while TrackPopupMenuEx is
        # tracking mouse events causes the hover state on menu items to freeze/lock.
        if self._is_menu_open or is_popup_menu_open():
            return

        try:
            self._icon.menu = self._build_menu()
        except Exception:
            pass

    def _on_notification(self, message: str, title: str) -> None:
        if self._icon:
            try:
                self._icon.notify(message, title)
            except Exception as exc:
                logger.warning("Notification error: %s", exc)

    def _toggle_meeting(self) -> None:
        self.tracker.toggle_meeting()
        self._on_tracker_state_change()

    def _toggle_break(self) -> None:
        self.tracker.toggle_break()
        self._on_tracker_state_change()

    def _toggle_overtime(self) -> None:
        self.tracker.toggle_overtime()
        self._on_tracker_state_change()

    def _add_past_meeting(self) -> None:
        def on_submit(
            d_str: str,
            s_str: str,
            e_str: str,
            note: str,
            as_ot: bool,
            reason: str,
        ) -> None:
            if not d_str or not s_str or not e_str:
                return  # Tk-unavailable fallback (BUG-05)
            self.tracker.add_past_meeting(d_str, s_str, e_str, note, as_ot, reason)

        show_past_meeting_dialog_async(on_submit, lang=self.config.language)

    def _toggle_pause(self) -> None:
        if self.tracker.is_paused():
            self.tracker.toggle_pause()
            self._on_tracker_state_change()
        else:
            def on_selected(minutes: int | None) -> None:
                if minutes is None:
                    return  # Tk-unavailable fallback (BUG-05)
                self.tracker.toggle_pause(minutes)
                self._on_tracker_state_change()

            show_pause_dialog_async(on_selected, lang=self.config.language)

    def _toggle_half_day(self) -> None:
        self.tracker.toggle_half_day()
        self._on_tracker_state_change()

    def _open_settings(self) -> None:
        def on_save(new_config: Config) -> None:
            self.config = new_config
            self.tracker.set_config(new_config)
            # Apply the persisted verbosity immediately (no restart needed).
            set_level(new_config.log_level)
            logging.getLogger(__name__).info("Settings saved; language=%s", new_config.language)
            # Update autostart registry key if changed
            set_autostart(new_config.autostart_enabled)

            # The selected project may have been archived in the Projects tab.
            current = self.tracker.current_project_id
            if current is not None:
                project = self.db.get_project(current)
                if not project or int(project.get("archived") or 0) == 1:
                    self.tracker.set_current_project(None)

            self._on_tracker_state_change()
            if self._icon:
                self._icon.notify(t("notif_settings_saved", new_config.language), "BUSSMA_TTS")

        show_settings_dialog_async(self.config, on_save, db=self.db)

    def _select_project(self, project_id: int | None) -> None:
        """Attribute following active time to the chosen project."""
        try:
            self.tracker.set_current_project(project_id)
            name = self.tracker.current_project_name or t("menu_project_none", self.config.language)
            if self._icon:
                self._icon.notify(
                    t("menu_current_project", self.config.language, project=name),
                    "BUSSMA_TTS",
                )
        except Exception as exc:
            logger.error("Could not change current project: %s", exc)
        self._on_tracker_state_change()

    def _open_dashboard(self) -> None:
        try:
            dashboard_file = open_dashboard(self.db, self.config, target_date=date.today())
            if self._icon:
                self._icon.notify(t("notif_dashboard_opened", self.config.language), "BUSSMA_TTS")
        except Exception as exc:
            logger.error("Error opening dashboard: %s", exc)
            if self._icon:
                self._icon.notify(f"Could not open dashboard: {exc}", "BUSSMA_TTS")

    def _warn_if_documents_synced(self) -> None:
        """Show the one-time cloud-sync warning for the export folder (D6)."""
        try:
            if self.db.get_setting("documents_sync_warning_shown") == "1":
                return
            from reporter import documents_folder_is_cloud_synced

            if not documents_folder_is_cloud_synced():
                return
            self.db.set_setting("documents_sync_warning_shown", "1")
            if self._icon:
                self._icon.notify(
                    t("notif_documents_sync", self.config.language),
                    t("app_short", self.config.language),
                )
        except Exception as exc:
            logger.warning("Could not evaluate the Documents sync warning: %s", exc)

    def _export_csv_to_folder(self) -> None:
        """Export the CSV bundle to a folder the user picks (D6)."""
        today = date.today()
        lang = self.config.language

        def on_range(start_d: date, end_d: date) -> None:
            def on_folder(folder) -> None:
                if folder is None:
                    return  # Tk-unavailable fallback (BUG-05)

                # BUG-07: keep the heavy export off both the pystray loop and
                # the Tk UI thread that invoked this callback.
                def worker() -> None:
                    try:
                        files = export_csv_bundle(
                            self.db, self.config, start_d, end_d,
                            open_folder=False, output_dir=folder,
                            excel_safe=getattr(self.config, "csv_excel_safe", True),
                        )
                        if self._icon:
                            self._icon.notify(
                                t("export_folder_success", lang, folder=str(folder), count=len(files)),
                                t("app_short", lang),
                            )
                    except Exception as exc:
                        logger.error("CSV export to folder failed: %s", exc)
                        if self._icon:
                            self._icon.notify(t("export_csv_fail", lang, error=str(exc)), t("app_short", lang))

                threading.Thread(target=worker, name="CsvFolderExportWorker", daemon=True).start()

            show_directory_picker_async(on_folder, lang=lang)

        show_date_range_picker_dialog_async(
            on_submit=on_range,
            lang=lang,
            default_start=today.replace(day=1),
            default_end=today,
        )

    def _export_official_pdf_report(self, start_d: date | None = None, end_d: date | None = None) -> None:
        """Export official HR timesheet PDF report with Bussma seal with customizable date range."""
        if start_d is not None and end_d is not None:
            self._do_custom_pdf_export(start_d, end_d)
            return

        today = date.today()
        month_start = today.replace(day=1)
        lang = self.config.language
        show_date_range_picker_dialog_async(
            on_submit=self._do_custom_pdf_export,
            lang=lang,
            default_start=month_start,
            default_end=today,
            title=t("custom_pdf_export_title", lang),
            button_text=t("custom_pdf_export_btn", lang),
            icon="📄",
        )

    def _do_custom_pdf_export(self, start_d: date, end_d: date) -> None:
        # BUG-07: run the (potentially 45s) export off the tray message loop.
        def _run() -> None:
            lang = self.config.language
            self._warn_if_documents_synced()
            try:
                today = date.today()
                half_days = {today} if self.tracker.is_half_day_today else None
                out_file = export_pdf_report(
                    self.db,
                    self.config,
                    start_d,
                    end_d,
                    open_folder=True,
                    half_day_dates=half_days,
                )
                if self._icon:
                    self._icon.notify(
                        t("notif_pdf_export_success", lang, filename=out_file.name),
                        "BUSSMA_TTS",
                    )
            except Exception as exc:
                logger.error("Official PDF Export error: %s", exc, exc_info=True)
                if self._icon:
                    self._icon.notify(t("notif_export_fail", lang, error=str(exc)), "BUSSMA_TTS")

        threading.Thread(target=_run, name="PdfExportWorker", daemon=True).start()

    def _export_range(self, range_key: str) -> None:
        # BUG-07: run the export off the tray message loop.
        def _run() -> None:
            today = date.today()
            lang = self.config.language
            self._warn_if_documents_synced()
            if range_key == "today":
                start_d, end_d = today, today
            elif range_key == "7days":
                start_d, end_d = today - timedelta(days=6), today
            elif range_key == "month":
                start_d, end_d = today.replace(day=1), today
            else:
                start_d, end_d = today, today

            try:
                half_days = {today} if self.tracker.is_half_day_today else None
                out_file = export_report(self.db, self.config, start_d, end_d, open_folder=True, half_day_dates=half_days)
                if self._icon:
                    self._icon.notify(t("notif_export_success", lang, filename=out_file.name), "BUSSMA_TTS")
            except Exception as exc:
                logger.error("Export error: %s", exc)
                if self._icon:
                    self._icon.notify(t("notif_export_fail", lang, error=str(exc)), "BUSSMA_TTS")

        threading.Thread(target=_run, name="ExcelExportWorker", daemon=True).start()

    def _export_custom_range(self) -> None:
        today = date.today()
        month_start = today.replace(day=1)
        show_date_range_picker_dialog_async(
            on_submit=self._do_custom_export,
            lang=self.config.language,
            default_start=month_start,
            default_end=today,
        )

    def _do_custom_export(self, start_d: date, end_d: date) -> None:
        # BUG-07: run the export off the tray message loop.
        def _run() -> None:
            lang = self.config.language
            try:
                today = date.today()
                half_days = {today} if (self.tracker.is_half_day_today and start_d <= today <= end_d) else None
                out_file = export_report(self.db, self.config, start_d, end_d, open_folder=True, half_day_dates=half_days)
                if self._icon:
                    self._icon.notify(t("notif_export_success", lang, filename=out_file.name), "BUSSMA_TTS")
            except Exception as exc:
                logger.error("Export error: %s", exc)
                if self._icon:
                    self._icon.notify(t("notif_export_fail", lang, error=str(exc)), "BUSSMA_TTS")

        threading.Thread(target=_run, name="CustomExcelExportWorker", daemon=True).start()

    def _do_csv_export(self, start_d: date, end_d: date) -> None:
        # BUG-07: run the export off the tray message loop.
        def _run() -> None:
            lang = self.config.language
            self._warn_if_documents_synced()
            try:
                today = date.today()
                half_days = {today} if (self.tracker.is_half_day_today and start_d <= today <= end_d) else None
                files = export_csv_bundle(
                    self.db, self.config, start_d, end_d,
                    open_folder=True, half_day_dates=half_days,
                    excel_safe=getattr(self.config, "csv_excel_safe", True),
                )
                if self._icon:
                    self._icon.notify(t("export_csv_success", lang, count=len(files)), "BUSSMA_TTS")
            except Exception as exc:
                logger.error("CSV Export error: %s", exc)
                if self._icon:
                    self._icon.notify(t("export_csv_fail", lang, error=str(exc)), "BUSSMA_TTS")

        threading.Thread(target=_run, name="CsvExportWorker", daemon=True).start()

    def _export_csv_range(self, range_key: str) -> None:
        today = date.today()
        if range_key == "week":
            start_d = today - timedelta(days=today.weekday())
            end_d = today
        elif range_key == "month":
            start_d, end_d = today.replace(day=1), today
        else:
            start_d, end_d = today, today
        self._do_csv_export(start_d, end_d)

    def _export_csv_custom_range(self) -> None:
        today = date.today()
        show_date_range_picker_dialog_async(
            on_submit=self._do_csv_export,
            lang=self.config.language,
            default_start=today.replace(day=1),
            default_end=today,
        )

    def _check_update_manual(self) -> None:
        """Triggered when the user clicks 'Check for Updates' in tray menu."""
        lang = self.config.language
        if self._icon:
            try:
                self._icon.notify(t("update_checking", lang), "BUSSMA_TTS")
            except Exception:
                pass

        def _worker() -> None:
            source = (getattr(self.config, "update_source_path", "") or "").strip()
            if not source:
                from config import DEFAULT_CONFIG
                source = DEFAULT_CONFIG.get("update_source_path", "")

            error_reason: list[str | None] = [None]

            def on_reject(reason: str) -> None:
                error_reason[0] = reason

            try:
                info = check_for_update(source, APP_VERSION, on_reject=on_reject)
            except Exception as exc:
                logger.warning("Manual update check failed: %s", exc)
                info = None
                error_reason[0] = str(exc)

            if info:
                self._on_update_available(info, is_manual=True)
            elif error_reason[0]:
                show_info_dialog_async(
                    title=t("update_check_title", lang),
                    message=t("update_check_fail_msg", lang),
                    lang=lang,
                    is_success=False,
                )
            else:
                show_info_dialog_async(
                    title=t("update_check_title", lang),
                    message=t("update_up_to_date_msg", lang, version=APP_VERSION),
                    lang=lang,
                    is_success=True,
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_update_available(self, info: UpdateInfo, is_manual: bool = False) -> None:
        """Offer to run or download the newer installer."""
        lang = self.config.language
        today_str = datetime.now().date().isoformat()

        def on_later() -> None:
            # Snooze for today: next day auto-checker will ask again
            try:
                self.db.set_setting("last_update_declined_date", today_str)
            except Exception as exc:
                logger.debug("Could not store update decline date: %s", exc)

        def on_install_ready(installer_path: Path) -> None:
            if not launch_installer(
                installer_path,
                info.sha256,
                required_publisher=getattr(self.config, "required_update_publisher", ""),
                prompt_after_install=True,
                language=lang,
            ):
                if self._icon:
                    self._icon.notify(t("update_launch_fail", lang), "BUSSMA_TTS")
                return
            logger.info("Installer launched for version %s; exiting.", info.version)
            self._quit()

        def on_click_install() -> None:
            if info.download_url:
                show_download_progress_dialog(info, on_install_ready, lang=lang)
            elif info.installer_path:
                on_install_ready(info.installer_path)

        show_update_dialog_async(info.version, info.notes, on_click_install, on_later=on_later, lang=lang)

    def _open_data_folder(self) -> None:
        try:
            os.startfile(get_app_dir())
        except Exception as exc:
            logger.warning("Error opening data directory: %s", exc)

    def _open_log_folder(self) -> None:
        """Reveal the log files from the tray menu."""
        logger.info("Opening the log folder from the tray menu.")
        if not open_log_folder():
            self._on_notification(
                t("notif_open_logs_failed", self.config.language, path=get_log_dir()),
                "BUSSMA_TTS",
            )

    def _create_diagnostics(self) -> None:
        """Zip logs + a system report in the background and reveal the result."""
        lang = self.config.language

        def _worker() -> None:
            extra: dict[str, object] = {}
            try:
                extra["current_status"] = self.tracker.current_status
                extra["current_project"] = self.tracker.current_project_name or ""
                extra["database"] = str(self.db.db_path)
            except Exception:
                logger.debug("Could not collect tracker state for diagnostics", exc_info=True)

            try:
                bundle = create_diagnostics_bundle(extra)
            except Exception as exc:
                logger.error("Diagnostics bundle failed", exc_info=True)
                self._on_notification(
                    t("notif_diagnostics_failed", lang, error=describe_exception(exc)),
                    "BUSSMA_TTS",
                )
                return

            try:
                os.startfile(str(bundle.parent))
            except Exception:
                logger.debug("Could not open the diagnostics folder", exc_info=True)

            self._on_notification(
                t("notif_diagnostics_saved", lang, filename=bundle.name), "BUSSMA_TTS"
            )

        threading.Thread(target=_worker, name="Diagnostics", daemon=True).start()

    def _show_about(self) -> None:
        show_about_dialog_async(version=APP_VERSION, lang=self.config.language)

    def _quit(self) -> None:
        logger.info("Quit requested via tray menu.")
        self.tracker.stop()
        if self._icon:
            self._icon.stop()

    def _hook_icon_menu_events(self) -> None:
        """Safely intercept right-clicks to refresh the menu before it opens
        and mark it as open so background metric ticks don't destroy its Win32 handle.
        """
        if not self._icon or sys.platform != "win32":
            return
        try:
            from pystray._util import win32
            orig_on_notify = self._icon._on_notify

            def safe_on_notify(wparam, lparam):
                if lparam == win32.WM_RBUTTONUP:
                    # User clicked to open menu: rebuild menu freshly before TrackPopupMenuEx runs
                    try:
                        self._icon.menu = self._build_menu()
                    except Exception as exc:
                        logger.warning("Error building menu on right-click: %s", exc)
                    self._is_menu_open = True
                    try:
                        return orig_on_notify(wparam, lparam)
                    finally:
                        self._is_menu_open = False
                return orig_on_notify(wparam, lparam)

            self._icon._message_handlers[win32.WM_NOTIFY] = safe_on_notify
            self._menu_hook_active = True
        except Exception as exc:
            self._menu_hook_active = False
            logger.warning(
                "Could not hook tray icon menu events (%s); falling back to plain menu rebuilds.",
                exc,
            )

    def run(self) -> None:
        initial_img = self._get_icon_image("Active")
        initial_menu = self._build_menu()
        initial_tooltip = self._format_tooltip()

        self._icon = pystray.Icon(
            name="TimeTracker",
            icon=initial_img,
            title=initial_tooltip,
            menu=initial_menu,
        )

        self._hook_icon_menu_events()

        logger.info("TrayApp starting event loop.")
        self._icon.run()
