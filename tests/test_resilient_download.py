import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from resilient_download import download_file, download_ranges


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

    def close(self):
        pass

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

    def test_session_reconnect_preserves_validated_range_resume(self):
        session = Mock()
        session.get.side_effect = [
            Response(200, {"Content-Length": "6"}, [b"abc", ConnectionResetError("reset")]),
            Response(206, {"Content-Range": "bytes 3-5/6"}, [b"def"]),
        ]
        download_file("https://example.test/video", self.path, {}, lambda *_: None,
                      session=session, sleep=lambda _: None)
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        session.close.assert_called_once()
        self.assertEqual(session.get.call_args.kwargs["headers"]["Range"], "bytes=3-")

    def test_cancelled_download_never_publishes_partial_file(self):
        cancelled = Mock(side_effect=[False, False, True])
        session = Mock()
        session.get.return_value = Response(200, {"Content-Length": "6"}, [b"abc", b"def"])
        with self.assertRaisesRegex(RuntimeError, "отменена"):
            download_file("https://example.test/video", self.path, {}, lambda *_: None,
                          session=session, cancelled=cancelled)
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


class RangeDownloadTests(unittest.TestCase):
    def run_download(self, responses):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = responses
        with tempfile.TemporaryDirectory() as directory, patch('resilient_download.requests.Session',return_value=session):
            path=Path(directory)/'video.mp4'
            download_ranges('https://example.test/video',path,{},lambda *_:None,
                            block_size=3,attempts=2,sleep=lambda _:None)
            self.assertEqual(path.read_bytes(),b'abcdef')
        return session

    def test_partial_block_is_retried_without_appending_partial_bytes(self):
        session=self.run_download([
            Response(206,{'Content-Range':'bytes 0-2/6','ETag':'"v1"'},[b'ab',ConnectionResetError()]),
            Response(206,{'Content-Range':'bytes 0-2/6','ETag':'"v1"'},[b'abc']),
            Response(206,{'Content-Range':'bytes 3-5/6','ETag':'"v1"'},[b'def']),
        ])
        self.assertEqual([call.kwargs['headers']['Range'] for call in session.get.call_args_list],
                         ['bytes=0-2','bytes=0-2','bytes=3-5'])
        self.assertEqual(session.get.call_args.kwargs['headers']['If-Range'],'"v1"')

    def test_changed_size_or_etag_or_wrong_offset_never_publishes(self):
        for headers in ({'Content-Range':'bytes 3-5/7'},
                        {'Content-Range':'bytes 3-5/6','ETag':'"v2"'},
                        {'Content-Range':'bytes 2-4/6'}):
            with self.subTest(headers=headers), self.assertRaises(RuntimeError):
                self.run_download([Response(206,{'Content-Range':'bytes 0-2/6','ETag':'"v1"'},[b'abc']),
                                   Response(206,headers,[b'def'])])

    def test_ignored_range_uses_full_download_at_start(self):
        self.run_download([Response(200,{'Content-Length':'6'},[]),
                           Response(200,{'Content-Length':'6'},[b'abcdef'])])

    def test_retries_are_bounded_for_missing_block(self):
        with self.assertRaisesRegex(RuntimeError,'после 2 попыток'):
            self.run_download([requests.ConnectionError('reset')]*2)
