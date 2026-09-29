import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from main import MainWindow, merge_audio_tracks
import alloha_runtime


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_video_player_does_not_limit_audio_or_reset_selection(self):
        with patch("main.load_config", return_value={"public_token": "test"}), \
             patch.object(MainWindow, "schedule_quality_probe"):
            window = MainWindow()
            self.addCleanup(window.close)
            raw = [
                {"data": {"player": player, "dubbing": dub}, "number": str(ep),
                 "index": ep, "iframe_url": "", "video_id": index}
                for index, (player, dub, ep) in enumerate([
                    ("CVH", "Video voice", 1), ("CVH", "Video voice", 2),
                    ("Kodik", "Audio voice", 1), ("Sibnet", "Rare voice", 1)])]
            window.on_loaded({"title": "Test"}, raw)
            self.assertEqual(window.dub_list.count(), 3)
            for i in range(window.dub_list.count()):
                window.dub_list.item(i).setCheckState(Qt.Checked)
            matrix = window.episode_matrix()
            self.assertEqual(matrix[1.0]["__video__"].player, "CVH")
            self.assertEqual(matrix[1.0]["Audio voice"].player, "Kodik")
            self.assertEqual(matrix[1.0]["Rare voice"].player, "Sibnet")
            self.assertEqual(window.ep_list.count(), 1)
            self.assertTrue(window.merge_check.isChecked())
            window.player_combo.setCurrentText("Kodik")
            self.assertEqual(len(window.selected_dubbings()), 3)
            self.assertEqual(window.episode_matrix()[1.0]["__video__"].player, "Kodik")

    def test_audio_quality_does_not_constrain_video_quality(self):
        from main import WorkThread, VideoItem
        from resolvers import StreamResult
        item = VideoItem(1, "CVH", "Voice", "1", 1, "")
        worker = WorkThread("CVH", ["Voice"], {}, "1080p", ".", "Test", False, "", False)
        result = StreamResult("low", "cvh", {"720p": "audio-url"}, {})
        with patch.object(worker.resolver, "resolve", return_value=result):
            audio = worker.resolve_stream(item, audio_only=True)
        self.assertEqual(audio.url, "audio-url")

    def test_mux_separates_video_and_audio_inputs(self):
        process = MagicMock()
        process.stdout = []
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as directory, \
             patch("main.audio_track_ids", return_value=[1]), \
             patch("main.subprocess.Popen", return_value=process) as popen:
            root = Path(directory)
            merge_audio_tracks("mkvmerge", [(root / "audio.mkv", "Voice")],
                               root / "out.mkv", video_source=root / "video.mkv")
            cmd = popen.call_args.args[0]
            self.assertEqual(cmd[cmd.index(str(root / "video.mkv")) - 1], "--no-audio")
            self.assertIn("--no-video", cmd)


class RuntimeTests(unittest.TestCase):
    def test_remote_resolver_is_not_installed_locally(self):
        with patch.object(alloha_runtime, "node_runtime") as node:
            alloha_runtime.ensure_resolver("https://resolver.example.com")
            node.assert_not_called()

    def test_running_local_resolver_is_reused(self):
        with patch.object(alloha_runtime, "healthy", return_value=True), \
             patch.object(alloha_runtime, "node_runtime") as node:
            alloha_runtime.ensure_resolver("http://127.0.0.1:8790")
            node.assert_not_called()

    def test_archive_cannot_escape_install_directory(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / "bad.zip", "w") as archive:
                archive.writestr("../escape.txt", "bad")
            with self.assertRaises(RuntimeError):
                alloha_runtime.extract(root / "bad.zip", root / "install")
