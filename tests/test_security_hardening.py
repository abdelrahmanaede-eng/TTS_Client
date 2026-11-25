"""Regression tests for the security findings in CODE_REVIEW_REPORT.md."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest import mock

import updater
from config import Config
from database import Database
from reporter import _build_official_pdf_html
from visualizer import build_dashboard_data, generate_dashboard_html

WORK_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute)).timestamp())


class TestUpdateSourceHardening(unittest.TestCase):
    def test_http_source_is_rejected(self) -> None:
        self.assertFalse(updater.is_url("http://example.com/version.json"))
        self.assertEqual(updater.normalize_update_source("http://example.com/version.json"), "")

    def test_https_source_is_accepted(self) -> None:
        self.assertTrue(updater.is_url("https://example.com/version.json"))

    def test_manifest_version_must_be_strict(self) -> None:
        self.assertTrue(updater.is_valid_version("v1.4.0"))
        self.assertFalse(updater.is_valid_version(r"9.9.9\..\..\Startup\x"))
        self.assertFalse(updater.is_valid_version("1.2.3-evil"))
        self.assertEqual(updater.safe_version_slug(r"9.9.9\..\x"), "0.0.0")
        self.assertEqual(updater.safe_version_slug("v1.4.0"), "1_4_0")

    def test_malicious_version_is_rejected_before_use(self) -> None:
        manifest = {
            "version": r"9.9.9\..\..\Roaming\Startup\x",
            "sha256": "a" * 64,
            "installer": "TimeTracker_Setup.exe",
        }
        with mock.patch.object(updater, "read_manifest", return_value=manifest):
            self.assertIsNone(updater.check_for_update("https://example.com/version.json", "1.0.0"))

    def test_non_https_download_url_is_rejected(self) -> None:
        manifest = {
            "version": "1.5.0",
            "sha256": "a" * 64,
            "installer": "TimeTracker_Setup.exe",
            "download_url": "http://example.com/setup.exe",
        }
        with mock.patch.object(updater, "read_manifest", return_value=manifest):
            self.assertIsNone(updater.check_for_update("https://example.com/version.json", "1.0.0"))

    def test_download_refuses_http_and_outside_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "setup.exe"
            self.assertFalse(updater.download_installer("http://example.com/setup.exe", dest, "a" * 64))
            outside = Path.home() / "tt_security_test_should_not_exist.exe"
            self.assertFalse(updater.download_installer("https://example.com/setup.exe", outside, "a" * 64))
            self.assertFalse(outside.exists())

    def test_dest_path_uses_sanitised_version(self) -> None:
        source = Path(updater.__file__).read_text(encoding="utf-8")
        dialogs_src = Path(__import__("dialogs").__file__).read_text(encoding="utf-8")
        self.assertIn("safe_version_slug(info.version)", dialogs_src)
        self.assertNotIn("f\"TimeTracker_Setup_{info.version}.exe\"", dialogs_src)
        self.assertIn("MAX_INSTALLER_BYTES", source)


class TestNoHardcodedCredential(unittest.TestCase):
    def test_uploader_has_no_default_token(self) -> None:
        # The script is not a package; resolve it by path.
        path = Path(updater.__file__).parent / "scripts" / "upload_release.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("ghp_", source)
        self.assertNotIn("DEFAULT_TOKEN", source)
        self.assertIn("GITHUB_TOKEN", source)


class TestOutputEscaping(unittest.TestCase):
    PAYLOAD = '<img src=x onerror="alert(1)">'

    def test_dashboard_escapes_app_and_project_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "x.db")
            d = date(2026, 11, 9)
            pid = db.add_project(self.PAYLOAD)
            db.record_input_event(_ts(d, 10, 0), app_name=self.PAYLOAD, project_id=pid)
            html = generate_dashboard_html(build_dashboard_data(db, Config(), d))
            # Raw payload must never reach the document; JSON escaping plus the
            # explicit esc() calls keep it as inert text (see test_audit_fixes
            # for the </script> breakout case).
            self.assertNotIn(self.PAYLOAD, html)
            self.assertIn("Content-Security-Policy", html)

    def test_official_pdf_html_escapes_project_and_employee(self) -> None:
        html = _build_official_pdf_html(
            "", date(2026, 11, 1), date(2026, 11, 30), "uid", "now",
            self.PAYLOAD,
            {"worked": 8.0, "target": 8.0, "overtime": 0.0, "meeting": 0.0, "break": 0.0},
            100.0, [], [{"name": self.PAYLOAD, "worked_hours": 8.0, "sessions": 1, "percentage": 100.0}],
        )
        self.assertNotIn(self.PAYLOAD, html)
        self.assertIn("&lt;img", html)
        self.assertIn("Content-Security-Policy", html)


class TestDashboardServerHeaders(unittest.TestCase):
    def test_no_wildcard_cors_and_no_query_token_for_api(self) -> None:
        source = Path(__import__("visualizer").__file__).read_text(encoding="utf-8")
        self.assertNotIn('Access-Control-Allow-Origin", "*"', source)
        self.assertIn('"Authorization"', source)
        self.assertIn("Referrer-Policy", source)
        self.assertIn("Forbidden - Invalid Host", source)


if __name__ == "__main__":
    unittest.main()
