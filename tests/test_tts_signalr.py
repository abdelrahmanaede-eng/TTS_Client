from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from signalrcore.hub.errors import UnAuthorizedHubError

from time_tracker.core.config import Config
from time_tracker.core.database import Database
from time_tracker.core.tracker import ActivityTracker
from time_tracker.system import tts_signalr
from time_tracker.system.tts_api import (
    TrackerHelloData,
    TrackerReauthenticationRequired,
    TrackerServerSettings,
    TrackerWorkSession,
)
from time_tracker.system.tts_auth import TrackerAuthProfile, TrackerAuthSession


def _session(token: str = "access-token") -> TrackerAuthSession:
    profile = TrackerAuthProfile(
        "tenant",
        "employee@example.com",
        "device-id",
        "device-token",
        "https://apiv2.bussma.com",
    )
    return TrackerAuthSession(profile, token, None, "employee-1", "Employee One", profile.email)


def _hello_payload() -> dict[str, object]:
    return {
        "session": {
            "attendanceId": 10,
            "projectId": 25,
            "projectName": "ERP Project",
            "checkInUtc": "2026-11-26T08:00:00Z",
            "open": True,
            "trackerAllowed": True,
        },
        "settings": {
            "trackingEnabled": True,
            "appTracking": True,
            "idleThresholdSeconds": 180,
            "presenceCheckMinutes": 3,
        },
        "shift": None,
        "holidays": [],
        "serverTimeUtc": "2026-11-26T08:00:00Z",
    }


class _ApiClient:
    def __init__(self) -> None:
        self.session = _session()
        self.refresh_calls: list[tuple[bool, str | None]] = []
        self.refresh_error: Exception | None = None
        self.reauthentication_notified = 0

    def set_relogin_handler(self, _handler) -> None:
        pass

    def refresh_access_token(
        self,
        *,
        force: bool = False,
        expected_access_token: str | None = None,
    ) -> TrackerAuthSession:
        self.refresh_calls.append((force, expected_access_token))
        if self.refresh_error is not None:
            raise self.refresh_error
        if expected_access_token is not None and self.session.access_token != expected_access_token:
            return self.session
        self.session = _session("refreshed-access-token")
        return self.session

    def notify_reauthentication_required(self) -> None:
        self.reauthentication_notified += 1


class _FakeHub:
    def __init__(self, hello: object | None = None) -> None:
        self.hello = hello if hello is not None else _hello_payload()
        self.handlers: dict[str, object] = {}
        self.close_handler = None
        self.error_handler = None
        self.started = False
        self.stopped = False
        self.invocations: list[tuple[str, list[object]]] = []

    def on(self, event: str, callback) -> None:
        self.handlers[event] = callback

    def on_open(self, callback) -> None:
        self.open_handler = callback

    def on_close(self, callback) -> None:
        self.close_handler = callback

    def on_error(self, callback) -> None:
        self.error_handler = callback

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def invoke(self, method: str, arguments: list[object], on_invocation=None):
        self.invocations.append((method, arguments))
        on_invocation(SimpleNamespace(result=self.hello, error=None))

    def close(self) -> None:
        if self.close_handler:
            self.close_handler()


def _client(api_client: _ApiClient, factory, **callbacks) -> tts_signalr.TrackerSignalRClient:
    defaults = {
        "on_hello": lambda _data: None,
        "on_session_changed": lambda _session: None,
        "on_settings_changed": lambda _settings: None,
        "on_relogin_required": lambda _reason: None,
        "on_status_changed": lambda _status: None,
    }
    defaults.update(callbacks)
    return tts_signalr.TrackerSignalRClient(
        api_client,
        connection_factory=factory,
        hello_timeout=1,
        reconnect_delays=(0,),
        **defaults,
    )


