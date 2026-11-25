"""Local and online auto-update support for TimeTracker.

Supports both:
1. Online sources (e.g. GitHub Releases / raw manifest URL).
2. Offline sources (local folder or UNC network share).

Manifest format (``version.json``)::

    {
      "version": "1.4.0",
      "installer": "TimeTracker_Setup.exe",
      "download_url": "https://github.com/owner/repo/releases/download/v1.4.0/TimeTracker_Setup.exe",
      "sha256": "<hex sha256 of the installer file>",
      "notes": "Optional release notes"
    }

The SHA-256 checksum is mandatory: an installer whose hash does not match the
manifest is never executed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "version.json"
SILENT_INSTALL_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]

# SEC-02: remote manifests and installers are HTTPS-only; a manifest hash is
# integrity, not authenticity, so cleartext HTTP is never accepted.
HTTPS_URL_RE = re.compile(r"^https://[^\s/]+(?:/[^\s]*)?$", re.IGNORECASE)
MAX_INSTALLER_BYTES = 200 * 1024 * 1024  # 200 MiB, well above a real installer

# SEC-03: anchored, digits-and-dots only — a version is also used to build a
# temp filename, so a prefix-only match would allow path traversal.
VERSION_REGEX = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?$")
GITHUB_REPO_REGEX = re.compile(r"^(?:https://github\.com/)?([a-zA-Z0-9_\-\.]+)/([a-zA-Z0-9_\-\.]+?)(?:\.git)?/?$")
RAW_GITHUB_REGEX = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/([^?#]+)", re.IGNORECASE)


@dataclass
class UpdateInfo:
    version: str
    installer_path: Path | None = None
    download_url: str = ""
    sha256: str = ""
    notes: str = ""


def parse_version(value: str) -> tuple[int, ...]:
    """Parse ``1.2.3`` (or ``v1.2``) into a comparable tuple."""
    match = VERSION_REGEX.match((value or "").strip())
    if not match:
        return (0,)
    parts = [int(p) if p is not None else 0 for p in match.groups()]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(remote_version: str, current_version: str) -> bool:
    """Return True if remote_version is strictly newer than current_version."""
    return parse_version(remote_version) > parse_version(current_version)


def is_valid_version(value: str) -> bool:
    """SEC-03: only a strict ``v?N[.N...]`` version is accepted from a manifest."""
    return bool(VERSION_REGEX.fullmatch((value or "").strip()))


def safe_version_slug(value: str) -> str:
    """SEC-03: filename-safe version (digits and dots only, no traversal)."""
    cleaned = (value or "").strip().lstrip("vV")
    if not is_valid_version(value):
        return "0.0.0"
    return cleaned.replace(".", "_")


def is_https_url(url: str) -> bool:
    """SEC-02: HTTPS only, with a host and no whitespace/control characters."""
    return bool(HTTPS_URL_RE.fullmatch((url or "").strip()))


def verify_authenticode(path: Path, expected_publisher: str = "") -> bool:
    """Opt-in publisher check (SEC-02). Returns True when no publisher is required.

    Hash equality proves the bytes match the manifest, not who published them.
    Callers that ship signed installers can require a valid Authenticode
    signature and, optionally, a substring match of the signer's subject.
    Unsigned builds keep the historical hash-only behaviour by passing "".
    """
    if not expected_publisher:
        return True
    if sys.platform != "win32":
        logger.warning("Authenticode verification requested on a non-Windows host; refusing update.")
        return False
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-AuthenticodeSignature -LiteralPath $args[0]).Status",
                str(path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        status = (result.stdout or "").strip()
        if status != "Valid":
            logger.warning("Installer Authenticode status is %r, expected Valid", status)
            return False
        if expected_publisher:
            subject = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-AuthenticodeSignature -LiteralPath $args[0]).SignerCertificate.Subject",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30, check=False,
            ).stdout or ""
            if expected_publisher.lower() not in subject.lower():
                logger.warning("Installer publisher %r does not match %r", subject, expected_publisher)
                return False
        return True
    except Exception as exc:
        logger.warning("Authenticode verification failed: %s", exc)
        return False


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    """Return True only if the file hash matches ``expected`` (hex, case-insensitive)."""
    expected_clean = (expected or "").strip().lower()
    if not expected_clean or len(expected_clean) != 64:
        return False
    try:
        actual = sha256_file(path).lower()
    except OSError as exc:
        logger.warning("Could not hash installer %s: %s", path, exc)
        return False
    return actual == expected_clean


def is_url(source: str) -> bool:
    """True only for a HTTPS URL (SEC-02: cleartext HTTP is never accepted)."""
    return is_https_url((source or "").strip())


def normalize_update_source(source: str) -> str:
    """Normalize a source string into either a manifest URL or local path.

    Accepts:
    - Direct manifest URL: ``https://.../version.json``
    - GitHub repository URL: ``https://github.com/owner/repo``
    - Short repo name: ``owner/repo``
    - Local directory path: ``C:\\Updates`` or ``\\\\Server\\Share``
    """
    clean = (source or "").strip()
    if not clean:
        return ""
    if clean.lower().startswith("http://"):
        logger.warning("Rejected cleartext HTTP update source (HTTPS required): %s", clean)
        return ""

    # Check if short repo format owner/repo or github url
    m = GITHUB_REPO_REGEX.match(clean)
    if m:
        owner, repo = m.groups()
        # Avoid matching local paths with single slash on windows if not github
        if "/" in clean or "github.com" in clean:
            return f"https://raw.githubusercontent.com/{owner}/{repo}/main/version.json"

    # If it's a URL ending in slash or without version.json
    if is_url(clean) and not clean.lower().endswith(".json"):
        clean = clean.rstrip("/") + "/" + MANIFEST_FILENAME

    return clean


def read_manifest(source_path: str | Path) -> dict | None:
    """Read ``version.json`` from either an online URL or a local folder.
    
    For GitHub raw URLs, queries the GitHub Contents API directly to bypass Fastly
    CDN's 5-minute caching on raw.githubusercontent.com, with fallback to direct HTTP
    fetching using cache-busting headers and query parameters.
    """
    source_str = str(source_path or "").strip()
    if not source_str:
        return None

    norm_source = normalize_update_source(source_str)

    if is_url(norm_source):
        # 1. GitHub Raw URL: bypass Fastly CDN cache by querying GitHub API directly
        m_raw = RAW_GITHUB_REGEX.match(norm_source)
        if m_raw:
            owner, repo, branch, path = m_raw.groups()
            api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}&_nocache={int(time.time())}"
            req_api = urllib.request.Request(
                api_url,
                headers={
                    "User-Agent": "TimeTracker-AutoUpdater",
                    "Accept": "application/vnd.github.raw+json",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                },
            )
            try:
                with urllib.request.urlopen(req_api, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, dict):
                        logger.debug("Fetched fresh manifest from GitHub API for %s/%s", owner, repo)
                        return data
            except Exception as exc:
                logger.debug("GitHub API fetch fallback for %s: %s", api_url, exc)

        # 2. Direct HTTP fetch with cache-busting query parameter and headers
        sep = "&" if "?" in norm_source else "?"
        url_with_cb = f"{norm_source}{sep}_nocache={int(time.time())}"
        req = urllib.request.Request(
            url_with_cb,
            headers={
                "User-Agent": "TimeTracker-AutoUpdater",
                "Accept": "application/json",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("Could not fetch remote update manifest from %s: %s", norm_source, exc)
            return None
        if not isinstance(data, dict):
            logger.warning("Remote update manifest at %s is not a JSON object", norm_source)
            return None
        return data

    # Local file path
    source = Path(source_str)
    manifest_path = source / MANIFEST_FILENAME
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.info("No update manifest found at %s", manifest_path)
        return None
    except Exception as exc:
        logger.warning("Could not read update manifest %s: %s", manifest_path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Update manifest at %s is not a JSON object", manifest_path)
        return None
    return data


def is_plain_filename(name: str) -> bool:
    """True when ``name`` is a bare file name (no path separators, drive, ..)."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or ":" in name:
        return False
    if name != Path(name).name:
        return False
    return not Path(name).is_absolute()


