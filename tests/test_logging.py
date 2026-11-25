"""Unit tests for the centralized logging / diagnostics module."""

import json
import logging
import logging.handlers
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import time_tracker.system.logging_setup as logging_setup
from time_tracker.core.config import Config, load_config, normalize_log_level


class LoggingSetUpCase(unittest.TestCase):
    """Base case that keeps every test inside its own temporary log folder."""

    def setUp(self) -> None:
        logging_setup.reset_logging()
        self._tmp = TemporaryDirectory()
        self.log_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        logging_setup.reset_logging()
        self._tmp.cleanup()

    def configure(self, level: str = "INFO", **kwargs) -> Path:
        return logging_setup.setup_logging(
            level=level,
            log_dir=self.log_dir,
            console=False,
            enable_faults=False,
            **kwargs,
        )

    @property
    def main_log(self) -> Path:
        return self.log_dir / logging_setup.MAIN_LOG_NAME

    @property
    def error_log(self) -> Path:
        return self.log_dir / logging_setup.ERROR_LOG_NAME

    def main_text(self) -> str:
        if not self.main_log.exists():
            return ""
        return self.main_log.read_text(encoding="utf-8")


class TestLogFiles(LoggingSetUpCase):
    def test_utf8_payload_and_line_metadata(self) -> None:
        self.configure("DEBUG")
        logging.getLogger("tray").info("اختبار عربي — تم بنجاح")

        text = self.main_text()
        self.assertIn("اختبار عربي — تم بنجاح", text)
        self.assertIn("INFO", text)
        self.assertIn("tray:", text)
        self.assertIn(f"[{logging_setup.SESSION_ID}]", text)
        # Millisecond timestamps make a 5-second tick loop debuggable.
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")

    def test_error_log_keeps_only_warnings_and_above(self) -> None:
        self.configure("DEBUG")
        log = logging.getLogger("t")
        log.info("plain-info-line")
        log.warning("careful-warning-line")
        log.error("broken-error-line")

        errors = self.error_log.read_text(encoding="utf-8")
        self.assertNotIn("plain-info-line", errors)
        self.assertIn("careful-warning-line", errors)
        self.assertIn("broken-error-line", errors)

        main = self.main_text()
        self.assertIn("plain-info-line", main)
        self.assertIn("broken-error-line", main)

    def test_rotation_produces_backups(self) -> None:
        self.configure("INFO", max_bytes=2048, backup_count=2)
        log = logging.getLogger("rot")
        for index in range(200):
            log.info("%s %d", "x" * 100, index)

        backups = [p for p in self.log_dir.glob(logging_setup.MAIN_LOG_NAME + ".*")]
        self.assertGreaterEqual(len(backups), 1)
        self.assertTrue(self.main_log.exists())

    def test_debug_is_hidden_until_level_is_raised(self) -> None:
        self.configure("INFO")
        logging.getLogger("lvl").debug("hidden-debug-line")
        self.assertNotIn("hidden-debug-line", self.main_text())

        logging_setup.set_level("DEBUG")
        logging.getLogger("lvl").debug("visible-debug-line")
        self.assertIn("visible-debug-line", self.main_text())

    def test_setup_twice_does_not_duplicate_handlers(self) -> None:
        self.configure("INFO")
        first = len([h for h in logging.getLogger().handlers if getattr(h, "_timetracker", False)])
        self.configure("INFO")
        second = len([h for h in logging.getLogger().handlers if getattr(h, "_timetracker", False)])
        self.assertEqual(first, second)

    def test_third_party_noise_stays_at_warning_in_verbose_mode(self) -> None:
        self.configure("DEBUG")
        logging.getLogger("PIL").debug("pil-import-spam")
        self.assertNotIn("pil-import-spam", self.main_text())
        self.assertGreaterEqual(logging.getLogger("PIL").level, logging.WARNING)

    def test_setup_falls_back_to_temp_when_data_dir_is_unwritable(self) -> None:
        from unittest import mock

        with mock.patch.object(logging_setup, "get_app_dir", side_effect=OSError("denied")):
            path = logging_setup.setup_logging(
                "INFO", log_dir=None, console=False, enable_faults=False
            )
            logging.getLogger("fallback").info("still logging")
        self.assertTrue(path.exists())
        self.assertIn("TimeTracker", str(path))
        self.assertEqual(path.name, logging_setup.MAIN_LOG_NAME)

    def test_custom_handler_uses_rotating_file_handler(self) -> None:
        self.configure("INFO")
        handlers = [
            h
            for h in logging.getLogger().handlers
            if getattr(h, "_tt_role", "") == "main"
        ]
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], logging.handlers.RotatingFileHandler)
        self.assertEqual(handlers[0].encoding.lower().replace("-", ""), "utf8")


