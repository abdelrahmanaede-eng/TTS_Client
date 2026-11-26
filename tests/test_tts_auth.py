from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from time_tracker.system import tts_auth


class _Response:
    def __init__(self, value: dict[str, object]) -> None:
        self._body = json.dumps(value).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _EmptyResponse:
    def __enter__(self) -> _EmptyResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b""


def test_login_sends_tenant_and_credentials_to_tracker_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(
            {
                "deviceToken": "device-token",
                "tenantIdentifier": "bussma",
                "login": {
                    "token": "access-token",
                    "expiryDate": "2026-11-27T00:00:00Z",
                    "userId": "employee-1",
                    "username": "Employee One",
                },
            }
        )

    monkeypatch.setattr(tts_auth, "urlopen", fake_urlopen)
    profile = tts_auth.TrackerAuthProfile("bussma", "employee@example.com", "device-id")

    session = tts_auth.TrackerAuthClient().login(profile, "secret-password")

    request = captured["request"]
    assert request.full_url == "https://apiv2.bussma.com/api/tracker/auth/login"
    assert request.get_header("X-tenant") == "bussma"
    assert json.loads(request.data) == {
        "email": "employee@example.com",
        "password": "secret-password",
        "deviceId": "device-id",
        "deviceName": tts_auth.device_name(),
    }
    assert session.access_token == "access-token"
    assert session.profile.device_token == "device-token"
    assert session.profile.tenant == "bussma"


def test_refresh_reports_relogin_required_without_hiding_http_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: int) -> None:
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            None,
            io.BytesIO(b'{"code":"tracker.relogin_required","detail":"Sign in again."}'),
        )

    monkeypatch.setattr(tts_auth, "urlopen", fake_urlopen)
    profile = tts_auth.TrackerAuthProfile("bussma", "employee@example.com", "device-id", "device-token")

    with pytest.raises(tts_auth.TrackerAuthError) as raised:
        tts_auth.TrackerAuthClient().refresh(profile)

    assert raised.value.status_code == 401
    assert raised.value.code == "tracker.relogin_required"


def test_logout_uses_saved_device_token_and_accepts_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> _EmptyResponse:
        captured["request"] = request
        return _EmptyResponse()

    monkeypatch.setattr(tts_auth, "urlopen", fake_urlopen)
    profile = tts_auth.TrackerAuthProfile(
        "bussma",
        "employee@example.com",
        "device-id",
        "device-token",
    )

    tts_auth.TrackerAuthClient().logout(profile)

    request = captured["request"]
    assert request.full_url == "https://apiv2.bussma.com/api/tracker/auth/logout"
    assert request.get_header("X-tenant") == "bussma"
    assert json.loads(request.data) == {"deviceId": "device-id", "deviceToken": "device-token"}


def test_server_url_must_use_https() -> None:
    with pytest.raises(tts_auth.TrackerAuthError) as raised:
        tts_auth.TrackerAuthClient("http://apiv2.bussma.com")

    assert raised.value.code == "tracker.server_url_invalid"


def test_configured_server_url_is_used_for_login(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> _Response:
        captured["request"] = request
        return _Response(
            {
                "deviceToken": "device-token",
                "tenantIdentifier": "bussma",
                "login": {"token": "access-token"},
            }
        )

    monkeypatch.setattr(tts_auth, "urlopen", fake_urlopen)
    profile = tts_auth.TrackerAuthProfile(
        "bussma",
        "employee@example.com",
        "device-id",
        server_url="https://tracker.example.test",
    )

    tts_auth.TrackerAuthClient(profile.server_url).login(profile, "secret-password")

    assert captured["request"].full_url == "https://tracker.example.test/api/tracker/auth/login"


def test_saved_profile_is_encrypted_and_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    protected_values: dict[bytes, bytes] = {}

    def protect(data: bytes) -> bytes:
        protected_values[b"opaque-dpapi-ciphertext"] = data
        return b"opaque-dpapi-ciphertext"

    monkeypatch.setattr(tts_auth, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(tts_auth, "_protect", protect)
    monkeypatch.setattr(tts_auth, "_unprotect", lambda data: protected_values[data])
    profile = tts_auth.TrackerAuthProfile(
        "bussma",
        "employee@example.com",
        "device-id",
        "private-device-token",
        "https://tracker.example.test",
    )

    path = tts_auth.save_profile(profile)

    assert b"private-device-token" not in path.read_bytes()
    assert b"employee@example.com" not in path.read_bytes()
    assert tts_auth.load_profile() == profile


def test_clear_profile_removes_saved_device_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(tts_auth, "get_app_dir", lambda: tmp_path)
    path = tmp_path / "tracker_device_auth.bin"
    path.write_bytes(b"opaque")

    assert tts_auth.clear_profile()
    assert not path.exists()
    assert not tts_auth.clear_profile()


def test_startup_prompts_again_when_device_refresh_is_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    from time_tracker import main as app_main
    from time_tracker.core.config import Config
    from time_tracker.ui import dialogs

    old_profile = tts_auth.TrackerAuthProfile(
        "bussma",
        "employee@example.com",
        "stable-device-id",
        "revoked-device-token",
        server_url="https://tracker.example.test",
    )
    new_profile = tts_auth.TrackerAuthProfile(
        "bussma",
        "employee@example.com",
        "stable-device-id",
        "fresh-device-token",
        server_url="https://tracker.example.test",
    )
    expected_session = tts_auth.TrackerAuthSession(
        new_profile,
        "fresh-access-token",
        "2026-11-27T00:00:00Z",
        "employee-1",
        "Employee One",
    )
    saved_profiles: list[tts_auth.TrackerAuthProfile] = []
    prompt: dict[str, str] = {}
    client_urls: list[str] = []

    class _Client:
        def __init__(self, server_url: str) -> None:
            client_urls.append(server_url)

        def refresh(self, _profile: tts_auth.TrackerAuthProfile) -> tts_auth.TrackerAuthSession:
            raise tts_auth.TrackerAuthError(401, "tracker.relogin_required", "Sign in again.")

        def login(self, profile: tts_auth.TrackerAuthProfile, password: str) -> tts_auth.TrackerAuthSession:
            assert profile.tenant == "bussma"
            assert profile.email == "employee@example.com"
            assert profile.device_id == "stable-device-id"
            assert password == "new-password"
            return expected_session

    def show_login(on_submit, _on_cancel, **kwargs: object) -> None:
        prompt["message"] = str(kwargs["error_message"])
        on_submit("bussma", "employee@example.com", "new-password", str(kwargs["server_url"]))

    monkeypatch.setattr(app_main, "load_profile", lambda: old_profile)
    monkeypatch.setattr(app_main, "save_profile", saved_profiles.append)
    monkeypatch.setattr(app_main, "TrackerAuthClient", _Client)
    monkeypatch.setattr(dialogs, "show_tracker_login_dialog_async", show_login)

    session = app_main._authenticate_tracker_at_startup(Config())

    assert session == expected_session
    assert prompt["message"] == "Your saved sign-in is no longer valid. Sign in again; your password may have changed or device access may have been revoked."
    assert client_urls == ["https://tracker.example.test", "https://tracker.example.test"]
    assert saved_profiles[0].device_token == ""
    assert saved_profiles[-1] == new_profile
