import os
import tempfile
import unittest
from pathlib import Path
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

    def test_quality_checks_are_debounced_and_never_overlap(self):
        with patch('main.load_config', return_value={'public_token':'test'}), \
             patch('main.QualityProbeThread') as thread:
            window=MainWindow()
            self.addCleanup(window.close)
            self.addCleanup(window.quality_probe_timer.stop)
            raw=[{'data':{'player':'CVH','dubbing':'Voice'},'number':'1','index':1,'iframe_url':''}]
            window.on_loaded({'title':'Test'},raw)
            for _ in range(5): window.schedule_quality_probe()
            self.assertEqual(thread.call_count,0)
            window.quality_probe_timer.timeout.emit()
            self.assertEqual(thread.call_count,1)
            for _ in range(5): window.schedule_quality_probe()
            window.quality_probe_timer.timeout.emit()
            self.assertEqual(thread.call_count,1)
            thread.return_value.finished.connect.call_args.args[0]()
            window.quality_probe_timer.timeout.emit()
            self.assertEqual(thread.call_count,2)

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
            self.assertTrue(window.source_options.isHidden())
            self.assertIn("CVH", window.source_toggle.text())
            window.source_toggle.click()
            self.assertFalse(window.source_options.isHidden())
            self.assertNotIn("parad-smerti", window.url_edit.lineEdit().placeholderText())

    def test_failed_update_is_reported_once(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("main.CONFIG_DIR", Path(directory)), \
             patch("main.load_config", return_value={"public_token": "test"}):
            marker = Path(directory) / "update-result.txt"
            marker.write_text("error: access denied", encoding="utf-8")
            window = MainWindow()
            self.addCleanup(window.close)
            self.assertFalse(marker.exists())
            self.assertIsNone(window.previous_update_error())


if __name__ == "__main__":
    unittest.main()
