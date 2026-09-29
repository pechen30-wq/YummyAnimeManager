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
        def download(url, path, headers, progress):
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
        def download(url, path, headers, progress):
            fetched.append(url)
            if url.endswith("old/last.ts"):
                raise requests.HTTPError(response=response)
            path.write_bytes(b"complete segment")
        with tempfile.TemporaryDirectory() as directory, \
             patch("hls_download.fetch_playlist", side_effect=[(old,"https://example.com/old.m3u8"),
                                                                 (new,"https://example.com/new.m3u8")]), \
             patch("hls_download.download_file", side_effect=download):
            refresh = Mock(return_value="https://example.com/new.m3u8")
            result = stage_hls("url", directory, {}, lambda *_: None, refresh=refresh)
            self.assertTrue(result.is_file())
            self.assertNotIn("https://example.com/new/first.ts", fetched)
            self.assertIn("https://example.com/new/last.ts", fetched)
            refresh.assert_called_once()

    def test_completed_segments_survive_retry_after_later_failure(self):
        playlist = "#EXTM3U\n#EXTINF:6,\nfirst.ts\n#EXTINF:6,\nlast.ts\n#EXT-X-ENDLIST\n"
        fetched = []
        def download(url, path, headers, progress):
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
            def success(url, path, headers, progress):
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
             patch("hls_download.download_file", side_effect=lambda url,path,headers,progress:path.write_bytes(b"data")):
            result = stage_hls("url", directory, {}, lambda *_: None)
            body = result.read_text()
            self.assertNotIn('URI="key"', body)
            self.assertNotIn('URI="init.mp4"', body)
            self.assertEqual(len(list(Path(directory).glob("*.key"))), 1)
            self.assertEqual(len(list(Path(directory).glob("*.mp4"))), 1)
