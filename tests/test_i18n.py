"""Unit tests for the internationalization (i18n) module."""

import unittest
from time_tracker.localization.i18n import TRANSLATIONS, t


class TestI18n(unittest.TestCase):
    def test_translations_dict_structure(self):
        """Both 'en' and 'ar' language sets must exist."""
        self.assertIn("en", TRANSLATIONS)
        self.assertIn("ar", TRANSLATIONS)
        self.assertGreater(len(TRANSLATIONS["en"]), 40)
        self.assertGreater(len(TRANSLATIONS["ar"]), 40)

    def test_translation_symmetry(self):
        """All keys in 'en' must also be translated into 'ar'."""
        en_keys = set(TRANSLATIONS["en"].keys())
        ar_keys = set(TRANSLATIONS["ar"].keys())
        missing_in_ar = en_keys - ar_keys
        missing_in_en = ar_keys - en_keys
        self.assertEqual(missing_in_ar, set(), f"Keys missing in Arabic: {missing_in_ar}")
        self.assertEqual(missing_in_en, set(), f"Keys missing in English: {missing_in_en}")

    def test_basic_translation(self):
        """Translate basic strings without placeholders."""
        self.assertEqual(t("action_quit", "en"), "Quit")
        self.assertEqual(t("action_quit", "ar"), "خروج")
        self.assertEqual(t("ot_yes", "ar"), "نعم، سجّل كوقت إضافي")
        self.assertEqual(t("btn_save_settings", "en"), "Save Settings")
        self.assertEqual(t("btn_save_settings", "ar"), "حفظ الإعدادات")

    def test_formatting_placeholders(self):
        """Translate strings containing formatting placeholders."""
        en_res = t("menu_today_worked", "en", worked="5h 20m", target=8)
        self.assertEqual(en_res, "Today: 5h 20m / 8h worked")

        ar_res = t("menu_today_worked", "ar", worked="5h 20m", target=8)
        self.assertEqual(ar_res, "اليوم: 5h 20m / 8 س عمل")

        en_eod = t("notif_eod_summary", "en", worked="7h 30m", target=8, overtime="0h")
        self.assertEqual(en_eod, "Worked 7h 30m of 8h today | Overtime: 0h")

        ar_eod = t("notif_eod_summary", "ar", worked="7h 30m", target=8, overtime="0h")
        self.assertEqual(ar_eod, "عملت اليوم 7h 30m من أصل 8 س | وقت إضافي: 0h")

    def test_fallback_behavior(self):
        """Fallback to English if key missing or language unrecognized."""
        # Non-existent language code falls back to 'en'
        self.assertEqual(t("action_quit", "fr"), "Quit")
        # Non-existent key returns key itself
        self.assertEqual(t("non_existent_key_xyz", "en"), "non_existent_key_xyz")
        self.assertEqual(t("non_existent_key_xyz", "ar"), "non_existent_key_xyz")


if __name__ == "__main__":
    unittest.main()