def resolve_installer_path(source: Path, installer_name: str) -> Path | None:
    """Resolve the installer strictly inside ``source`` (P0-7).

    Returns ``None`` when the manifest name is not a plain basename or when the
    resolved path is not a direct child of the update source. This keeps a
    manifest from pointing at an arbitrary executable elsewhere on the machine.
    """
    if not is_plain_filename(installer_name):
        return None
    try:
        source_resolved = source.resolve()
        installer_path = (source_resolved / installer_name).resolve()
    except OSError as exc:
        logger.warning("Could not resolve update installer path: %s", exc)
        return None
    if installer_path.parent != source_resolved:
        return None
    return installer_path


def check_for_update(
    source_path: str | Path,
    current_version: str,
    on_reject: Callable[[str], None] | None = None,
) -> UpdateInfo | None:
    """Return an :class:`UpdateInfo` when the source holds a newer, valid release.

    Supports both online (GitHub / HTTP URL) and offline (local/UNC folder) sources.
    Returns ``None`` when the setting is empty, manifest is missing/invalid,
    the version is not newer, or the installer fails verification.
    """

    def _reject(reason: str) -> None:
        logger.warning("Update rejected: %s", reason)
        if on_reject is not None:
            try:
                on_reject(reason)
            except Exception as exc:
                logger.error("Update rejection callback failed: %s", exc)

    source_str = str(source_path or "").strip()
    if not source_str:
        return None

    manifest = read_manifest(source_str)
    if not manifest:
        return None

    remote_version = str(manifest.get("version") or "").strip()
    if not is_valid_version(remote_version):
        _reject(f"invalid manifest version {remote_version!r}")
        return None
    if not is_newer(remote_version, current_version):
        logger.info("Update source version %s is not newer than %s", remote_version, current_version)
        return None

    sha_expected = str(manifest.get("sha256") or "").strip().lower()
    if len(sha_expected) != 64:
        _reject("manifest has no valid SHA-256 checksum")
        return None

    norm_source = normalize_update_source(source_str)

    # 1. Online URL Source
    if is_url(norm_source):
        download_url = str(manifest.get("download_url") or "").strip()
        installer_name = str(manifest.get("installer") or "TimeTracker_Setup.exe").strip()
        if not is_plain_filename(installer_name):
            _reject(f"unsafe installer name {installer_name!r}")
            return None

        # If download_url was not explicitly given, try constructing from GitHub repo
        if not download_url and "raw.githubusercontent.com" in norm_source:
            parts = norm_source.split("/")
            if len(parts) >= 5:
                owner, repo = parts[3], parts[4]
                slug = safe_version_slug(remote_version)
                download_url = f"https://github.com/{owner}/{repo}/releases/download/v{slug}/{installer_name}"

        if download_url and not is_https_url(download_url):
            _reject(f"download URL must be HTTPS: {download_url!r}")
            return None

        return UpdateInfo(
            version=remote_version,
            installer_path=None,
            download_url=download_url,
            sha256=sha_expected,
            notes=str(manifest.get("notes") or ""),
        )

    # 2. Local Folder Source
    source = Path(source_str)
    installer_name = str(manifest.get("installer") or "").strip()
    if not installer_name:
        _reject("manifest has no installer filename")
        return None

    installer_path = resolve_installer_path(source, installer_name)
    if installer_path is None:
        _reject(f"unsafe installer name {installer_name!r}")
        return None

    if not installer_path.is_file():
        _reject(f"installer not found: {installer_name}")
        return None

    if not verify_sha256(installer_path, sha_expected):
        _reject("installer checksum mismatch")
        return None

    return UpdateInfo(
        version=remote_version,
        installer_path=installer_path,
        download_url="",
        sha256=sha_expected,
        notes=str(manifest.get("notes") or ""),
    )


