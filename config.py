"""Configuration manager for TimeTracker.

Handles loading, validating, and saving the configuration file located at
%LOCALAPPDATA%\\TimeTracker\\config.json.
Supports holidays, notification toggles, reminder thresholds, and auto-export/backup options.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import time
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
APP_VERSION = "1.4.2"
DEFAULT_UPDATE_SOURCE = "https://raw.githubusercontent.com/abdelrahmanaede-eng/TimeTracker-Updates/main/version.json"

#: Levels accepted by ``logging_setup`` / ``log_level`` in config.json.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "INFO"

DEFAULT_CONFIG: dict[str, Any] = {
    "work_start": "09:30",
    "work_end": "17:30",
    "work_days": ["Sun", "Mon", "Tue", "Wed", "Thu"],
    "daily_target_hours": 8.0,
    "weekly_target_hours": 40.0,
    "grace_period_seconds": 180,
    "tick_interval_seconds": 5,
    "autostart_enabled": True,
    "language": "en",
    "holidays": [],
    "enable_end_of_day_notification": True,
    "enable_meeting_reminder": True,
    "meeting_reminder_minutes": 120,
    "enable_stretch_reminder": True,
    "stretch_reminder_minutes": 120,
    "enable_auto_weekly_export": True,
    "enable_auto_weekly_backup": True,
    "enable_app_tracking": True,
    "enable_retention_cleanup": True,
    "retention_days": 90,
    "active_check_enabled": True,
    "active_check_interval_min": 3,
    "update_source_path": DEFAULT_UPDATE_SOURCE,
    # SEC-02: when set, the updater requires a valid Authenticode signature
    # whose subject contains this publisher string. Empty = hash-only trust
    # (kept for the opt-in signing builds).
    "required_update_publisher": "",
    "enable_smart_meeting_detection": True,
    "smart_meeting_cooldown_minutes": 20,
    "enable_dashboard_server": True,
    "backup_keep_count": 8,
    "overtime_prompt_timeout_seconds": 90,
    "log_level": DEFAULT_LOG_LEVEL,
    # Keep date/clock/duration strings literal when CSV is opened in Excel.
    # Set to false when another tool consumes the CSV and needs raw values.
    "csv_excel_safe": True,
}


def normalize_log_level(value: Any) -> str:
    """Return a valid logging level name, falling back to INFO.

    Accepts any case and the numeric forms Python allows (``10``, ``"20"``),
    and never raises: a typo in config.json must not stop the app from
    starting, it just loses the verbose output.
    """
    if isinstance(value, bool):
        return DEFAULT_LOG_LEVEL
    if isinstance(value, int):
        name = logging.getLevelName(value)
        if isinstance(name, str) and name in LOG_LEVELS:
            return name
        return DEFAULT_LOG_LEVEL
    text = str(value or "").strip().upper()
    if text.isdigit():
        return normalize_log_level(int(text))
    return text if text in LOG_LEVELS else DEFAULT_LOG_LEVEL


def get_app_dir() -> Path:
    """Return %LOCALAPPDATA%\\TimeTracker directory."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base_dir = Path(local_app_data)
    else:
        base_dir = Path.home() / "AppData" / "Local"
    app_dir = base_dir / "TimeTracker"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_config_path() -> Path:
    """Return path to config.json."""
    return get_app_dir() / "config.json"


def resolve_writable_dir(candidates: list[Path], fallback: Path) -> Path:
    """Return the first writable directory among ``candidates``.

    Used by the backups/reports/dashboard helpers to avoid duplicating the
    three-candidate write-test fallback loop (QUAL-04).
    """
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            test_file = directory / ".write_test"
            test_file.touch(exist_ok=True)
            test_file.unlink(missing_ok=True)
            return directory
        except (OSError, PermissionError):
            continue
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


@dataclass
class Config:
    work_start: str = "09:30"
    work_end: str = "17:30"
    work_days: list[str] = field(default_factory=lambda: ["Sun", "Mon", "Tue", "Wed", "Thu"])
    daily_target_hours: float = 8.0
    weekly_target_hours: float = 40.0
    grace_period_seconds: int = 180
    tick_interval_seconds: int = 5
    autostart_enabled: bool = True
    language: str = "en"
    holidays: list[str] = field(default_factory=list)
    enable_end_of_day_notification: bool = True
    enable_meeting_reminder: bool = True
    meeting_reminder_minutes: int = 120
    enable_stretch_reminder: bool = True
    stretch_reminder_minutes: int = 120
    enable_auto_weekly_export: bool = True
    enable_auto_weekly_backup: bool = True
    enable_app_tracking: bool = True
    enable_retention_cleanup: bool = True
    retention_days: int = 90
    active_check_enabled: bool = True
    active_check_interval_min: int = 3
    update_source_path: str = DEFAULT_UPDATE_SOURCE
    required_update_publisher: str = ""
    backup_keep_count: int = 8
    # Item 4: an unanswered overtime prompt auto-resolves to "No" after this
    # many seconds (30-600).
    overtime_prompt_timeout_seconds: int = 90
    enable_smart_meeting_detection: bool = True
    smart_meeting_cooldown_minutes: int = 20
    enable_dashboard_server: bool = True
    # Logging verbosity: DEBUG, INFO, WARNING, ERROR or CRITICAL (default INFO).
    log_level: str = DEFAULT_LOG_LEVEL
    # Excel re-interprets text like '1h 31m' / '13:31:00' as time values (1:31).
    csv_excel_safe: bool = True

    @property
    def work_start_time(self) -> time:
        parts = self.work_start.split(":")
        return time(int(parts[0]), int(parts[1]))

    @property
    def work_end_time(self) -> time:
        parts = self.work_end.split(":")
        return time(int(parts[0]), int(parts[1]))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path | None = None) -> None:
        """Save current configuration to disk."""
        target_path = path or get_config_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)


