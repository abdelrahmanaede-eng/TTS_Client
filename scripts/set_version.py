"""Helper script to get or update APP_VERSION in config.py."""
from __future__ import annotations

import re
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "time_tracker" / "core" / "config.py"


def get_version() -> str:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.py not found at {CONFIG_PATH}")
    content = CONFIG_PATH.read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if not match:
        raise ValueError("APP_VERSION variable not found in config.py")
    return match.group(1).strip()


def set_version(new_version: str) -> str:
    cleaned = new_version.strip().strip("\"'").lstrip("vV").strip()
    if not cleaned:
        raise ValueError("Version string cannot be empty")

    if not re.match(r"^\d+(\.\d+)*(?:-[a-zA-Z0-9.]+)?$", cleaned):
        raise ValueError(
            f"Invalid version format: '{new_version}'. Expected format like '1.3.5' or '1.4.0'."
        )

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.py not found at {CONFIG_PATH}")

    content = CONFIG_PATH.read_text(encoding="utf-8")
    new_content, count = re.subn(
        r'^(APP_VERSION\s*=\s*["\'])[^"\']*(["\'])',
        rf"\g<1>{cleaned}\g<2>",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 0:
        raise ValueError("Could not find APP_VERSION definition to replace in config.py")

    CONFIG_PATH.write_text(new_content, encoding="utf-8")
    return cleaned


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--get":
        try:
            val = set_version(sys.argv[1])
            print(val)
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            print(get_version())
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
