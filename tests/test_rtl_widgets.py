"""RTL selection-widget tests (indicator mirrors with the language)."""

from __future__ import annotations

import unittest
from pathlib import Path

import dialogs

SOURCE = Path(dialogs.__file__).read_text(encoding="utf-8")


class TestSelectionLayout(unittest.TestCase):
    @staticmethod
    def _side(kind: str, rtl: bool) -> str:
        return dialogs.selection_layout(kind, rtl)[0][1]["children"][0][1]["side"]

    def test_arabic_indicator_on_right(self) -> None:
        self.assertEqual(self._side("Checkbutton", True), "right")
        self.assertEqual(self._side("Radiobutton", True), "right")

    def test_english_indicator_on_left(self) -> None:
        self.assertEqual(self._side("Checkbutton", False), "left")
        self.assertEqual(self._side("Radiobutton", False), "left")

    def test_focus_sticky_mirrors_too(self) -> None:
        ar = dialogs.selection_layout("Checkbutton", True)[0][1]["children"][1][1]
        en = dialogs.selection_layout("Checkbutton", False)[0][1]["children"][1][1]
        self.assertEqual((ar["side"], ar["sticky"]), ("right", "e"))
        self.assertEqual((en["side"], en["sticky"]), ("left", "w"))


class TestRtlWiring(unittest.TestCase):
    def test_apply_rtl_reconfigures_selection_styles(self) -> None:
        src = SOURCE[SOURCE.index("def apply_rtl("): SOURCE.index("def _apply_rtl_tree(")]
        self.assertIn("configure_rtl_widget_styles(lang)", src)

    def test_all_selection_widgets_use_rtl_factory(self) -> None:
        # No classic tk.Checkbutton / tk.Radiobutton constructions remain: they
        # cannot put the indicator on the right for Arabic.
        import re

        self.assertIsNone(re.search(r"(?<!t)tk\.Checkbutton\(", SOURCE))
        self.assertIsNone(re.search(r"(?<!t)tk\.Radiobutton\(", SOURCE))
        self.assertGreaterEqual(SOURCE.count('make_selection_widget('), 8)

    def test_settings_dialog_builds_with_factory(self) -> None:
        start = SOURCE.index("def show_settings_dialog_async(")
        end = SOURCE.index("\ndef ", start + 1)
        settings = SOURCE[start:end]
        self.assertIn('make_selection_widget(', settings)
        self.assertIn("apply_rtl(root, lang)", settings)


if __name__ == "__main__":
    unittest.main()
