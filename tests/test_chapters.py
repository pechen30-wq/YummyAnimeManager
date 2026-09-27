import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import imageio_ffmpeg

from chapters import aniskip_points, inspect_media, remux_to_mkv
from main import WorkThread


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"found": True, "results": [
            {"skipType": "op", "episodeLength": 120,
             "interval": {"startTime": 12, "endTime": 30}},
            {"skipType": "ed", "episodeLength": 120,
             "interval": {"startTime": 100, "endTime": 115}},
        ]}


class MissingResponse(FakeResponse):
    status_code = 404


class ChapterTests(unittest.TestCase):
    def test_aniskip_creates_ordered_chapter_boundaries(self):
        requests = []
        def get(url, **kwargs):
            requests.append((url, kwargs))
            return FakeResponse()
        points = aniskip_points(21, 9, 120, get=get)
        self.assertEqual(points, [(0, "Episode"), (12000, "Opening"),
                                  (30000, "Episode"), (100000, "Ending"),
                                  (115000, "After credits")])
        self.assertIn("/21/9", requests[0][0])
        self.assertEqual(requests[0][1]["timeout"], (5, 12))

    def test_aniskip_skips_unmatched_episodes(self):
        self.assertEqual(aniskip_points(None, 9, 120, get=lambda *_a, **_k: None), [])
        self.assertEqual(aniskip_points(21, 9.5, 120, get=lambda *_a, **_k: None), [])
        self.assertEqual(aniskip_points(21, 9, 120,
                                        get=lambda *_a, **_k: MissingResponse()), [])

    def test_aniskip_uses_one_opening_when_mixed_alternative_exists(self):
        class Alternatives(FakeResponse):
            def json(self):
                data = super().json()
                data["results"].append({"skipType": "mixed-op", "episodeLength": 120,
                                        "interval": {"startTime": 10, "endTime": 28}})
                return data
        points = aniskip_points(21, 9, 120, get=lambda *_a, **_k: Alternatives())
        self.assertEqual([title for _, title in points].count("Opening"), 1)
        self.assertIn((12000, "Opening"), points)

    def test_remux_adds_and_preserves_chapters(self):
        import subprocess
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "plain.mp4"
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=black:s=64x64:r=1", "-t", "4",
                            "-c:v", "mpeg4", str(plain)], check=True)
            self.assertEqual(inspect_media(ffmpeg, plain).chapters, 0)
            with_chapters = root / "with_chapters.mkv"
            remux_to_mkv(ffmpeg, plain, with_chapters,
                         points=[(0, "Episode"), (2000, "Ending")], duration=4)
            self.assertEqual(inspect_media(ffmpeg, with_chapters).chapters, 2)
            copied = root / "copied.mkv"
            remux_to_mkv(ffmpeg, plain, copied, chapter_source=with_chapters)
            self.assertEqual(inspect_media(ffmpeg, copied).chapters, 2)

    def test_worker_turns_direct_video_with_found_chapters_into_mkv(self):
        import subprocess
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "episode.mp4"
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=black:s=64x64:r=1", "-t", "4",
                            "-c:v", "mpeg4", str(plain)], check=True)
            worker = WorkThread("player", ["dub"], {}, "auto", root, "anime", False,
                                "", False, anime_metadata={"remote_ids": {"myanimelist_id": 21}},
                                ffmpeg_path=ffmpeg)
            with patch("main.aniskip_points", return_value=[(0, "Episode"), (2000, "Ending")]):
                output = worker.ensure_chapters(plain, SimpleNamespace(duration=4), 1)
            self.assertEqual(output.suffix, ".mkv")
            self.assertFalse(plain.exists())
            self.assertEqual(inspect_media(ffmpeg, output).chapters, 2)


if __name__ == "__main__":
    unittest.main()
