"""Authentication and per-Windows-user storage for the TTS server."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from time_tracker.core.config import get_app_dir

logger = logging.getLogger(__name__)

TRACKER_API_BASE_URL = "https://apiv2.bussma.com"
TRACKER_AUTH_TIMEOUT_SECONDS = 10
_CREDENTIALS_FILE = "tracker_device_auth.bin"


class TrackerCredentialStoreError(RuntimeError):
    """Raised when the device credentials cannot be safely persisted."""


class TrackerAuthError(RuntimeError):
    def __init__(self, status_code: int | None, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    @property
    def is_temporary(self) -> bool:
        return self.status_code is None or self.status_code == 429 or self.status_code >= 500


@dataclass(frozen=True)
class TrackerAuthProfile:
    tenant: str
    email: str
    device_id: str
    device_token: str = ""
    server_url: str = TRACKER_API_BASE_URL

    @classmethod
    def new(cls) -> TrackerAuthProfile:
        return cls("", "", str(uuid.uuid4()))


@dataclass(frozen=True)
class TrackerAuthSession:
    profile: TrackerAuthProfile
    access_token: str
    expiry_date: str | None
    user_id: str | None
    username: str | None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi() -> tuple[Any, Any]:
    if os.name != "nt":
        raise TrackerCredentialStoreError("Windows DPAPI is required to store tracker credentials.")
    return (
        ctypes.WinDLL("crypt32", use_last_error=True),
        ctypes.WinDLL("kernel32", use_last_error=True),
    )


def _protect(plaintext: bytes) -> bytes:
    crypt32, kernel32 = _dpapi()
    buffer = ctypes.create_string_buffer(plaintext, len(plaintext))
    blob_in = _DataBlob(len(plaintext), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "BUSSMA TTS device credentials",
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect(ciphertext: bytes) -> bytes:
    crypt32, kernel32 = _dpapi()
    buffer = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    blob_in = _DataBlob(len(ciphertext), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def credentials_path() -> Path:
    return get_app_dir() / _CREDENTIALS_FILE


def save_profile(profile: TrackerAuthProfile) -> Path:
    """Encrypt the server and device profile with the current user's DPAPI key."""
    if not profile.device_id.strip():
        raise ValueError("A device id is required.")
    data = json.dumps(
        {
            "tenant": profile.tenant.strip(),
            "email": profile.email.strip(),
            "device_id": profile.device_id.strip(),
            "device_token": profile.device_token.strip(),
            "server_url": profile.server_url.strip(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        encrypted = _protect(data)
        path = credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encrypted)
        return path
    except (OSError, UnicodeError, TrackerCredentialStoreError) as exc:
        raise TrackerCredentialStoreError("Could not securely save the TTS device credentials.") from exc


def load_profile() -> TrackerAuthProfile | None:
    """Load the encrypted profile; return None when this Windows user has not signed in."""
    path = credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Stored tracker profile is not an object.")
        values = (
            data.get("tenant", ""),
            data.get("email", ""),
            data.get("device_id", ""),
            data.get("device_token", ""),
            data.get("server_url", TRACKER_API_BASE_URL),
        )
        if not all(isinstance(value, str) for value in values):
            raise ValueError("Stored tracker profile contains invalid values.")
        profile = TrackerAuthProfile(
            tenant=values[0],
            email=values[1],
            device_id=values[2],
            device_token=values[3],
            server_url=values[4],
        )
        if not profile.device_id.strip():
            raise ValueError("Stored tracker profile has no device id.")
        return profile
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, TrackerCredentialStoreError) as exc:
        logger.warning("Stored TTS credentials could not be read; asking the employee to sign in again: %s", exc)
        return None


def clear_profile() -> bool:
    """Remove the encrypted device profile after a successful server logout."""
    path = credentials_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        raise TrackerCredentialStoreError("Could not remove the saved TTS device credentials.") from exc


def device_name() -> str:
    return platform.node()[:200] or "Windows desktop"


class TrackerAuthClient:
    def __init__(
        self,
        base_url: str = TRACKER_API_BASE_URL,
        timeout: int = TRACKER_AUTH_TIMEOUT_SECONDS,
    ) -> None:
        server_url = base_url.strip()
        try:
            parts = urlsplit(server_url)
            valid = (
                parts.scheme.lower() == "https"
                and bool(parts.hostname)
                and not parts.username
                and not parts.password
                and not parts.query
                and not parts.fragment
                and not any(char.isspace() for char in server_url)
            )
            parts.port
        except ValueError:
            valid = False
        if not valid:
            raise TrackerAuthError(
                400,
                "tracker.server_url_invalid",
                "Enter a valid HTTPS server URL.",
            )
        self.base_url = server_url.rstrip("/")
        self.timeout = timeout

    def login(self, profile: TrackerAuthProfile, password: str) -> TrackerAuthSession:
        if not profile.tenant.strip() or not profile.email.strip() or not password:
            raise ValueError("Tenant, email, and password are required.")
        payload = self._post(
            "/api/tracker/auth/login",
            profile.tenant,
            {
                "email": profile.email.strip(),
                "password": password,
                "deviceId": profile.device_id,
                "deviceName": device_name(),
            },
        )
        return self._session(payload, profile)

    def refresh(self, profile: TrackerAuthProfile) -> TrackerAuthSession:
        if not profile.device_token:
            raise ValueError("A device token is required to refresh the tracker login.")
        payload = self._post(
            "/api/tracker/auth/token",
            profile.tenant,
            {"deviceId": profile.device_id, "deviceToken": profile.device_token},
        )
        return self._session(payload, profile)

    def logout(self, profile: TrackerAuthProfile) -> None:
        if not profile.device_token:
            raise ValueError("A device token is required to log out of the tracker.")
        self._post(
            "/api/tracker/auth/logout",
            profile.tenant,
            {"deviceId": profile.device_id, "deviceToken": profile.device_token},
            allow_empty=True,
        )

    def _post(
        self,
        route: str,
        tenant: str,
        payload: dict[str, str],
        *,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        request = Request(
            self.base_url + route,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Tenant": tenant.strip(),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                error_body = {}
            code, message = _problem_details(error_body)
            raise TrackerAuthError(exc.code, code, message) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TrackerAuthError(None, "network_error", str(exc) or "The ERP server is unavailable.") from exc

        if not body and allow_empty:
            return {}
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TrackerAuthError(200, "invalid_response", "The ERP server returned an invalid response.") from exc
        if not isinstance(result, dict):
            raise TrackerAuthError(200, "invalid_response", "The ERP server returned an invalid response.")
        return result

    @staticmethod
    def _session(payload: dict[str, Any], profile: TrackerAuthProfile) -> TrackerAuthSession:
        login = payload.get("login")
        if not isinstance(login, dict):
            raise TrackerAuthError(200, "invalid_response", "The ERP server returned an incomplete login response.")
        access_token = login.get("token")
        device_token = payload.get("deviceToken", profile.device_token)
        if not isinstance(access_token, str) or not access_token or not isinstance(device_token, str) or not device_token:
            raise TrackerAuthError(200, "invalid_response", "The ERP server returned an incomplete login response.")
        updated_profile = TrackerAuthProfile(
            tenant=str(payload.get("tenantIdentifier") or profile.tenant).strip(),
            email=profile.email.strip(),
            device_id=profile.device_id,
            device_token=device_token,
            server_url=profile.server_url,
        )
        user_id = login.get("userId")
        username = login.get("username")
        return TrackerAuthSession(
            profile=updated_profile,
            access_token=access_token,
            expiry_date=login.get("expiryDate") if isinstance(login.get("expiryDate"), str) else None,
            user_id=str(user_id) if user_id is not None else None,
            username=str(username) if username is not None else None,
        )


def _problem_details(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "http_error", "The ERP server rejected the request."
    extensions = data.get("extensions")
    error = data.get("error")
    candidates = [data, extensions, error]
    code = ""
    message = ""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if not code:
            value = candidate.get("code") or candidate.get("errorCode")
            if isinstance(value, str):
                code = value
        if not message:
            value = candidate.get("detail") or candidate.get("message") or candidate.get("title")
            if isinstance(value, str):
                message = value
    return code or "http_error", message or "The ERP server rejected the request."
