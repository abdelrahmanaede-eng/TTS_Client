"""SignalR lifecycle and documented TTS Tracker hub messages."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Protocol

from signalrcore.hub.errors import HubError, UnAuthorizedHubError
from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.protocol.json_hub_protocol import JsonHubProtocol

from time_tracker.core.config import APP_VERSION
from time_tracker.system.tts_api import (
    TrackerApiClient,
    TrackerApiError,
    TrackerHelloData,
    TrackerReauthenticationRequired,
    TrackerServerSettings,
    TrackerWorkSession,
    local_time_zone_id,
    parse_hello,
    parse_session,
    parse_settings,
)

logger = logging.getLogger(__name__)

HUB_PATH = "/hubs/tracker"
SESSION_CHANGED = "SessionChanged"
SETTINGS_CHANGED = "SettingsChanged"
RELOGIN_REQUIRED = "ReloginRequired"
RECONNECT_DELAYS_SECONDS = (1, 3, 5, 10, 15)
HELLO_TIMEOUT_SECONDS = 15
STABLE_CONNECTION_SECONDS = 60


class HubConnection(Protocol):
    def on(self, event: str, callback: Callable[[Any], None]) -> None: ...
    def on_open(self, callback: Callable[[], None]) -> None: ...
    def on_close(self, callback: Callable[[], None]) -> None: ...
    def on_error(self, callback: Callable[[Any], None]) -> None: ...
    def start(self) -> Any: ...
    def stop(self) -> Any: ...
    def invoke(
        self,
        method: str,
        arguments: list[Any],
        on_invocation: Callable[[Any], None] | None = None,
    ) -> Any: ...


ConnectionFactory = Callable[[str, str], HubConnection]


class TrackerSignalRClient:
    def __init__(
        self,
        api_client: TrackerApiClient,
        *,
        on_hello: Callable[[TrackerHelloData], None],
        on_session_changed: Callable[[TrackerWorkSession | None], None],
        on_settings_changed: Callable[[TrackerServerSettings], None],
        on_relogin_required: Callable[[str | None], None],
        on_status_changed: Callable[[str], None],
        connection_factory: ConnectionFactory | None = None,
        hello_timeout: int = HELLO_TIMEOUT_SECONDS,
        reconnect_delays: tuple[int, ...] = RECONNECT_DELAYS_SECONDS,
    ) -> None:
        self.api_client = api_client
        self._on_hello = on_hello
        self._on_session_changed = on_session_changed
        self._on_settings_changed = on_settings_changed
        self._on_relogin_required = on_relogin_required
        self._on_status_changed = on_status_changed
        self._connection_factory = connection_factory or self._build_connection
        self._hello_timeout = hello_timeout
        self._reconnect_delays = reconnect_delays
        self._stop_event = threading.Event()
        self._closed_event = threading.Event()
        self._retry_event = threading.Event()
        self._lock = threading.RLock()
        self._connection: HubConnection | None = None
        self._worker: threading.Thread | None = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connection is not None and not self._closed_event.is_set()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._closed_event.clear()
            self._retry_event.clear()
            self._worker = threading.Thread(
                target=self._run,
                name="TrackerSignalR",
                daemon=True,
            )
            self._worker.start()

    def retry(self) -> None:
        self._retry_event.set()

    def request_stop(self) -> None:
        self._stop_event.set()
        self._retry_event.set()
        self._closed_event.set()

    def stop(self) -> None:
        self.request_stop()
        with self._lock:
            connection = self._connection
            worker = self._worker
        if connection is not None:
            try:
                connection.stop()
            except Exception:
                logger.debug("Error stopping the TTS SignalR connection.", exc_info=True)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)

    def handle_session_changed(self, arguments: Any) -> None:
        try:
            value = _single_argument(arguments, SESSION_CHANGED)
            session = parse_session(value)
        except TrackerApiError as exc:
            logger.warning("Ignoring invalid SessionChanged payload: %s", exc.message)
            return
        try:
            self._on_session_changed(session)
        except Exception:
            logger.exception("Could not apply a TTS SessionChanged update.")

    def handle_settings_changed(self, arguments: Any) -> None:
        try:
            value = _single_argument(arguments, SETTINGS_CHANGED)
            settings = parse_settings(value)
            if settings is None:
                raise TrackerApiError(200, "invalid_response", "SettingsChanged payload cannot be null.")
        except TrackerApiError as exc:
            logger.warning("Ignoring invalid SettingsChanged payload: %s", exc.message)
            return
        try:
            self._on_settings_changed(settings)
        except Exception:
            logger.exception("Could not apply a TTS SettingsChanged update.")

    def handle_relogin_required(self, arguments: Any) -> None:
        try:
            payload = _single_argument(arguments, RELOGIN_REQUIRED)
            if not isinstance(payload, dict):
                raise TrackerApiError(200, "invalid_response", "ReloginRequired payload must be an object.")
            if "deviceId" not in payload or "reason" not in payload:
                raise TrackerApiError(
                    200,
                    "invalid_response",
                    "ReloginRequired payload is missing deviceId or reason.",
                )
            device_id = payload["deviceId"]
            reason = payload["reason"]
            if device_id is not None and not isinstance(device_id, str):
                raise TrackerApiError(200, "invalid_response", "ReloginRequired deviceId must be a string or null.")
            if reason is not None and not isinstance(reason, str):
                raise TrackerApiError(200, "invalid_response", "ReloginRequired reason must be a string or null.")
        except TrackerApiError as exc:
            logger.warning("Ignoring invalid ReloginRequired payload: %s", exc.message)
            return

        current_device_id = self.api_client.session.profile.device_id
        if device_id is not None and device_id.casefold() != current_device_id.casefold():
            logger.info("Ignoring ReloginRequired for a different TTS device.")
            return
        try:
            self._on_relogin_required(reason)
        except Exception:
            logger.exception("Could not start the TTS reauthentication flow.")
        finally:
            self.request_stop()

    def _build_connection(self, url: str, access_token: str) -> HubConnection:
        # signalrcore 1.1.0 calls access_token_factory only in AuthHubConnection.start().
        # Its automatic transport reconnect reuses the old Authorization header.
        # We therefore create a fresh connection per reconnect with a current bearer header.
        return (
            HubConnectionBuilder()
            .with_url(url, options={"headers": {"Authorization": f"Bearer {access_token}"}})
            .with_hub_protocol(JsonHubProtocol())
            .configure_logging(logging.WARNING)
            .build()
        )

    def _run(self) -> None:
        failures = 0
        first_connection = True
        last_connected_at = 0.0
        while not self._stop_event.is_set():
            if not first_connection:
                if failures > len(self._reconnect_delays):
                    self._set_status("disconnected")
                    self._retry_event.wait()
                    self._retry_event.clear()
                    failures = 0
                    if self._stop_event.is_set():
                        return
                else:
                    delay = self._reconnect_delays[failures - 1]
                    if self._stop_event.wait(delay):
                        return
                try:
                    current_token = self.api_client.session.access_token
                    self.api_client.refresh_access_token(
                        force=True,
                        expected_access_token=current_token,
                    )
                except TrackerReauthenticationRequired:
                    self._set_status("reauth_required")
                    return
                except TrackerApiError as exc:
                    logger.warning("Could not refresh the TTS token before reconnecting: %s", exc.code)
                    failures += 1
                    self._set_status("disconnected")
                    first_connection = False
                    continue

            connected_at = time.monotonic()
            try:
                connection = self._connect_with_auth_recovery()
            except TrackerReauthenticationRequired:
                self._set_status("reauth_required")
                return
            except TrackerApiError as exc:
                logger.warning("Could not connect to the TTS hub: %s", exc.code)
                failures += 1
                first_connection = False
                self._set_status("disconnected")
                continue
            except Exception as exc:
                logger.warning("Could not connect to the TTS hub: %s", exc)
                failures += 1
                first_connection = False
                self._set_status("disconnected")
                continue

            with self._lock:
                self._connection = connection
            self._set_status("connected")
            self._retry_event.clear()
            first_connection = False
            last_connected_at = connected_at

            while not self._stop_event.is_set() and not self._closed_event.wait(0.25):
                pass

            try:
                connection.stop()
            except Exception:
                logger.debug("Error closing a disconnected TTS hub connection.", exc_info=True)
            with self._lock:
                if self._connection is connection:
                    self._connection = None
            if self._stop_event.is_set():
                return

            if time.monotonic() - last_connected_at >= STABLE_CONNECTION_SECONDS:
                failures = 0
            failures += 1
            self._set_status("disconnected")

    def _connect_with_auth_recovery(self) -> HubConnection:
        rejected_token = self.api_client.session.access_token
        try:
            return self._connect_once(rejected_token)
        except UnAuthorizedHubError:
            logger.info("TTS hub rejected the access token; refreshing once before reconnecting.")
            try:
                session = self.api_client.refresh_access_token(
                    force=True,
                    expected_access_token=rejected_token,
                )
            except TrackerReauthenticationRequired:
                raise
            except TrackerApiError as exc:
                if exc.status_code == 401:
                    self.api_client.notify_reauthentication_required()
                    raise TrackerReauthenticationRequired(
                        exc.status_code,
                        exc.code,
                        exc.message,
                    ) from exc
                raise

        try:
            return self._connect_once(session.access_token)
        except UnAuthorizedHubError as exc:
            self.api_client.notify_reauthentication_required()
            raise TrackerReauthenticationRequired(
                401,
                "hub.unauthorized",
                "The refreshed access token was rejected by the TTS hub.",
            ) from exc

    def _connect_once(self, access_token: str) -> HubConnection:
        url = self.api_client.session.profile.server_url.rstrip("/") + HUB_PATH
        connection = self._connection_factory(url, access_token)
        self._closed_event.clear()
        connection.on(SESSION_CHANGED, self.handle_session_changed)
        connection.on(SETTINGS_CHANGED, self.handle_settings_changed)
        connection.on(RELOGIN_REQUIRED, self.handle_relogin_required)
        connection.on_close(self._closed_event.set)
        connection.on_error(self._on_hub_error)
        self._set_status("connecting")
        try:
            connection.start()
            result = _invoke_hello(connection, self._hello_timeout)
            self._on_hello(result)
            return connection
        except Exception:
            try:
                connection.stop()
            except Exception:
                logger.debug("Error stopping an unsuccessful TTS hub connection.", exc_info=True)
            raise

    def _on_hub_error(self, error: Any) -> None:
        logger.warning("TTS hub invocation failed: %s", getattr(error, "error", error))

    def _set_status(self, status: str) -> None:
        try:
            self._on_status_changed(status)
        except Exception:
            logger.exception("Could not report TTS hub connection state.")


def _single_argument(arguments: Any, event_name: str) -> Any:
    if not isinstance(arguments, list) or len(arguments) != 1:
        raise TrackerApiError(
            200,
            "invalid_response",
            f"{event_name} must contain exactly one argument.",
        )
    return arguments[0]


def _invoke_hello(connection: HubConnection, timeout: int) -> TrackerHelloData:
    completed = threading.Event()
    result: dict[str, Any] = {}

    def on_invocation(message: Any) -> None:
        result["message"] = message
        completed.set()

    connection.invoke(
        "Hello",
        [{
            "clientVersion": APP_VERSION,
            "timeZoneId": local_time_zone_id(),
        }],
        on_invocation,
    )
    if not completed.wait(timeout):
        raise TimeoutError("The TTS hub did not return a Hello response in time.")

    message = result.get("message")
    if message is None:
        raise TrackerApiError(200, "invalid_response", "The TTS hub returned no Hello response.")
    if getattr(message, "error", None):
        raise HubError(str(message.error))

    return parse_hello(getattr(message, "result", None))
