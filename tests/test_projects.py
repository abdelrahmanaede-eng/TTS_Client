"""Tests for project / task tags (Part B3)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

import openpyxl

from time_tracker.core.calculator import calculate_project_usage
from time_tracker.core.config import Config
from time_tracker.core.database import Database
from time_tracker.reporting.reporter import export_report, export_csv_bundle
from time_tracker.core.retention import project_breakdown
from time_tracker.core.tracker import ActivityTracker


def _ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute)).timestamp())


class TestProjectsDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp_dir.name) / "data.db")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_project_crud(self) -> None:
        pid = self.db.add_project("Alpha")
        self.assertGreater(pid, 0)
        self.assertEqual(self.db.get_project(pid)["name"], "Alpha")

        # Names are unique case-insensitively.
        with self.assertRaises(ValueError):
            self.db.add_project("alpha")
        with self.assertRaises(ValueError):
            self.db.add_project("   ")

        self.db.rename_project(pid, "Alpha 2")
        self.assertEqual(self.db.get_project(pid)["name"], "Alpha 2")

        self.db.set_project_archived(pid, True)
        self.assertEqual(self.db.list_projects(), [])
        archived = self.db.list_projects(include_archived=True)
        self.assertEqual(len(archived), 1)
        self.assertEqual(int(archived[0]["archived"]), 1)

        self.db.set_project_archived(pid, False)
        self.assertEqual(len(self.db.list_projects()), 1)

    def test_events_and_sessions_store_project(self) -> None:
        pid = self.db.add_project("Client X")
        self.db.record_input_event(1000, app_name="chrome.exe", project_id=pid)
        details = self.db.get_input_events_with_details(900, 1100)
        self.assertEqual(details[0][3], pid)

        sid = self.db.start_session("meeting", "tray", 1000, project_id=pid)
        opened = self.db.get_open_session("meeting")
        self.assertEqual(opened["id"], sid)
        self.assertEqual(opened["project_id"], pid)
        closed = self.db.end_session("meeting", 1600)
        self.assertEqual(closed[0]["project_id"], pid)

        past = self.db.add_past_session("meeting", 2000, 2600, project_id=pid)
        self.assertGreater(past, 0)
        sessions = self.db.get_sessions_between(1900, 2700)
        self.assertEqual(sessions[0]["project_id"], pid)


class TestProjectAttribution(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp_dir.name) / "data.db")
        self.config = Config(work_days=["Sun", "Mon", "Tue", "Wed", "Thu"])
        self.day = date(2026, 11, 9)  # Monday

        self.alpha = self.db.add_project("Alpha")
        self.beta = self.db.add_project("Beta")

        # 10:00-10:30 on Alpha, 10:30-11:00 on Beta (1 event per minute).
        base = _ts(self.day, 10, 0)
        for i in range(31):
            self.db.record_input_event(base + i * 60, app_name="chrome.exe", project_id=self.alpha)
        for i in range(1, 31):
            self.db.record_input_event(base + 1800 + i * 60, app_name="excel.exe", project_id=self.beta)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_calculate_project_usage_groups_by_project(self) -> None:
        day_start, day_end = _ts(self.day, 0, 0), _ts(self.day, 23, 59)
        records = self.db.get_input_events_with_details(day_start, day_end)
        usage = calculate_project_usage(records, grace_seconds=180)
        seconds = {item.project_id: item.active_seconds for item in usage}
        self.assertGreater(seconds[self.alpha], 0)
        self.assertGreater(seconds[self.beta], 0)

    def test_project_breakdown_sums_to_worked(self) -> None:
        rows = project_breakdown(self.db, self.config, self.day, self.day)
        names = {row["name"]: row for row in rows}
        self.assertIn("Alpha", names)
        self.assertIn("Beta", names)
        from time_tracker.core.retention import get_day_metrics

        worked = get_day_metrics(self.db, self.config, self.day).worked_seconds
        attributed = sum(int(row["worked_sec"]) for row in rows)
        # Attribution is scaled to the day's worked total (rounding tolerance).
        self.assertLessEqual(abs(attributed - worked), 2)

    def test_unassigned_time_is_labelled(self) -> None:
        # Two untagged events before the tagged work so the first has a contribution.
        self.db.record_input_event(_ts(self.day, 8, 0), app_name="notepad.exe", project_id=None)
        self.db.record_input_event(_ts(self.day, 8, 10), app_name="notepad.exe", project_id=None)
        rows = project_breakdown(self.db, self.config, self.day, self.day)
        names = {row["name"] for row in rows}
        self.assertIn("Unassigned", names)

    def test_excel_projects_sheet(self) -> None:
        out = export_report(
            self.db, self.config, self.day, self.day,
            open_folder=False, output_dir=Path(self.tmp_dir.name),
        )
        wb = openpyxl.load_workbook(out)
        self.assertIn("Projects", wb.sheetnames)
        ws = wb["Projects"]
        self.assertEqual([cell.value for cell in ws[1]][0], "Project")
        project_names = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]
        self.assertIn("Alpha", project_names)

    def test_csv_bundle_includes_projects_file(self) -> None:
        files = export_csv_bundle(
            self.db, self.config, self.day, self.day,
            open_folder=False, output_dir=Path(self.tmp_dir.name),
        )
        self.assertEqual(len(files), 4)
        project_csv = [p for p in files if "Projects" in p.name]
        self.assertEqual(len(project_csv), 1)
        content = project_csv[0].read_text(encoding="utf-8-sig")
        self.assertIn("Alpha", content)


class TestTrackerCurrentProject(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp_dir.name) / "data.db")
        self.config = Config()
        self.tracker = ActivityTracker(self.db, self.config)
        self.pid = self.db.add_project("Tracked")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_selecting_project_persists_and_tags_events(self) -> None:
        self.tracker.set_current_project(self.pid)
        self.assertEqual(self.tracker.current_project_id, self.pid)
        self.assertEqual(self.tracker.current_project_name, "Tracked")
        self.assertEqual(self.db.get_setting("current_project_id"), str(self.pid))

        self.tracker.record_active_confirmation(1_700_000_000)
        records = self.db.get_input_events_with_details(1_699_999_000, 1_700_001_000)
        self.assertEqual(records[-1][3], self.pid)

        # Project selection survives a restart.
        restarted = ActivityTracker(self.db, self.config)
        self.assertEqual(restarted.current_project_id, self.pid)

    def test_archived_project_cannot_stay_selected(self) -> None:
        self.tracker.set_current_project(self.pid)
        self.db.set_project_archived(self.pid, True)
        self.tracker.set_current_project(self.pid)
        self.assertIsNone(self.tracker.current_project_id)
        self.assertEqual(self.db.get_setting("current_project_id"), "")

    def test_started_sessions_carry_project(self) -> None:
        self.tracker.set_current_project(self.pid)
        self.tracker.toggle_meeting()
        session = self.db.get_open_session("meeting")
        assert session is not None
        self.assertEqual(session["project_id"], self.pid)
        self.tracker.toggle_meeting()

    def test_tray_menu_builds_with_projects(self) -> None:
        from time_tracker.ui.tray import TrayApp
        app = TrayApp(self.db, self.config, self.tracker)
        menu = app._build_menu()
        self.assertIsNotNone(menu)
        self.assertGreater(len(menu.items), 0)

    def test_tray_menu_includes_logout_for_signed_in_user(self) -> None:
        from time_tracker.localization.i18n import t
        from time_tracker.system.tts_auth import TrackerAuthProfile, TrackerAuthSession
        from time_tracker.system.tts_api import TrackerHelloData, TrackerWorkSession
        from time_tracker.ui.tray import TrayApp

        local_project_id = self.db.add_project("Legacy local")
        self.tracker.set_current_project(local_project_id)
        profile = TrackerAuthProfile("bussma", "employee@example.com", "device-id", "device-token")
        session = TrackerAuthSession(profile, "access-token", None, "employee-1", "Employee One")
        app = TrayApp(self.db, self.config, self.tracker, auth_session=session)
        app._server_load_state = "ready"
        app._server_snapshot = TrackerHelloData(
            TrackerWorkSession("attendance-1", "server-project", "ERP Project", None, True, True),
            None,
            None,
            (),
            None,
        )

        menu = app._build_menu()
        labels = [item.text for item in menu.items if hasattr(item, "text")]

        self.assertIn(t("action_logout", self.config.language), labels)
        self.assertIn("Local report project: Legacy local", labels)
        def translate(key: str, _lang: str | None = None, **kwargs: object) -> str:
            return t(key, self.config.language, **kwargs)

        server_menu = app._build_server_submenu(translate)
        server_labels = [item.text for item in server_menu.items if hasattr(item, "text")]
        self.assertIn("Server project: ERP Project", server_labels)

        from unittest.mock import patch
        with patch("time_tracker.ui.tray.dispatch_to_ui"):
            app._on_signalr_session_changed(None)
        cleared_server_menu = app._build_server_submenu(translate)
        cleared_server_labels = [item.text for item in cleared_server_menu.items if hasattr(item, "text")]
        self.assertIn("Server project: None", cleared_server_labels)

if __name__ == "__main__":
    unittest.main()
