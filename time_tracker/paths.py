from __future__ import annotations

import sys
from pathlib import Path


def get_app_root() -> Path:
    """Return the project root in source runs and the bundle root when frozen."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root)
    return Path(__file__).resolve().parent.parent
