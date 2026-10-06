import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from requests.structures import CaseInsensitiveDict

from main import SourceHealth, VideoItem
from resilient_download import DownloadControl, PauseDownload
from resilient_download import SourceProbe, probe_media


class Response:
    def __init__(self,url,data,headers=None):
        self.url=url
        self.data=data
        self.headers=CaseInsensitiveDict(headers or {"content-length":str(len(data))})
        self.read=0
        self.closed=False
    def __enter__(self): return self
    def __exit__(self,*_args): self.closed=True
    def raise_for_status(self): pass
    def iter_content(self,size):
        for start in range(0,len(self.data),size):
            chunk=self.data[start:start+size]
            self.read+=len(chunk)
            yield chunk


class SourceProbeTests(unittest.TestCase):
    def test_ignored_range_is_closed_after_bounded_sample(self):
        response=Response("https://cdn.test/file.mp4",b"x"*(2*1024*1024))
        control=DownloadControl()
        with patch("resilient_download.requests.Session") as session:
            session.return_value.get.return_value=response
            result=probe_media(response.url,{},control)
            self.assertEqual(session.return_value.get.call_args.kwargs["headers"]["Range"],"bytes=0-262143")
        self.assertTrue(response.closed)
        self.assertEqual(response.read,256*1024)
        self.assertEqual(result.expected_bytes,2*1024*1024)
        self.assertEqual(control.transfer_stats()[2],256*1024)

    def test_hls_estimates_full_transfer_from_segment_range(self):
        manifest=b"#EXTM3U\n#EXTINF:6,\n0.ts\n#EXTINF:6,\n1.ts\n#EXT-X-ENDLIST\n"
        responses=[Response("https://cdn.test/audio.m3u8",manifest),
                   Response("https://cdn.test/0.ts",b"x"*262144,
                            {"content-range":"bytes 0-262143/1048576"})]
        with patch("resilient_download.requests.Session") as session:
            session.return_value.get.side_effect=responses
            result=probe_media(responses[0].url,{})
        self.assertEqual(result.expected_bytes,2*1048576)
        self.assertEqual(result.sampled_bytes,262144+len(manifest))
        self.assertTrue(all(response.closed for response in responses))

    def test_pause_does_not_start_a_request(self):
        control=DownloadControl(); control.request("paused")
        with patch("resilient_download.requests.Session") as session:
            with self.assertRaises(PauseDownload):
                probe_media("https://cdn.test/file.mp4",{},control)
            session.return_value.get.assert_not_called()

    def test_competing_voices_share_samples_and_choose_measured_source(self):
        health=SourceHealth()
        self.addCleanup(health.close)
        cvh=VideoItem(1,"CVH","Voice","1",1,"https://frame.test/cvh")
        kodik=VideoItem(2,"Kodik","Voice","1",1,"https://frame.test/kodik")
        barrier=threading.Barrier(2)
        calls=[]
        lock=threading.Lock()
        def probe(item):
            with lock: calls.append(item.video_id)
            barrier.wait(timeout=3)
            speed=10000 if item==cvh else 1000000
            return SourceProbe(0.1,speed,1000000,262144,"cdn.test")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:health.prepare([cvh,kodik],probe),range(2)))
        self.assertCountEqual(calls,[1,2])
        self.assertTrue(all(result[0]==kodik for result in results))
        health.prepare([cvh,kodik],probe)
        self.assertEqual(len(calls),2)
        health.succeeded(cvh,0.01,1000)
        self.assertEqual(health.sort([cvh,kodik])[0],cvh)

    def test_probe_failure_does_not_prevent_fallback(self):
        health=SourceHealth(); self.addCleanup(health.close)
        cvh=VideoItem(1,"CVH","Voice","1",1,"")
        kodik=VideoItem(2,"Kodik","Voice","1",1,"")
        def probe(item):
            if item==cvh: raise TimeoutError("sample timeout")
            return SourceProbe(0.1,100000,1000000,262144,"cdn.test")
        self.assertEqual(health.prepare([cvh,kodik],probe),[kodik,cvh])


if __name__=="__main__": unittest.main()
