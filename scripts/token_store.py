"""Secure, user-scoped storage for the GitHub upload token.

The token is protected with Windows DPAPI (CryptProtectData / CryptUnprotectData),
so the ciphertext is encrypted at rest and tied to the current Windows account.
It lives OUTSIDE the source tree, in:

    %LOCALAPPDATA%\\TimeTracker\\github_token.bin

There is no plaintext token in source, in the repository, or in the built
binary. Run this file once to store a token:

    python scripts/token_store.py

The uploader (upload_release.py) reads it automatically as a fallback after
--token and the GITHUB_TOKEN environment variable.
"""

from __future__ import annotations

import ctypes
import getpass
import os
import sys
from ctypes import wintypes
from pathlib import Path

SERVICE_NAME = "TimeTracker GitHub Release Uploader"
_STORE_PATH = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    / "TimeTracker"
    / "github_token.bin"
)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _crypt32():
    return ctypes.WinDLL("crypt32", use_last_error=True)


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _protect(plaintext: bytes) -> bytes:
    """Encrypt bytes for the current Windows user with DPAPI."""
    buf = ctypes.create_string_buffer(plaintext, len(plaintext))
    blob_in = _DATA_BLOB(len(plaintext), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DATA_BLOB()
    ok = _crypt32().CryptProtectData(
        ctypes.byref(blob_in),
        SERVICE_NAME,  # LPCWSTR description
        None,          # pOptionalEntropy
        None,          # pvReserved
        None,          # pPromptStruct  (no UI prompt)
        0,             # dwFlags: current user (not CRYPTPROTECT_LOCAL_MACHINE)
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


def _unprotect(ciphertext: bytes) -> bytes:
    """Decrypt DPAPI bytes protected for the current Windows user."""
    buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    blob_in = _DATA_BLOB(len(ciphertext), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DATA_BLOB()
    ok = _crypt32().CryptUnprotectData(
        ctypes.byref(blob_in),
        None,          # ppszDataDescr (not needed)
        None,          # pOptionalEntropy
        None,          # pvReserved
        None,          # pPromptStruct
        0,             # dwFlags
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


def store_path() -> Path:
    """Return the path of the encrypted token file."""
    return _STORE_PATH


def save_token(token: str) -> Path:
    """Encrypt and persist a token; return the file path."""
    token = token.strip()
    if not token:
        raise ValueError("Token must not be empty.")
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ciphertext = _protect(token.encode("utf-8"))
    fd = os.open(str(_STORE_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(ciphertext)
    return _STORE_PATH


def get_stored_token() -> str:
    """Return the decrypted token, or '' when none is stored / cannot decrypt."""
    try:
        if not _STORE_PATH.exists():
            return ""
        return _unprotect(_STORE_PATH.read_bytes()).decode("utf-8").strip()
    except Exception:
        return ""


def clear_token() -> bool:
    """Remove the stored token file. Returns True if something was removed."""
    if _STORE_PATH.exists():
        _STORE_PATH.unlink()
        return True
    return False


def main() -> None:
    if "--clear" in sys.argv:
        print("[OK] Removed stored token." if clear_token() else "[INFO] No stored token to remove.")
        return

    print("========================================================")
    print("  TimeTracker - Store GitHub token (Windows DPAPI)")
    print("========================================================")
    print(f"Encrypted file: {store_path()}")
    print("The token is encrypted by Windows and tied to your user account.\n")
    token = getpass.getpass("Paste GitHub token (input hidden): ").strip()
    if not token:
        print("[ERROR] Empty token - nothing stored.")
        sys.exit(1)

    try:
        save_token(token)
    except Exception as exc:
        print(f"[ERROR] Could not store token: {exc}")
        sys.exit(1)

    if get_stored_token() != token:
        print("[ERROR] Stored token failed verification.")
        sys.exit(1)

    print("\n[OK] Token stored and verified successfully.")
    print("You can now run upload_installer.bat - no --token or env var needed.")


if __name__ == "__main__":
    main()
