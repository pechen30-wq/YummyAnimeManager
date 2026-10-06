import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from main import (AdaptiveAudioPolicy, DownloadCheckpoint, SourceHealth,
                  VideoItem, WorkThread, matching_audio_variant)
from resolvers import StreamResult
from resilient_download import DownloadControl, PauseDownload


class AudioOptimizationTests(unittest.TestCase):
    def test_source_ranking_learns_failures_and_adaptive_limit(self):
        health=SourceHealth()
        fast=VideoItem(1,"CVH","A","1",1,"")
        slow=VideoItem(2,"Kodik","A","1",1,"")
        self.assertEqual(health.sort([slow,fast])[0],fast)
        health.failed(fast,RuntimeError("TLS"))
        health.failed(fast,RuntimeError("TLS"))
        self.assertEqual(health.sort([fast,slow])[0],slow)
        policy=AdaptiveAudioPolicy()
        for now in (0,20,40,60): policy.observe_network(now,now*100,2)
        self.assertEqual(policy.limit,3)
        for now in (60,80,100,120): policy.observe_network(now,6000+(now-60)*100,3)
        self.assertEqual(policy.limit,2)
        cautious=AdaptiveAudioPolicy()
        cautious.failed()
        for now in (0,20,40,60): cautious.observe_network(now,now*100,2)
        self.assertEqual(cautious.limit,2)
        self.assertEqual(cautious.phase,"stable")

    def test_adaptive_limit_uses_sustained_aggregate_network_throughput(self):
        policy=AdaptiveAudioPolicy()
        for now in (0,20,40,60): policy.observe_network(now,now*100,2)
        policy.observe_network(80,20000,2)  # Third track has not started yet.
        self.assertEqual(policy.phase,"trial")
        for now in (80,100,120,140): policy.observe_network(now,20000+(now-80)*140,3)
        self.assertEqual(policy.limit,3)
        self.assertEqual(policy.phase,"stable")
        interrupted=AdaptiveAudioPolicy()
        interrupted.observe_network(0,0,2)
        interrupted.observe_network(19,1900,2)
        interrupted.observe_network(20,2000,1)
        interrupted.observe_network(40,4000,2)
        interrupted.observe_network(59,5900,2)
        self.assertEqual(interrupted.samples,[])

    def test_adaptive_limit_does_not_compare_different_sources(self):
        policy=AdaptiveAudioPolicy()
        for now in (0,20,40,60): policy.observe_network(now,now*1000,2,"cvh@cdn")
        self.assertEqual(policy.limit,3)
        policy.observe_network(61,61000,3,"kodik@cdn")
        self.assertEqual(policy.limit,2)
        self.assertEqual(policy.phase,"baseline")
        for now in (62,82,102,122): policy.observe_network(now,61000+(now-61)*100,2,"kodik@cdn")
        self.assertEqual(policy.limit,3)
        for now in (123,143,163,183): policy.observe_network(now,67200+(now-123)*140,3,"kodik@cdn")
        self.assertEqual(policy.limit,3)
        self.assertEqual(policy.phase,"stable")

    def test_fast_kodik_audio_keeps_video_quality_and_skips_comparison(self):
        qualities={"720p":"https://cdn.test/high.m3u8?token=example",
                   "360p":"https://cdn.test/low.m3u8?token=example"}
        item=VideoItem(1,"Kodik","A","1",1,"")
        worker=WorkThread("Kodik",["A"],{},"720p",Path("."),"Test",False,"",False,
                          resolver_config={"fast_kodik_audio":True})
        def resolved(_item):
            return StreamResult(qualities["720p"],"kodik",qualities,{},"720p")
        with patch.object(worker.resolver,"resolve",side_effect=resolved), \
             patch("main.matching_audio_variant") as compare:
            video=worker.resolve_stream(item)
            audio=worker.resolve_stream(item,audio_only=True)
        self.assertEqual(video.url,qualities["720p"])
        self.assertEqual(audio.url,qualities["360p"])
        self.assertTrue(audio.audio_only)
        compare.assert_not_called()
        worker.resolver.session.close()

    def test_strict_kodik_audio_retains_original_when_variants_differ(self):
        qualities={"360p":"https://cdn.test/low.m3u8",
                   "720p":"https://cdn.test/high.m3u8"}
        item=VideoItem(1,"Kodik","A","1",1,"")
        worker=WorkThread("Kodik",["A"],{},"720p",Path("."),"Test",False,"",False)
        with patch.object(worker.resolver,"resolve",return_value=StreamResult(
                qualities["720p"],"kodik",qualities,{},"720p")), \
             patch("main.matching_audio_variant",return_value=None) as compare:
            audio=worker.resolve_stream(item,audio_only=True)
        self.assertEqual(audio.url,qualities["720p"])
        compare.assert_called_once()
        worker.resolver.session.close()

    def test_identical_audio_resource_has_one_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            item=VideoItem(1,"CVH","A","1",1,"")
            worker=WorkThread("CVH",["A"],{},"720p",root,"Test",False,"",False)
            stream=StreamResult("https://cdn.test/same.mp4","direct",{}, {})
            writes=[]
            def download(_stream,path,_item,_progress):
                writes.append(path)
                time.sleep(0.1)
                path.write_bytes(b"shared audio")
            with patch.object(worker,"download_stream",side_effect=download):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(lambda name: worker.download_audio_shared(
                        stream,root/name,item,lambda *_:None),["a.mka","b.mka"]))
            self.assertEqual(len(writes),1)
            self.assertEqual(results,[writes[0],writes[0]])

    def test_direct_audio_reuses_identical_video_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            video=root/"video.mp4"; video.write_bytes(b"video with audio")
            worker=WorkThread("CVH",["A"],{},"720p",root,"Test",False,"",False)
            url="https://cdn.test/same.mp4"
            worker._video_resource=(url,(),video,1)
            stream=StreamResult(url,"direct",{}, {})
            item=VideoItem(1,"CVH","A","1",1,"")
            with patch.object(worker,"download_stream") as download:
                result=worker.download_audio_shared(stream,root/"audio.mka",item,lambda *_:None)
            self.assertEqual(result,video)
            download.assert_not_called()

    def test_smaller_variant_requires_identical_audio_and_timing(self):
        qualities={"360p":"https://cdn.test/low.m3u8",
                   "720p":"https://cdn.test/high.m3u8"}
        playlist="#EXTM3U\n#EXTINF:10,\n0.ts\n#EXTINF:10,\n1.ts\n#EXTINF:10,\n2.ts\n"

        def get(url, **_kwargs):
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def raise_for_status(self): pass
                def iter_content(self, _size): yield url.encode()
            return Response()

        with patch("main.media_playlist", side_effect=lambda url, _headers: (playlist,url.replace(".m3u8","/"))), \
             patch("main.requests.get", side_effect=get), \
             patch("main.subprocess.run", return_value=SimpleNamespace(returncode=0,stdout=b"SHA256=same")):
            self.assertEqual(matching_audio_variant(qualities,{},"ffmpeg"),
                             ("360p",qualities["360p"]))

        with patch("main.media_playlist", side_effect=lambda url, _headers:
                   (playlist if "low" in url else playlist.replace("10,","9,",1),url)), \
             patch("main.requests.get", side_effect=get) as fetch:
            self.assertIsNone(matching_audio_variant(qualities,{},"ffmpeg"))
            fetch.assert_not_called()

        with patch("main.media_playlist", side_effect=lambda url, _headers: (playlist,url.replace(".m3u8","/"))), \
             patch("main.requests.get", side_effect=get), \
             patch("main.subprocess.run", side_effect=lambda _args, **kw:
                   SimpleNamespace(returncode=0,stdout=kw["input"])):
            self.assertIsNone(matching_audio_variant(qualities,{},"ffmpeg"))

        with patch("main.media_playlist", side_effect=lambda url, _headers: (playlist,url.replace(".m3u8","/"))), \
             patch("main.requests.get", side_effect=get), \
             patch("main.subprocess.run", side_effect=lambda _args, **kw:
                   SimpleNamespace(returncode=0,stdout=(
                       b"different" if b"/high/1.ts" in kw["input"] else b"same"))):
            self.assertIsNone(matching_audio_variant(qualities,{},"ffmpeg"))

        control=DownloadControl()
        control.request("paused")
        with self.assertRaises(PauseDownload):
            matching_audio_variant(qualities,{},"ffmpeg",control)

    def test_two_tracks_prefetch_and_reuse_without_checkpoint(self):
        video=VideoItem(1,"CVH","Video","1",1,"")
        voices=[VideoItem(index+2,"CVH",f"Voice {index}","1",1,"") for index in range(3)]
        matrix={1.0:{"__video__":video,"Video":video,
                     **{item.dubbing:item for item in voices}}}
        barrier=threading.Barrier(2)
        lock=threading.Lock()
        active=0; peak=0; downloads=[]

        def download(stream,path,item,_progress):
            nonlocal active,peak
            if item != video:
                with lock:
                    active+=1; peak=max(peak,active); downloads.append(item.dubbing)
                if item in voices[:2]: barrier.wait(timeout=3)
                with lock: active-=1
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b"media")

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); mkv=root/"mkvmerge.exe"; mkv.touch()
            worker=WorkThread("CVH",[item.dubbing for item in voices]+["Video"],
                              matrix,"720p",root,"Test",True,str(mkv),False,
                              plexmatch_enabled=False,chapters_enabled=False)
            with patch.object(worker,"resolve_stream",side_effect=lambda item,audio_only=False:
                              StreamResult(f"https://cdn.test/{item.video_id}.mp4","direct",{}, {},
                                           audio_only=audio_only)), \
                 patch.object(worker,"download_stream",side_effect=download), \
                 patch("main.PlayerResolver.release"), \
                 patch("main.merge_audio_tracks") as merge:
                worker.run()
            self.assertEqual(peak,2)
            self.assertCountEqual(downloads,[item.dubbing for item in voices])
            merge.assert_called_once()

    def test_finished_track_frees_slot_while_first_track_is_still_pending(self):
        video=VideoItem(1,"CVH","Video","1",1,"")
        voices=[VideoItem(index+2,"CVH",f"Voice {index}","1",1,"") for index in range(3)]
        matrix={1.0:{"__video__":video,"Video":video,
                     **{item.dubbing:item for item in voices}}}
        next_started=threading.Event()
        def download(_stream,path,item,_progress):
            if item==voices[0]:
                if not next_started.wait(timeout=3):
                    raise RuntimeError("Finished second track did not free a slot")
            elif item==voices[2]:
                next_started.set()
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b"media")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); mkv=root/"mkvmerge.exe"; mkv.touch()
            worker=WorkThread("CVH",[item.dubbing for item in voices]+["Video"],
                              matrix,"720p",root,"Test",True,str(mkv),False,
                              plexmatch_enabled=False,chapters_enabled=False)
            with patch.object(worker,"resolve_stream",side_effect=lambda item,audio_only=False:
                              StreamResult(f"https://cdn.test/{item.video_id}.mp4","direct",{}, {},
                                           audio_only=audio_only)), \
                 patch.object(worker,"download_stream",side_effect=download), \
                 patch("main.PlayerResolver.release"), \
                 patch("main.merge_audio_tracks") as merge:
                worker.run()
            self.assertTrue(next_started.is_set())
            merge.assert_called_once()
            self.assertEqual([name for _path,name in merge.call_args.args[1]],
                             [item.dubbing for item in voices]+["Video"])

    def test_cached_video_still_prefetches_remaining_tracks(self):
        video=VideoItem(1,"CVH","Video","1",1,"")
        voices=[VideoItem(index+2,"CVH",f"Voice {index}","1",1,"") for index in range(2)]
        matrix={1.0:{"__video__":video,"Video":video,
                     **{item.dubbing:item for item in voices}}}
        barrier=threading.Barrier(2)
        downloaded=[]

        def download(_stream,path,item,_progress):
            downloaded.append(item.dubbing)
            barrier.wait(timeout=3)
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b"audio")

        def merge(_m,_s,out,**_kw):
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_bytes(b"merged")

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); mkv=root/"mkvmerge.exe"; mkv.touch()
            saved_video=root/"video.mp4"; saved_video.write_bytes(b"video")
            checkpoint=DownloadCheckpoint({"schema":1,"settings":{},"files":{},"episodes":{}},
                                          root/"pending.json")
            checkpoint.mark_file("1.0:__video__",saved_video,video)
            worker=WorkThread("CVH",[item.dubbing for item in voices]+["Video"],
                              matrix,"720p",root,"Test",True,str(mkv),False,
                              plexmatch_enabled=False,chapters_enabled=False,
                              checkpoint=checkpoint)
            with patch.object(worker,"resolve_stream",side_effect=lambda item,audio_only=False:
                              StreamResult(f"https://cdn.test/{item.video_id}.mp4","direct",{}, {},
                                           audio_only=audio_only)), \
                 patch.object(worker,"download_stream",side_effect=download), \
                 patch("main.PlayerResolver.release"), \
                 patch("main.merge_audio_tracks",side_effect=merge):
                worker.run()
            self.assertCountEqual(downloaded,[item.dubbing for item in voices])
            self.assertEqual(saved_video.read_bytes(),b"video")

    def test_thirty_two_tracks_are_merged_with_bounded_parallelism(self):
        voices=[VideoItem(index+1,"CVH",f"Voice {index}","1",1,"") for index in range(32)]
        matrix={1.0:{"__video__":voices[0],
                     **{item.dubbing:item for item in voices}}}
        lock=threading.Lock()
        active=0; peak=0

        def download(_stream,path,item,_progress):
            nonlocal active,peak
            if item!=voices[0]:
                with lock:
                    active+=1; peak=max(peak,active)
                time.sleep(0.02)
                with lock: active-=1
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b"audio" if item!=voices[0] else b"video")

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); mkv=root/"mkvmerge.exe"; mkv.touch()
            worker=WorkThread("CVH",[item.dubbing for item in voices],matrix,
                              "720p",root,"Test",True,str(mkv),False,
                              plexmatch_enabled=False,chapters_enabled=False)
            with patch.object(worker,"resolve_stream",side_effect=lambda item,audio_only=False:
                              StreamResult(f"https://cdn.test/{item.video_id}.mp4","direct",{}, {},
                                           audio_only=audio_only)), \
                 patch.object(worker,"download_stream",side_effect=download), \
                 patch("main.PlayerResolver.release"), \
                 patch("main.merge_audio_tracks") as merge:
                worker.run()
            self.assertEqual(len(merge.call_args.args[1]),32)
            self.assertGreaterEqual(peak,2)
            self.assertLessEqual(peak,3)


if __name__ == "__main__":
    unittest.main()
