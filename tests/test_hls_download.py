"""Regression: a finite HLS manifest must reach EOF rather than reconnect forever."""

import functools
import http.server
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from main import WorkThread, find_ffmpeg
from resolvers import StreamResult


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class HlsDownloadTests(unittest.TestCase):
    def test_finite_hls_download_starts_and_finishes(self):
        ffmpeg = find_ffmpeg()
        self.assertTrue(ffmpeg)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                "color=c=red:s=160x90:d=2", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac",
                "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
                str(root / "video.m3u8")], check=True, timeout=20)
            handler = functools.partial(QuietHandler, directory=directory)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            worker = WorkThread("Alloha", ["Voice"], {}, "Лучшее", root, "Test",
                                False, "", False, ffmpeg_path=ffmpeg)
            stream = StreamResult(f"http://127.0.0.1:{server.server_port}/video.m3u8",
                                  "alloha", {}, {})
            progress = []
            original_popen = subprocess.Popen
            timers = []

            def bounded_popen(*args, **kwargs):
                proc = original_popen(*args, **kwargs)
                timer = threading.Timer(12, proc.kill)
                timer.daemon = True
                timers.append(timer)
                timer.start()
                return proc

            try:
                with patch("main.subprocess.Popen", side_effect=bounded_popen):
                    worker.download_stream(stream, root / "output.mkv",
                                           SimpleNamespace(duration=2),
                                           lambda pct, _: progress.append(pct))
            finally:
                for timer in timers:
                    timer.cancel()
            self.assertGreater((root / "output.mkv").stat().st_size, 1000)
            self.assertEqual(progress[-1], 100)
