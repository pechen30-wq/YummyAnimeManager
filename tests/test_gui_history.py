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


if __name__ == "__main__":
    unittest.main()
