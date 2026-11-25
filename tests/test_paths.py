from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from time_tracker.paths import get_app_root


class TestApplicationPaths(unittest.TestCase):
    def test_source_root_is_project_directory(self) -> None:
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(get_app_root(), expected)

    def test_frozen_root_uses_pyinstaller_bundle_directory(self) -> None:
        bundle_root = Path("pyinstaller-bundle")
        with (
            mock.patch("time_tracker.paths.sys.frozen", True, create=True),
            mock.patch("time_tracker.paths.sys._MEIPASS", str(bundle_root), create=True),
        ):
            self.assertEqual(get_app_root(), bundle_root)


if __name__ == "__main__":
    unittest.main()
