import unittest
from unittest.mock import MagicMock, patch

from main import estimate_manifest_duration
from resolvers import StreamResult


class ManifestDurationTests(unittest.TestCase):
    def test_direct_file_never_downloaded_for_duration(self):
        with patch('main.requests.get') as get:
            self.assertEqual(estimate_manifest_duration(
                StreamResult('https://example.com/video.mp4', 'sibnet', {}, {})), 0)
            get.assert_not_called()

    def test_oversized_manifest_response_is_closed_without_buffering_media(self):
        response = MagicMock()
        response.__enter__.return_value = response
        chunks = [b'x' * 65536] * 100
        response.iter_content.return_value = iter(chunks)
        with patch('main.requests.get', return_value=response) as get:
            self.assertEqual(estimate_manifest_duration(
                StreamResult('https://example.com/video.m3u8', 'cvh', {}, {})), 0)
        self.assertTrue(get.call_args.kwargs['stream'])
        response.__exit__.assert_called_once()
        self.assertGreater(len(list(response.iter_content.return_value)), 0)

    def test_finite_playlist_duration_remains_available(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = iter([b'#EXTM3U\n#EXTINF:6,\n1.ts\n#EXTINF:5.5,\n2.ts\n#EXT-X-ENDLIST\n'])
        with patch('main.requests.get', return_value=response):
            self.assertEqual(estimate_manifest_duration(
                StreamResult('https://example.com/video.m3u8', 'cvh', {}, {})), 11.5)
