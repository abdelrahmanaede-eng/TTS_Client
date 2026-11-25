"""Tests for the work-window (instead of fixed 24h) dashboard timeline."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, time
from pathlib import Path

from config import Config
from database import Database
from visualizer import (
    DashboardServer,
    _get_app_timeline_blocks,
    _get_status_timeline_blocks,
    _timeline_window,
    build_dashboard_data,
    generate_dashboard_html,
)

WORK_DAY = date(2026, 11, 9)  # Monday


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute)).timestamp())


class TestTimelineWindow(unittest.TestCase):
    def test_work_window_with_margin(self) -> None:
        cfg = Config()
        start, end = _timeline_window(WORK_DAY, [], [], [], cfg)
        self.assertEqual(datetime.fromtimestamp(start).strftime("%H:%M"), "09:00")
        self.assertEqual(datetime.fromtimestamp(end).strftime("%H:%M"), "18:00")

    def test_window_expands_for_overtime(self) -> None:
        cfg = Config()
        events = [_ts(WORK_DAY, 20)]
        start, end = _timeline_window(WORK_DAY, events, [], [], cfg)
        self.assertLessEqual(datetime.fromtimestamp(start).hour, 9)
        self.assertGreaterEqual(datetime.fromtimestamp(end).hour, 20)

    def test_blocks_use_window_not_24h(self) -> None:
        cfg = Config()
        events = [_ts(WORK_DAY, 10), _ts(WORK_DAY, 11)]
        window = _timeline_window(WORK_DAY, events, [], [], cfg)
        status = _get_status_timeline_blocks(WORK_DAY, events, [], cfg, window=window)
        self.assertTrue(status)
        worked = [b for b in status if b["type"] == "worked"]
        self.assertTrue(worked)

        # Same data on the old fixed 24h ruler, for a direct comparison.
        from visualizer import get_day_boundaries

        day_start, day_end = get_day_boundaries(WORK_DAY)
        old = _get_status_timeline_blocks(WORK_DAY, events, [], cfg, window=(day_start, day_end))
        old_worked = [b for b in old if b["type"] == "worked"]
        self.assertTrue(old_worked)
        self.assertGreater(
            worked[0]["width_pct"], old_worked[0]["width_pct"] * 2,
            "the work-window view should make the same block at least twice as wide",
        )
        self.assertLessEqual(worked[0]["start_pct"] + worked[0]["width_pct"], 100.0)

        apps = [(t, "chrome.exe") for t in events]
        app_blocks = _get_app_timeline_blocks(apps, window[0], window[1], 180, window=window)
        self.assertTrue(app_blocks)
        self.assertGreater(app_blocks[0]["width_pct"], 0)

    def test_html_exposes_window_and_dynamic_ruler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            db.record_input_event(_ts(WORK_DAY, 10), app_name="chrome.exe")
            data = build_dashboard_data(db, Config(), WORK_DAY)
            win = data.get("timeline_window") or {}
            self.assertEqual(win.get("start_label"), "09:00")
            self.assertEqual(win.get("end_label"), "18:00")

            html = generate_dashboard_html(data)
            self.assertIn("Interactive Work-Window Timeline", html)
            self.assertIn("timeline_window", html)
            self.assertIn("CURRENT_DATA.timeline_window", html)
            self.assertNotIn("Interactive 24-Hour Timeline Stream", html)
            # CSP script-src-attr 'none': no inline HTML event handlers.
            self.assertNotIn("onclick=", html)
            self.assertNotIn("onchange=", html)
            self.assertIn("bindInlineHandlers()", html)
            self.assertIn("tt_dash_token", html)
            self.assertIn("sessionStorage.getItem", html)
            self.assertNotIn("window.location.reload()", html)


class TestDashboardReloadAuth(unittest.TestCase):
    def test_cookie_authenticates_page_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "srv.db")
            db.record_input_event(_ts(WORK_DAY, 10), app_name="chrome.exe")
            srv = DashboardServer(db, Config(), port=0)
            try:
                base = f"http://127.0.0.1:{srv.port}/"
                # First load with token in the query (what get_url() returns).
                with urllib.request.urlopen(f"{base}?token={srv.token}", timeout=5) as resp:
                    self.assertIn("BUSSMA_TTS", resp.read().decode("utf-8"))

                # Reload after replaceState: no query token, but the Strict
                # cookie set by the page keeps the navigation authenticated.
                req = urllib.request.Request(
                    base, headers={"Cookie": f"tt_dash_token={srv.token}"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertIn("BUSSMA_TTS", resp.read().decode("utf-8"))

                # No token at all is still rejected.
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(base, timeout=5)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                srv.shutdown()


if __name__ == "__main__":
    unittest.main()
