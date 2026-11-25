"""Centralized, production-grade logging for TimeTracker.

Everything that decides "where do messages go and what happens on a crash"
lives here so ``main.py`` stays a bootstrap file:

* rotating UTF-8 files (main log + a WARNING/ERROR-only log + native faults),
* millisecond timestamps, thread names, module:line and a per-run session id,
* an environment banner written once per start (version, PID, OS, frozen?),
* ``sys.excepthook`` / ``threading.excepthook`` so a dying worker thread can
  never disappear silently in the windowed build,
* ``faulthandler`` so native access violations still leave a stack trace,
* a user-facing fatal-error dialog that points at the log and can open it,
* a diagnostics bundle (logs + system report + config) the user can send to
  support.

Log files (all in ``%LOCALAPPDATA%\\TimeTracker``):

======================  ===========================================
``timetracker.log``     INFO and above, 5 MB x 5 rotations
``timetracker-errors.log``  WARNING and above only, 2 MB x 3
``timetracker-faults.log``  Native crash stacks (faulthandler)
======================  ===========================================
"""

from __future__ import annotations

import atexit
import ctypes
import faulthandler
import json
import locale
import logging
import logging.handlers
import os
import platform
import sys
import tempfile
import threading
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from time_tracker.core.config import (
    APP_VERSION,
    get_app_dir,
    normalize_log_level,
    resolve_writable_dir,
)

MAIN_LOG_NAME = "timetracker.log"
ERROR_LOG_NAME = "timetracker-errors.log"
FAULT_LOG_NAME = "timetracker-faults.log"

LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-8s [%(threadName)s] "
    "[%(session_id)s] %(name)s:%(lineno)d - %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
MAX_ERROR_LOG_BYTES = 2 * 1024 * 1024
ERROR_BACKUP_COUNT = 3
MAX_FAULT_BYTES = 1024 * 1024

SESSION_ID = uuid.uuid4().hex[:8]

#: Third-party loggers that are chatty at DEBUG (PIL alone emits ~50 import
#: lines). They stay at WARNING even in a ``--verbose`` support session.
NOISY_LOGGERS = ("PIL", "urllib3", "matplotlib", "asyncio", "http.client")

_ROOT = logging.getLogger()
_CONFIGURED = False
_LOG_DIR: Path | None = None
_MAIN_LOG: Path | None = None
_ERROR_LOG: Path | None = None
_FAULT_LOG: Path | None = None
_FAULT_STREAM = None
_FATAL_SHOWN = False
_ORIGINAL_THREAD_HOOK = threading.excepthook


class _ContextFilter(logging.Filter):
    """Attach the session id (and PID) to every record."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib name
        record.session_id = self.session_id
        record.pid = os.getpid()
        return True


def is_frozen() -> bool:
    """True when running from the PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def get_log_dir() -> Path:
    """Directory that holds every log file (``%LOCALAPPDATA%\\TimeTracker``)."""
    global _LOG_DIR
    if _LOG_DIR is None:
        _LOG_DIR = get_app_dir()
    return _LOG_DIR


def get_log_file() -> Path:
    return get_log_dir() / MAIN_LOG_NAME


def get_error_log_file() -> Path:
    return get_log_dir() / ERROR_LOG_NAME


def get_fault_log_file() -> Path:
    return get_log_dir() / FAULT_LOG_NAME


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def _new_file_handler(
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
    role: str,
) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(level)
    handler.setFormatter(_build_formatter())
    handler.addFilter(_ContextFilter(SESSION_ID))
    # Markers used by set_level()/reset_logging() to recognise our own handlers.
    handler._timetracker = True  # type: ignore[attr-defined]
    handler._tt_role = role  # type: ignore[attr-defined]
    return handler