def is_valid_config(data: Any) -> bool:
    """Verify that configuration data contains valid types and ranges."""
    if not isinstance(data, dict):
        return False

    required_keys = ["work_start", "work_end", "work_days", "daily_target_hours"]
    for key in required_keys:
        if key not in data:
            return False

    # Check times
    if not (isinstance(data["work_start"], str) and TIME_REGEX.match(data["work_start"])):
        return False
    if not (isinstance(data["work_end"], str) and TIME_REGEX.match(data["work_end"])):
        return False

    # Check work days
    if not isinstance(data["work_days"], list):
        return False
    if not data["work_days"] or not all(d in VALID_DAYS for d in data["work_days"]):
        return False

    # Check numeric bounds
    if not (isinstance(data["daily_target_hours"], (int, float)) and data["daily_target_hours"] > 0):
        return False

    # A working window must move forward in time: night shifts are not
    # supported (D4). Comparing "HH:MM" strings lexicographically is exact for
    # zero-padded 24-hour times.
    if data["work_end"] <= data["work_start"]:
        return False

    return True


def load_config(path: Path | None = None) -> Config:
    """Load configuration from disk. Fill missing keys with defaults, or recreate if corrupted."""
    config_path = path or get_config_path()

    if not config_path.exists():
        cfg = Config()
        cfg.save(config_path)
        return cfg

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not is_valid_config(data):
            raise ValueError("Invalid configuration values")

        # Fill any newly added config fields from defaults
        merged: dict[str, Any] = dict(DEFAULT_CONFIG)
        merged.update(data)

        # Sanitize holidays
        holidays_clean = [
            h.strip() for h in merged.get("holidays", [])
            if isinstance(h, str) and DATE_REGEX.match(h.strip())
        ]

        cfg = Config(
            work_start=str(merged["work_start"]),
            work_end=str(merged["work_end"]),
            work_days=list(merged["work_days"]),
            daily_target_hours=float(merged["daily_target_hours"]),
            weekly_target_hours=float(merged.get("weekly_target_hours", 40.0)),
            grace_period_seconds=int(merged.get("grace_period_seconds", 180)),
            tick_interval_seconds=int(merged.get("tick_interval_seconds", 5)),
            autostart_enabled=bool(merged.get("autostart_enabled", True)),
            language=str(merged.get("language", "en")).lower() if str(merged.get("language", "en")).lower() in ("en", "ar") else "en",
            holidays=holidays_clean,
            enable_end_of_day_notification=bool(merged.get("enable_end_of_day_notification", True)),
            enable_meeting_reminder=bool(merged.get("enable_meeting_reminder", True)),
            meeting_reminder_minutes=int(merged.get("meeting_reminder_minutes", 120)),
            enable_stretch_reminder=bool(merged.get("enable_stretch_reminder", True)),
            stretch_reminder_minutes=int(merged.get("stretch_reminder_minutes", 120)),
            enable_auto_weekly_export=bool(merged.get("enable_auto_weekly_export", True)),
            enable_auto_weekly_backup=bool(merged.get("enable_auto_weekly_backup", True)),
            enable_app_tracking=bool(merged.get("enable_app_tracking", True)),
            enable_retention_cleanup=bool(merged.get("enable_retention_cleanup", True)),
            retention_days=max(0, int(merged.get("retention_days", 90))),
            active_check_enabled=bool(merged.get("active_check_enabled", True)),
            active_check_interval_min=max(1, min(60, int(merged.get("active_check_interval_min", 3)))),
            update_source_path=str(merged.get("update_source_path") or DEFAULT_UPDATE_SOURCE).strip(),
            required_update_publisher=str(merged.get("required_update_publisher") or "").strip(),
            backup_keep_count=max(1, min(52, int(merged.get("backup_keep_count", 8)))),
            overtime_prompt_timeout_seconds=max(
                30, min(600, int(merged.get("overtime_prompt_timeout_seconds", 90)))
            ),
            enable_smart_meeting_detection=bool(merged.get("enable_smart_meeting_detection", True)),
            smart_meeting_cooldown_minutes=max(1, min(180, int(merged.get("smart_meeting_cooldown_minutes", 20)))),
            enable_dashboard_server=bool(merged.get("enable_dashboard_server", True)),
            log_level=normalize_log_level(merged.get("log_level", DEFAULT_LOG_LEVEL)),
            csv_excel_safe=bool(merged.get("csv_excel_safe", True)),
        )
        return cfg

    except Exception as exc:
        logger.warning("Config corrupted or unreadable (%s); recreating defaults.", exc)
        cfg = Config()
        cfg.save(config_path)
        return cfg
