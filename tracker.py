"""Core Tracker engine for TimeTracker.

Monitors user activity using Windows GetLastInputInfo, manages meeting, break,
and overtime sessions, handles pause mode, half-day mode, end-of-day summary notifications,
long meeting reminders, health stretch alerts, and automated weekly exports/backups.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from calculator import (
    AppUsageItem,
    DayMetrics,
    calculate_day_metrics,
    format_duration_hm,
    get_day_boundaries,
    half_day_target_hours,
    presence_credit_intervals,
)
from command_bus import CommandBus
from config import APP_VERSION, Config
from database import Database
from dialogs import (
    is_dialog_showing,
    show_active_check_dialog_async,
    show_meeting_end_note_dialog_async,
    show_overtime_prompt_async,
    show_smart_meeting_prompt_async,
)
from i18n import t
from retention import (
    active_check_interval_seconds,
    bucket_records_by_day,
    get_day_app_usage,
    get_day_metrics,
    metrics_from_summary,
    purge_old_data,
)
from updater import UpdateInfo, check_for_update

logger = logging.getLogger(__name__)

# Named constants (QUAL-06) instead of inline magic numbers.
PAUSE_UNTIL_RESUMED_SECONDS = 86400 * 30  # ~30 days, effectively "until resumed"
SLEEP_WAKE_THRESHOLD_SECONDS = 300.0
WORKSTATION_LOCK_IDLE_FLOOR_SECONDS = 1800.0
ACTIVE_CHECK_TIMEOUT_SECONDS = 60
GRACE_PERIOD_DEFAULT_SECONDS = 180.0


def _command(method):
    """Execute a tracker method on the single command-bus owner thread.

    Re-entrant calls (a handler calling another handler) run inline because the
    bus recognises its owner; calls made while the bus is not running also run
    inline, which is how the unit tests drive the tracker directly.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        return self._bus.invoke(method, self, *args, **kwargs)

    return wrapper


# Windows API structures
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),
    ]


# Win32 prototypes are declared explicitly (P1-7). Without argtypes/restype the
# default c_int return type truncates 64-bit HANDLEs, so QueryFullProcessImageNameW
# fails ("Unknown" app names) and CloseHandle leaks a handle per call.
_IS_WINDOWS = sys.platform == "win32"
_user32: Any = None
_kernel32: Any = None

if _IS_WINDOWS:
    try:
        from ctypes import wintypes

        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        _user32.GetLastInputInfo.restype = wintypes.BOOL

        _user32.GetForegroundWindow.argtypes = []
        _user32.GetForegroundWindow.restype = wintypes.HWND

        _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _user32.GetWindowTextW.restype = ctypes.c_int

        _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        _user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _user32.OpenInputDesktop.restype = wintypes.HANDLE
        _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        _user32.CloseDesktop.restype = wintypes.BOOL

        _kernel32.GetTickCount.argtypes = []
        _kernel32.GetTickCount.restype = wintypes.DWORD

        _kernel32.GetTickCount64.argtypes = []
        _kernel32.GetTickCount64.restype = ctypes.c_ulonglong

        _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _kernel32.OpenProcess.restype = wintypes.HANDLE

        _kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
    except Exception:  # pragma: no cover - non-Windows or restricted runtime
        _user32 = None
        _kernel32 = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_READOBJECTS = 0x0001


def get_last_input_tick() -> int | None:
    """Return GetLastInputInfo dwTime in milliseconds, or None when unavailable.

    ``None`` means "unknown" (P1-7): callers must skip the tick instead of
    assuming the machine has been idle since boot.
    """
    if _user32 is None:
        return None
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if _user32.GetLastInputInfo(ctypes.byref(lii)):
        return int(lii.dwTime)
    return None


def get_system_tick_count() -> int:
    """Return system tick count in milliseconds (32-bit DWORD matching GetLastInputInfo)."""
    if _kernel32 is None:
        return 0
    try:
        if hasattr(_kernel32, "GetTickCount"):
            return int(_kernel32.GetTickCount())
        return int(_kernel32.GetTickCount64() & 0xFFFFFFFF)
    except Exception:
        return 0


def get_active_window_title(hwnd: int | None = None) -> str:
    """Return the title text of the foreground window (or given HWND)."""
    if _user32 is None:
        return ""
    try:
        target_hwnd = hwnd or _user32.GetForegroundWindow()
        if not target_hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        length = _user32.GetWindowTextW(target_hwnd, buf, 512)
        if length > 0:
            return buf.value
        return ""
    except Exception:
        return ""


# Cache active foreground process to avoid high-frequency OpenProcess/CloseHandle polling
_last_foreground_hwnd: int | None = None
_last_foreground_app: str = "Unknown"
_app_name_cache_lock = threading.Lock()


def reset_active_app_cache() -> None:
    """Reset the cached foreground window HWND and application name."""
    global _last_foreground_hwnd, _last_foreground_app
    with _app_name_cache_lock:
        _last_foreground_hwnd = None
        _last_foreground_app = "Unknown"


def get_active_application_name() -> str:
    """Return the executable name of the current foreground window.

    Caches the result by foreground window handle (HWND) to prevent frequent
    expensive OpenProcess/CloseHandle calls during polling on low-end systems.
    """
    global _last_foreground_hwnd, _last_foreground_app
    if _user32 is None or _kernel32 is None:
        return "Unknown"
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            with _app_name_cache_lock:
                _last_foreground_hwnd = None
                _last_foreground_app = "Idle / System"
            return "Idle / System"

        with _app_name_cache_lock:
            if hwnd == _last_foreground_hwnd and _last_foreground_app != "Unknown":
                return _last_foreground_app

        pid = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            with _app_name_cache_lock:
                _last_foreground_hwnd = hwnd
                _last_foreground_app = "Idle / System"
            return "Idle / System"

        h_process = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_process:
            with _app_name_cache_lock:
                _last_foreground_hwnd = hwnd
                _last_foreground_app = "Unknown"
            return "Unknown"

        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            app_name = "Unknown"
            if _kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size)):
                app_name = Path(buf.value).name
            with _app_name_cache_lock:
                _last_foreground_hwnd = hwnd
                _last_foreground_app = app_name
            return app_name
        finally:
            _kernel32.CloseHandle(h_process)
    except Exception:
        return "Unknown"


# Known meeting desktop process names (lowercase)
MEETING_APP_PROCESSES = {
    "zoom.exe",
    "teams.exe",
    "msteams.exe",
    "slack.exe",
    "webex.exe",
    "webexmta.exe",
    "ciscowebexstart.exe",
    "skype.exe",
    "discord.exe",
}

# Browser titles identifying online meetings
MEETING_TITLE_KEYWORDS = [
    "google meet",
    "meet.google.com",
    "meet –",
    "meet -",
    "meet:",
    "meet |",
    "zoom meeting",
    "zoom workplace",
    "microsoft teams meeting",
    "teams meeting",
    "webex meeting",
]

BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}


def is_workstation_locked() -> bool:
    """Return True if the Windows workstation/desktop session is locked (Win+L / Screen Saver)."""
    if _user32 is None:
        return False
    h_desk = None
    try:
        h_desk = _user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        return not h_desk
    except Exception:
        return False
    finally:
        if h_desk:
            try:
                _user32.CloseDesktop(h_desk)
            except Exception:
                pass


class ActivityTracker:
    def __init__(
        self,
        db: Database,
        config: Config,
        show_active_dialog_fn: Callable[..., Any] | None = None,
        get_last_input_tick_fn: Callable[[], int] | None = None,
        get_system_tick_count_fn: Callable[[], int] | None = None,
        is_locked_fn: Callable[[], bool] | None = None,
        time_fn: Callable[[], float] | None = None,
        now_dt_fn: Callable[[], datetime] | None = None,
        get_active_app_fn: Callable[[], str] | None = None,
        get_foreground_hwnd_fn: Callable[[], int | None] | None = None,
        get_window_title_fn: Callable[[int | None], str] | None = None,
        show_smart_meeting_prompt_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.db = db
        self.config = config

        self._running = False
        # Single writer: all state mutations run on the command bus worker.
        self._bus = CommandBus(tick_fn=self._tick_safe, get_interval=self._get_tick_interval)

        # Injected helpers for testing
        self._show_active_dialog_fn = show_active_dialog_fn or show_active_check_dialog_async
        self._get_last_input_tick = get_last_input_tick_fn or get_last_input_tick
        self._get_system_tick_count = get_system_tick_count_fn or get_system_tick_count
        self._is_workstation_locked = is_locked_fn or is_workstation_locked
        self._time_fn = time_fn or time.time
        self._now_dt_fn = now_dt_fn or datetime.now
        self._get_active_app_fn = get_active_app_fn or get_active_application_name
        self._get_foreground_hwnd_fn = get_foreground_hwnd_fn
        self._get_window_title_fn = get_window_title_fn or get_active_window_title
        self._show_smart_meeting_prompt_fn = show_smart_meeting_prompt_fn or show_smart_meeting_prompt_async

        # Window polling caching (Proposal 1)
        self._last_hwnd: int | None = None
        self._last_app_name: str = "Unknown"
        self._last_window_title: str = ""

        # Smart meeting detection state (Proposal 4)
        self._last_meeting_suggestion_ts: float = 0.0

        # Input tracking state
        self._last_dw_time: int = 0
        self._last_input_ts: int = int(self._time_fn())
        self._prev_tick_wall_clock: float = self._time_fn()
        self._last_heartbeat_ts: float = 0.0
        self._idle_seconds: float = 0.0
        self._continuous_active_seconds: float = 0.0

        # Active Check (Cattr-style) state
        self._active_check_cycle_start_ts: float = self._time_fn()
        self._active_check_popup_showing: bool = False
        self._active_check_popup_started_ts: float = 0.0
        self._active_check_idle_mode: bool = False

        # Overtime & Day state machine
        self._overtime_declined: bool = False
        self._had_30m_idle_since_decline: bool = False
        self._prev_was_in_work_window: bool = True
        self._current_date: date = date.today()

        # New Features: Pause, Half-day, Reminders
        self._pause_until_ts: int | None = None
        self._half_day_date: date | None = None
        self._last_eod_notified_date: date | None = None
        self._last_meeting_reminder_ts: int = 0
        self._last_stretch_reminder_ts: int = 0

        # Project attribution (optional tags on tracked time)
        self._current_project_id: int | None = self._load_current_project()

        # Metrics cache (P1-3): the day is recomputed at most every 30 s and the
        # week/month ranges at most once a minute, unless a mutation forces it.
        self._metrics_ts: float = 0.0
        self._period_totals_ts: float = 0.0
        self._last_event_ts: int = 0
        self.DAY_METRICS_MAX_AGE = 30.0
        self.PERIOD_TOTALS_MAX_AGE = 60.0

        # Metrics cache
        self._current_status: str = "Active"
        self._today_metrics: DayMetrics | None = None
        self._today_app_usage: list[AppUsageItem] = []
        self._week_worked_seconds: int = 0
        self._week_target_seconds: int = int(config.weekly_target_hours * 3600)
        self._month_worked_seconds: int = 0
        self._month_target_seconds: int = 0
        self._has_error: bool = False

        # Callbacks
        self.on_state_change: Callable[[], None] | None = None
        self.on_notification: Callable[[str, str], None] | None = None
        self.on_update_available: Callable[[UpdateInfo], None] | None = None

    # ---------------------------------------------------------
    # Properties & Status
    # ---------------------------------------------------------
    @property
    def current_status(self) -> str:
        return self._current_status

    @property
    def today_metrics(self) -> DayMetrics | None:
        return self._today_metrics

    @property
    def today_app_usage(self) -> list[AppUsageItem]:
        return self._today_app_usage

    @property
    def top_app_formatted(self) -> str:
        if self._today_app_usage:
            top = self._today_app_usage[0]
            return f"{top.app_name} ({top.formatted})"
        return "None"

    @property
    def week_summary_formatted(self) -> str:
        worked = format_duration_hm(self._week_worked_seconds)
        target = f"{int(self._week_target_seconds / 3600)}h"
        return f"{worked} / {target}"

    @property
    def month_summary_formatted(self) -> str:
        worked = format_duration_hm(self._month_worked_seconds)
        target = f"{int(self._month_target_seconds / 3600)}h" if self._month_target_seconds > 0 else "160h"
        return f"{worked} / {target}"

    @property
    def is_half_day_today(self) -> bool:
        return self._half_day_date == date.today()

    @property
    def half_day_target_hours(self) -> float:
        """Target used in half-day mode (half of the configured daily target)."""
        return half_day_target_hours(self.config.daily_target_hours)

    @property
    def has_error(self) -> bool:
        return self._has_error

    def _notify(self, key: str, **kwargs: Any) -> None:
        """Send a localized tray notification in the configured language."""
        if not self.on_notification:
            return
        lang = self.config.language
        try:
            self.on_notification(t(key, lang, **kwargs), t("app_short", lang))
        except Exception as exc:
            logger.error("Notification error: %s", exc)

    def is_paused(self) -> bool:
        """Pure check: is a pause window active right now?"""
        if self._pause_until_ts is None:
            return False
        return int(self._time_fn()) < self._pause_until_ts

    def _expire_pause(self) -> bool:
        """End an expired pause, closing only the pause session (P1-8)."""
        if self._pause_until_ts is None or int(self._time_fn()) < self._pause_until_ts:
            return False
        now_ts = int(self._time_fn())
        self._pause_until_ts = None
        open_break = self.db.get_open_session("break")
        if open_break and int(open_break.get("is_pause") or 0) == 1:
            self.db.end_session("break", now_ts)
        return True

    # ---------------------------------------------------------
    # Projects
    # ---------------------------------------------------------
    def _load_current_project(self) -> int | None:
        raw = self.db.get_setting("current_project_id")
        if not raw:
            return None
        try:
            project_id = int(raw)
        except (TypeError, ValueError):
            return None
        project = self.db.get_project(project_id)
        if not project or int(project.get("archived") or 0) == 1:
            return None
        return project_id

    @property
    def current_project_id(self) -> int | None:
        return self._current_project_id

    @property
    def current_project_name(self) -> str:
        project = self.db.get_project(self._current_project_id)
        return str(project["name"]) if project else ""

    @_command
    def set_current_project(self, project_id: int | None) -> None:
        """Select the project that subsequent active time is attributed to."""
        if project_id is not None:
            project = self.db.get_project(project_id)
            if not project or int(project.get("archived") or 0) == 1:
                project_id = None
        self._current_project_id = project_id
        if project_id is None:
            self.db.set_setting("current_project_id", "")
        else:
            self.db.set_setting("current_project_id", str(project_id))
        self._refresh_metrics(force=True)

    @_command
    def set_config(self, config: Config) -> None:
        """Apply a newly saved configuration on the owner thread."""
        self.config = config
        self._refresh_metrics(force=True)

    def is_meeting_active(self) -> bool:
        return self.db.get_open_session("meeting") is not None

    def is_break_active(self) -> bool:
        return self.db.get_open_session("break") is not None

    def is_overtime_active(self) -> bool:
        return self.db.get_open_session("overtime") is not None

    # ---------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return

        # 1. Clean up any orphan sessions left open from an unexpected shutdown/crash
        try:
            orphan_count = self.db.cleanup_orphan_sessions()
            if orphan_count > 0:
                logger.warning("Cleaned up %d orphan session(s) from previous unexpected shutdown.", orphan_count)
        except Exception as exc:
            logger.error("Failed to cleanup orphan sessions on startup: %s", exc)

        # 2. Enforce retention policy on startup (aggregate first, then purge)
        try:
            result = purge_old_data(self.db, self.config)
            if result["deleted"] or result["aggregated"]:
                logger.info(
                    "Retention cleanup: aggregated %d day(s), purged %d input event(s) before %s.",
                    result["aggregated"],
                    result["deleted"],
                    result["cutoff"],
                )
        except Exception as exc:
            logger.warning("Retention cleanup error on startup: %s", exc)

        # Initialise the tick baseline before the worker owns the state.
        self._init_tick_state()

        self._running = True
        # The bus worker is the single thread that ever mutates tracker state:
        # it drains commands and runs the periodic tick itself.
        self._bus.start(name="TrackerLoopThread")
        logger.info("ActivityTracker started.")

    def stop(self) -> None:
        self._running = False
        # The final state write is queued last, so it runs on the owner thread
        # after every pending command and before the worker exits.
        self._bus.stop(final_fn=self._finalize_stop)

    def _finalize_stop(self) -> None:
        """Close open sessions on the owner thread (P1-10)."""
        close_ts = int(self._time_fn())
        closed_count = self.db.close_all_open_sessions(close_ts, auto_end_ts=self._last_input_ts)
        logger.info("Tracker stopped. Closed %d open sessions.", closed_count)

    def _get_tick_interval(self) -> float:
        """Current polling interval, read live so Settings changes apply at once."""
        try:
            return float(self.config.tick_interval_seconds)
        except (TypeError, ValueError):
            return 5.0

    def _init_tick_state(self) -> None:
        """Seed the state machine's per-run baselines (old _loop() preamble)."""
        self._last_dw_time = self._get_last_input_tick()
        self._prev_tick_wall_clock = self._time_fn()
        now_dt = self._now_dt_fn()
        self._current_date = now_dt.date()

        is_holiday = now_dt.strftime("%Y-%m-%d") in self.config.holidays
        is_work_day = (now_dt.strftime("%a") in self.config.work_days) and not is_holiday
        now_time = now_dt.time()
        self._prev_was_in_work_window = (
            is_work_day and (self.config.work_start_time <= now_time < self.config.work_end_time)
        )

    # ---------------------------------------------------------
    # Actions & Controls
    # ---------------------------------------------------------
    @_command
    def toggle_pause(self, minutes: int = 30) -> bool:
        """Pause or unpause tracking."""
        now_ts = int(self._time_fn())
        if self.is_paused():
            self._pause_until_ts = None
            open_break = self.db.get_open_session("break")
            if open_break and int(open_break.get("is_pause") or 0) == 1:
                self.db.end_session("break", now_ts)
            self._notify("notif_pause_resumed")
            self._refresh_metrics(force=True)
            return False

        # A pause must not be layered over a real break session (P1-8).
        if self.is_break_active():
            self._notify("notif_pause_break_open")
            return False

        if minutes <= 0:
            self._pause_until_ts = now_ts + PAUSE_UNTIL_RESUMED_SECONDS  # Effectively until resumed
        else:
            self._pause_until_ts = now_ts + (minutes * 60)
        self.db.start_session(
            "break", "tray", now_ts,
            note="Personal Pause / إيقاف مؤقت",
            project_id=self._current_project_id,
            is_pause=True,
        )
        if minutes > 0:
            duration = t("pause_duration_minutes", self.config.language, minutes=minutes)
        else:
            duration = t("pause_duration_manual", self.config.language)
        self._notify("notif_pause_started", duration=duration)
        self._refresh_metrics(force=True)
        return True

    @_command
    def toggle_half_day(self) -> bool:
        """Toggle half-day mode for today (half of the configured target, P0-4)."""
        today = date.today()
        target = self.half_day_target_hours
        full = self.config.daily_target_hours
        if self._half_day_date == today:
            self._half_day_date = None
            self._notify("notif_half_day_off", hours=f"{full:g}")
            self._refresh_metrics(force=True)
            return False
        else:
            self._half_day_date = today
            self._notify("notif_half_day_on", hours=f"{target:g}")
            self._refresh_metrics(force=True)
            return True

    def get_current_active_window_info(self) -> tuple[str, str]:
        """Return cached (app_name, window_title) based on foreground window handle (HWND).
        
        If HWND matches the previously queried window, returns cached results instantly,
        avoiding expensive Win32 Process queries and reducing CPU load by up to 80%
        during long work sessions.
        """
        if self._get_foreground_hwnd_fn is not None:
            try:
                hwnd = self._get_foreground_hwnd_fn()
            except Exception:
                hwnd = None
        elif _user32 is not None:
            try:
                hwnd = _user32.GetForegroundWindow()
            except Exception:
                hwnd = None
        else:
            hwnd = None

        if hwnd is not None and hwnd == self._last_hwnd and self._last_app_name != "Unknown":
            return self._last_app_name, self._last_window_title

        app_name = self._get_active_app_fn()
        title = self._get_window_title_fn(hwnd) if hwnd else ""
        self._last_hwnd = hwnd
        self._last_app_name = app_name
        self._last_window_title = title
        return app_name, title

    def reset_window_cache(self) -> None:
        """Reset cached active window handle, app name, and title."""
        self._last_hwnd = None
        self._last_app_name = "Unknown"
        self._last_window_title = ""

    @_command
    def start_meeting(self, source: str = "tray", note: str = "") -> tuple[bool, int]:
        """Start a new meeting session (enabling Do Not Disturb mode).
        
        Returns (started: bool, session_start_ts: int).
        """
        open_meeting = self.db.get_open_session("meeting")
        now_ts = int(self._time_fn())
        if open_meeting:
            return False, 0
        safe_source = source if source in ("auto", "tray", "manual") else "auto"
        sid = self.db.start_session("meeting", safe_source, now_ts, project_id=self._current_project_id)
        if note and sid:
            try:
                self.db.update_session_note_or_reason(sid, note=note)
            except Exception as exc:
                logger.warning("Could not save meeting note for session %s: %s", sid, exc)
        self._notify("notif_meeting_started")
        if self._outside_paid_hours(self._now_dt_fn()):
            self._prompt_overtime_for_meeting(now_ts)
        self._refresh_metrics(force=True)
        return True, now_ts

    def _evaluate_smart_meeting_detection(self, active_app: str, win_title: str, now_ts: int) -> None:
        """Inspect foreground app & window title for meetings and prompt user with 1-click start."""
        cooldown_sec = getattr(self.config, "smart_meeting_cooldown_minutes", 20) * 60
        if (now_ts - self._last_meeting_suggestion_ts) < cooldown_sec:
            return

        app_lower = (active_app or "").lower()
        title_lower = (win_title or "").lower()

        detected_label: str | None = None
        if app_lower in MEETING_APP_PROCESSES:
            detected_label = app_lower.replace(".exe", "").capitalize()
            if detected_label.lower() == "msteams":
                detected_label = "Microsoft Teams"
        elif app_lower in BROWSER_PROCESSES:
            for kw in MEETING_TITLE_KEYWORDS:
                if kw in title_lower:
                    detected_label = "Google Meet" if "meet" in kw else "Online Meeting"
                    break

        if detected_label:
            self._last_meeting_suggestion_ts = now_ts
            logger.info("Smart Meeting detected: %s (app=%s, title=%s)", detected_label, active_app, win_title)

            def _on_start() -> None:
                self.start_meeting(source="auto", note=f"Auto-detected {detected_label}")

            def _on_ignore() -> None:
                pass

            try:
                self._show_smart_meeting_prompt_fn(
                    detected_label,
                    _on_start,
                    _on_ignore,
                    lang=self.config.language,
                )
            except Exception as exc:
                logger.warning("Could not display smart meeting prompt: %s", exc)

    @_command
    def toggle_meeting(self) -> tuple[bool, int]:
        open_meeting = self.db.get_open_session("meeting")
        now_ts = int(time.time())

        if open_meeting:
            closed = self.db.end_session("meeting", now_ts)
            dur = closed[0]["duration"] if closed else 0
            dur_min = max(1, dur // 60)
            sid = closed[0]["id"] if closed else None

            # Prompt for optional note
            duration_text = format_duration_hm(max(60, dur))
            if sid:
                def on_note(note_text: str) -> None:
                    self._apply_meeting_note(sid, note_text, duration_text)

                show_meeting_end_note_dialog_async(dur_min, on_note, lang=self.config.language)
            else:
                self._notify("notif_meeting_ended", duration=duration_text)

            self._refresh_metrics(force=True)
            return False, dur
        else:
            return self.start_meeting(source="tray")

    @_command
    def _apply_meeting_note(self, sid: int, note_text: str, duration_text: str) -> None:
        """Persist the optional meeting-end note on the owner thread."""
        if note_text:
            self.db.update_session_note_or_reason(sid, note=note_text)
            self._notify("notif_meeting_ended_note", duration=duration_text, note=note_text)
        else:
            self._notify("notif_meeting_ended", duration=duration_text)

    @_command
    def toggle_break(self) -> tuple[bool, int]:
        open_break = self.db.get_open_session("break")
        now_ts = int(time.time())

        if open_break:
            closed = self.db.end_session("break", now_ts)
            dur = closed[0]["duration"] if closed else 0
            self._notify("notif_break_ended", duration=format_duration_hm(max(60, dur)))
            self._refresh_metrics(force=True)
            return False, dur
        else:
            self.db.start_session("break", "tray", now_ts, project_id=self._current_project_id)
            self._notify("notif_break_started")
            self._refresh_metrics(force=True)
            return True, 0

    @_command
    def toggle_overtime(self) -> bool:
        open_ot = self.db.get_open_session("overtime")
        now_ts = int(time.time())

        if open_ot:
            self.db.end_session("overtime", now_ts)
            self._overtime_declined = False
            self._refresh_metrics(force=True)
            return False
        else:
            self.db.start_session(
                "overtime", "tray", now_ts,
                reason="Manual Tray",
                project_id=self._current_project_id,
            )
            self._overtime_declined = False
            self._refresh_metrics(force=True)
            return True

    def _outside_paid_hours(self, now_dt: datetime) -> bool:
        """True on a non-work day/holiday or outside the working window (D3)."""
        day_str = now_dt.strftime("%Y-%m-%d")
        is_holiday = day_str in self.config.holidays
        is_work_day = (now_dt.strftime("%a") in self.config.work_days) and not is_holiday
        now_time = now_dt.time()
        return not (
            is_work_day
            and (self.config.work_start_time <= now_time < self.config.work_end_time)
        )

    def _prompt_overtime_for_meeting(self, meeting_start_ts: int) -> None:
        """Ask once whether a meeting outside paid hours is Overtime (D3).

        Accepting opens an overtime session that starts with the meeting; declining
        keeps the meeting as Meeting only and says so, so nothing is silently
        booked as Worked.
        """
        if self.db.get_open_session("overtime") is not None:
            return

        def on_answer(accepted: bool, reason: str) -> None:
            self._apply_meeting_overtime_answer(meeting_start_ts, accepted, reason)

        show_overtime_prompt_async(
            "This meeting is outside your working hours. Record it as Overtime?",
            on_answer,
            lang=self.config.language,
            timeout_seconds=int(getattr(self.config, "overtime_prompt_timeout_seconds", 90)),
        )

    @_command
    def _apply_meeting_overtime_answer(
        self, meeting_start_ts: int, accepted: bool, reason: str
    ) -> None:
        """Apply the overtime answer for an out-of-hours meeting on the owner thread."""
        if accepted:
            self.db.start_session(
                "overtime",
                "auto",
                meeting_start_ts,
                reason=reason or "Meeting",
                project_id=self._current_project_id,
            )
            self._overtime_declined = False
            self._notify("notif_meeting_overtime_accepted")
        else:
            self._overtime_declined = True
            self._notify("notif_meeting_overtime_declined")
        self._refresh_metrics(force=True)

    @_command
    def add_past_meeting(
        self,
        d_str: str,
        start_str: str,
        end_str: str,
        note: str = "",
        count_as_overtime: bool = False,
        reason: str = "",
    ) -> None:
        """Add a retroactive meeting session (optionally with matching overtime)."""
        st_dt = datetime.strptime(f"{d_str} {start_str}", "%Y-%m-%d %H:%M")
        et_dt = datetime.strptime(f"{d_str} {end_str}", "%Y-%m-%d %H:%M")
        st = int(st_dt.timestamp())
        et = int(et_dt.timestamp())

        self.db.add_past_session(
            session_type="meeting",
            start_ts=st,
            end_ts=et,
            source="manual",
            note=note,
            project_id=self._current_project_id,
        )
        if count_as_overtime:
            self.db.add_past_session(
                session_type="overtime",
                start_ts=st,
                end_ts=et,
                source="manual",
                reason=reason or "Past meeting",
                project_id=self._current_project_id,
            )
        self._notify("notif_past_meeting_added", start=start_str, end=end_str)
        self._refresh_metrics(force=True)

    # ---------------------------------------------------------
    # Main Polling Loop
    # ---------------------------------------------------------
    def _tick_safe(self) -> None:
        """Tick wrapper used by the bus worker; preserves the Error status."""
        try:
            self._tick()
            self._has_error = False
        except Exception as exc:
            logger.error("Exception in tracker tick: %s", exc, exc_info=True)
            self._has_error = True

    def _tick(self, now_time_wall: float | None = None, now_dt: datetime | None = None) -> None:
        if now_time_wall is None:
            now_time_wall = self._time_fn()
        now_ts = int(now_time_wall)
        if now_dt is None:
            now_dt = self._now_dt_fn()

        # 1. Sleep/Suspend Check (> 5 min)
        wall_elapsed = now_time_wall - self._prev_tick_wall_clock
        if wall_elapsed > SLEEP_WAKE_THRESHOLD_SECONDS:
            logger.warning("Clock jump / wake from sleep detected (%.1f s).", wall_elapsed)
            open_ot = self.db.get_open_session("overtime")
            if open_ot and open_ot.get("source") == "auto":
                self.db.end_session("overtime", self._last_input_ts)
            self._continuous_active_seconds = 0.0
            # Decisons 21.4: a wake must also restart the presence-check cycle so
            # no popup appears before the user has had a chance to return.
            self._active_check_cycle_start_ts = now_time_wall
            self._active_check_idle_mode = False
            self._active_check_popup_showing = False
            self._active_check_popup_started_ts = 0.0
            # Foreground window may have changed while asleep; invalidate the HWND cache.
            reset_active_app_cache()

        self._prev_tick_wall_clock = now_time_wall

        # 2. Day Change
        today = now_dt.date()
        if today != self._current_date:
            self._current_date = today
            self._overtime_declined = False
            self._half_day_date = None
            self._active_check_cycle_start_ts = now_time_wall
            self._active_check_idle_mode = False
            self._active_check_popup_showing = False
            self._active_check_popup_started_ts = 0.0
            open_ot = self.db.get_open_session("overtime")
            if open_ot and open_ot.get("source") == "auto":
                self.db.end_session("overtime", self._last_input_ts)
            # Reset app cache at day boundary — a new day likely starts with a
            # different foreground window than the one cached from the previous day.
            reset_active_app_cache()

            # Item 1b: aggregate the finished day in the background so the
            # dashboard never reads an empty day for a day that had data.
            def _aggregate_after_midnight() -> None:
                try:
                    from retention import aggregate_missing_days

                    aggregate_missing_days(self.db, self.config, today=today)
                except Exception as exc:
                    logger.error("Post-midnight aggregation failed: %s", exc)

            threading.Thread(
                target=_aggregate_after_midnight,
                name="DayChangeAggregation",
                daemon=True,
            ).start()

        # 3. Input Polling. An unavailable idle timer is "unknown", never "idle
        # since boot" (P1-7), so the input branch is skipped for this tick.
        dw_time = self._get_last_input_tick()
        input_known = dw_time is not None
        if input_known:
            sys_ticks = self._get_system_tick_count()
            # Unsigned 32-bit modulo arithmetic handles wrap-around after ~49.7 days.
            idle_ms = (int(sys_ticks) - int(dw_time)) & 0xFFFFFFFF
            # In race conditions or clock sampling anomalies where dw_time is sampled
            # slightly ahead of sys_ticks, unsigned subtraction wraps to ~0xFFFFFFFF (~49.7 days).
            # Any difference greater than 0x7FFFFFFF (~24.8 days) indicates dw_time > sys_ticks.
            if idle_ms > 0x7FFFFFFF:
                idle_ms = 0
            self._idle_seconds = idle_ms / 1000.0
            is_new_input = (dw_time != self._last_dw_time)
        else:
            is_new_input = False

        # Check workstation lock state (Win + L)
        locked = self._is_workstation_locked()
        if locked:
            self._idle_seconds = max(self._idle_seconds, WORKSTATION_LOCK_IDLE_FLOOR_SECONDS)
            self._continuous_active_seconds = 0.0
            open_ot = self.db.get_open_session("overtime")
            if open_ot and open_ot.get("source") == "auto":
                self.db.end_session("overtime", self._last_input_ts)
            is_new_input = False

        # If not paused and workstation not locked, record input
        if input_known and not self.is_paused() and not locked:
            if is_new_input:
                self._last_dw_time = int(dw_time)
                event_ts = self._event_timestamp(now_ts)
                self._last_input_ts = event_ts
                if getattr(self.config, "enable_app_tracking", True):
                    active_app, win_title = self.get_current_active_window_info()
                else:
                    active_app, win_title = "Disabled", ""
                self.db.record_input_event(
                    event_ts,
                    app_name=active_app,
                    source="input",
                    project_id=self._current_project_id,
                )
                if self._active_check_idle_mode:
                    self._active_check_idle_mode = False
                    self._active_check_cycle_start_ts = now_time_wall

                if getattr(self.config, "enable_smart_meeting_detection", True) and not self.is_meeting_active():
                    self._evaluate_smart_meeting_detection(active_app, win_title, now_ts)

            # Continuous activity accumulation for health reminder
            if self._idle_seconds < self.config.grace_period_seconds:
                self._continuous_active_seconds += self.config.tick_interval_seconds
            else:
                self._continuous_active_seconds = 0.0
        else:
            self._continuous_active_seconds = 0.0

        # 4. 30-Minute Idle Handling
        if self._idle_seconds >= WORKSTATION_LOCK_IDLE_FLOOR_SECONDS:
            open_ot = self.db.get_open_session("overtime")
            if open_ot and open_ot.get("source") == "auto":
                self.db.end_session("overtime", self._last_input_ts)
            if self._overtime_declined:
                self._had_30m_idle_since_decline = True
        else:
            if self._had_30m_idle_since_decline and is_new_input:
                self._overtime_declined = False
                self._had_30m_idle_since_decline = False

        # 5. Overtime Prompts
        if not self.is_paused():
            self._evaluate_overtime_triggers(now_dt, now_ts, is_new_input)

        # 5b. Active Check (Cattr-style)
        self._evaluate_active_check(now_dt, now_time_wall)

        # 6. Check Smart Reminders (End of Day, Meeting reminder, Health reminder)
        self._check_reminders(now_dt, now_ts)

        # 7. Check Automated Weekly Backups & Exports
        self._check_weekly_automations(now_dt)

        # 7b. Local (offline) auto-update check, once per day
        self._check_for_update(now_dt)

        # 8. Record Heartbeat (every 60s) for crash recovery
        if (now_time_wall - self._last_heartbeat_ts) >= 60.0:
            self._last_heartbeat_ts = now_time_wall
            try:
                self.db.record_heartbeat(now_ts)
            except Exception as exc:
                logger.debug("Failed to record heartbeat: %s", exc)

        # 9. Refresh Metrics
        self._refresh_metrics()

    def _event_timestamp(self, now_ts: int) -> int:
        """Timestamp of the input itself, not of the tick (B14).

        ``now - idle`` recovers the real input moment; the result is kept
        strictly increasing so two events never collide on the timestamp
        primary key.
        """
        candidate = now_ts - max(0, int(self._idle_seconds))
        if self._last_event_ts and candidate <= self._last_event_ts:
            candidate = self._last_event_ts + 1
        if candidate > now_ts:
            candidate = now_ts
        self._last_event_ts = candidate
        return candidate

    @_command
    def _apply_overtime_answer(self, accepted: bool, reason: str) -> None:
        """Apply an overtime prompt answer on the owner thread."""
        if accepted:
            self.db.start_session(
                "overtime", "auto", int(time.time()),
                reason=reason,
                project_id=self._current_project_id,
            )
            self._overtime_declined = False
        else:
            self._overtime_declined = True
            self._had_30m_idle_since_decline = False
        self._refresh_metrics(force=True)

    def _evaluate_overtime_triggers(self, now_dt: datetime, now_ts: int, is_new_input: bool) -> None:
        day_str = now_dt.strftime("%Y-%m-%d")
        is_holiday = day_str in self.config.holidays
        day_name = now_dt.strftime("%a")
        is_work_day = (day_name in self.config.work_days) and not is_holiday
        now_time = now_dt.time()

        is_in_work_window = (
            is_work_day and (self.config.work_start_time <= now_time < self.config.work_end_time)
        )

        user_is_active = self._idle_seconds < self.config.grace_period_seconds
        has_open_ot = self.db.get_open_session("overtime") is not None

        # Outside work window prompt
        if not is_in_work_window:
            if user_is_active and not has_open_ot and not self._overtime_declined and not is_dialog_showing():
                def on_answer(accepted: bool, reason: str) -> None:
                    self._apply_overtime_answer(accepted, reason)

                show_overtime_prompt_async(
                    "You're outside working hours. Record this as Overtime?",
                    on_answer,
                    lang=self.config.language,
                    timeout_seconds=int(
                        getattr(self.config, "overtime_prompt_timeout_seconds", 90)
                    ),
                )

        # Work hours ended prompt
        if is_work_day and self._prev_was_in_work_window and (now_time >= self.config.work_end_time):
            if user_is_active and not has_open_ot and not self._overtime_declined and not is_dialog_showing():
                def on_answer_end(accepted: bool, reason: str) -> None:
                    self._apply_overtime_answer(accepted, reason)

                show_overtime_prompt_async(
                    "Working hours ended. Continue as Overtime?",
                    on_answer_end,
                    lang=self.config.language,
                    timeout_seconds=int(
                        getattr(self.config, "overtime_prompt_timeout_seconds", 90)
                    ),
                )

        self._prev_was_in_work_window = is_in_work_window

    @_command
    def record_active_confirmation(self, ts: int) -> None:
        """Record confirmed active presence as a synthetic event with source='confirm'.

        The confirmation event proves the user was present during the whole idle
        gap that preceded it.  ``calculator.presence_credit_intervals`` turns
        this record into a credited interval covering that gap (capped at the
        presence-check interval), so a confirmation credits the full period and
        not just the trailing grace.
        """
        if getattr(self.config, "enable_app_tracking", True):
            active_app, _ = self.get_current_active_window_info()
        else:
            active_app = "Disabled"
        self.db.record_input_event(
            ts,
            app_name=active_app,
            source="confirm",
            project_id=self._current_project_id,
        )
        self._last_input_ts = ts

    @_command
    def _apply_active_check_result(self, confirmed: bool) -> None:
        """Apply the presence-check outcome on the owner thread."""
        self._active_check_popup_showing = False
        self._active_check_popup_started_ts = 0.0
        if confirmed:
            logger.info("ActiveCheck: user confirmed presence — gap credited.")
            confirm_ts = int(self._time_fn())
            self.record_active_confirmation(confirm_ts)
            self._active_check_idle_mode = False
            self._active_check_cycle_start_ts = self._time_fn()
        else:
            logger.info("ActiveCheck: popup timed out with no response — entering idle mode.")
            self._active_check_idle_mode = True
        self._refresh_metrics(force=True)

    def _evaluate_active_check(self, now_dt: datetime, now_time_wall: float) -> None:
        """Evaluate whether to prompt user with Cattr-style 'Are you active?' presence check."""
        if not getattr(self.config, "active_check_enabled", True):
            self._active_check_cycle_start_ts = now_time_wall
            self._active_check_idle_mode = False
            return

        # BUG-02 watchdog: if a dialog neither raised nor called back (e.g. its
        # UI thread died mid-display), the flag would suppress every future
        # check. Resolve it as a timeout once the dialog window is long gone.
        if (
            self._active_check_popup_showing
            and self._active_check_popup_started_ts > 0
            and (now_time_wall - self._active_check_popup_started_ts)
            > ACTIVE_CHECK_TIMEOUT_SECONDS + 30
        ):
            logger.warning("ActiveCheck: stale popup detected; clearing via watchdog.")
            self._apply_active_check_result(False)

        # 1. Do NOT show during active Meeting
        if self.is_meeting_active():
            self._active_check_cycle_start_ts = now_time_wall
            return

        # 2. Do NOT show during Pause
        if self.is_paused():
            self._active_check_cycle_start_ts = now_time_wall
            return

        # 3. Do NOT show during declined-overtime state
        if self._overtime_declined:
            self._active_check_cycle_start_ts = now_time_wall
            return

        # 4. Do NOT show outside tracking hours when no overtime session is open
        day_str = now_dt.strftime("%Y-%m-%d")
        is_holiday = day_str in self.config.holidays
        day_name = now_dt.strftime("%a")
        is_work_day = (day_name in self.config.work_days) and not is_holiday
        now_time = now_dt.time()
        is_in_work_window = (
            is_work_day and (self.config.work_start_time <= now_time < self.config.work_end_time)
        )
        if not is_in_work_window and not self.is_overtime_active():
            self._active_check_cycle_start_ts = now_time_wall
            return

        # 5. Do NOT show if workstation is locked
        if self._is_workstation_locked():
            self._active_check_cycle_start_ts = now_time_wall
            return

        # 6. Do NOT show if another dialog (e.g. overtime prompt) is open
        if is_dialog_showing():
            return

        # 7. Already showing active check popup
        if self._active_check_popup_showing:
            return

        # 8. In idle mode awaiting user return
        if self._active_check_idle_mode:
            return

        interval_min = max(1, min(60, getattr(self.config, "active_check_interval_min", 3)))
        interval_sec = interval_min * 60.0

        elapsed = now_time_wall - self._active_check_cycle_start_ts
        if elapsed < interval_sec:
            return

        # Interval has elapsed! Check idle time via GetLastInputInfo
        if self._idle_seconds < interval_sec:
            # Real input occurred within the interval: no popup, time counts as active as usual.
            logger.info(
                "ActiveCheck: interval (%.0fs) elapsed but user is active (idle=%.0fs) — resetting cycle.",
                interval_sec, self._idle_seconds,
            )
            self._active_check_cycle_start_ts = now_time_wall
            return

        # No input during the interval: show popup!
        logger.info(
            "ActiveCheck: idle=%.0fs >= interval=%.0fs — showing presence check popup.",
            self._idle_seconds, interval_sec,
        )
        self._active_check_popup_showing = True
        self._active_check_popup_started_ts = now_time_wall

        def on_confirmed() -> None:
            self._apply_active_check_result(True)

        def on_timeout() -> None:
            # No response: nothing is credited for the idle gap.
            self._apply_active_check_result(False)

        try:
            self._show_active_dialog_fn(
                on_confirmed=on_confirmed,
                on_timeout=on_timeout,
                timeout_seconds=ACTIVE_CHECK_TIMEOUT_SECONDS,
                lang=self.config.language,
            )
        except Exception as exc:
            logger.error("Failed to show active check dialog: %s", exc)
            try:
                on_timeout()
            except Exception as cb_exc:
                logger.error("Error in active check timeout callback: %s", cb_exc)
                self._active_check_popup_showing = False

    def _check_reminders(self, now_dt: datetime, now_ts: int) -> None:
        today = now_dt.date()
        now_time = now_dt.time()
        lang = self.config.language

        # 1. End of Day Notification at work_end (e.g. 17:30)
        if self.config.enable_end_of_day_notification:
            is_work_day = (now_dt.strftime("%a") in self.config.work_days) and (now_dt.strftime("%Y-%m-%d") not in self.config.holidays)
            # Only inside a short window after the shift end (B18): starting the
            # app at 22:00 must not fire "Worked 0h 00m of 8h".
            shift_end = datetime.combine(today, self.config.work_end_time)
            if (
                is_work_day
                and shift_end <= now_dt <= shift_end + timedelta(minutes=30)
                and self._last_eod_notified_date != today
            ):
                self._last_eod_notified_date = today
                if self._today_metrics and self.on_notification:
                    target_h = int(self._today_metrics.target_hours)
                    msg = t(
                        "notif_eod_summary",
                        lang,
                        worked=self._today_metrics.worked_formatted,
                        target=target_h,
                        overtime=self._today_metrics.overtime_formatted,
                    )
                    title = "BUSSMA_TTS — " + ("ملخص نهاية اليوم" if lang == "ar" else "End of Day Summary")
                    self.on_notification(msg, title)

        # 2. Meeting Running > 2 Hours Reminder
        if self.config.enable_meeting_reminder:
            open_meet = self.db.get_open_session("meeting")
            if open_meet:
                dur_sec = now_ts - int(open_meet["start_ts"])
                threshold_sec = self.config.meeting_reminder_minutes * 60
                if dur_sec >= threshold_sec and (now_ts - self._last_meeting_reminder_ts > 1800):
                    self._last_meeting_reminder_ts = now_ts
                    if self.on_notification:
                        dur_text = f"{dur_sec // 3600}h"
                        msg = t("notif_meeting_reminder", lang, duration=dur_text)
                        title = "BUSSMA_TTS — " + ("تنبيه الاجتماع" if lang == "ar" else "Meeting Alert")
                        self.on_notification(msg, title)

        # 3. Continuous 2-Hour Work Reminder (Health Stretch)
        if self.config.enable_stretch_reminder:
            stretch_threshold_sec = self.config.stretch_reminder_minutes * 60
            if self._continuous_active_seconds >= stretch_threshold_sec and (now_ts - self._last_stretch_reminder_ts > 1800):
                self._last_stretch_reminder_ts = now_ts
                if self.on_notification:
                    msg = t("notif_stretch", lang)
                    title = "BUSSMA_TTS — " + ("تنبيه الراحة" if lang == "ar" else "Health Alert")
                    self.on_notification(msg, title)

    def _check_for_update(self, now_dt: datetime) -> None:
        """Check the configured update source (GitHub or local/network) at most once per day."""
        source = (getattr(self.config, "update_source_path", "") or "").strip()
        if not source:
            from config import DEFAULT_CONFIG
            source = DEFAULT_CONFIG.get("update_source_path", "")
        if not source:
            return

        today_str = now_dt.date().isoformat()

        # 1. If user snoozed/declined the update today, do not nag again today
        try:
            if self.db.get_setting("last_update_declined_date") == today_str:
                return
        except Exception as exc:
            logger.debug("Could not read update decline marker: %s", exc)

        # 2. Check if already checked today
        try:
            if self.db.get_setting("last_update_check_date") == today_str:
                return
        except Exception as exc:
            logger.debug("Could not read update check marker: %s", exc)
            return

        # If local path is unreachable, leave unmarked to retry later
        if not (source.startswith("http://") or source.startswith("https://") or "github" in source):
            try:
                if not Path(source).exists():
                    logger.warning("Update source %s is not reachable; will retry later.", source)
                    return
            except OSError as exc:
                logger.warning("Update source %s could not be read (%s); will retry later.", source, exc)
                return

        def on_reject(reason: str) -> None:
            self._notify("update_rejected", reason=reason)

        try:
            info = check_for_update(source, APP_VERSION, on_reject=on_reject)
        except Exception as exc:
            logger.warning("Update check failed: %s", exc)
            return

        # Record successful check date
        try:
            self.db.set_setting("last_update_check_date", today_str)
        except Exception as exc:
            logger.debug("Could not store update check marker: %s", exc)

        if info and self.on_update_available:
            try:
                self.on_update_available(info)
            except Exception as exc:
                logger.error("Error in on_update_available callback: %s", exc)

    def _check_weekly_automations(self, now_dt: datetime) -> None:
        """Run automated weekly backup and Excel export once per week."""
        today = now_dt.date()
        # Run on Thursday or Friday afternoon (or last workday of the week)
        day_name = now_dt.strftime("%a")
        if day_name in ("Thu", "Fri") and now_dt.hour >= 17:
            # Weekly Backup
            if self.config.enable_auto_weekly_backup:
                last_bk_key = f"last_backup_week_{today.isocalendar().year}_W{today.isocalendar().week:02d}"
                if not self.db.get_setting(last_bk_key):
                    try:
                        self.db.backup_database(keep=self.config.backup_keep_count)
                        self.db.set_setting(last_bk_key, today.isoformat())
                    except Exception as exc:
                        logger.error("Auto backup failed: %s", exc)

            # Weekly Export
            if self.config.enable_auto_weekly_export:
                last_exp_key = f"last_export_week_{today.isocalendar().year}_W{today.isocalendar().week:02d}"
                if not self.db.get_setting(last_exp_key):
                    try:
                        from reporter import export_report
                        start_w = today - timedelta(days=6)
                        export_report(self.db, self.config, start_w, today, open_folder=False)
                        self.db.set_setting(last_exp_key, today.isoformat())
                    except Exception as exc:
                        logger.error("Auto weekly export failed: %s", exc)

            # Weekly Retention (aggregate into daily_summary, then purge)
            if getattr(self.config, "enable_retention_cleanup", True):
                last_ret_key = f"last_retention_week_{today.isocalendar().year}_W{today.isocalendar().week:02d}"
                if not self.db.get_setting(last_ret_key):
                    try:
                        result = purge_old_data(self.db, self.config, today)
                        if result["deleted"] or result["aggregated"]:
                            logger.info(
                                "Weekly retention: aggregated %d day(s), purged %d event(s).",
                                result["aggregated"], result["deleted"],
                            )
                        self.db.set_setting(last_ret_key, today.isoformat())
                    except Exception as exc:
                        logger.error("Weekly retention cleanup failed: %s", exc)

    def _range_totals(self, start_d: date, end_d: date, today: date) -> tuple[int, int]:
        """Return ``(worked_seconds, work_days)`` for [start_d, end_d].

        Raw events are used where available; days already purged by the retention
        policy are read from ``daily_summary`` so historical totals stay intact.
        """
        start_ts = get_day_boundaries(start_d)[0]
        end_ts = get_day_boundaries(end_d)[1]

        records = self.db.get_input_events_with_details(start_ts, end_ts)
        credits = presence_credit_intervals(records, active_check_interval_seconds(self.config))
        sessions = self.db.get_sessions_between(start_ts, end_ts)
        summaries = {s["date"]: s for s in self.db.get_daily_summaries(start_d, end_d)}

        # PERF-05 / QUAL-03: shared bucket-by-day primitive.
        records_by_day = bucket_records_by_day(records)

        worked = 0
        work_days_count = 0
        cur = start_d
        while cur <= end_d:
            key = cur.isoformat()
            if key in summaries:
                metrics = metrics_from_summary(self.config, cur, summaries[key])
            else:
                d_st, d_et = get_day_boundaries(cur)
                day_records = records_by_day.get(cur, [])
                d_ev = [rec[0] for rec in day_records]
                d_sess = [
                    s for s in sessions
                    if int(s["start_ts"]) < d_et
                    and (s.get("end_ts") is None or int(s["end_ts"]) >= d_st)
                ]
                metrics = calculate_day_metrics(
                    target_date=cur,
                    events=d_ev,
                    sessions=d_sess,
                    work_start=self.config.work_start_time,
                    work_end=self.config.work_end_time,
                    work_days=self.config.work_days,
                    holidays=self.config.holidays,
                    daily_target_hours=(
                        half_day_target_hours(self.config.daily_target_hours)
                        if self._half_day_date == cur
                        else self.config.daily_target_hours
                    ),
                    grace_seconds=self.config.grace_period_seconds,
                    is_live=(cur == today),
                    confirm_credits=credits,
                )

            worked += metrics.worked_seconds
            if metrics.is_work_day:
                work_days_count += 1
            cur += timedelta(days=1)

        return worked, work_days_count

    @_command
    def _refresh_metrics(self, force: bool = False) -> None:
        """Refresh cached metrics on the bus owner thread (P1-3 caching)."""
        """Refresh cached metrics without recomputing everything on every tick.

        Day totals are refreshed at most every 30 s and week/month ranges at most
        once a minute; explicit mutations (sessions, projects, confirmations) and
        UI actions pass ``force=True`` (P1-3).
        """
        self._expire_pause()
        now_wall = float(self._time_fn())
        today = self._now_dt_fn().date()
        changed = False

        if force or self._today_metrics is None or (now_wall - self._metrics_ts) >= self.DAY_METRICS_MAX_AGE:
            # App usage and today's metrics include confirmed-presence credits so
            # the whole idle gap the user confirmed counts as active.
            self._today_app_usage = get_day_app_usage(self.db, self.config, today, is_live=True)
            self._today_metrics = get_day_metrics(
                self.db,
                self.config,
                today,
                half_day_dates={today} if self.is_half_day_today else None,
                is_live=True,
                now_ts=int(now_wall),
            )
            self._metrics_ts = now_wall
            changed = True

        if force or self._period_totals_ts <= 0 or (now_wall - self._period_totals_ts) >= self.PERIOD_TOTALS_MAX_AGE:
            # Weekly aggregation (current week starting Monday)
            start_week = today - timedelta(days=today.weekday())
            self._week_worked_seconds, _ = self._range_totals(start_week, today, today)

            # Monthly aggregation
            start_month = today.replace(day=1)
            month_worked, work_days_count = self._range_totals(start_month, today, today)
            self._month_worked_seconds = month_worked
            self._month_target_seconds = int(work_days_count * self.config.daily_target_hours * 3600)
            self._period_totals_ts = now_wall
            changed = True

        # Status Priority
        if self._has_error:
            status = "Error"
        elif self.is_paused():
            status = "Paused"
        elif self.is_meeting_active():
            status = "In Meeting"
        elif self.is_break_active():
            status = "In Break"
        elif self.is_overtime_active():
            status = "Overtime"
        elif self._active_check_idle_mode:
            status = "Idle"
        elif self._idle_seconds < self.config.grace_period_seconds:
            status = "Active"
        else:
            status = "Idle"
        if status != self._current_status:
            self._current_status = status
            changed = True

        if changed and self.on_state_change:
            try:
                self.on_state_change()
            except Exception as exc:
                logger.error("Error in on_state_change: %s", exc)
