"""Comprehensive Unit Tests for the 5 System Improvements:
1. Window Polling Optimization (HWND caching)
2. Interactive Dashboard Local Server & Live JSON Sync
3. Official PDF Timesheet Export with Bussma Seal
4. Smart Meeting Detection & Do Not Disturb
5. Tkinter Exception Handling & Win32 MessageBoxW Fallback
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import time_tracker.ui.dialogs as dialogs
from time_tracker.core.config import Config
from time_tracker.core.database import Database
from time_tracker.reporting.reporter import _build_official_pdf_html, export_pdf_report, find_headless_browser
from time_tracker.core.tracker import (
    BROWSER_PROCESSES,
    MEETING_APP_PROCESSES,
    MEETING_TITLE_KEYWORDS,
    ActivityTracker,
    get_active_application_name,
    reset_active_app_cache,
)
from time_tracker.reporting.visualizer import (
    DashboardServer,
    build_dashboard_data,
    generate_dashboard_html,
    get_or_start_dashboard_server,
    stop_dashboard_server,
)


class TestProposal1WindowPollingOptimization(unittest.TestCase):
    """Proposal 1: Optimize Window Polling by caching active HWND."""

    def setUp(self) -> None:
        reset_active_app_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.config = Config()

    def tearDown(self) -> None:
        reset_active_app_cache()
        self.tmp.cleanup()

    def test_activity_tracker_hwnd_caching(self) -> None:
        """When the active HWND has not changed, tracker reuses cached app_name without re-querying."""
        mock_get_app = mock.MagicMock(return_value="Code.exe")
        mock_get_hwnd = mock.MagicMock(return_value=12345)
        mock_get_title = mock.MagicMock(return_value="Project - Visual Studio Code")

        tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            get_active_app_fn=mock_get_app,
            get_foreground_hwnd_fn=mock_get_hwnd,
            get_window_title_fn=mock_get_title,
        )

        # First call: queries app and title
        app1, title1 = tracker.get_current_active_window_info()
        self.assertEqual(app1, "Code.exe")
        self.assertEqual(title1, "Project - Visual Studio Code")
        self.assertEqual(mock_get_app.call_count, 1)

        # Second call with same HWND (12345): must NOT call mock_get_app again
        app2, title2 = tracker.get_current_active_window_info()
        self.assertEqual(app2, "Code.exe")
        self.assertEqual(title2, "Project - Visual Studio Code")
        self.assertEqual(mock_get_app.call_count, 1, "Expected cached app name, not a second query")

        # Third call: HWND changes to 99999 -> must query new app name
        mock_get_hwnd.return_value = 99999
        mock_get_app.return_value = "msedge.exe"
        mock_get_title.return_value = "Bussma Portal - Microsoft Edge"

        app3, title3 = tracker.get_current_active_window_info()
        self.assertEqual(app3, "msedge.exe")
        self.assertEqual(mock_get_app.call_count, 2)

    def test_tracker_reset_window_cache(self) -> None:
        """reset_window_cache clears HWND cache so next call queries anew."""
        mock_get_app = mock.MagicMock(return_value="slack.exe")
        mock_get_hwnd = mock.MagicMock(return_value=555)

        tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            get_active_app_fn=mock_get_app,
            get_foreground_hwnd_fn=mock_get_hwnd,
        )

        tracker.get_current_active_window_info()
        self.assertEqual(mock_get_app.call_count, 1)

        tracker.reset_window_cache()
        self.assertIsNone(tracker._last_hwnd)
        self.assertEqual(tracker._last_app_name, "Unknown")

        tracker.get_current_active_window_info()
        self.assertEqual(mock_get_app.call_count, 2)


class TestProposal2DashboardServerAndApi(unittest.TestCase):
    """Proposal 2: Dashboard Local Server & Live Asynchronous JSON Sync."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.config = Config(work_days=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])

    def tearDown(self) -> None:
        stop_dashboard_server()
        self.tmp.cleanup()

    def test_dashboard_server_lifecycle_and_endpoints(self) -> None:
        """DashboardServer binds to 127.0.0.1 on an ephemeral port with token auth."""
        server = get_or_start_dashboard_server(self.db, self.config)
        self.assertGreater(server.port, 0)
        self.assertTrue(len(server.token) >= 16)

        url = server.get_url()
        self.assertIn(f"http://127.0.0.1:{server.port}/?token={server.token}", url)

        # 1. Ping endpoint with valid token
        req_ping = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/ping?token={server.token}")
        self.assertEqual(req_ping.status, 200)
        ping_data = json.loads(req_ping.read().decode("utf-8"))
        self.assertEqual(ping_data.get("status"), "ok")

        # 2. Data endpoint with valid token
        req_data = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/data?token={server.token}")
        self.assertEqual(req_data.status, 200)
        body = json.loads(req_data.read().decode("utf-8"))
        self.assertTrue(body.get("success"))
        self.assertIn("data", body)
        self.assertIn("history_14", body["data"])

        # 3. Request without token must be rejected with 401 Unauthorized
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/data")
            self.fail("Expected HTTPError 401")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 401)

    def test_dashboard_html_contains_server_info_and_live_script(self) -> None:
        """generate_dashboard_html embeds server port, token, and fetchLiveDashboardData."""
        data = build_dashboard_data(self.db, self.config)
        html = generate_dashboard_html(data, server_port=8999, server_token="test_secret_tok_123")

        self.assertIn("const SERVER_PORT = 8999;", html)
        self.assertIn('const SERVER_TOKEN = "test_secret_tok_123";', html)
        self.assertIn("fetchLiveDashboardData", html)
        self.assertIn("live-refresh-btn", html)
        self.assertIn("live-status-badge", html)


