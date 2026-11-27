"""Authenticated HTTP client for documented TTS Tracker endpoints."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, overload
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone_name

from time_tracker.core.config import APP_VERSION
from time_tracker.system.tts_auth import (
    TRACKER_AUTH_TIMEOUT_SECONDS,
    TrackerAuthClient,
    TrackerAuthError,
    TrackerAuthSession,
    TrackerCredentialStoreError,
    problem_details,
    save_profile,
)

logger = logging.getLogger(__name__)


def parse_utc_iso(value: str | None, field_name: str) -> datetime | None:
    """Parse an API UTC timestamp without accepting timezone-ambiguous strings."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TrackerApiError(200, "invalid_response", f"{field_name} must be a UTC ISO-8601 string or null.")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TrackerApiError(200, "invalid_response", f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise TrackerApiError(200, "invalid_response", f"{field_name} must include an explicit timezone.")
    return parsed.astimezone(timezone.utc)


def _format_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TrackerApiError(RuntimeError):
    def __init__(self, status_code: int | None, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    @property
    def is_temporary(self) -> bool:
        return (
            self.code == "network_error"
            or self.status_code == 429
            or (self.status_code is not None and self.status_code >= 500)
        )


class TrackerReauthenticationRequired(TrackerApiError):
    pass


@dataclass(frozen=True)
class TrackerWorkSession:
    attendance_id: str | None
    project_id: str | None
    project_name: str | None
    check_in_utc: str | None
    is_open: bool
    tracker_allowed: bool


@dataclass(frozen=True)
class TrackerServerSettings:
    tracking_enabled: bool
    app_tracking: bool
    idle_threshold_seconds: int
    presence_check_minutes: int


@dataclass(frozen=True)
class TrackerShift:
    date: str | None
    start: str | None
    end: str | None
    time_zone_id: str | None
    is_day_off: bool
    start_utc: str | None
    end_utc: str | None


@dataclass(frozen=True)
class TrackerHoliday:
    start_date: str
    end_date: str
    name: str


@dataclass(frozen=True)
class TrackerHelloData:
    session: TrackerWorkSession | None
    settings: TrackerServerSettings | None
    shift: TrackerShift | None
    holidays: tuple[TrackerHoliday, ...]
    server_time_utc: str | None


def local_time_zone_id() -> str:
    try:
        value = get_localzone_name()
        if not isinstance(value, str) or not value:
            raise ValueError("The local time zone name is empty.")
        ZoneInfo(value)
    except (OSError, ValueError, ZoneInfoNotFoundError) as exc:
        raise TrackerApiError(None, "timezone_unavailable", "Could not determine the local IANA time zone.") from exc
    if not value:
        raise TrackerApiError(None, "timezone_unavailable", "Could not determine the local IANA time zone.")
    return value


class TrackerApiClient:
    """Uses the access token in memory and refreshes it once after an HTTP 401."""

    def __init__(
        self,
        session: TrackerAuthSession,
        *,
        on_relogin_required: Callable[[], None] | None = None,
        timeout: int = TRACKER_AUTH_TIMEOUT_SECONDS,
    ) -> None:
        self._session = session
        self._session_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._relogin_notified = False
        self._on_relogin_required = on_relogin_required
        self.timeout = timeout

    @property
    def session(self) -> TrackerAuthSession:
        with self._session_lock:
            return self._session

    def set_relogin_handler(self, handler: Callable[[], None] | None) -> None:
        with self._session_lock:
            self._on_relogin_required = handler

    def replace_session(self, session: TrackerAuthSession) -> None:
        with self._session_lock:
            self._session = session
            self._relogin_notified = False

    def hello(self) -> TrackerHelloData:
        payload = self._authorized_request(
            "POST",
            "/api/Attendance/tracker/hello",
            {"clientVersion": APP_VERSION, "timeZoneId": local_time_zone_id()},
        )
        return parse_hello(payload)

    def get_current_session(self) -> TrackerWorkSession | None:
        payload = self._authorized_request("GET", "/api/Attendance/tracker/current")
        return parse_session(payload)

    def _authorized_request(
        self,
        method: str,
        route: str,
        payload: dict[str, str] | None = None,
    ) -> Any:
        session = self.session
        try:
            return self._request_once(method, route, payload, session)
        except TrackerApiError as exc:
            if exc.status_code != 401:
                raise

        refreshed = self.refresh_access_token(
            force=True,
            expected_access_token=session.access_token,
        )
        return self._request_once(method, route, payload, refreshed)

    def refresh_access_token(
        self,
        *,
        force: bool = False,
        expected_access_token: str | None = None,
    ) -> TrackerAuthSession:
        """Refresh via the device token, coalescing concurrent requests."""
        if not force and expected_access_token is None:
            raise ValueError("An access-token refresh requires a rejection or force=True.")
        with self._refresh_lock:
            with self._session_lock:
                current = self._session
            if expected_access_token is not None and current.access_token != expected_access_token:
                return current

            try:
                refreshed = TrackerAuthClient(current.profile.server_url).refresh(current.profile)
            except TrackerAuthError as exc:
                if exc.status_code == 401 and exc.code == "tracker.relogin_required":
                    self._notify_relogin_required()
                    raise TrackerReauthenticationRequired(
                        exc.status_code,
                        exc.code,
                        exc.message,
                    ) from exc
                raise TrackerApiError(exc.status_code, exc.code, exc.message) from exc

            try:
                save_profile(refreshed.profile)
            except TrackerCredentialStoreError as exc:
                raise TrackerApiError(
                    None,
                    "secure_storage_error",
                    "Could not securely save the refreshed device credentials.",
                ) from exc

            with self._session_lock:
                self._session = refreshed
                self._relogin_notified = False
            return refreshed

    def notify_reauthentication_required(self) -> None:
        self._notify_relogin_required()

    def _notify_relogin_required(self) -> None:
        with self._session_lock:
            if self._relogin_notified:
                return
            self._relogin_notified = True
            handler = self._on_relogin_required
        if handler is not None:
            try:
                handler()
            except Exception:
                logger.exception("Could not open the TTS sign-in dialog after device revocation.")

    def _request_once(
        self,
        method: str,
        route: str,
        payload: dict[str, str] | None,
        session: TrackerAuthSession,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {session.access_token}",
            "X-Tenant": session.profile.tenant,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            session.profile.server_url.rstrip("/") + route,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_body = response.read()
        except HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                error_body = {}
            code, message = problem_details(error_body)
            raise TrackerApiError(exc.code, code, message) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TrackerApiError(None, "network_error", str(exc) or "The ERP server is unavailable.") from exc

        if not response_body:
            return None
        try:
            return json.loads(response_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TrackerApiError(200, "invalid_response", "The ERP server returned invalid JSON.") from exc


def parse_hello(payload: Any) -> TrackerHelloData:
    data = _require_object(payload, "Hello response")
    if "session" not in data or "settings" not in data or "shift" not in data or "holidays" not in data:
        raise _invalid_response("Hello response is missing documented fields.")

    holidays_value = data["holidays"]
    if not isinstance(holidays_value, list):
        raise _invalid_response("Hello response holidays must be a list.")
    holidays = tuple(_parse_holiday(value) for value in holidays_value)

    server_time = data.get("serverTimeUtc")
    if server_time is not None and not isinstance(server_time, str):
        raise _invalid_response("Hello response serverTimeUtc must be a string or null.")
    server_time_parsed = parse_utc_iso(server_time, "Hello response serverTimeUtc")
    if server_time_parsed is not None:
        server_time = _format_utc_iso(server_time_parsed)

    return TrackerHelloData(
        session=parse_session(data["session"]),
        settings=parse_settings(data["settings"]),
        shift=_parse_shift(data["shift"]),
        holidays=holidays,
        server_time_utc=server_time,
    )


def parse_session(value: Any) -> TrackerWorkSession | None:
    if value is None:
        return None
    data = _require_object(value, "session")
    attendance_id = _required_id(data, "attendanceId")
    project_id = _required_id(data, "projectId")
    project_name = _required_string(data, "projectName", nullable=True)
    check_in_utc = _required_string(data, "checkInUtc", nullable=True)
    check_in_parsed = parse_utc_iso(check_in_utc, "session checkInUtc")
    if check_in_parsed is not None:
        check_in_utc = _format_utc_iso(check_in_parsed)
    is_open = _required_bool(data, "open")
    tracker_allowed = _required_bool(data, "trackerAllowed")
    return TrackerWorkSession(
        attendance_id=attendance_id,
        project_id=project_id,
        project_name=project_name,
        check_in_utc=check_in_utc,
        is_open=is_open,
        tracker_allowed=tracker_allowed,
    )


def parse_settings(value: Any) -> TrackerServerSettings | None:
    if value is None:
        return None
    data = _require_object(value, "settings")
    return TrackerServerSettings(
        tracking_enabled=_required_bool(data, "trackingEnabled"),
        app_tracking=_required_bool(data, "appTracking"),
        idle_threshold_seconds=_required_int(data, "idleThresholdSeconds"),
        presence_check_minutes=_required_int(data, "presenceCheckMinutes"),
    )


def _parse_shift(value: Any) -> TrackerShift | None:
    if value is None:
        return None
    data = _require_object(value, "shift")
    return TrackerShift(
        date=_required_string(data, "date", nullable=True),
        start=_required_string(data, "start", nullable=True),
        end=_required_string(data, "end", nullable=True),
        time_zone_id=_required_string(data, "timeZoneId", nullable=True),
        is_day_off=_required_bool(data, "isDayOff"),
        start_utc=_required_string(data, "startUtc", nullable=True),
        end_utc=_required_string(data, "endUtc", nullable=True),
    )


def _parse_holiday(value: Any) -> TrackerHoliday:
    data = _require_object(value, "holiday")
    return TrackerHoliday(
        start_date=_required_string(data, "startDate"),
        end_date=_required_string(data, "endDate"),
        name=_required_string(data, "name"),
    )


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid_response(f"{label} must be an object.")
    return value


@overload
def _required_string(data: dict[str, Any], key: str) -> str: ...


@overload
def _required_string(data: dict[str, Any], key: str, *, nullable: Literal[True]) -> str | None: ...


def _required_string(
    data: dict[str, Any],
    key: str,
    *,
    nullable: bool = False,
) -> str | None:
    if key not in data:
        raise _invalid_response(f"Response is missing {key}.")
    value = data[key]
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise _invalid_response(f"Response field {key} must be a string.")
    return value


def _required_id(data: dict[str, Any], key: str) -> str | None:
    if key not in data:
        raise _invalid_response(f"Response is missing {key}.")
    value = data[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _invalid_response(f"Response field {key} must be a string, integer, or null.")
    return str(value)


def _required_bool(data: dict[str, Any], key: str) -> bool:
    if key not in data or not isinstance(data[key], bool):
        raise _invalid_response(f"Response field {key} must be a boolean.")
    return data[key]


def _required_int(data: dict[str, Any], key: str) -> int:
    if key not in data or isinstance(data[key], bool) or not isinstance(data[key], int):
        raise _invalid_response(f"Response field {key} must be an integer.")
    return data[key]


def _invalid_response(message: str) -> TrackerApiError:
    return TrackerApiError(200, "invalid_response", message)
