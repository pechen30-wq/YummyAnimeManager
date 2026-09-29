import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from main import (DownloadCheckpoint, MainWindow, VideoItem, WorkThread,
                  decode_episode_matrix, encode_episode_matrix)


class CheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_roundtrip_and_completed_episode_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            item=VideoItem(1,"CVH","Voice","1",1,"https://example.test/frame")
            matrix={1.0:{"__video__":item,"Voice":item,"__video_candidates__":[item]}}
            payload={"schema":1,"settings":{"anime_title":"Test"},
                     "matrix":encode_episode_matrix(matrix),"files":{},"episodes":{}}
            checkpoint=DownloadCheckpoint(payload,root/"pending.json")
            output=root/"episode.mp4"
            output.write_bytes(b"complete")
            checkpoint.complete_episode(1.0,output)
            restored=DownloadCheckpoint.load(root/"pending.json")
            worker=WorkThread("CVH",["Voice"],decode_episode_matrix(restored.payload["matrix"]),
                              "1080p",root,"Test",False,"",False,
                              plexmatch_enabled=False,chapters_enabled=False,checkpoint=restored)
            with patch.object(worker,"resolve_stream") as resolve:
                worker.run()
            resolve.assert_not_called()
            self.assertFalse((root/"pending.json").exists())
            self.assertEqual(output.read_bytes(),b"complete")

    def test_pending_task_is_shown_after_new_window(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("main.CONFIG_DIR",Path(directory)), \
             patch("main.load_config",return_value={"public_token":"test"}):
            checkpoint=DownloadCheckpoint({"schema":1,"settings":{
                "anime_title":"Test","base_dir":directory},"matrix":{},
                "files":{},"episodes":{}},Path(directory)/"pending-download.json")
            checkpoint.save()
            window=MainWindow()
            self.addCleanup(window.close)
            self.assertTrue(window.resume_btn.isEnabled())
            self.assertTrue(window.stop_btn.isEnabled())
            self.assertFalse(window.download_btn.isEnabled())
            window.stop_download()
            self.assertFalse(checkpoint.path.exists())
            self.assertTrue(window.download_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
