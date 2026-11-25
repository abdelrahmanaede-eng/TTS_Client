"""GitHub Release and version.json uploader for TimeTracker.

Uploads ``dist/TimeTracker_Setup.exe`` to GitHub Releases and publishes the
updated ``version.json`` manifest to the repository's main branch.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Add project root to path
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

try:
    from time_tracker.core.config import APP_VERSION
except ImportError:
    APP_VERSION = "1.0.0"

# SEC-01: no credential is stored in source. The token is resolved in this
# order: --token  >  GITHUB_TOKEN env var  >  DPAPI-encrypted local store
# (populated once via `python scripts/token_store.py`).
try:
    from token_store import get_stored_token
except Exception:  # store unavailable (e.g. non-Windows) -> simply skip it
    def get_stored_token() -> str:
        return ""

DEFAULT_REPO = "abdelrahmanaede-eng/TimeTracker-Updates"


def sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest().lower()


def api_request(
    url: str,
    token: str,
    data: bytes | None = None,
    method: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict | list | None:
    """Execute an authenticated GitHub REST API request."""
    req_headers = {
        "Authorization": f"token {token}",
        "User-Agent": "TimeTracker-Release-Uploader",
        "Accept": "application/vnd.github.v3+json",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
            if not content:
                return None
            return json.loads(content.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API error {exc.code} for {url}: {err_msg}") from exc


def get_or_create_release(repo: str, token: str, tag: str, version: str, notes: str) -> dict:
    """Find existing release for tag or create a new one."""
    tag_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        res = api_request(tag_url, token)
        if isinstance(res, dict):
            print(f"[+] Found existing GitHub Release for {tag}")
            return res
    except RuntimeError:
        pass

    # Create new release
    print(f"[+] Creating GitHub Release for {tag}...")
    create_url = f"https://api.github.com/repos/{repo}/releases"
    payload = json.dumps({
        "tag_name": tag,
        "name": f"TimeTracker v{version}",
        "body": notes or f"TimeTracker update release {version}.",
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")

    res = api_request(create_url, token, data=payload, method="POST", headers={"Content-Type": "application/json"})
    if not isinstance(res, dict):
        raise RuntimeError(f"Failed to create release for {tag}")
    return res


def upload_release_asset(upload_url_template: str, token: str, file_path: Path, release_info: dict, repo: str) -> str:
    """Upload setup installer asset to GitHub release."""
    installer_name = file_path.name
    # Remove any existing asset with same name
    assets = release_info.get("assets", [])
    for a in assets:
        if a.get("name") == installer_name:
            asset_id = a.get("id")
            print(f"[-] Removing previous asset {installer_name} (ID: {asset_id})...")
            del_url = f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"
            api_request(del_url, token, method="DELETE")

    upload_url = upload_url_template.split("{")[0] + f"?name={installer_name}"
    file_size = file_path.stat().st_size
    print(f"[+] Uploading {installer_name} ({file_size / (1024 * 1024):.2f} MB) to GitHub Releases...")

    start_time = time.time()
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    res = api_request(
        upload_url,
        token,
        data=file_bytes,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
        },
    )
    elapsed = max(0.1, time.time() - start_time)
    speed = (file_size / (1024 * 1024)) / elapsed
    print(f"[+] Upload complete in {elapsed:.1f}s ({speed:.2f} MB/s)!")

    download_url = res.get("browser_download_url") if isinstance(res, dict) else ""
    if not download_url:
        download_url = f"https://github.com/{repo}/releases/download/{release_info['tag_name']}/{installer_name}"
    return download_url


def update_repo_version_manifest(repo: str, token: str, manifest_data: dict) -> str:
    """Commit and push version.json to the repository main branch."""
    content_str = json.dumps(manifest_data, indent=2, ensure_ascii=False)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")

    url = f"https://api.github.com/repos/{repo}/contents/version.json"

    # Get existing SHA if file exists
    sha = None
    try:
        res = api_request(url, token)
        if isinstance(res, dict) and "sha" in res:
            sha = res["sha"]
    except RuntimeError:
        pass

    body = {
        "message": f"Release TimeTracker v{manifest_data['version']}",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha

    res = api_request(
        url,
        token,
        data=json.dumps(body).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    if isinstance(res, dict) and "content" in res:
        return res["content"].get("html_url", "")
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload TimeTracker Installer to GitHub Releases")
    parser.add_argument("--notes", type=str, default="", help="Release notes")
    parser.add_argument("--token", type=str, default="", help="GitHub Personal Access Token")
    parser.add_argument("--repo", type=str, default="", help="GitHub target repository")
    args = parser.parse_args()

    token = (args.token or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        token = get_stored_token().strip()
    repo = args.repo or os.environ.get("GITHUB_REPO") or DEFAULT_REPO

    if not token:
        print("[ERROR] No GitHub token supplied.")
        print("Choose ONE of these options:")
        print("  1) python scripts/token_store.py   (stores it encrypted on this PC)")
        print("  2) set GITHUB_TOKEN=<your-token>   (current session only)")
        print("  3) python scripts/upload_release.py --token <your-token>")
        sys.exit(2)

    dist_dir = PROJECT_DIR / "dist"
    installer_path = dist_dir / "TimeTracker_Setup.exe"

    if not installer_path.exists():
        print(f"[ERROR] Installer not found: {installer_path}")
        print("Please run build_installer.bat first to compile TimeTracker_Setup.exe!")
        sys.exit(1)

    print("========================================================")
    print("      TimeTracker - GitHub Automatic Release Uploader   ")
    print("========================================================")
    print(f"Version:      {APP_VERSION}")
    print(f"Repository:   {repo}")
    print(f"Installer:    {installer_path} ({installer_path.stat().st_size / (1024*1024):.2f} MB)")

    # SHA-256 checksum
    print("[+] Calculating SHA-256 checksum...")
    sha256 = sha256_file(installer_path)
    print(f"SHA-256:      {sha256}")

    notes = args.notes.strip()
    if not notes and sys.stdin.isatty():
        try:
            print("\nEnter release notes (optional, press Enter to skip):")
            notes = input("> ").strip()
        except EOFError:
            pass

    tag = f"v{APP_VERSION}"

    # 1. GitHub Release
    release_info = get_or_create_release(repo, token, tag, APP_VERSION, notes)

    # 2. Upload asset
    download_url = upload_release_asset(release_info["upload_url"], token, installer_path, release_info, repo)
    print(f"[+] Download URL: {download_url}")

    # 3. Create version manifest
    manifest_data = {
        "version": APP_VERSION,
        "installer": installer_path.name,
        "download_url": download_url,
        "sha256": sha256,
        "notes": notes,
    }

    # Save local dist/version.json
    local_manifest = dist_dir / "version.json"
    local_manifest.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[+] Saved local manifest: {local_manifest}")

    # 4. Push version.json to GitHub main branch
    print("[+] Publishing version.json to GitHub repository main branch...")
    manifest_url = update_repo_version_manifest(repo, token, manifest_data)
    print(f"[+] Published manifest: {manifest_url}")

    print("\n========================================================")
    print("[SUCCESS] Update published successfully to GitHub!")
    print(f"Version:      {APP_VERSION}")
    print(f"Download URL: {download_url}")
    print("Installed TimeTracker apps will now detect this update.")
    print("========================================================\n")


if __name__ == "__main__":
    main()