def test_initial_connection_uses_hub_url_bearer_and_invokes_hello(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_client = _ApiClient()
    hub = _FakeHub()
    captured: dict[str, object] = {}
    snapshots: list[TrackerHelloData] = []

    def factory(url: str, token: str) -> _FakeHub:
        captured.update(url=url, token=token)
        return hub

    monkeypatch.setattr(tts_signalr, "local_time_zone_id", lambda: "Africa/Cairo")
    client = _client(api_client, factory, on_hello=snapshots.append)

    connection = client._connect_once(api_client.session.access_token)

    assert connection is hub
    assert captured == {
        "url": "https://apiv2.bussma.com/hubs/tracker",
        "token": "access-token",
    }
    assert set(hub.handlers) == {
        "SessionChanged",
        "SettingsChanged",
        "ReloginRequired",
    }
    method, arguments = hub.invocations[0]
    assert method == "Hello"
    assert arguments == [{"clientVersion": "1.4.5", "timeZoneId": "Africa/Cairo"}]
    assert snapshots[0].session is not None
    assert snapshots[0].session.project_name == "ERP Project"


def test_builder_uses_documented_authorization_header_without_query_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    hub = _FakeHub()

    class _Builder:
        def with_url(self, url, options):
            captured["url"] = url
            captured["options"] = options
            return self

        def with_hub_protocol(self, protocol):
            captured["protocol"] = protocol
            return self

        def configure_logging(self, level):
            captured["logging"] = level
            return self

        def build(self):
            return hub

    monkeypatch.setattr(tts_signalr, "HubConnectionBuilder", _Builder)
    client = _client(_ApiClient(), lambda _url, _token: hub)

    assert client._build_connection("https://apiv2.bussma.com/hubs/tracker", "secret-token") is hub
    assert captured["url"] == "https://apiv2.bussma.com/hubs/tracker"
    assert captured["options"] == {"headers": {"Authorization": "Bearer secret-token"}}
    assert "access_token" not in captured["url"]


@pytest.mark.parametrize(
    ("payload", "expected_project"),
    [
        (
            {
                "attendanceId": 10,
                "projectId": 25,
                "projectName": "Changed Project",
                "checkInUtc": "2026-11-26T08:00:00Z",
                "open": True,
                "trackerAllowed": True,
            },
            "Changed Project",
        ),
        (None, None),
    ],
)
def test_session_changed_updates_or_clears_remote_session(
    payload: object,
    expected_project: str | None,
) -> None:
    api_client = _ApiClient()
    received: list[TrackerWorkSession | None] = []
    client = _client(api_client, lambda _url, _token: _FakeHub(), on_session_changed=received.append)

    client.handle_session_changed([payload])

    assert len(received) == 1
    assert (received[0].project_name if received[0] else None) == expected_project


def test_settings_changed_updates_only_the_server_settings_callback() -> None:
    received: list[TrackerServerSettings] = []
    client = _client(_ApiClient(), lambda _url, _token: _FakeHub(), on_settings_changed=received.append)
    payload = {
        "trackingEnabled": False,
        "appTracking": False,
        "idleThresholdSeconds": 300,
        "presenceCheckMinutes": 0,
    }

    client.handle_settings_changed([payload])

    assert received == [TrackerServerSettings(False, False, 300, 0)]


def test_relogin_required_only_accepts_null_or_matching_device_id() -> None:
    reasons: list[str | None] = []
    client = _client(
        _ApiClient(),
        lambda _url, _token: _FakeHub(),
        on_relogin_required=reasons.append,
    )

    client.handle_relogin_required([{"deviceId": "another-device", "reason": "revoked"}])
    assert reasons == []
    assert not client._stop_event.is_set()

    client.handle_relogin_required([{"deviceId": "DEVICE-ID", "reason": "revoked"}])
    assert reasons == ["revoked"]
    assert client._stop_event.is_set()

    client._stop_event.clear()
    client.handle_relogin_required([{"deviceId": None, "reason": "account changed"}])
    assert reasons[-1] == "account changed"


def test_reconnect_refreshes_token_and_runs_hello_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_client = _ApiClient()
    made_hubs: list[_FakeHub] = []
    used_tokens: list[str] = []
    hello_snapshots: list[TrackerHelloData] = []
    second_hello = threading.Event()

    def factory(url: str, token: str) -> _FakeHub:
        used_tokens.append(token)
        hub = _FakeHub()
        made_hubs.append(hub)
        if len(made_hubs) == 1:
            original_invoke = hub.invoke

            def invoke_then_close(method: str, arguments: list[object], on_invocation=None):
                original_invoke(method, arguments, on_invocation)
                threading.Timer(0.02, hub.close).start()

            hub.invoke = invoke_then_close
        else:
            original_invoke = hub.invoke

            def invoke_and_mark(method: str, arguments: list[object], on_invocation=None):
                original_invoke(method, arguments, on_invocation)
                second_hello.set()

            hub.invoke = invoke_and_mark
        return hub

    monkeypatch.setattr(tts_signalr, "local_time_zone_id", lambda: "Africa/Cairo")
    client = _client(api_client, factory, on_hello=hello_snapshots.append)

    client.start()
    assert second_hello.wait(3)
    client.stop()

    assert used_tokens == ["access-token", "refreshed-access-token"]
    assert len(hello_snapshots) == 2
    assert api_client.refresh_calls == [(True, "access-token")]
    assert all(hub.invocations[0][0] == "Hello" for hub in made_hubs)


def test_expired_hub_token_and_failed_refresh_require_sign_in() -> None:
    api_client = _ApiClient()
    api_client.refresh_error = TrackerReauthenticationRequired(
        401,
        "tracker.relogin_required",
        "Sign in again.",
    )
    connection_attempts = 0

    def factory(_url: str, _token: str) -> _FakeHub:
        nonlocal connection_attempts
        connection_attempts += 1
        raise UnAuthorizedHubError()

    client = _client(api_client, factory)

    with pytest.raises(TrackerReauthenticationRequired):
        client._connect_with_auth_recovery()

    assert connection_attempts == 1
    assert api_client.refresh_calls == [(True, "access-token")]


def test_expired_hub_token_refreshes_once_and_retries_with_latest_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_client = _ApiClient()
    tokens: list[str] = []
    hub = _FakeHub()

    def factory(_url: str, token: str):
        tokens.append(token)
        if len(tokens) == 1:
            raise UnAuthorizedHubError()
        return hub

    monkeypatch.setattr(tts_signalr, "local_time_zone_id", lambda: "Africa/Cairo")
    client = _client(api_client, factory)

    assert client._connect_with_auth_recovery() is hub
    assert tokens == ["access-token", "refreshed-access-token"]
    assert api_client.refresh_calls == [(True, "access-token")]
    assert hub.invocations[0][0] == "Hello"


def test_network_outage_does_not_stop_local_tracking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from time_tracker.ui import tray

    database = Database(tmp_path / "tracker.db")
    tracker = ActivityTracker(database, Config())
    tracker._running = True
    app = tray.TrayApp(database, Config(), tracker, api_client=_ApiClient())
    app._icon = object()
    queued: list[tuple[object, object]] = []
    monkeypatch.setattr(
        tray,
        "dispatch_to_ui",
        lambda callback, *, fallback=None: queued.append((callback, fallback)),
    )

    app._on_signalr_status_changed("disconnected")

    assert tracker._running
    assert len(queued) == 1
    assert app._signalr_status == "disconnected"


def test_signalr_outage_refreshes_session_from_documented_http_endpoint(
    tmp_path,
) -> None:
    from time_tracker.ui import tray

    database = Database(tmp_path / "tracker.db")
    app = tray.TrayApp(database, Config(), ActivityTracker(database, Config()), api_client=_ApiClient())
    app._server_load_state = "ready"
    app._server_snapshot = TrackerHelloData(None, None, None, (), None)
    requests: list[bool] = []
    app._start_server_request = lambda *, load_hello: requests.append(load_hello)

    app._signalr_status = "disconnected"
    app._refresh_server_session_if_signalr_unavailable()
    assert requests == [False]

    app._signalr_status = "connected"
    app._refresh_server_session_if_signalr_unavailable()
    assert requests == [False]


def test_signalr_session_update_dispatches_tray_mutation_to_ui_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from time_tracker.ui import tray

    database = Database(tmp_path / "tracker.db")
    app = tray.TrayApp(database, Config(), ActivityTracker(database, Config()), api_client=_ApiClient())
    updates: list[object] = []

    class _Icon:
        @property
        def menu(self):
            return None

        @menu.setter
        def menu(self, value):
            updates.append(value)

    app._icon = _Icon()
    app._build_menu = lambda: "rebuilt"
    queued: list[tuple[object, object]] = []
    monkeypatch.setattr(
        tray,
        "dispatch_to_ui",
        lambda callback, *, fallback=None: queued.append((callback, fallback)),
    )
    monkeypatch.setattr(tray, "is_popup_menu_open", lambda: False)

    app._on_signalr_session_changed(
        TrackerWorkSession("attendance", "project", "Server Project", None, True, True)
    )

    assert updates == []
    assert len(queued) == 1
    queued[0][0]()
    assert updates == ["rebuilt"]


def test_logout_confirmation_does_not_block_tray_and_handles_both_answers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from time_tracker.ui import tray

    database = Database(tmp_path / "tracker.db")
    app = tray.TrayApp(
        database,
        Config(),
        ActivityTracker(database, Config()),
        auth_session=_session(),
        api_client=_ApiClient(),
    )
    threads: list[object] = []

    class _Thread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False

        def start(self) -> None:
            self.started = True
            threads.append(self)

    monkeypatch.setattr(tray.threading, "Thread", _Thread)
    app._logout()

    assert len(threads) == 1
    assert threads[0].target == app._confirm_logout_worker
    assert threads[0].started
    assert app._logout_in_progress

    logout_calls: list[bool] = []
    app._logout_worker = lambda: logout_calls.append(True)
    monkeypatch.setattr(tray, "win32_message_box", lambda *_args: 7)
    app._confirm_logout_worker()
    assert logout_calls == []
    assert not app._logout_in_progress

    app._logout_in_progress = True
    monkeypatch.setattr(tray, "win32_message_box", lambda *_args: 6)
    app._confirm_logout_worker()
    assert logout_calls == [True]
