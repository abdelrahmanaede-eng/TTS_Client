"""Windows Registry autostart manager for TimeTracker.

Configures HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.
No administrator privileges required.
"""

from __future__ import annotations

import logging
import os
import sys
import winreg
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "TimeTracker"


def get_launch_command() -> str:
    """Return the executable command line for autostart."""
    if getattr(sys, "frozen", False):
        # Compiled executable
        return f'"{sys.executable}"'

    # Development python script: prefer pythonw.exe if available to avoid console window
    python_exe = sys.executable
    pythonw_exe = Path(python_exe).with_name("pythonw.exe")
    exe_to_use = str(pythonw_exe) if pythonw_exe.exists() else python_exe

    main_script = Path(__file__).parent / "main.py"
    return f'"{exe_to_use}" "{main_script.resolve()}"'


def is_autostart_enabled() -> bool:
    """Check if TimeTracker is registered in HKCU Run key."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception as exc:
        logger.warning("Error checking autostart registry key: %s", exc)
        return False


def set_autostart(enabled: bool) -> bool:
    """Enable or disable autostart in HKCU Run key."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = get_launch_command()
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                logger.info("Autostart enabled with command: %s", cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    logger.info("Autostart disabled")
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        logger.error("Failed to set autostart in registry: %s", exc)
        return False