def download_installer(
    download_url: str,
    dest_path: Path,
    expected_sha256: str,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> bool:
    """Download installer file from URL to dest_path with progress and SHA-256 verification.

    Returns True if downloaded and verified successfully.
    """
    if not download_url or not is_https_url(download_url):
        logger.error("Refusing non-HTTPS download URL: %r", download_url)
        return False

    # SEC-03: never let a manifest-controlled value write outside %TEMP%.
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved_dest = dest_path.resolve()
        if not resolved_dest.is_relative_to(temp_root):
            logger.error("Refusing download destination outside %s: %s", temp_root, resolved_dest)
            return False
    except OSError as exc:
        logger.error("Could not validate download destination: %s", exc)
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = dest_path.with_suffix(dest_path.suffix + ".part")

    req = urllib.request.Request(
        download_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TimeTracker-Updater",
            "Accept": "*/*",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(temp_file, "wb") as out_f:
            # SEC-02: a redirect to plain HTTP must not downgrade the download.
            geturl = getattr(resp, "geturl", None)
            final_url = geturl() if callable(geturl) else None
            if isinstance(final_url, str) and not is_https_url(final_url):
                logger.error("Download redirected to a non-HTTPS URL: %s", final_url)
                out_f.close()
                temp_file.unlink(missing_ok=True)
                return False

            total_size = int(resp.headers.get("Content-Length") or 0)
            if total_size > MAX_INSTALLER_BYTES:
                logger.error("Installer too large (%d bytes); refusing download", total_size)
                out_f.close()
                temp_file.unlink(missing_ok=True)
                return False
            downloaded = 0
            hasher = hashlib.sha256()

            while chunk := resp.read(64 * 1024):
                downloaded += len(chunk)
                if downloaded > MAX_INSTALLER_BYTES:
                    logger.error("Installer exceeded %d bytes; aborting download", MAX_INSTALLER_BYTES)
                    out_f.close()
                    temp_file.unlink(missing_ok=True)
                    return False
                if cancel_flag and cancel_flag():
                    logger.info("Download cancelled by user.")
                    out_f.close()
                    if temp_file.exists():
                        temp_file.unlink()
                    return False

                out_f.write(chunk)
                hasher.update(chunk)

                if progress_callback:
                    try:
                        progress_callback(downloaded, total_size)
                    except Exception:
                        pass

        # Verify checksum
        actual_hash = hasher.hexdigest().lower()
        expected_clean = (expected_sha256 or "").strip().lower()

        if expected_clean and actual_hash != expected_clean:
            logger.error("Downloaded file checksum mismatch: got %s, expected %s", actual_hash, expected_clean)
            if temp_file.exists():
                temp_file.unlink()
            return False

        if dest_path.exists():
            dest_path.unlink()
        temp_file.rename(dest_path)
        logger.info("Installer downloaded and verified successfully: %s", dest_path)
        return True

    except Exception as exc:
        logger.error("Download failed from %s: %s", download_url, exc)
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        return False


def launch_installer(
    target: UpdateInfo | Path | str,
    sha256: str = "",
    required_publisher: str = "",
) -> bool:
    """Re-verify checksum (and optionally Authenticode) then launch silently."""
    if isinstance(target, UpdateInfo):
        installer_path = target.installer_path
        expected_sha = target.sha256
    else:
        installer_path = Path(target)
        expected_sha = sha256

    if installer_path is None or not installer_path.is_file():
        logger.error("Installer executable not found: %s", installer_path)
        return False

    if expected_sha and not verify_sha256(installer_path, expected_sha):
        logger.error("Installer checksum changed after verification; aborting update.")
        return False

    # SEC-02: opt-in authenticity check. Unsigned builds keep hash-only trust.
    if not verify_authenticode(installer_path, (required_publisher or "").strip()):
        logger.error("Installer publisher verification failed; aborting update.")
        return False

    try:
        subprocess.Popen([str(installer_path), *SILENT_INSTALL_ARGS], close_fds=True)
        logger.info("Launched silent installer: %s", installer_path)
        return True
    except Exception as exc:
        logger.error("Could not launch installer %s: %s", installer_path, exc)
        return False
