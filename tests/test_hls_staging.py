import functools
import http.server
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import requests
from hls_download import stage_hls


class HlsStagingTests(unittest.TestCase):
    def test_audio_rendition_skips_video_playlist_and_segments(self):
        master = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",'
                  'NAME="Russian",DEFAULT=YES,URI="sound/index.m3u8"\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=4000000,AUDIO="sound"\nvideo/index.m3u8\n')
        audio = '#EXTM3U\n#EXTINF:6,\npart.aac\n#EXT-X-ENDLIST\n'
        fetched = []
        def fetch(url, headers):
            fetched.append(url)
            if url == 'https://example.com/master.m3u8':
                return master, url
            self.assertEqual(url, 'https://example.com/sound/index.m3u8')
            return audio, url
        def download(url, path, headers, progress, **kwargs):
            fetched.append(url)
            self.assertEqual(url, 'https://example.com/sound/part.aac')
            path.write_bytes(b'audio')
        with tempfile.TemporaryDirectory() as directory, \
             patch('hls_download.fetch_playlist', side_effect=fetch), \
             patch('hls_download.download_file', side_effect=download):
            stage_hls('https://example.com/master.m3u8', directory, {},
                      lambda *_: None, audio_only=True)
        self.assertFalse(any('/video/' in url for url in fetched))

    def test_expired_links_are_refreshed_without_redownloading_completed_segments(self):
        old = "#EXTM3U\n#EXTINF:6,\nold/first.ts\n#EXTINF:6,\nold/last.ts\n#EXT-X-ENDLIST\n"
        new = old.replace("old/", "new/")
        fetched = []
        response = requests.Response()
        response.status_code = 403
        def download(url, path, headers, progress, **kwargs):
            fetched.append(url)
            if url.endswith("old/last.ts"):
                raise requests.HTTPError(response=response)
            path.write_bytes(b"complete segment")
        with tempfile.TemporaryDirectory() as directory, \
             patch("hls_download.fetch_playlist", side_effect=[(old,"https://example.com/old.m3u8"),
                                                                 (new,"https://example.com/new.m3u8")]), \
             patch("hls_download.download_file", side_effect=download):
            refresh = Mock(return_value="https://example.com/new.m3u8")
            result = stage_hls("url", directory, {}, lambda *_: None, refresh=refresh, workers=1)
            self.assertTrue(result.is_file())
            self.assertNotIn("https://example.com/new/first.ts", fetched)
            self.assertIn("https://example.com/new/last.ts", fetched)
            refresh.assert_called_once()

    def test_completed_segments_survive_retry_after_later_failure(self):
        playlist = "#EXTM3U\n#EXTINF:6,\nfirst.ts\n#EXTINF:6,\nlast.ts\n#EXT-X-ENDLIST\n"
        fetched = []
        def download(url, path, headers, progress, **kwargs):
            fetched.append(url)
            if url.endswith("last.ts"):
                raise requests.ConnectionError("reset")
            path.write_bytes(b"complete segment")
        with tempfile.TemporaryDirectory() as directory, \
             patch("hls_download.fetch_playlist", return_value=(playlist, "https://example.com/video.m3u8")):
            cache = Path(directory)
            with patch("hls_download.download_file", side_effect=download):
                with self.assertRaises(requests.ConnectionError):
                    stage_hls("url", cache, {}, lambda *_: None)
            self.assertFalse((cache / "complete.m3u8").exists())
            def success(url, path, headers, progress, **kwargs):
                fetched.append(url)
                path.write_bytes(b"complete segment")
            with patch("hls_download.download_file", side_effect=success):
                result = stage_hls("url", cache, {}, lambda *_: None)
            self.assertEqual(fetched.count("https://example.com/first.ts"), 1)
            for name in result.read_text().splitlines():
                if name and not name.startswith("#"):
                    self.assertTrue((cache/name).is_file())

    def test_key_and_initialization_assets_are_local(self):
        playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6,\npart.ts\n#EXT-X-ENDLIST\n'
        with tempfile.TemporaryDirectory() as directory, \
             patch("hls_download.fetch_playlist", return_value=(playlist, "https://example.com/video.m3u8")), \
             patch("hls_download.download_file", side_effect=lambda url,path,headers,progress,**kwargs:path.write_bytes(b"data")):
            result = stage_hls("url", directory, {}, lambda *_: None)
            body = result.read_text()
            self.assertNotIn('URI="key"', body)
            self.assertNotIn('URI="init.mp4"', body)
            self.assertEqual(len(list(Path(directory).glob("*.key"))), 1)
            self.assertEqual(len(list(Path(directory).glob("*.mp4"))), 1)


