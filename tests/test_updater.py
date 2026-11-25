"""Tests for the local (offline) auto-update support (Part B5)."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import updater
from updater import (
    check_for_update,
    is_newer,
    launch_installer,
    parse_version,
    sha256_file,
    verify_sha256,
)

INSTALLER_BYTES = b"fake installer payload"


class TestVersionComparison(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(parse_version("1.3.0"), (1, 3))
        self.assertEqual(parse_version("v2.0.1.4"), (2, 0, 1, 4))
        self.assertEqual(parse_version(""), (0,))

    def test_is_newer(self) -> None:
        self.assertTrue(is_newer("1.4.0", "1.3.0"))
        self.assertTrue(is_newer("2.0", "1.9.9"))
        self.assertFalse(is_newer("1.3.0", "1.3.0"))
        self.assertFalse(is_newer("1.2.9", "1.3.0"))


class TestLocalUpdateSource(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.share = Path(self.tmp_dir.name)
        self.installer = self.share / "TimeTracker_Setup_1.4.0.exe"
        self.installer.write_bytes(INSTALLER_BYTES)
        self.digest = sha256_file(self.installer)
        self.write_manifest({"version": "1.4.0", "installer": self.installer.name, "sha256": self.digest})

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def write_manifest(self, payload: dict | str) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.share / "version.json").write_text(text, encoding="utf-8")

    def test_detects_newer_version(self) -> None:
        info = check_for_update(self.share, "1.3.0")
        assert info is not None
        self.assertEqual(info.version, "1.4.0")
        self.assertEqual(info.installer_path, self.installer.resolve())
        self.assertEqual(info.sha256, self.digest)

    def test_same_or_older_version_ignored(self) -> None:
        self.assertIsNone(check_for_update(self.share, "1.4.0"))
        self.assertIsNone(check_for_update(self.share, "1.5.0"))

    def test_empty_source_disabled(self) -> None:
        self.assertIsNone(check_for_update("", "1.0.0"))
        self.assertIsNone(check_for_update("   ", "1.0.0"))

    def test_missing_or_invalid_manifest(self) -> None:
        (self.share / "version.json").unlink()
        self.assertIsNone(check_for_update(self.share, "1.0.0"))

        self.write_manifest("{not json")
        self.assertIsNone(check_for_update(self.share, "1.0.0"))

    def test_checksum_mismatch_is_refused(self) -> None:
        self.write_manifest({"version": "1.4.0", "installer": self.installer.name, "sha256": "0" * 64})
        self.assertIsNone(check_for_update(self.share, "1.0.0"))

    def test_missing_sha_is_refused(self) -> None:
        self.write_manifest({"version": "1.4.0", "installer": self.installer.name})
        self.assertIsNone(check_for_update(self.share, "1.0.0"))

    def test_missing_installer_is_refused(self) -> None:
        self.installer.unlink()
        self.assertIsNone(check_for_update(self.share, "1.0.0"))

    def test_verify_sha256(self) -> None:
        self.assertTrue(verify_sha256(self.installer, self.digest.upper()))
        self.assertFalse(verify_sha256(self.installer, "abc"))
        self.assertFalse(verify_sha256(self.installer, ""))

    def test_launch_installer_silently(self) -> None:
        info = check_for_update(self.share, "1.0.0")
        assert info is not None
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            self.assertTrue(launch_installer(info))
        popen.assert_called_once()
        args = popen.call_args[0][0]
        # Windows may hand back an 8.3 short path for temp folders; compare names.
        self.assertEqual(Path(args[0]).name, self.installer.name)
        self.assertTrue(Path(args[0]).exists())
        self.assertIn("/VERYSILENT", args)
        self.assertIn("/SUPPRESSMSGBOXES", args)

    def test_launch_refuses_tampered_installer(self) -> None:
        info = check_for_update(self.share, "1.0.0")
        assert info is not None
        self.installer.write_bytes(b"tampered")
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            self.assertFalse(launch_installer(info))
        popen.assert_not_called()


class TestOnlineUpdateSource(unittest.TestCase):
    def test_normalize_update_source(self) -> None:
        self.assertEqual(
            updater.normalize_update_source("owner/repo"),
            "https://raw.githubusercontent.com/owner/repo/main/version.json",
        )
        self.assertEqual(
            updater.normalize_update_source("https://github.com/owner/repo"),
            "https://raw.githubusercontent.com/owner/repo/main/version.json",
        )
        self.assertEqual(
            updater.normalize_update_source("https://example.com/manifest.json"),
            "https://example.com/manifest.json",
        )
        self.assertEqual(
            updater.normalize_update_source("C:\\Updates"),
            "C:\\Updates",
        )

    def test_online_update_detected(self) -> None:
        manifest = {
            "version": "1.5.0",
            "installer": "TimeTracker_Setup.exe",
            "download_url": "https://example.com/releases/TimeTracker_Setup.exe",
            "sha256": "a" * 64,
            "notes": "Bug fixes",
        }
        with mock.patch.object(updater, "read_manifest", return_value=manifest):
            info = check_for_update("https://example.com/version.json", "1.4.0")
            assert info is not None
            self.assertEqual(info.version, "1.5.0")
            self.assertEqual(info.download_url, "https://example.com/releases/TimeTracker_Setup.exe")
            self.assertEqual(info.sha256, "a" * 64)
            self.assertEqual(info.notes, "Bug fixes")

    def test_download_installer_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "downloaded.exe"
            data = b"installer download test content"
            expected_hash = hashlib_hash = updater.hashlib.sha256(data).hexdigest()

            # Mock urllib.request.urlopen
            mock_resp = mock.MagicMock()
            mock_resp.headers = {"Content-Length": str(len(data))}
            mock_resp.read.side_effect = [data, b""]
            mock_resp.__enter__.return_value = mock_resp

            with mock.patch("urllib.request.urlopen", return_value=mock_resp):
                # Valid hash
                self.assertTrue(updater.download_installer("https://example.com/app.exe", dest, expected_hash))
                self.assertTrue(dest.exists())
                self.assertEqual(dest.read_bytes(), data)

                # Invalid hash should fail and clean up
                mock_resp.read.side_effect = [data, b""]
                dest.unlink(missing_ok=True)
                self.assertFalse(updater.download_installer("https://example.com/app.exe", dest, "0" * 64))
                self.assertFalse(dest.exists())

    def test_read_manifest_github_raw_bypasses_cache_via_api(self) -> None:
        raw_url = "https://raw.githubusercontent.com/owner/repo/main/version.json"
        manifest_data = b'{"version": "1.5.0", "sha256": "' + b"0" * 64 + b'"}'

        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = manifest_data
        mock_resp.__enter__.return_value = mock_resp

        with mock.patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = updater.read_manifest(raw_url)
            self.assertIsNotNone(result)
            self.assertEqual(result["version"], "1.5.0")
            # Verify the first call targeted the GitHub API with no-cache headers
            called_req = mock_urlopen.call_args[0][0]
            self.assertIn("api.github.com/repos/owner/repo/contents/version.json", called_req.full_url)
            self.assertIn("_nocache=", called_req.full_url)
            self.assertEqual(called_req.headers.get("Pragma"), "no-cache")
            self.assertEqual(called_req.headers.get("Cache-control"), "no-cache, no-store, must-revalidate")

    def test_read_manifest_fallback_appends_nocache(self) -> None:
        raw_url = "https://raw.githubusercontent.com/owner/repo/main/version.json"
        manifest_data = b'{"version": "1.5.0", "sha256": "' + b"0" * 64 + b'"}'

        mock_api_resp = mock.MagicMock()
        # First call (GitHub API) fails with 403 / rate limit
        mock_resp_fallback = mock.MagicMock()
        mock_resp_fallback.read.return_value = manifest_data
        mock_resp_fallback.__enter__.return_value = mock_resp_fallback

        with mock.patch("urllib.request.urlopen", side_effect=[urllib.error.HTTPError(raw_url, 403, "Rate limit", {}, None), mock_resp_fallback]) as mock_urlopen:
            result = updater.read_manifest(raw_url)
            self.assertIsNotNone(result)
            self.assertEqual(result["version"], "1.5.0")
            self.assertEqual(mock_urlopen.call_count, 2)
            fallback_req = mock_urlopen.call_args_list[1][0][0]
            self.assertIn("raw.githubusercontent.com", fallback_req.full_url)
            self.assertIn("_nocache=", fallback_req.full_url)


if __name__ == "__main__":
    unittest.main()
