"""Accessibility & RTL audit tests for the Settings/consent scroll wrappers."""

from __future__ import annotations

import unittest
from pathlib import Path

import dialogs

DIALOGS_PATH = Path(dialogs.__file__)


def _function_source(name: str) -> str:
    source = DIALOGS_PATH.read_text(encoding="utf-8")
    start = source.index(f"def {name}(")
    rest = source[start + 1 :]
    nxt = rest.find("\ndef ")
    return rest if nxt == -1 else rest[:nxt]


class TestFocusScrollMath(unittest.TestCase):
    """Pure part of keyboard focus-follow; no Tk display required."""

    def test_already_visible_does_not_scroll(self) -> None:
        self.assertIsNone(dialogs._scroll_fraction_for_focus(20, 80, 100, 500, 0))
        self.assertIsNone(dialogs._scroll_fraction_for_focus(120, 180, 100, 500, 100))

    def test_focus_above_viewport_scrolls_to_it(self) -> None:
        self.assertEqual(dialogs._scroll_fraction_for_focus(50, 90, 100, 500, 200), 50 / 500)

    def test_focus_below_viewport_scrolls_to_bottom(self) -> None:
        self.assertEqual(dialogs._scroll_fraction_for_focus(480, 520, 100, 600, 100), 420 / 600)

    def test_oversized_widget_aligns_its_top(self) -> None:
        self.assertEqual(dialogs._scroll_fraction_for_focus(300, 900, 100, 1000, 0), 0.3)

    def test_result_is_clamped_to_zero_one(self) -> None:
        self.assertEqual(dialogs._scroll_fraction_for_focus(-40, 10, 100, 500, 200), 0.0)
        self.assertEqual(dialogs._scroll_fraction_for_focus(490, 520, 100, 500, 0), 0.8)

    def test_degenerate_dimensions_do_nothing(self) -> None:
        self.assertIsNone(dialogs._scroll_fraction_for_focus(0, 10, 0, 500, 0))
        self.assertIsNone(dialogs._scroll_fraction_for_focus(0, 10, 100, 0, 0))


class TestScrollWrapperRegistration(unittest.TestCase):
    def test_scrollbar_side_is_cached_not_hardcoded(self) -> None:
        src = _function_source("make_scrollable")
        self.assertIn("_SCROLLBAR_SIDE_ATTR", src)
        # The initial pack is LTR by construction; only that one occurrence is
        # allowed. The re-pack in _sync must read the cached (RTL-aware) side.
        self.assertEqual(src.count('vbar.pack(side="right", fill="y")'), 1)
        self.assertIn("vbar.pack(side=getattr(canvas", src)

    def test_keyboard_focus_follow_is_installed(self) -> None:
        src = _function_source("make_scrollable")
        self.assertIn("_reveal_focused", src)
        self.assertIn('inner.bind("<FocusIn>"', src)
        self.assertIn('outer.bind("<FocusIn>"', src)
        self.assertIn("_scroll_fraction_for_focus", src)

    def test_rtl_mirror_updates_cached_scrollbar_side(self) -> None:
        self.assertIn("_SCROLLBAR_SIDE_ATTR", _function_source("_apply_rtl_widget"))

    def test_wheel_dispatcher_stays_non_hijacking(self) -> None:
        source = DIALOGS_PATH.read_text(encoding="utf-8")
        wheel = _function_source("_dispatch_mousewheel")
        self.assertIn('bind_all("<MouseWheel>"', source)
        self.assertIn('add="+"', source)
        self.assertNotIn(".unbind_all(", source)
        self.assertNotIn('canvas.bind_all("<MouseWheel>"', source)
        self.assertIn("isinstance(widget, (tk.Listbox", wheel)
        self.assertIn('canvas.yview_scroll(steps, "units")', wheel)


class TestConsentDialogAccessibility(unittest.TestCase):
    def setUp(self) -> None:
        self.src = _function_source("show_consent_dialog_async")

    def test_long_localised_copy_is_scrollable(self) -> None:
        self.assertIn("make_scrollable(win", self.src)
        self.assertIn("content_outer.pack(fill=\"both\", expand=True", self.src)

    def test_keyboard_defaults_and_safe_escape(self) -> None:
        self.assertIn('win.bind("<Escape>"', self.src)
        self.assertIn("agree_btn.focus_set()", self.src)
        self.assertIn("win.protocol(\"WM_DELETE_WINDOW\", decline)", self.src)

    def test_terminal_callback_fires_at_most_once(self) -> None:
        # accept and decline each carry an early guard, so Enter on the focused
        # button plus a window-level binding can never answer twice.
        self.assertEqual(self.src.count("if answered:\n                    return"), 2)

    def test_rtl_still_applied_after_building(self) -> None:
        self.assertIn("apply_rtl(win, lang)", self.src)


if __name__ == "__main__":
    unittest.main()