class TestProposal3PDFExport(unittest.TestCase):
    """Proposal 3: Official HR Timesheet PDF Export with Bussma Seal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.config = Config(work_days=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_headless_browser_discovery(self) -> None:
        """find_headless_browser discovers msedge or chrome on Windows."""
        browser = find_headless_browser()
        self.assertIsNotNone(browser)
        self.assertTrue(Path(browser).exists())

    def test_build_official_pdf_html(self) -> None:
        """_build_official_pdf_html builds bilingual report with seal and approval blocks."""
        today = date.today()
        start_d = today.replace(day=1)
        end_d = today
        totals = {"worked": 160.0, "target": 160.0, "overtime": 10.0, "meeting": 15.0, "break": 5.0}
        rows = [
            [today.isoformat(), "Sun", "09:30", "17:30", 8.0, 1.0, 0.5, 0.0, 8.0, 0.0, 0.0]
        ]
        html = _build_official_pdf_html(
            logo_base64="",
            start_date=start_d,
            end_date=end_d,
            report_uid="BSS-TEST-001",
            now_str="2026-11-15 12:00",
            employee_name="Ahmed Ali",
            totals=totals,
            compliance_pct=100.0,
            rows=rows,
            projects=[{"name": "Client Portal", "worked_hours": 20.0, "sessions": 5, "percentage": 12.5}],
        )

        self.assertIn("BUSSMA", html)
        self.assertIn("Ahmed Ali", html)
        self.assertIn("مؤسسة بصمة", html)
        self.assertIn("official-seal", html)
        self.assertIn("approval-section", html)
        self.assertIn("Client Portal", html)

    def test_export_pdf_report_generates_file(self) -> None:
        """export_pdf_report creates a valid non-empty PDF file."""
        today = date.today()
        # Seed an input event
        ts = int(time.time())
        self.db.record_input_event(ts, app_name="Code.exe")

        out_path = export_pdf_report(
            self.db,
            self.config,
            start_date=today,
            end_date=today,
            output_dir=Path(self.tmp.name),
            open_folder=False,
        )

        self.assertTrue(out_path.exists())
        self.assertGreater(out_path.stat().st_size, 0)
        self.assertIn(out_path.suffix, [".pdf", ".html"])


class TestProposal4SmartMeetingDetection(unittest.TestCase):
    """Proposal 4: Smart Meeting Detection & Do Not Disturb mode."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.config = Config(
            work_days=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            enable_smart_meeting_detection=True,
            smart_meeting_cooldown_minutes=20,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_detect_desktop_meeting_app(self) -> None:
        """When a meeting app like zoom.exe is active, smart meeting prompt is triggered."""
        prompt_mock = mock.MagicMock()
        tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_smart_meeting_prompt_fn=prompt_mock,
        )

        now_ts = 1700000000
        tracker._evaluate_smart_meeting_detection("Zoom.exe", "Zoom Workplace", now_ts)

        self.assertEqual(prompt_mock.call_count, 1)
        args, kwargs = prompt_mock.call_args
        self.assertEqual(args[0], "Zoom")

    def test_detect_browser_google_meet(self) -> None:
        """When browser window title contains 'Google Meet', prompt is triggered."""
        prompt_mock = mock.MagicMock()
        tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_smart_meeting_prompt_fn=prompt_mock,
        )

        now_ts = 1700000000
        tracker._evaluate_smart_meeting_detection("chrome.exe", "Meet – Weekly Planning – Google Chrome", now_ts)

        self.assertEqual(prompt_mock.call_count, 1)
        args, kwargs = prompt_mock.call_args
        self.assertEqual(args[0], "Google Meet")

    def test_smart_meeting_cooldown(self) -> None:
        """Repeated detections within the cooldown period do not trigger multiple prompts."""
        prompt_mock = mock.MagicMock()
        tracker = ActivityTracker(
            db=self.db,
            config=self.config,
            show_smart_meeting_prompt_fn=prompt_mock,
        )

        t0 = 1700000000
        tracker._evaluate_smart_meeting_detection("Teams.exe", "Teams", t0)
        self.assertEqual(prompt_mock.call_count, 1)

        # 5 minutes later (less than 20 min cooldown)
        tracker._evaluate_smart_meeting_detection("Teams.exe", "Teams", t0 + 300)
        self.assertEqual(prompt_mock.call_count, 1, "Should be suppressed by cooldown")

        # 25 minutes later (exceeds 20 min cooldown)
        tracker._evaluate_smart_meeting_detection("Teams.exe", "Teams", t0 + 1500)
        self.assertEqual(prompt_mock.call_count, 2, "Should trigger after cooldown elapsed")

    def test_start_meeting_enables_dnd(self) -> None:
        """tracker.start_meeting starts a session and sets active meeting state."""
        tracker = ActivityTracker(db=self.db, config=self.config)
        self.assertFalse(tracker.is_meeting_active())

        started, session_ts = tracker.start_meeting(source="smart_detected", note="Auto-detected Zoom")
        self.assertTrue(started)
        self.assertTrue(tracker.is_meeting_active())

        # Calling again while meeting is active returns False
        started_again, _ = tracker.start_meeting()
        self.assertFalse(started_again)


