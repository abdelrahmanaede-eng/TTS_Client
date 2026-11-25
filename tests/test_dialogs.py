"""Tests for the dialogs module watchdog against a stuck dialog flag."""
from __future__ import annotations

import time
import unittest

import time_tracker.ui.dialogs as dialogs


class TestDialogFlagWatchdog(unittest.TestCase):
    def tearDown(self) -> None:
        with dialogs._dialog_lock:
            dialogs._is_dialog_active = False

    def _set(self, active: bool, heartbeat_age: float) -> None:
        with dialogs._dialog_lock:
            dialogs._is_dialog_active = active
            dialogs._dialog_heartbeat_ts = time.monotonic() - heartbeat_age

    def test_fresh_heartbeat_reports_showing(self) -> None:
        self._set(True, heartbeat_age=0.0)
        self.assertTrue(dialogs.is_dialog_showing())
        # Flag must remain set for a healthy dialog.
        self.assertTrue(dialogs._is_dialog_active)

    def test_stale_flag_auto_clears(self) -> None:
        """A dialog that died without cleanup must not block future popups forever."""
        self._set(True, heartbeat_age=dialogs._DIALOG_STALE_SECONDS + 30.0)
        self.assertFalse(dialogs.is_dialog_showing())
        self.assertFalse(dialogs._is_dialog_active)

        # Once cleared, a new popup is allowed again.
        self.assertFalse(dialogs.is_dialog_showing())

    def test_clear_flag_reports_not_showing(self) -> None:
        self._set(False, heartbeat_age=0.0)
        self.assertFalse(dialogs.is_dialog_showing())

    def test_heartbeat_helper_updates_timestamp(self) -> None:
        with dialogs._dialog_lock:
            dialogs._dialog_heartbeat_ts = time.monotonic() - 500.0
        dialogs._touch_dialog_heartbeat()
        with dialogs._dialog_lock:
            self.assertGreaterEqual(
                dialogs._dialog_heartbeat_ts, time.monotonic() - 1.0
            )


    def test_date_range_picker_dialog_callable(self) -> None:
        """Verify show_date_range_picker_dialog_async accepts custom title and buttons."""
        called = []
        dialogs.show_date_range_picker_dialog_async(
            on_submit=lambda s, e: called.append((s, e)),
            lang="ar",
            title="تصدير مخصص",
            button_text="تصدير الآن",
            icon="📄",
        )
        self.assertTrue(callable(dialogs.show_date_range_picker_dialog_async))


if __name__ == "__main__":
    unittest.main()
