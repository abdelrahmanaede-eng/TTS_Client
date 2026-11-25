"""Unit tests for the v1.3.2 RTL and DPI layout helpers (audit items 1 and 2).

These tests are pure-Python: they do not create a Tk window, so they run in CI
and on any machine without a display.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import time_tracker.ui.dialogs as dialogs
from time_tracker.localization.i18n import TRANSLATIONS

DIALOGS_PATH = Path(dialogs.__file__)


class TestRtlFlags(unittest.TestCase):
    def test_is_rtl(self) -> None:
        for lang in ("ar", "AR", "ar-EG", "Ar"):
            self.assertTrue(dialogs.is_rtl(lang), lang)
        for lang in ("en", "EN", "", None, "fr"):
            self.assertFalse(dialogs.is_rtl(lang), lang)

    def test_rtl_layout_english(self) -> None:
        flags = dialogs.rtl_layout("en")
        self.assertFalse(flags["rtl"])
        self.assertEqual(flags["anchor"], "w")
        self.assertEqual(flags["justify"], "left")
        self.assertEqual(flags["side"], "left")
        self.assertEqual(flags["direction"], dialogs._LRM)

    def test_rtl_layout_arabic(self) -> None:
        flags = dialogs.rtl_layout("ar")
        self.assertTrue(flags["rtl"])
        self.assertEqual(flags["anchor"], "e")
        self.assertEqual(flags["justify"], "right")
        self.assertEqual(flags["side"], "right")
        self.assertEqual(flags["direction"], dialogs._RLM)

    def test_flip_side(self) -> None:
        self.assertEqual(dialogs.flip_side("left", "ar"), "right")
        self.assertEqual(dialogs.flip_side("right", "ar"), "left")
        self.assertEqual(dialogs.flip_side("top", "ar"), "top")
        self.assertEqual(dialogs.flip_side("left", "en"), "left")

    def test_flip_sticky(self) -> None:
        self.assertEqual(dialogs.flip_sticky("w", "ar"), "e")
        self.assertEqual(dialogs.flip_sticky("we", "ar"), "ew")
        self.assertEqual(dialogs.flip_sticky("nsew", "ar"), "nswe")
        self.assertEqual(dialogs.flip_sticky("w", "en"), "w")

    def test_mirror_column(self) -> None:
        self.assertEqual(dialogs.mirror_column(0, 3, "ar"), 3)
        self.assertEqual(dialogs.mirror_column(1, 3, "ar"), 2)
        self.assertEqual(dialogs.mirror_column(1, 3, "en"), 1)


class TestBidiText(unittest.TestCase):
    def test_english_is_unchanged(self) -> None:
        self.assertEqual(dialogs.bidi_text("Worked 8:00", "en"), "Worked 8:00")

    def test_arabic_wraps_latin_and_numbers(self) -> None:
        result = dialogs.bidi_text("العمل 8:30 ساعة", "ar")
        self.assertIn("\u200e8:30\u200e", result)
        # The Arabic words themselves are untouched.
        self.assertIn("العمل", result)

    def test_arabic_wraps_exe_names(self) -> None:
        result = dialogs.bidi_text("التطبيق chrome.exe نشط", "ar")
        self.assertIn("\u200echrome.exe\u200e", result)

    def test_idempotent(self) -> None:
        once = dialogs.bidi_text("العمل 8:30", "ar")
        twice = dialogs.bidi_text(once, "ar")
        self.assertEqual(once, twice)

    def test_empty_and_none(self) -> None:
        self.assertEqual(dialogs.bidi_text("", "ar"), "")
        self.assertIsNone(dialogs.bidi_text(None, "ar"))


class TestDialogGeometry(unittest.TestCase):
    SCREEN = (1920, 1080)

    def test_grows_with_scale(self) -> None:
        sizes = [
            dialogs.compute_dialog_geometry(600, 720, scale, *self.SCREEN)
            for scale in (1.0, 1.25, 1.5, 1.75)
        ]
        for previous, current in zip(sizes, sizes[1:]):
            self.assertGreater(current[0], previous[0])
            # Height stops growing once it hits the 90% cap.
            self.assertGreaterEqual(current[1], previous[1])

    def test_scales_at_each_dpi(self) -> None:
        self.assertEqual(dialogs.compute_dialog_geometry(600, 720, 1.0, *self.SCREEN), (600, 720))
        self.assertEqual(dialogs.compute_dialog_geometry(600, 720, 1.25, *self.SCREEN), (750, 900))
        self.assertEqual(dialogs.compute_dialog_geometry(600, 720, 1.5, *self.SCREEN), (900, 972))

    def test_capped_to_90_percent_of_screen(self) -> None:
        # 175% on a small laptop screen must not exceed the screen.
        width, height = dialogs.compute_dialog_geometry(600, 720, 1.75, 1366, 768)
        self.assertLessEqual(width, int(1366 * 0.9))
        self.assertLessEqual(height, int(768 * 0.9))
        self.assertEqual(height, int(768 * 0.9))

    def test_never_below_minimum(self) -> None:
        width, height = dialogs.compute_dialog_geometry(10, 10, 1.0, 800, 600)
        self.assertGreaterEqual(width, 240)
        self.assertGreaterEqual(height, 160)

    def test_90_percent_of_full_hd(self) -> None:
        self.assertEqual(int(1080 * 0.9), 972)
        self.assertEqual(dialogs.compute_dialog_geometry(600, 720, 1.75, *self.SCREEN), (1050, 972))


class TestDialogsUseHelpers(unittest.TestCase):
    def test_every_dialog_applies_rtl(self) -> None:
        source = DIALOGS_PATH.read_text(encoding="utf-8")
        # consent, about, settings, pause, past meeting, meeting note, overtime,
        # presence popup, update dialog, date range picker.
        self.assertGreaterEqual(source.count("apply_rtl("), 10)
        self.assertIn("def make_scrollable(", source)
        self.assertIn("fit_dialog(", source)
        # No dialog may keep a hard-coded geometry string any more.
        self.assertNotIn('root.geometry(f"{width}x{height}', source)

    def test_settings_uses_scrollable_tabs(self) -> None:
        source = DIALOGS_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("make_scrollable(notebook)"), 4)
        self.assertIn("_switch_language", source)

    def test_mouse_wheel_does_not_hijack_other_widgets(self) -> None:
        source = DIALOGS_PATH.read_text(encoding="utf-8")
        self.assertIn("_dispatch_mousewheel", source)
        self.assertNotIn('canvas.bind_all("<MouseWheel>"', source)
        self.assertNotIn("canvas.unbind_all", source)


class TestSingleLanguageHeaders(unittest.TestCase):
    def _assert_single_language(self, value: str, lang: str) -> None:
        self.assertNotIn("/", value, f"{lang}: {value}")
        if lang == "en":
            self.assertFalse(any("\u0600" <= ch <= "\u06ff" for ch in value), value)
        else:
            self.assertFalse(any(ch.isascii() and ch.isalpha() for ch in value), value)

    def test_headers(self) -> None:
        for key in ("sec_language", "sec_update", "settings_lang_label"):
            for lang in ("en", "ar"):
                self.assertIn(key, TRANSLATIONS[lang])
                self._assert_single_language(TRANSLATIONS[lang][key], lang)

    def test_new_keys_are_symmetric(self) -> None:
        en_keys = set(TRANSLATIONS["en"])
        ar_keys = set(TRANSLATIONS["ar"])
        self.assertEqual(en_keys, ar_keys)


if __name__ == "__main__":
    unittest.main()
