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
    def test_direct_file_is_saved_as_audio_without_video(self):
        ffmpeg = find_ffmpeg()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                            'color=c=red:s=160x90:d=2', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=2', '-c:v', 'libx264',
                            '-c:a', 'aac', '-movflags', '+faststart', str(root/'video.mp4')],
                           check=True, timeout=20)
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                functools.partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            worker = WorkThread('Sibnet', ['Voice'], {}, 'Лучшее', root, 'Test',
                                False, '', False, ffmpeg_path=ffmpeg)
            stream = StreamResult(f'http://127.0.0.1:{server.server_port}/video.mp4',
                                  'sibnet', {}, {}, audio_only=True)
            worker.download_stream(stream, root/'audio.mka', SimpleNamespace(duration=2),
                                   lambda *_: None)
            probe = subprocess.run([ffmpeg, '-hide_banner', '-i', str(root/'audio.mka')],
                                   capture_output=True, text=True, timeout=10)
            self.assertIn('Audio:', probe.stderr)
            self.assertNotIn('Video:', probe.stderr)

    def test_muxed_hls_audio_output_contains_no_video(self):
        self.download_hls(missing=False, audio_only=True)

    def test_separate_hls_audio_download_does_not_need_video(self):
        self.download_hls(missing=False, audio_only=True, separate_audio=True)

    def test_finite_hls_download_starts_and_finishes(self):
        self.download_hls(missing=False)

    def test_missing_segment_is_not_reported_as_a_success(self):
        self.download_hls(missing=True)

    def download_hls(self, missing, audio_only=False, separate_audio=False):
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
            if separate_audio:
                subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=2', '-c:a', 'aac', '-f', 'hls',
                    '-hls_time', '1', '-hls_list_size', '0', str(root/'audio.m3u8')],
                    check=True, timeout=20)
                (root/'master.m3u8').write_text(
                    '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Voice",'
                    'DEFAULT=YES,URI="audio.m3u8"\n'
                    '#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO="sound"\n'
                    'missing-video.m3u8\n', encoding='utf-8')
            if missing:
                for segment in root.glob("*.ts"):
                    segment.unlink()
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
            stream.audio_only = audio_only
            if separate_audio:
                stream.url = stream.url.replace('video.m3u8', 'master.m3u8')
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
                    if missing:
                        with self.assertRaises(RuntimeError):
                            worker.download_stream(stream, root / "output.mkv",
                                                   SimpleNamespace(duration=2),
                                                   lambda pct, _: progress.append(pct))
                    else:
                        worker.download_stream(stream, root / "output.mkv",
                                               SimpleNamespace(duration=2),
                                               lambda pct, _: progress.append(pct))
            finally:
                for timer in timers:
                    timer.cancel()
            if missing:
                self.assertNotIn(100, progress)
            else:
                self.assertGreater((root / "output.mkv").stat().st_size, 1000)
                self.assertEqual(progress[-1], 100)
                if audio_only:
                    probe = subprocess.run([ffmpeg, '-hide_banner', '-i', str(root/'output.mkv')],
                                           capture_output=True, text=True, timeout=10)
                    self.assertIn('Audio:', probe.stderr)
                    self.assertNotIn('Video:', probe.stderr)
