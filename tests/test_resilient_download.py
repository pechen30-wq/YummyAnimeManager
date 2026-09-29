import http.server
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from resilient_download import download_file, download_ranges, DownloadControl, PauseDownload


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

    def test_pause_keeps_partial_file_for_next_process(self):
        control=DownloadControl()
        def pause_at_first_chunk(*_):
            control.request("paused")
        with patch("resilient_download.requests.get", return_value=Response(
                200,{"Content-Length":"6"},[b"abc",b"def"])):
            with self.assertRaises(PauseDownload):
                download_file("https://example.test/video",self.path,{},pause_at_first_chunk,
                              control=control,resume=True)
        self.assertEqual(self.path.with_name(self.path.name+".part").read_bytes(),b"abc")
        self.assertFalse(self.path.exists())
        with patch("resilient_download.requests.get",return_value=Response(
                206,{"Content-Range":"bytes 3-5/6"},[b"def"])) as get:
            download_file("https://example.test/video",self.path,{},lambda *_:None,
                          control=DownloadControl(),resume=True)
        self.assertEqual(get.call_args.kwargs["headers"]["Range"],"bytes=3-")
        self.assertEqual(self.path.read_bytes(),b"abcdef")


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
                            block_size=3,attempts=2,sleep=lambda _:None,workers=1)
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


class ParallelRangeTests(unittest.TestCase):
    def test_pause_reuses_verified_blocks_after_restart(self):
        block_size=65536
        data=b''.join(bytes([index])*block_size for index in range(12))
        requested=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_): pass
            def do_GET(self):
                start,end=map(int,self.headers['Range'].removeprefix('bytes=').split('-'))
                end=min(end,len(data)-1)
                requested.append(start)
                self.send_response(206)
                self.send_header('Content-Range',f'bytes {start}-{end}/{len(data)}')
                self.send_header('Content-Length',str(end-start+1))
                self.send_header('ETag','"v1"')
                self.end_headers()
                try: self.wfile.write(data[start:end+1]);self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError): pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'video.mp4'
                control=DownloadControl()
                def pause(pct,_):
                    if pct>=40: control.request('paused')
                url=f'http://127.0.0.1:{server.server_port}/video.mp4'
                with self.assertRaises(PauseDownload):
                    download_ranges(url,path,{},pause,block_size=block_size,
                                    control=control,resume=True)
                self.assertFalse(path.exists())
                ledger=path.with_name('video.mp4.range.part.json')
                self.assertTrue(ledger.is_file())
                before=len(requested)
                download_ranges(url+'?renewed=1',path,{},lambda *_:None,
                                block_size=block_size,control=DownloadControl(),resume=True)
                self.assertEqual(path.read_bytes(),data)
                self.assertLess(len(requested)-before,12)
                self.assertFalse(ledger.exists())
        finally:
            server.shutdown();server.server_close()

    def check_server(self, mode):
        data=b''.join(bytes([index])*65536 for index in range(24))
        state={'active':0,'peak':0,'ports':set(),'retried':0}
        lock=threading.Lock()
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def handle(self):
                try:super().handle()
                except (ConnectionResetError,ConnectionAbortedError):pass
            def do_GET(self):
                start,end=map(int,self.headers['Range'].removeprefix('bytes=').split('-'))
                end=min(end,len(data)-1)
                with lock:
                    state['active']+=1;state['peak']=max(state['peak'],state['active'])
                    state['ports'].add(self.client_address[1])
                    fail=mode=='reset' and start==5*65536 and state['retried']==0
                    if fail:state['retried']+=1
                try:
                    if start>0:time.sleep(0.08 if start==65536 else 0.01)
                    self.send_response(206)
                    left=start-1 if mode=='bad_range' and start>0 else start
                    size=len(data)+1 if mode=='changed_size' and start>0 else len(data)
                    self.send_header('Content-Range',f'bytes {left}-{end}/{size}')
                    self.send_header('Content-Length',str(end-start+1))
                    self.send_header('ETag','"changed"' if mode=='changed' and start>0 else '"stable"')
                    self.end_headers()
                    if fail:
                        self.wfile.write(data[start:start+(end-start+1)//2]);self.wfile.flush()
                        self.close_connection=True
                        return
                    self.wfile.write(data[start:end+1]);self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError,OSError):pass
                finally:
                    with lock:state['active']-=1
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'video.mp4';progress=[]
                if mode in ('bad_range','changed','changed_size'):
                    with self.assertRaises(RuntimeError):
                        download_ranges(f'http://127.0.0.1:{server.server_port}/video.mp4',path,{},
                                        lambda pct,_:progress.append(pct),block_size=65536,sleep=lambda _:None)
                    self.assertFalse(path.exists());self.assertNotIn(100,progress)
                else:
                    download_ranges(f'http://127.0.0.1:{server.server_port}/video.mp4',path,{},
                                    lambda pct,_:progress.append(pct),block_size=65536,sleep=lambda _:None)
                    self.assertEqual(path.read_bytes(),data)
                    self.assertEqual(progress,sorted(progress));self.assertEqual(progress[-1],100)
                    self.assertGreater(state['peak'],1);self.assertLessEqual(state['peak'],4)
                    self.assertLessEqual(len(state['ports']),6)
                    if mode=='reset':self.assertEqual(state['retried'],1)
        finally:
            server.shutdown();server.server_close()

    def test_out_of_order_ranges_are_written_at_correct_offsets(self):self.check_server('normal')
    def test_interrupted_parallel_block_is_retried_without_corruption(self):self.check_server('reset')
    def test_wrong_offset_never_publishes(self):self.check_server('bad_range')
    def test_changed_etag_never_publishes(self):self.check_server('changed')
    def test_changed_size_never_publishes(self):self.check_server('changed_size')

    def test_range_unsupported_does_not_start_full_download_when_required(self):
        from resilient_download import RangeUnsupportedError
        session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        session.get.return_value=Response(200,{'Content-Length':'100'},[])
        with tempfile.TemporaryDirectory() as directory,patch('resilient_download.requests.Session',return_value=session):
            with self.assertRaises(RangeUnsupportedError):
                download_ranges('https://example.test/video',Path(directory)/'video.mp4',{},
                                lambda *_:None,require_ranges=True)
            self.assertEqual(session.get.call_count,1)
