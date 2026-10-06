import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from chapters import inspect_media
from main import (DownloadCheckpoint, MainWindow, VideoItem, WorkThread,
                  dubbing_stats, encode_episode_matrix)
from resolvers import StreamResult


class SelectionAndResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dubbings_show_unique_episode_counts_and_site_view_order(self):
        items = [VideoItem(1, "CVH", "A", "1", 1, "", views=100),
                 VideoItem(2, "Kodik", "A", "1", 1, "", views=90),
                 VideoItem(3, "CVH", "A", "2", 2, "", views=100)]
        self.assertEqual(dubbing_stats(items)["A"], ({1.0, 2.0}, 200))
        raw = [
            {"data": {"player": "CVH", "dubbing": dub}, "number": str(ep),
             "index": ep, "video_id": index, "views": views, "iframe_url": ""}
            for index, (dub, ep, views) in enumerate([
                ("A", 1, 100), ("A", 2, 100), ("B", 1, 80),
                ("C", 1, 20), ("C", 2, 20),
            ], 1)
        ]
        with patch("main.load_config", return_value={"public_token": "test"}), \
             patch.object(MainWindow, "schedule_quality_probe"):
            window = MainWindow()
            self.addCleanup(window.close)
            window.on_loaded({"title": "Test"}, raw)
            self.assertEqual(window.selected_episodes(), [1.0, 2.0])
            self.assertEqual([window.dub_list.item(i).data(Qt.UserRole)
                              for i in range(window.dub_list.count())], ["A", "C"])
            self.assertIn("серий: 2 · просмотров: 200", window.dub_list.item(0).text())
            window.ep_list.item(1).setCheckState(Qt.Unchecked)
            self.assertEqual([window.dub_list.item(i).data(Qt.UserRole)
                              for i in range(window.dub_list.count())], ["A", "B", "C"])
            window.dub_list.item(1).setCheckState(Qt.Checked)
            window.ep_list.item(1).setCheckState(Qt.Checked)
            self.assertEqual(window.selected_dubbings(), ["A"])
            window.dub_all.click()
            self.assertEqual(window.selected_dubbings(), ["A", "C"])
            window.dub_none.click()
            self.assertEqual(window.selected_dubbings(), [])
            self.assertFalse(window.quality_combo.isEnabled())
            window.dub_all.click()
            self.assertEqual(window.selected_dubbings(), ["A", "C"])

    def test_failed_download_resumes_without_redownloading_completed_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = VideoItem(1, "CVH", "Voice", "1", 1, "")
            second = VideoItem(2, "CVH", "Voice", "2", 2, "")
            matrix = {1.0: {"__video__": first, "Voice": first},
                      2.0: {"__video__": second, "Voice": second}}
            checkpoint = DownloadCheckpoint({"schema": 1, "settings": {},
                "matrix": encode_episode_matrix(matrix), "files": {}, "episodes": {}},
                root / "pending.json")
            finished = root / "finished.mp4"
            finished.write_bytes(b"existing episode")
            checkpoint.complete_episode(1.0, finished)
            worker = WorkThread("CVH", ["Voice"], matrix, "720p", root, "Test",
                False, "", False, plexmatch_enabled=False,
                chapters_enabled=False, checkpoint=checkpoint)
            with patch.object(worker, "resolve_stream", return_value=StreamResult(
                    "https://example.test/media.mp4", "direct", {}, {})) as resolve, \
                 patch.object(worker, "download_stream", side_effect=RuntimeError("reset")), \
                 patch("main.PlayerResolver.release"):
                worker.run()
            self.assertEqual(resolve.call_count, 2)
            self.assertTrue(checkpoint.path.exists())

            resumed = WorkThread("CVH", ["Voice"], matrix, "720p", root, "Test",
                False, "", False, plexmatch_enabled=False,
                chapters_enabled=False, checkpoint=checkpoint)
            def download(_stream, path, _item, _progress):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"new episode")
            with patch.object(resumed, "resolve_stream", return_value=StreamResult(
                    "https://example.test/media.mp4", "direct", {}, {})) as resolve, \
                 patch.object(resumed, "download_stream", side_effect=download), \
                 patch("main.PlayerResolver.release"):
                resumed.run()
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(finished.read_bytes(), b"existing episode")
            self.assertFalse(checkpoint.path.exists())

    def test_chapter_probe_hides_ffmpeg_console_on_windows(self):
        seen = {}
        def run(_command, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(returncode=0, stderr="Duration: 00:01:00.00", stdout="")
        inspect_media("ffmpeg", "episode.mkv", subprocess_run=run)
        if os.name == "nt":
            self.assertNotEqual(seen["creationflags"], 0)
            self.assertIn("startupinfo", seen)


if __name__ == "__main__":
    unittest.main()