def setup_logging(
    level: str = "INFO",
    *,
    log_dir: Path | None = None,
    console: bool | None = None,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    enable_faults: bool = True,
) -> Path:
    """Configure the root logger once and return the main log file path.

    ``console=None`` adds a stderr handler only when a console really exists
    (the windowed build has ``sys.stderr is None``, and writing to it makes
    every record raise internally).
    """
    global _CONFIGURED, _LOG_DIR, _MAIN_LOG, _ERROR_LOG

    if log_dir is not None:
        target_dir = Path(log_dir)
    else:
        try:
            target_dir = get_app_dir()
        except OSError:  # read-only profile: still leave a trail somewhere
            target_dir = Path(tempfile.gettempdir()) / "TimeTracker" / "logs"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = Path(tempfile.gettempdir()) / "TimeTracker" / "logs"
        target_dir.mkdir(parents=True, exist_ok=True)
    _LOG_DIR = target_dir
    _MAIN_LOG = target_dir / MAIN_LOG_NAME
    _ERROR_LOG = target_dir / ERROR_LOG_NAME

    level_value = logging.getLevelName(normalize_log_level(level))
    _ROOT.setLevel(level_value)
    _apply_noise_floor(level_value)

    if _CONFIGURED:
        set_level(level)
        return _MAIN_LOG

    _ROOT.addHandler(
        _new_file_handler(
            _MAIN_LOG,
            level=level_value,
            max_bytes=max_bytes,
            backup_count=backup_count,
            role="main",
        )
    )
    _ROOT.addHandler(
        _new_file_handler(
            _ERROR_LOG,
            level=logging.WARNING,
            max_bytes=MAX_ERROR_LOG_BYTES,
            backup_count=ERROR_BACKUP_COUNT,
            role="errors",
        )
    )

    if console is None:
        console = sys.stderr is not None and not is_frozen()
    if console:
        stream = sys.stderr
        if stream is not None:
            has_console = any(
                isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                for h in _ROOT.handlers
            )
            if not has_console:
                handler = logging.StreamHandler(stream)
                handler.setLevel(level_value)
                handler.setFormatter(_build_formatter())
                handler.addFilter(_ContextFilter(SESSION_ID))
                handler._timetracker = True  # type: ignore[attr-defined]
                handler._tt_role = "console"  # type: ignore[attr-defined]
                _ROOT.addHandler(handler)

    logging.captureWarnings(True)
    _CONFIGURED = True

    if enable_faults:
        _start_faulthandler()

    return _MAIN_LOG


def _apply_noise_floor(level_value: int) -> None:
    """Keep third-party libraries quiet even when the app runs at DEBUG."""
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level_value, logging.WARNING))


def set_level(level: str) -> None:
    """Apply a new level to the root logger and our verbosity-aware handlers."""
    name = normalize_log_level(level)
    value = logging.getLevelName(name)
    _ROOT.setLevel(value)
    for handler in _ROOT.handlers:
        if getattr(handler, "_timetracker", False) and getattr(handler, "_tt_role", "") in (
            "main",
            "console",
        ):
            handler.setLevel(value)
    _apply_noise_floor(value)
    logging.getLogger(__name__).info("Log level set to %s", name)


def reset_logging() -> None:
    """Tear everything down (used by tests and by the CLI diagnostic mode)."""
    global _CONFIGURED, _FAULT_STREAM, _FATAL_SHOWN

    _FATAL_SHOWN = False

    for handler in list(_ROOT.handlers):
        if getattr(handler, "_timetracker", False):
            _ROOT.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - defensive
                pass
    try:
        faulthandler.disable()
    except Exception:  # pragma: no cover - defensive
        pass
    if _FAULT_STREAM is not None:
        try:
            _FAULT_STREAM.close()
        except Exception:  # pragma: no cover - defensive
            pass
        _FAULT_STREAM = None
    threading.excepthook = _ORIGINAL_THREAD_HOOK
    sys.excepthook = sys.__excepthook__
    _CONFIGURED = False


