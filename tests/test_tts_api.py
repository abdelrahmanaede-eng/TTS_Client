from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from time_tracker.system import tts_api
from time_tracker.system.tts_auth import (
    TrackerAuthError,
    TrackerAuthProfile,
    TrackerAuthSession,
)


class _Response:
    def __init__(self, value: object) -> None:
        self._body = json.dumps(value).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _session(access_token: str = "access-token", device_token: str = "device-token") -> TrackerAuthSession:
    profile = TrackerAuthProfile(
        "tenant",
        "employee@example.com",
        "device-id",
        device_token,
        "https://apiv2.bussma.com",
    )
    return TrackerAuthSession(profile, access_token, None, "employee-1", "Employee One", profile.email)


def test_hello_sends_documented_payload_and_parses_server_state(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    response = {
        "session": {
            "attendanceId": "attendance-1",
            "projectId": 42,
            "projectName": "ERP Project",
            "checkInUtc": "2026-11-26T08:00:00Z",
            "open": True,
            "trackerAllowed": True,
        },
        "settings": {
            "trackingEnabled": True,
            "appTracking": False,
            "idleThresholdSeconds": 180,
            "presenceCheckMinutes": 3,
        },
        "shift": {
            "date": "2026-11-26",
            "start": "09:30",
            "end": "17:30",
            "timeZoneId": "Africa/Cairo",
            "isDayOff": False,
            "startUtc": "2026-11-26T07:30:00Z",
            "endUtc": "2026-11-26T15:30:00Z",
        },
        "holidays": [{"startDate": "2026-12-01", "endDate": "2026-12-01", "name": "Holiday"}],
        "serverTimeUtc": "2026-11-26T08:00:00Z",
    }

    def fake_urlopen(request: object, timeout: int) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(response)

    monkeypatch.setattr(tts_api, "urlopen", fake_urlopen)
    monkeypatch.setattr(tts_api, "local_time_zone_id", lambda: "Africa/Cairo")

    result = tts_api.TrackerApiClient(_session()).hello()

    request = captured["request"]
    assert request.full_url == "https://apiv2.bussma.com/api/Attendance/tracker/hello"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer access-token"
    assert request.get_header("X-tenant") == "tenant"
    assert json.loads(request.data) == {"clientVersion": "1.4.5", "timeZoneId": "Africa/Cairo"}
    assert result.session is not None
    assert result.session.project_name == "ERP Project"
    assert result.settings is not None
    assert result.settings.app_tracking is False
    assert result.shift is not None and result.shift.start_utc == "2026-11-26T07:30:00Z"
    assert result.holidays[0].name == "Holiday"


def test_get_current_session_uses_documented_http_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> _Response:
        captured["request"] = request
        return _Response(None)

    monkeypatch.setattr(tts_api, "urlopen", fake_urlopen)

    result = tts_api.TrackerApiClient(_session()).get_current_session()

    request = captured["request"]
    assert request.full_url == "https://apiv2.bussma.com/api/Attendance/tracker/current"
    assert request.get_method() == "GET"
    assert request.data is None
    assert result is None


def test_unauthorized_request_refreshes_secure_device_token_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[object] = []
    saved_profiles: list[TrackerAuthProfile] = []
    refreshed_session = _session("new-access-token", "new-device-token")

    def fake_urlopen(request: object, timeout: int) -> _Response:
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                None,
                io.BytesIO(b'{"code":"unauthorized","detail":"Access token expired."}'),
            )
        return _Response(None)

    class _AuthClient:
        def __init__(self, server_url: str) -> None:
            assert server_url == "https://apiv2.bussma.com"

        def refresh(self, profile: TrackerAuthProfile) -> TrackerAuthSession:
            assert profile.device_token == "device-token"
            return refreshed_session

    monkeypatch.setattr(tts_api, "urlopen", fake_urlopen)
    monkeypatch.setattr(tts_api, "TrackerAuthClient", _AuthClient)
    monkeypatch.setattr(tts_api, "save_profile", saved_profiles.append)

    client = tts_api.TrackerApiClient(_session())
    assert client.get_current_session() is None

    assert requests[0].get_header("Authorization") == "Bearer access-token"
    assert requests[1].get_header("Authorization") == "Bearer new-access-token"
    assert saved_profiles == [refreshed_session.profile]
    assert client.session == refreshed_session


def test_revoked_device_invokes_reauthentication_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    notifications: list[str] = []

    def fake_urlopen(request: object, timeout: int) -> None:
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            None,
            io.BytesIO(b'{"code":"unauthorized","detail":"Access token expired."}'),
        )

    class _AuthClient:
        def __init__(self, server_url: str) -> None:
            pass

        def refresh(self, profile: TrackerAuthProfile) -> TrackerAuthSession:
            raise TrackerAuthError(401, "tracker.relogin_required", "Sign in again.")

    monkeypatch.setattr(tts_api, "urlopen", fake_urlopen)
    monkeypatch.setattr(tts_api, "TrackerAuthClient", _AuthClient)

    client = tts_api.TrackerApiClient(_session(), on_relogin_required=lambda: notifications.append("required"))

    with pytest.raises(tts_api.TrackerReauthenticationRequired):
        client.get_current_session()

    assert notifications == ["required"]


def test_network_failure_is_reported_as_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: int) -> None:
        raise URLError("offline")

    monkeypatch.setattr(tts_api, "urlopen", fake_urlopen)

    with pytest.raises(tts_api.TrackerApiError) as raised:
        tts_api.TrackerApiClient(_session()).get_current_session()

    assert raised.value.code == "network_error"
    assert raised.value.is_temporary