class ParallelHlsTests(unittest.TestCase):
    def test_parallel_requests_reuse_connections_and_preserve_manifest_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = [f"part{i}.ts" for i in range(12)]
            body = "#EXTM3U\n" + "".join(f"#EXTINF:6,\n{name}\n" for name in names) + "#EXT-X-ENDLIST\n"
            (root / "video.m3u8").write_text(body)
            for name in names:
                (root / name).write_bytes(name.encode())
            lock = threading.Lock()
            state = {"active": 0, "peak": 0, "ports": set()}
            class Handler(http.server.SimpleHTTPRequestHandler):
                protocol_version = "HTTP/1.1"
                def log_message(self, *_):
                    pass
                def do_GET(self):
                    if self.path.endswith('.ts'):
                        with lock:
                            state["active"] += 1
                            state["peak"] = max(state["peak"], state["active"])
                            state["ports"].add(self.client_address[1])
                        try:
                            time.sleep(0.05)
                            super().do_GET()
                        finally:
                            with lock:
                                state["active"] -= 1
                    else:
                        super().do_GET()
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                functools.partial(Handler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            progress = []
            result = stage_hls(f"http://127.0.0.1:{server.server_port}/video.m3u8",
                               root/'cache', {}, lambda pct, _: progress.append(pct))
            files = [line for line in result.read_text().splitlines() if line and not line.startswith('#')]
            self.assertEqual([(result.parent/name).read_bytes() for name in files],
                             [name.encode() for name in names])
            self.assertGreater(state["peak"], 1)
            self.assertLessEqual(state["peak"], 4)
            self.assertLessEqual(len(state["ports"]), 4)
            self.assertEqual(progress, sorted(progress))
            self.assertEqual(progress[-1], 90)

    def test_concurrent_expiry_renews_once_and_deduplicates_key(self):
        old = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="old/key"\n' + ''.join(
            f'#EXTINF:6,\nold/{i}.ts\n#EXT-X-KEY:METHOD=AES-128,URI="old/key"\n' for i in range(3)) + '#EXT-X-ENDLIST\n'
        barrier = threading.Barrier(4)
        response = requests.Response()
        response.status_code = 403
        fetched = []
        def download(url, path, headers, progress, **kwargs):
            fetched.append(url)
            if '/old/' in url:
                barrier.wait(timeout=5)
                raise requests.HTTPError(response=response)
            path.write_bytes(b'complete')
        with tempfile.TemporaryDirectory() as directory, \
             patch('hls_download.fetch_playlist', side_effect=[(old, 'https://example.com/video.m3u8'),
                  (old.replace('old/', 'new/'), 'https://example.com/video.m3u8')]), \
             patch('hls_download.download_file', side_effect=download):
            refresh = Mock(return_value='https://example.com/video.m3u8')
            result = stage_hls('url', directory, {}, lambda *_: None, refresh=refresh)
            refresh.assert_called_once()
            self.assertTrue(result.is_file())
            self.assertEqual(fetched.count('https://example.com/new/key'), 1)

    def test_first_failure_stops_queue_and_never_publishes_manifest(self):
        body = '#EXTM3U\n' + ''.join(f'#EXTINF:6,\n{i}.ts\n' for i in range(100)) + '#EXT-X-ENDLIST\n'
        barrier = threading.Barrier(4)
        fetched = []
        def download(url, path, headers, progress, **kwargs):
            fetched.append(url)
            barrier.wait(timeout=5)
            if url.endswith('/0.ts'):
                raise RuntimeError('CDN unavailable')
            while not kwargs['cancelled']():
                time.sleep(0.01)
            raise RuntimeError('cancelled')
        with tempfile.TemporaryDirectory() as directory, \
             patch('hls_download.fetch_playlist', return_value=(body, 'https://example.com/video.m3u8')), \
             patch('hls_download.download_file', side_effect=download):
            with self.assertRaisesRegex(RuntimeError, 'CDN unavailable'):
                stage_hls('url', directory, {}, lambda *_: None)
            self.assertEqual(len(fetched), 4)
            self.assertFalse((Path(directory)/'complete.m3u8').exists())