class TestCrashHandlers(LoggingSetUpCase):
    def test_worker_thread_crash_is_logged(self) -> None:
        self.configure("INFO")
        logging_setup.install_crash_handlers()

        def boom() -> None:
            raise RuntimeError("worker exploded")

        thread = threading.Thread(target=boom, name="BoomThread")
        thread.start()
        thread.join()

        text = self.main_text()
        self.assertIn("Unhandled exception in thread 'BoomThread'", text)
        self.assertIn("RuntimeError: worker exploded", text)
        self.assertIn("CRITICAL", text)

    def test_handle_fatal_writes_traceback(self) -> None:
        self.configure("INFO")
        try:
            raise ValueError("kaboom")
        except ValueError as exc:
            logging_setup.handle_fatal(exc, notify_user=False)

        text = self.main_text()
        self.assertIn("Fatal error: kaboom", text)
        self.assertIn("Traceback", text)
        self.assertIn("ValueError: kaboom", text)

    def test_describe_exception_points_at_the_last_frame(self) -> None:
        try:
            raise KeyError("missing")
        except KeyError as exc:
            message = logging_setup.describe_exception(exc)
        self.assertIn("KeyError", message)
        self.assertIn("missing", message)
        self.assertIn("test_describe_exception_points_at_the_last_frame", message)


class TestDiagnostics(LoggingSetUpCase):
    def test_bundle_contains_report_logs_and_session(self) -> None:
        self.configure("INFO")
        logging.getLogger("bundle").warning("diagnostics-marker")

        bundle = logging_setup.create_diagnostics_bundle(
            {"answer": 42}, dest_dir=self.log_dir / "diag", include_config=False
        )

        self.assertTrue(bundle.exists())
        self.assertEqual(bundle.suffix, ".zip")
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            self.assertIn("system.txt", names)
            self.assertIn("session.json", names)
            self.assertTrue(any(name.startswith("logs/") for name in names))

            report = archive.read("system.txt").decode("utf-8")
            self.assertIn("answer: 42", report)
            self.assertIn("TimeTracker diagnostics", report)

            session = json.loads(archive.read("session.json").decode("utf-8"))
            self.assertEqual(session["extra"]["answer"], 42)
            self.assertEqual(session["log_dir"], str(self.log_dir))

            log_payload = "".join(
                archive.read(name).decode("utf-8", "replace")
                for name in names
                if name.startswith("logs/")
            )
            self.assertIn("diagnostics-marker", log_payload)

    def test_bundle_falls_back_to_a_writable_folder(self) -> None:
        self.configure("INFO")
        target = self.log_dir / "nested" / "deeper"
        bundle = logging_setup.create_diagnostics_bundle(dest_dir=target, include_config=False)
        self.assertTrue(bundle.exists())
        self.assertEqual(bundle.parent, target)


class TestLogLevelConfig(unittest.TestCase):
    def test_normalize_accepts_case_and_numbers(self) -> None:
        self.assertEqual(normalize_log_level("debug"), "DEBUG")
        self.assertEqual(normalize_log_level(" Warning "), "WARNING")
        self.assertEqual(normalize_log_level(20), "INFO")
        self.assertEqual(normalize_log_level("20"), "INFO")

    def test_normalize_rejects_garbage_without_raising(self) -> None:
        for value in (None, "", "verbose", True, [], {"a": 1}):
            self.assertEqual(normalize_log_level(value), "INFO")

    def test_log_level_survives_a_config_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Config(log_level="DEBUG").save(path)
            self.assertEqual(load_config(path).log_level, "DEBUG")

    def test_invalid_log_level_in_file_falls_back_to_info(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config()
            cfg.save(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["log_level"] = "loud"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(load_config(path).log_level, "INFO")


if __name__ == "__main__":
    unittest.main()
