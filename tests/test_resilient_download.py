import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from resilient_download import download_file


class Response:
    def __init__(self, status, headers, chunks):
        self.status_code = status
        self.headers = headers
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "episode.mp4"
        self.progress = []

    def download(self, responses, attempts=4):
        with patch("resilient_download.requests.get", side_effect=responses) as get:
            download_file("https://example.test/video", self.path,
                          {"User-Agent": "test", "Referer": "https://example.test/"},
                          lambda *args: self.progress.append(args),
                          attempts=attempts, sleep=lambda _: None)
            return get

    def test_resumes_after_reset_with_range(self):
        get = self.download([
            Response(200, {"Content-Length": "6"}, [b"abc", ConnectionResetError("reset")]),
            Response(206, {"Content-Range": "bytes 3-5/6"}, [b"def"]),
        ])
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["Range"], "bytes=3-")
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["Referer"], "https://example.test/")

    def test_server_ignoring_range_restarts_file(self):
        self.download([
            Response(200, {"Content-Length": "6"}, [b"abc", requests.exceptions.ReadTimeout()]),
            Response(200, {"Content-Length": "6"}, [b"abcdef"]),
        ])
        self.assertEqual(self.path.read_bytes(), b"abcdef")

    def test_bounded_retries_report_last_error(self):
        with self.assertRaisesRegex(RuntimeError, "после 3 попыток.*reset"):
            self.download([requests.exceptions.ConnectionError("reset")] * 3, attempts=3)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.progress), 2)

    def test_wrong_range_does_not_append(self):
        with self.assertRaisesRegex(RuntimeError, "Content-Range"):
            self.download([
                Response(200, {"Content-Length": "6"}, [b"abc", ConnectionResetError()]),
                Response(206, {"Content-Range": "bytes 2-5/6"}, [b"cdef"]),
            ])
        self.assertFalse(self.path.exists())

    def test_rejected_range_restarts_without_range(self):
        get = self.download([
            Response(200, {"Content-Length": "6"}, [b"abc", ConnectionResetError()]),
            Response(416, {}, []),
            Response(200, {"Content-Length": "6"}, [b"abcdef"]),
        ])
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        self.assertNotIn("Range", get.call_args_list[2].kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
