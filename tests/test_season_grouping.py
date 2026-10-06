import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from main import (DownloadCheckpoint, FetchThread, MainWindow, VideoItem, WorkThread,
                  catalog_series_identity)
from resolvers import StreamResult


def anime(number, kind="tv"):
    order = [
        {"anime_id": 10 + i, "title": "Необъятный океан" + (f" {i + 1}" if i else ""),
         "year": year, "type": {"alias": kind}, "data": {"index": i, "id": 5},
         "anime_url": f"https://ru.yummyani.me/catalog/item/season-{i + 1}"}
        for i, year in enumerate((2018, 2025, 2026))
    ]
    return {"anime_id": 9 + number, "title": order[number - 1]["title"],
            "year": order[number - 1]["year"], "type": {"alias": kind},
            "viewing_order": order}


class SeasonGroupingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_linked_tv_cards_share_show_name_and_keep_season_numbers(self):
        for number in (1, 2, 3):
            with self.subTest(number=number):
                title, season, metadata = catalog_series_identity(anime(number))
                self.assertEqual((title, season), ("Необъятный океан", number))
                self.assertEqual(metadata["year"], 2018)
        self.assertEqual(catalog_series_identity(anime(2, "movie"))[0], "Необъятный океан 2")

    def test_sequel_uses_shared_plex_folder_and_current_season(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = VideoItem(1, "CVH", "Voice", "1", 1, "")
            metadata = anime(2)
            worker = WorkThread("CVH", ["Voice"], {1.0: {"__video__": item, "Voice": item}},
                                "720p", root, metadata["title"], False, "", False,
                                season_number=2, plex_structure=True, plexmatch_enabled=True,
                                anime_metadata=metadata, chapters_enabled=False,
                                series_title="Необъятный океан")
            def download(_stream, path, _item, _progress):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"episode")
            with patch.object(worker, "resolve_stream", return_value=StreamResult(
                    "https://example.test/video.mp4", "direct", {}, {})), \
                 patch.object(worker, "download_stream", side_effect=download), \
                 patch("main.PlayerResolver.release"):
                worker.run()
            show = root / "Необъятный океан"
            self.assertTrue((show / "Season 02" / "Необъятный океан - S02E01.mp4").exists())
            self.assertIn("Title: Необъятный океан", (show / ".plexmatch").read_text(encoding="utf-8"))
            self.assertIn("Year: 2018", (show / ".plexmatch").read_text(encoding="utf-8"))
            self.assertFalse((root / "Необъятный океан 2").exists())

    def test_stopping_one_season_keeps_other_season_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Необъятный океан" / ".tmp"
            (root / "season_01").mkdir(parents=True)
            (root / "season_02").mkdir()
            (root / "season_01" / "keep.ts").write_bytes(b"keep")
            (root / "season_02" / "remove.ts").write_bytes(b"remove")
            checkpoint = DownloadCheckpoint({"schema": 1, "settings": {
                "base_dir": directory, "anime_title": "Необъятный океан 2",
                "series_title": "Необъятный океан", "season_number": 2,
                "plex_structure": True}, "files": {}, "episodes": {}},
                Path(directory) / "pending.json")
            checkpoint.save()
            checkpoint.discard()
            self.assertTrue((root / "season_01" / "keep.ts").exists())
            self.assertFalse((root / "season_02").exists())

    def test_sequel_card_exposes_its_own_episodes_as_season_two(self):
        raw = [{"data": {"player": "CVH", "dubbing": "Voice"}, "number": "1",
                "index": 1, "video_id": 1, "iframe_url": "", "season": 1}]
        with patch("main.load_config", return_value={"public_token": "test"}), \
             patch.object(MainWindow, "schedule_quality_probe"):
            window = MainWindow()
            self.addCleanup(window.close)
            window.on_loaded(anime(2), raw)
            self.assertEqual(window.current_season(), 2)
            self.assertEqual(window.selected_episodes(), [1.0])
            self.assertIn("Season 02", window.series_label.text())

    def test_season_three_link_loads_all_published_seasons_in_background(self):
        cards = {number: anime(number) for number in (1, 2, 3)}
        cards[3]["viewing_order"].append({
            "anime_id": 13, "title": "Необъятный океан 4", "year": 2027,
            "type": {"alias": "tv"}, "data": {"index": 3, "id": 5},
            "anime_url": "https://ru.yummyani.me/catalog/item/season-4"})
        for card in cards.values():
            card["viewing_order"] = cards[3]["viewing_order"]
        cards[4] = {"anime_id": 13, "title": "Необъятный океан 4"}

        class Api:
            def __init__(self, *_args): pass
            def anime(self, slug):
                return cards[int(slug.rsplit("-", 1)[-1])]
            def videos(self, anime_id):
                if anime_id == 13:
                    return []
                return [{"data": {"player": "CVH", "dubbing": "Voice"},
                         "number": "1", "index": 1, "video_id": anime_id,
                         "iframe_url": "", "season": 1}]

        with patch("main.YummyApi", Api):
            worker = FetchThread("token", "ru", "season-3")
            result = []
            worker.loaded.connect(lambda card, records: result.append((card, records)))
            worker.run()
        self.assertEqual(len(result), 1)
        card, records = result[0]
        self.assertEqual(set(card["_catalog_seasons"]), {1, 2, 3})
        self.assertEqual([record["_catalog_season"] for record in records], [1, 2, 3])
        with patch("main.load_config", return_value={"public_token": "test"}), \
             patch.object(MainWindow, "schedule_quality_probe"):
            window = MainWindow()
            self.addCleanup(window.close)
            window.on_loaded(card, records)
            self.assertEqual(window.current_season(), 3)
            self.assertEqual(window.available_seasons_for_player(), [1, 2, 3])
            self.assertEqual([item.video_id for item in window.all_items_for_current_season()], [12])
            window.season_combo.setCurrentIndex(0)
            self.assertEqual(window.anime_title(), "Необъятный океан")
            self.assertEqual([item.video_id for item in window.all_items_for_current_season()], [10])
            self.assertEqual(window.selected_episodes(), [1.0])

        class PublishedApi(Api):
            def videos(self, anime_id):
                if anime_id == 13:
                    return [{"data": {"player": "CVH", "dubbing": "Voice"},
                             "number": "1", "index": 1, "video_id": 13,
                             "iframe_url": ""}]
                return super().videos(anime_id)
        with patch("main.YummyApi", PublishedApi):
            worker = FetchThread("token", "ru", "season-3")
            updated = []
            worker.loaded.connect(lambda new_card, _: updated.append(new_card))
            worker.run()
        self.assertEqual(set(updated[0]["_catalog_seasons"]), {1, 2, 3, 4})


if __name__ == "__main__":
    unittest.main()
