import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from main import DownloadCheckpoint, VideoItem, WorkThread, matching_audio_variant
from resolvers import StreamResult
from resilient_download import DownloadControl, PauseDownload


class AudioOptimizationTests(unittest.TestCase):
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
                              StreamResult("https://cdn.test/media.mp4","direct",{}, {},
                                           audio_only=audio_only)), \
                 patch.object(worker,"download_stream",side_effect=download), \
                 patch("main.PlayerResolver.release"), \
                 patch("main.merge_audio_tracks") as merge:
                worker.run()
            self.assertEqual(peak,2)
            self.assertCountEqual(downloads,[item.dubbing for item in voices])
            merge.assert_called_once()

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
                              StreamResult("https://cdn.test/media.mp4","direct",{}, {},
                                           audio_only=audio_only)), \
                 patch.object(worker,"download_stream",side_effect=download), \
                 patch("main.PlayerResolver.release"), \
                 patch("main.merge_audio_tracks",side_effect=merge):
                worker.run()
            self.assertCountEqual(downloaded,[item.dubbing for item in voices])
            self.assertEqual(saved_video.read_bytes(),b"video")


if __name__ == "__main__":
    unittest.main()
