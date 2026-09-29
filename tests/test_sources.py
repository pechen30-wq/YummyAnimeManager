import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from main import MainWindow, merge_audio_tracks
import alloha_runtime


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_video_player_does_not_limit_audio_or_reset_selection(self):
        with patch("main.load_config", return_value={"public_token": "test"}), \
             patch.object(MainWindow, "schedule_quality_probe"):
            window = MainWindow()
            self.addCleanup(window.close)
            raw = [
                {"data": {"player": player, "dubbing": dub}, "number": str(ep),
                 "index": ep, "iframe_url": "", "video_id": index}
                for index, (player, dub, ep) in enumerate([
                    ("CVH", "Video voice", 1), ("CVH", "Video voice", 2),
                    ("Kodik", "Audio voice", 1), ("Sibnet", "Rare voice", 1)])]
            window.on_loaded({"title": "Test"}, raw)
            self.assertEqual(window.dub_list.count(), 3)
            for i in range(window.dub_list.count()):
                window.dub_list.item(i).setCheckState(Qt.Checked)
            matrix = window.episode_matrix()
            self.assertEqual(matrix[1.0]["__video__"].player, "CVH")
            self.assertEqual(matrix[1.0]["Audio voice"].player, "Kodik")
            self.assertEqual(matrix[1.0]["Rare voice"].player, "Sibnet")
            self.assertEqual(window.ep_list.count(), 1)
            self.assertTrue(window.merge_check.isChecked())
            window.player_combo.setCurrentText("Kodik")
            self.assertEqual(len(window.selected_dubbings()), 3)
            self.assertEqual(window.episode_matrix()[1.0]["__video__"].player, "Kodik")

    def test_video_reuses_later_selected_voice_and_labels_keep_voice_identity(self):
        with patch('main.load_config',return_value={'public_token':'test'}), \
             patch.object(MainWindow,'schedule_quality_probe'):
            window=MainWindow(); self.addCleanup(window.close)
            raw=[{'data':{'player':player,'dubbing':dub},'number':str(ep),'index':ep,
                  'iframe_url':'','video_id':index} for index,(player,dub,ep) in enumerate([
                      ('CVH','Unused voice',1),('CVH','B selected voice',1),
                      ('Sibnet','A selected voice',1),('Kodik','B selected voice',1),
                      ('Sibnet','A selected voice',2)])]
            window.on_loaded({'title':'Test'},raw)
            for index in range(window.dub_list.count()):
                row=window.dub_list.item(index)
                row.setCheckState(Qt.Checked if row.data(Qt.UserRole) in
                                  ('A selected voice','B selected voice') else Qt.Unchecked)
            self.assertEqual(window.selected_dubbings(),['A selected voice','B selected voice'])
            matrix=window.episode_matrix()
            self.assertEqual(matrix[1.0]['__video__'].dubbing,'B selected voice')
            self.assertEqual(matrix[1.0]['B selected voice'],matrix[1.0]['__video__'])
            self.assertNotIn(2.0,matrix)
            b=next(window.dub_list.item(i) for i in range(window.dub_list.count())
                   if window.dub_list.item(i).data(Qt.UserRole)=='B selected voice')
            self.assertIn('CVH',b.text());self.assertIn('Kodik',b.text())
            window.player_combo.setCurrentText('Kodik')
            self.assertEqual(window.selected_dubbings(),['A selected voice','B selected voice'])

    def test_matching_video_audio_is_not_resolved_or_downloaded_again(self):
        from main import WorkThread,VideoItem
        from resolvers import StreamResult
        video=VideoItem(1,'CVH','B voice','1',1,'')
        audio=VideoItem(2,'Sibnet','A voice','1',1,'')
        seen=[]
        def resolve(item,audio_only=False):
            seen.append((item.dubbing,audio_only))
            return StreamResult('https://example.com/media.mp4','direct',{}, {},audio_only=audio_only)
        def download(stream,path,item,progress):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'media')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);mkv=root/'mkvmerge.exe';mkv.touch()
            worker=WorkThread('CVH',['A voice','B voice'],
                {1.0:{'__video__':video,'B voice':video,'A voice':audio}},'1080p',root,
                'Test',True,str(mkv),False,plexmatch_enabled=False,chapters_enabled=False)
            results=[];worker.done.connect(lambda summary,errors:results.append(errors))
            with patch.object(worker,'resolve_stream',side_effect=resolve), \
                 patch.object(worker,'download_stream',side_effect=download), \
                 patch('main.PlayerResolver.release'),patch('main.merge_audio_tracks') as merge:
                worker.run()
                inputs=merge.call_args.args[1]
                self.assertEqual(inputs[1][0],merge.call_args.kwargs['video_source'])
            self.assertEqual(seen,[('B voice',False),('A voice',True)])
            self.assertEqual(results,[[]])

    def test_direct_cvh_video_uses_verified_ranges(self):
        from main import WorkThread,VideoItem
        from resolvers import StreamResult
        worker=WorkThread('CVH',['Voice'],{},'1080p','.', 'Test',False,'',False)
        item=VideoItem(1,'CVH','Voice','1',1,'')
        stream=StreamResult('https://example.com/video.mp4','cvh',{}, {'Referer':'https://example.com/'})
        with tempfile.TemporaryDirectory() as directory, \
             patch('main.download_ranges') as ranged,patch('main.download_file') as regular:
            worker.download_stream(stream,Path(directory)/'video.mp4',item,lambda *_:None)
            ranged.assert_called_once();regular.assert_not_called()
            self.assertEqual(ranged.call_args.args[2]['Referer'],'https://example.com/')

    def test_direct_video_network_failure_retries_sequentially(self):
        from main import WorkThread,VideoItem
        from resolvers import StreamResult
        from resilient_download import RangeDownloadError
        worker=WorkThread('CVH',['Voice'],{},'1080p','.', 'Test',False,'',False)
        item=VideoItem(1,'CVH','Voice','1',1,'')
        stream=StreamResult('https://example.com/video.mp4','cvh',{}, {})
        with tempfile.TemporaryDirectory() as directory, \
             patch('main.download_ranges',side_effect=[RangeDownloadError('timeout'),None]) as ranged:
            worker.download_stream(stream,Path(directory)/'video.mp4',item,lambda *_:None)
            self.assertEqual(ranged.call_count,2)
            self.assertEqual(ranged.call_args.kwargs['workers'],1)

    def test_audio_quality_does_not_constrain_video_quality(self):
        from main import WorkThread, VideoItem
        from resolvers import StreamResult
        item = VideoItem(1, "CVH", "Voice", "1", 1, "")
        worker = WorkThread("CVH", ["Voice"], {}, "1080p", ".", "Test", False, "", False)
        result = StreamResult("low", "cvh", {"720p": "audio-url"}, {})
        with patch.object(worker.resolver, "resolve", return_value=result):
            audio = worker.resolve_stream(item, audio_only=True)
        self.assertEqual(audio.url, "audio-url")

    def test_video_fallback_keeps_player_and_does_not_relabel_its_audio(self):
        from main import WorkThread, VideoItem
        from resolvers import StreamResult
        bad = VideoItem(1, 'CVH', 'Selected voice', '1', 1, '')
        good_video = VideoItem(2, 'CVH', 'Other voice', '1', 1, '')
        audio = VideoItem(3, 'Kodik', 'Selected voice', '1', 1, '')
        seen = []
        def resolve(item, audio_only=False):
            seen.append((item.video_id, audio_only))
            if item == bad:
                raise RuntimeError('Unavailable file')
            return StreamResult('https://example.com/media.mp4', 'direct', {}, {}, audio_only=audio_only)
        def download(stream, path, item, progress):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'media')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); mkv=root/'mkvmerge.exe'; mkv.touch()
            matrix = {1.0: {'__video__': bad, '__video_candidates__': [bad, good_video],
                           'Selected voice': audio, '__audio_candidates__': {'Selected voice': [audio]}}}
            worker=WorkThread('CVH',['Selected voice'],matrix,'1080p',root,'Test',True,str(mkv),False,
                              plexmatch_enabled=False,chapters_enabled=False)
            with patch.object(worker,'resolve_stream',side_effect=resolve), \
                 patch.object(worker,'download_stream',side_effect=download), \
                 patch('main.PlayerResolver.release'), patch('main.merge_audio_tracks') as merge, \
                 patch('main.time.sleep'):
                worker.run()
            self.assertEqual(seen, [(1,False),(2,False),(3,True)])
            self.assertEqual(matrix[1.0]['__video__'], good_video)
            self.assertEqual(merge.call_args.args[1][0][1], 'Selected voice')
            self.assertTrue(str(merge.call_args.args[1][0][0]).endswith('.mka'))

    def test_single_voice_failure_does_not_switch_video_to_audio_provider(self):
        from main import WorkThread, VideoItem
        video=VideoItem(1,'CVH','Voice','1',1,'')
        alternate=VideoItem(2,'Kodik','Voice','1',1,'')
        with tempfile.TemporaryDirectory() as directory:
            matrix={1.0: {'__video__':video,'Voice':video,
                          '__audio_candidates__':{'Voice':[video,alternate]}}}
            worker=WorkThread('CVH',['Voice'],matrix,'1080p',directory,'Test',False,'',False,
                              plexmatch_enabled=False,chapters_enabled=False)
            with patch.object(worker,'resolve_stream',side_effect=RuntimeError('failure')) as resolve, \
                 patch('main.time.sleep'):
                worker.run()
            self.assertTrue(all(call.args[0].player=='CVH' for call in resolve.call_args_list))

    def test_mux_separates_video_and_audio_inputs(self):
        process = MagicMock()
        process.stdout = []
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as directory, \
             patch("main.audio_track_ids", return_value=[1]), \
             patch("main.subprocess.Popen", return_value=process) as popen:
            root = Path(directory)
            merge_audio_tracks("mkvmerge", [(root / "audio.mkv", "Voice")],
                               root / "out.mkv", video_source=root / "video.mkv")
            cmd = popen.call_args.args[0]
            self.assertEqual(cmd[cmd.index(str(root / "video.mkv")) - 1], "--no-audio")
            self.assertIn("--no-video", cmd)

    def test_audio_falls_back_after_provider_error(self):
        from main import WorkThread, VideoItem
        from resolvers import StreamResult
        video = VideoItem(1, "Alloha", "Video voice", "1", 1, "")
        bad = VideoItem(2, "Kodik", "Voice", "1", 1, "")
        good = VideoItem(3, "CVH", "Voice", "1", 1, "")
        seen = []
        def resolve(item, **kwargs):
            seen.append(item.player)
            if item == bad:
                raise RuntimeError("HTTP 500")
            return StreamResult("https://example.com/video.mp4", "direct", {}, {})
        def download(stream, path, item, progress):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mkv = root / "mkvmerge.exe"
            mkv.touch()
            matrix = {1.0: {"__video__": video, "Voice": bad,
                            "__audio_candidates__": {"Voice": [bad, good]}}}
            worker = WorkThread("Alloha", ["Voice"], matrix, "1080p", root,
                                "Test", True, str(mkv), False,
                                plexmatch_enabled=False, chapters_enabled=False)
            results = []
            worker.done.connect(lambda summary, errors: results.append((summary, errors)))
            with patch.object(worker, "resolve_stream", side_effect=resolve), \
                 patch.object(worker, "download_stream", side_effect=download), \
                 patch("main.PlayerResolver.release"), patch("main.merge_audio_tracks"):
                worker.run()
            self.assertEqual(seen, ["Alloha", "Kodik", "CVH"])
            self.assertEqual(results[0][1], [])
            self.assertIn("Обработано серий: 1", results[0][0])

    def test_short_video_with_success_exit_code_is_rejected(self):
        from main import WorkThread, find_ffmpeg
        from resolvers import StreamResult
        from types import SimpleNamespace
        process = MagicMock()
        process.stdout = ["out_time_us=306005333\n", "progress=end\n"]
        process.wait.return_value = 0
        worker = WorkThread("Alloha", ["Voice"], {}, "1080p", ".", "Test",
                            False, "", False, ffmpeg_path=find_ffmpeg())
        with tempfile.TemporaryDirectory() as directory, \
             patch("main.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "не полностью"):
                worker.download_stream(StreamResult("http://localhost/video.m3u8", "cvh", {}, {}),
                                       Path(directory)/"video.mkv", SimpleNamespace(duration=1552),
                                       lambda *_: None)


class RuntimeTests(unittest.TestCase):
    def test_resolver_stream_error_patch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "index.js"
            entry.write_text("Readable.fromWeb(upstream.body).pipe(response);", encoding="utf-8")
            alloha_runtime.patch_stream_errors(directory)
            patched = entry.read_text(encoding="utf-8")
            self.assertIn("stream.on('error'", patched)
            self.assertIn("response.destroy()", patched)
            alloha_runtime.patch_stream_errors(directory)
            self.assertEqual(entry.read_text(encoding="utf-8"), patched)

    def test_quality_probe_does_not_close_active_alloha_session(self):
        from main import QualityProbeThread
        from resolvers import StreamResult
        stream = StreamResult("url", "alloha", {"1080p": "url"}, {}, session="shared")
        with patch("main.PlayerResolver.resolve", return_value=stream), \
             patch("main.PlayerResolver.release") as release:
            QualityProbeThread(1, {}, [object()]).run()
        release.assert_not_called()

    def test_remote_resolver_is_not_installed_locally(self):
        with patch.object(alloha_runtime, "node_runtime") as node:
            alloha_runtime.ensure_resolver("https://resolver.example.com")
            node.assert_not_called()

    def test_running_local_resolver_is_reused(self):
        with patch.object(alloha_runtime, "healthy", return_value=True), \
             patch.object(alloha_runtime, "node_runtime") as node:
            alloha_runtime.ensure_resolver("http://127.0.0.1:8790")
            node.assert_not_called()

    def test_archive_cannot_escape_install_directory(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / "bad.zip", "w") as archive:
                archive.writestr("../escape.txt", "bad")
            with self.assertRaises(RuntimeError):
                alloha_runtime.extract(root / "bad.zip", root / "install")