def _start_faulthandler() -> None:
    """Write native crash stacks (access violation, stack overflow) to a file."""
    global _FAULT_STREAM, _FAULT_LOG

    try:
        _FAULT_LOG = get_fault_log_file()
        if _FAULT_LOG.exists() and _FAULT_LOG.stat().st_size > MAX_FAULT_BYTES:
            backup = _FAULT_LOG.with_name(_FAULT_LOG.name + ".1")
            try:
                if backup.exists():
                    backup.unlink()
                _FAULT_LOG.replace(backup)
            except OSError:
                pass
        # Line buffered so a hard crash cannot swallow the last lines.
        _FAULT_STREAM = open(_FAULT_LOG, "a", encoding="utf-8", buffering=1)
        _FAULT_STREAM.write(
            f"\n===== faulthandler armed {datetime.now().isoformat(timespec='seconds')} "
            f"(session {SESSION_ID}, pid {os.getpid()}) =====\n"
        )
        _FAULT_STREAM.flush()
        faulthandler.enable(file=_FAULT_STREAM, all_threads=True)
        atexit.register(faulthandler.disable)
    except Exception:  # pragma: no cover - faulthandler is best effort
        logging.getLogger(__name__).warning("Could not enable faulthandler", exc_info=True)


def _is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - defensive
        return False


def _platform_line() -> str:
    try:
        if sys.platform == "win32":
            version = sys.getwindowsversion()
            return (
                f"Windows {version.major}.{version.minor} build {version.build} "
                f"({platform.machine()}, {platform.architecture()[0]})"
            )
    except Exception:  # pragma: no cover - defensive
        pass
    return platform.platform()


def _locale_line() -> str:
    try:
        language, encoding = locale.getlocale()
        return f"{language or 'unknown'} / {encoding or 'unknown'} encoding"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def log_startup_banner() -> None:
    """Log one block that makes a support report self-contained."""
    log = logging.getLogger("startup")
    log.info("=" * 72)
    log.info("TimeTracker v%s  session=%s  pid=%s", APP_VERSION, SESSION_ID, os.getpid())
    log.info("Executable : %s", sys.executable)
    log.info("Frozen     : %s", is_frozen())
    log.info("Python     : %s", sys.version.split()[0])
    log.info("Platform   : %s", _platform_line())
    log.info("Locale     : %s", _locale_line())
    log.info("Elevated   : %s", _is_admin())
    log.info("Data dir   : %s", get_app_dir())
    log.info("Log file   : %s", get_log_file())
    log.info("Level      : %s", logging.getLevelName(_ROOT.level))
    log.info("=" * 72)


def _flush_handlers() -> None:
    for handler in _ROOT.handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover - defensive
            pass


def _show_fatal_dialog(title: str, message: str) -> None:
    """Message box with an offer to open the log folder (Windows only)."""
    if sys.platform != "win32":
        return
    try:
        # MB_ICONERROR | MB_YESNO ; IDYES == 6
        answer = ctypes.windll.user32.MessageBoxW(0, message, title, 0x10 | 0x04)
        if answer == 6:
            open_log_folder()
    except Exception:  # pragma: no cover - no interactive desktop
        pass


def _notify_fatal(exc_type: type[BaseException], exc: BaseException) -> None:
    global _FATAL_SHOWN
    if _FATAL_SHOWN:
        return
    _FATAL_SHOWN = True
    _flush_handlers()
    log_file = get_log_file()
    detail = f"{exc_type.__name__}: {exc}".strip()
    _show_fatal_dialog(
        "TimeTracker — Unexpected error",
        "TimeTracker hit an unexpected error and has to close.\n\n"
        f"{detail}\n\n"
        "The full details were written to:\n"
        f"{log_file}\n\n"
        "Do you want to open the folder that contains the log now?",
    )


def handle_fatal(exc: BaseException, *, notify_user: bool = True) -> None:
    """Log an exception that ends the process and tell the user where the log is."""
    logging.getLogger("crash").critical(
        "Fatal error: %s", exc, exc_info=(type(exc), exc, exc.__traceback__)
    )
    if notify_user:
        _notify_fatal(type(exc), exc)


