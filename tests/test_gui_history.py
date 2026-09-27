import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from main import MainWindow


class HistoryGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dropdown_persists_latest_link(self):
        config = {"public_token": "test", "url_history": ["https://example.com/old"]}
        with patch("main.load_config", return_value=config), patch("main.save_config") as save:
            window = MainWindow()
            self.addCleanup(window.close)
            self.assertEqual(window.url_edit.itemText(0), "https://example.com/old")
            window.url_edit.setEditText("https://example.com/catalog/item/new")
            with patch("main.FetchThread.start"):
                window.load_anime()
            self.assertEqual(window.url_edit.itemText(0), "https://example.com/catalog/item/new")
            self.assertEqual(window.url_edit.itemText(1), "https://example.com/old")
            save.assert_called()

    def test_cvh_is_initial_player_when_available(self):
        with patch("main.load_config", return_value={"public_token": "test"}):
            window = MainWindow()
            self.addCleanup(window.close)
            raw = [{"data": {"player": player, "dubbing": "AniDUB"},
                    "number": "1", "index": 1, "iframe_url": ""}
                   for player in ("Kodik", "CVH")]
            with patch.object(window, "schedule_quality_probe"):
                window.on_loaded({"title": "Test"}, raw)
            self.assertEqual(window.player_combo.currentText(), "CVH")
            self.assertNotIn("parad-smerti", window.url_edit.lineEdit().placeholderText())


if __name__ == "__main__":
    unittest.main()