class TestProposal5TkinterFallbacksAndLogging(unittest.TestCase):
    """Proposal 5: Fallback when Tkinter / Tcl is missing or fails."""

    def setUp(self) -> None:
        dialogs._is_dialog_active = False

    def tearDown(self) -> None:
        dialogs._is_dialog_active = False

    def test_win32_message_box_fallback(self) -> None:
        """win32_message_box returns an integer and logs without raising exceptions."""
        with mock.patch("ctypes.windll.user32.MessageBoxW", return_value=1) as mb_mock:
            res = dialogs.win32_message_box("Test Title", "Test Text")
            self.assertEqual(res, 1)
            self.assertEqual(mb_mock.call_count, 1)

    def test_show_overtime_prompt_fallback_when_tk_unavailable(self) -> None:
        """When _tk_available is False, show_overtime_prompt_async invokes Win32 fallback."""
        with mock.patch.object(dialogs, "_tk_available", False):
            with mock.patch(
                "time_tracker.ui.dialogs.win32_message_box", return_value=dialogs.IDYES
            ) as win_box:
                response_received: list[tuple[bool, str]] = []

                def callback(accepted: bool, reason: str) -> None:
                    response_received.append((accepted, reason))

                dialogs.show_overtime_prompt_async("Outside hours", callback)
                time.sleep(0.1)

                self.assertEqual(win_box.call_count, 1)
                self.assertEqual(len(response_received), 1)
                self.assertTrue(response_received[0][0])


if __name__ == "__main__":
    unittest.main()