def _excepthook(exc_type: type[BaseException], exc: BaseException, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("crash").critical(
        "Unhandled exception reached the top of main", exc_info=(exc_type, exc, tb)
    )
    _notify_fatal(exc_type, exc)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    thread_name = args.thread.name if args.thread is not None else "unknown"
    logging.getLogger("crash").critical(
        "Unhandled exception in thread '%s' (the thread is now dead)",
        thread_name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    if _is_main_thread_death(args):
        _notify_fatal(args.exc_type, args.exc_value)


def _is_main_thread_death(args: threading.ExceptHookArgs) -> bool:
    return args.thread is not None and args.thread is threading.main_thread()


def install_crash_handlers() -> None:
    """Install hooks that turn silent crashes into log entries (+ a dialog)."""
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


def open_folder(path: Path) -> bool:
    """Open a folder in the OS file manager; True on success."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        else:  # pragma: no cover - the product targets Windows
            import subprocess

            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        logging.getLogger(__name__).warning("Could not open %s", path, exc_info=True)
        return False


def open_log_folder() -> bool:
    """Open the folder that contains the log files; True on success."""
    return open_folder(get_log_dir())


def notify_user(title: str, message: str) -> None:
    """Show a plain information box (a windowed build has no console)."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)  # MB_ICONINFORMATION
    except Exception:  # pragma: no cover - no interactive desktop
        pass


def _iter_log_files() -> list[Path]:
    """Current logs plus their rotated backups."""
    files: list[Path] = []
    directory = get_log_dir()
    for base in (MAIN_LOG_NAME, ERROR_LOG_NAME, FAULT_LOG_NAME):
        for path in sorted(directory.glob(base + "*")):
            if path.is_file():
                files.append(path)
    return files


def _system_report(extra: dict[str, Any] | None) -> str:
    lines = [
        "TimeTracker diagnostics",
        "=======================",
        f"Generated : {datetime.now().isoformat(timespec='seconds')}",
        f"Version   : {APP_VERSION}",
        f"Session   : {SESSION_ID}",
        f"PID       : {os.getpid()}",
        f"Exe       : {sys.executable}",
        f"Frozen    : {is_frozen()}",
        f"Python    : {sys.version.replace(os.linesep, ' ')}",
        f"Platform  : {_platform_line()}",
        f"Locale    : {_locale_line()}",
        f"Elevated  : {_is_admin()}",
        f"Data dir  : {get_app_dir()}",
        f"Log dir   : {get_log_dir()}",
        f"Log level : {logging.getLevelName(_ROOT.level)}",
    ]
    if extra:
        lines.append("")
        lines.append("Application state")
        lines.append("-----------------")
        for key in sorted(extra):
            lines.append(f"{key}: {extra[key]}")
    return "\n".join(lines) + "\n"


def get_diagnostics_dir() -> Path:
    """Where diagnostics bundles are written (Documents, app dir, temp)."""
    candidates = [
        Path.home() / "Documents" / "TimeTracker" / "Diagnostics",
        get_app_dir() / "Diagnostics",
        Path(tempfile.gettempdir()) / "TimeTracker" / "Diagnostics",
    ]
    return resolve_writable_dir(candidates, candidates[-1])


def create_diagnostics_bundle(
    extra: dict[str, Any] | None = None,
    *,
    dest_dir: Path | None = None,
    include_config: bool = True,
) -> Path:
    """Zip the logs, a system report and ``config.json`` for support.

    Returns the path of the generated ``.zip`` file.
    """
    _flush_handlers()
    target_dir = Path(dest_dir) if dest_dir is not None else get_diagnostics_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle = target_dir / f"TimeTracker_diagnostics_{stamp}.zip"

    session = {
        "version": APP_VERSION,
        "session_id": SESSION_ID,
        "pid": os.getpid(),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "frozen": is_frozen(),
        "data_dir": str(get_app_dir()),
        "log_dir": str(get_log_dir()),
        "extra": extra or {},
    }

    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("system.txt", _system_report(extra))
        archive.writestr("session.json", json.dumps(session, indent=2, default=str))
        for path in _iter_log_files():
            try:
                archive.write(path, arcname=f"logs/{path.name}")
            except OSError:  # a rotating file may vanish mid-copy
                continue
        config_file = get_app_dir() / "config.json"
        if include_config and config_file.exists():
            try:
                archive.write(config_file, arcname="config.json")
            except OSError:
                pass

    logging.getLogger(__name__).info("Diagnostics bundle created: %s", bundle)
    return bundle


def describe_exception(exc: BaseException) -> str:
    """One-line ``Type: message`` plus the last traceback frame (for toasts)."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = ""
    if frames:
        last = frames[-1]
        where = f" ({Path(last.filename).name}:{last.lineno} in {last.name})"
    return f"{type(exc).__name__}: {exc}{where}"
