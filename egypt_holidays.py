"""Offline Egyptian official holidays loader.

Reads the bundled JSON data file ``assets/egypt_holidays.json`` and exposes the
holiday dates for a given year.  Fixed-date holidays recur every year, while
movable (Islamic / Coptic) holidays are stored per year so the data file can be
updated without touching code.

No network calls are ever made: the file is shipped with the application.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATE_REGEX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MONTH_DAY_REGEX = re.compile(r"^(\d{2})-(\d{2})$")

HOLIDAYS_FILENAME = "egypt_holidays.json"


def get_assets_dir() -> Path:
    """Return the assets directory (works both from source and PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets"  # type: ignore[attr-defined]
    return Path(__file__).parent / "assets"


def get_holidays_file() -> Path:
    return get_assets_dir() / HOLIDAYS_FILENAME


def load_holiday_data(path: Path | None = None) -> dict[str, Any]:
    """Load the bundled holiday data file. Returns an empty structure on error."""
    data_path = path or get_holidays_file()
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("holiday data must be a JSON object")
        return data
    except Exception as exc:
        logger.warning("Could not load Egyptian holidays data from %s: %s", data_path, exc)
        return {"fixed": [], "years": {}}


def list_supported_years(path: Path | None = None) -> list[int]:
    """Return the sorted list of years that have per-year movable holidays."""
    data = load_holiday_data(path)
    years: list[int] = []
    for key in (data.get("years") or {}):
        try:
            years.append(int(key))
        except (TypeError, ValueError):
            continue
    return sorted(set(years))


def _valid_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not DATE_REGEX.match(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def get_egyptian_holidays(year: int, path: Path | None = None) -> list[str]:
    """Return all Egyptian holiday dates (YYYY-MM-DD) for ``year``, sorted and unique."""
    data = load_holiday_data(path)
    dates: set[str] = set()

    for entry in data.get("fixed") or []:
        if not isinstance(entry, dict):
            continue
        month_day = entry.get("month_day")
        if not isinstance(month_day, str) or not MONTH_DAY_REGEX.match(month_day.strip()):
            continue
        candidate = f"{int(year):04d}-{month_day.strip()}"
        valid = _valid_date(candidate)
        if valid:
            dates.add(valid)

    year_entries = (data.get("years") or {}).get(str(int(year))) or []
    for entry in year_entries:
        if isinstance(entry, dict):
            valid = _valid_date(entry.get("date"))
        else:
            valid = _valid_date(entry)
        if valid and valid.startswith(f"{int(year):04d}-"):
            dates.add(valid)

    return sorted(dates)


def get_holiday_name(date_value: Any, lang: str = "en", path: Path | None = None) -> str | None:
    """Return the localized name of a known holiday date (``None`` if unknown).

    Fixed holidays are matched by their MM-DD part, movable ones by the exact
    YYYY-MM-DD date stored for that year.  Unknown/user-added dates simply have
    no name, which is fine for the settings list.
    """
    value = _valid_date(date_value)
    if value is None:
        return None
    data = load_holiday_data(path)
    preferred = "ar" if str(lang or "en").lower().startswith("ar") else "en"

    def _name(entry: Any) -> str | None:
        if not isinstance(entry, dict):
            return None
        for key in (preferred, "en", "ar"):
            name = entry.get(key)
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    month_day = value[5:]
    for entry in data.get("fixed") or []:
        if isinstance(entry, dict) and str(entry.get("month_day", "")).strip() == month_day:
            return _name(entry)
    for entry in (data.get("years") or {}).get(value[:4]) or []:
        stored_date = entry.get("date") if isinstance(entry, dict) else entry
        if stored_date == value:
            return _name(entry)
    return None


def merge_holidays(existing: list[str], new_holidays: list[str]) -> list[str]:
    """Merge two holiday lists without duplicates. Returns a sorted new list."""
    merged: set[str] = set()
    for value in list(existing) + list(new_holidays):
        valid = _valid_date(value)
        if valid:
            merged.add(valid)
    return sorted(merged)


def count_new_holidays(existing: list[str], new_holidays: list[str]) -> int:
    """Return how many of ``new_holidays`` are not already in ``existing``."""
    existing_set = {h for h in existing if _valid_date(h)}
    return len({h for h in new_holidays if _valid_date(h)} - existing_set)
