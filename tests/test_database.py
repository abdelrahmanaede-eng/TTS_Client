"""Unit tests for TimeTracker config and database."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from time_tracker.core.config import Config, load_config
from time_tracker.core.database import Database


class TestConfig(unittest.TestCase):
    def test_default_config_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "config.json"
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.work_start, "09:30")
            self.assertEqual(cfg.work_end, "17:30")
            self.assertEqual(cfg.work_days, ["Sun", "Mon", "Tue", "Wed", "Thu"])
            self.assertEqual(cfg.daily_target_hours, 8.0)
            self.assertEqual(cfg.grace_period_seconds, 180)
            self.assertEqual(cfg.tick_interval_seconds, 5)
            self.assertTrue(cfg.autostart_enabled)
            self.assertTrue(cfg_path.exists())

    def test_corrupt_config_recreates_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "config.json"
            cfg_path.write_text("{corrupted json contents", encoding="utf-8")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.work_start, "09:30")
            # File should now contain valid JSON
            self.assertIn('"work_start": "09:30"', cfg_path.read_text(encoding="utf-8"))


class TestDatabase(unittest.TestCase):
    def test_remote_work_session_snapshot_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db = Database(Path(tmp_dir) / "data.db")
            db.save_remote_work_session(
                attendance_id="123",
                project_id="25",
                project_name="ERP Project",
                check_in_utc="2026-11-27T07:00:00.000Z",
                is_open=True,
                tracker_allowed=True,
                source="hello",
            )

            active = db.get_active_remote_work_session()
            self.assertIsNotNone(active)
            assert active is not None
            self.assertEqual(active["attendance_id"], "123")
            self.assertEqual(active["project_id"], "25")
            self.assertEqual(active["project_name"], "ERP Project")
            self.assertEqual(active["is_open"], 1)

            db.clear_active_remote_work_sessions(source="signalr")
            self.assertIsNone(db.get_active_remote_work_session())

    def test_input_event_keeps_remote_session_and_project_separate_from_local_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db = Database(Path(tmp_dir) / "data.db")
            local_project = db.add_project("Local Report Project")

            db.record_input_event(
                1_700_000_000,
                app_name="chrome.exe",
                source="input",
                project_id=local_project,
                remote_attendance_id="123",
                remote_project_id="25",
                remote_project_name="ERP Project",
            )

            rows = db.get_input_events_with_remote_details(
                1_699_999_000,
                1_700_001_000,
            )
            self.assertEqual(
                rows,
                [(1_700_000_000, "chrome.exe", "input", local_project, "123", "25", "ERP Project")],
            )

    def test_input_events_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "data.db"
            db = Database(db_path)

            # Insert input events
            db.record_input_event(100)
            db.record_input_event(100)  # duplicate, should be ignored
            db.record_input_event(200)
            events = db.get_input_events_between(50, 250)
            self.assertEqual(events, [100, 200])

            # Session lifecycle
            sid = db.start_session(session_type="meeting", source="tray", start_ts=100)
            open_s = db.get_open_session("meeting")
            self.assertIsNotNone(open_s)
            self.assertEqual(open_s["id"], sid)

            # Ending session
            closed = db.end_session("meeting", end_ts=300)
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0]["duration"], 200)
            self.assertIsNone(db.get_open_session("meeting"))

            # Past session (retroactive meeting)
            past_id = db.add_past_session(
                session_type="meeting",
                start_ts=500,
                end_ts=900,
                source="manual",
                note="Client Review",
                reason="",
            )
            self.assertGreater(past_id, 0)

            # Database backup
            backup_file = db.backup_database(Path(tmp_dir))
            self.assertTrue(backup_file.exists())
            self.assertGreater(backup_file.stat().st_size, 0)

            # Close all on quit
            db.start_session("overtime", "auto", 400)
            count = db.close_all_open_sessions(end_ts=500)
            self.assertEqual(count, 1)
            self.assertEqual(len(db.get_all_open_sessions()), 0)

            # Test get_recorded_dates
            dates = db.get_recorded_dates()
            self.assertIsInstance(dates, list)
            self.assertGreaterEqual(len(dates), 1)

    def test_remote_segment_outbox_is_durable_and_deduplicates_by_segment_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db = Database(Path(tmp_dir) / "data.db")
            segment_id = db.save_remote_activity_segment(
                attendance_id="123",
                project_id="25",
                project_name="ERP",
                application="chrome.exe",
                start_utc="2026-11-27T07:00:00.000Z",
                end_utc="2026-11-27T07:30:00.000Z",
                original_start_epoch=1764226800,
                original_end_epoch=1764228600,
            )
            first_outbox_id = db.queue_remote_activity_segment(
                segment_id=segment_id,
                payload={"attendanceId": "123", "segmentId": segment_id},
            )
            second_outbox_id = db.queue_remote_activity_segment(
                segment_id=segment_id,
                payload={"attendanceId": "123", "segmentId": segment_id},
            )

            self.assertEqual(first_outbox_id, second_outbox_id)
            pending = db.get_pending_remote_segment_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["segment_id"], segment_id)
            self.assertEqual(pending[0]["state"], "pending")

            db.update_remote_segment_outbox_state(
                segment_id=segment_id,
                state="synced",
                server_ack_status="unchanged",
            )
            self.assertEqual(db.get_pending_remote_segment_outbox(), [])

    def test_retention_preserves_remote_attributed_raw_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db = Database(Path(tmp_dir) / "data.db")
            db.record_input_event(
                1_700_000_000,
                app_name="chrome.exe",
                source="input",
                remote_attendance_id="123",
                remote_project_id="25",
                remote_project_name="ERP",
            )
            db.record_input_event(
                1_699_999_999,
                app_name="local.exe",
                source="input",
            )

            deleted = db.delete_input_events_before(1_700_000_000)
            self.assertEqual(deleted, 1)
            rows = db.get_input_events_with_remote_details(
                1_699_999_000,
                1_700_000_002,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][4], "123")

    def test_orphan_session_cleanup_with_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "data.db"
            db = Database(db_path)

            # Start a meeting that is never cleanly closed (simulating crash)
            db.start_session("meeting", "tray", start_ts=1000, note="Sudden Crash Meeting")
            self.assertEqual(len(db.get_all_open_sessions()), 1)

            # Heartbeat was recorded at 1450
            db.record_heartbeat(1450)

            # Startup cleanup runs
            orphans_cleaned = db.cleanup_orphan_sessions()
            self.assertEqual(orphans_cleaned, 1)
            self.assertEqual(len(db.get_all_open_sessions()), 0)

            # Check that end_ts was assigned to heartbeat timestamp 1450
            sessions = db.get_sessions_between(900, 2000)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["end_ts"], 1450)
            self.assertIn("Recovered", sessions[0]["note"])

    def test_retention_cleanup(self) -> None:
        import time
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "data.db"
            db = Database(db_path)

            now_ts = int(time.time())
            old_ts = now_ts - (100 * 86400)  # 100 days old
            recent_ts = now_ts - (10 * 86400)  # 10 days old

            db.record_input_event(old_ts, "old_app.exe")
            db.record_input_event(recent_ts, "recent_app.exe")

            # Clean events older than 90 days
            deleted = db.cleanup_old_events(retention_days=90)
            self.assertEqual(deleted, 1)

            events = db.get_input_events_between(0, now_ts + 1000)
            self.assertEqual(events, [recent_ts])


if __name__ == "__main__":
    unittest.main()
