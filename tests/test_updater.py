import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import updater


class UpdaterTests(unittest.TestCase):
    def test_version_and_manifest_validation(self):
        self.assertTrue(updater.newer_version("4.4.0", "4.3.0"))
        self.assertFalse(updater.newer_version("4.4.0", "4.4.0"))
        with self.assertRaises(ValueError):
            updater.version_tuple("4.4.0-preview")
        manifest = {"version": "4.4.0", "exe": {"url": updater.RAW_BASE + "YummyAnimeManager.exe",
                    "sha256": "a" * 64, "size": 10},
                    "source": {"url": updater.RAW_BASE + "source.zip",
                    "sha256": "b" * 64, "size": 10}}
        response = Mock()
        response.json.return_value = manifest
        self.assertEqual(updater.fetch_manifest(get=Mock(return_value=response)), manifest)
        manifest["exe"]["url"] = "https://example.com/file.exe"
        with self.assertRaises(ValueError):
            updater.fetch_manifest(get=Mock(return_value=response))

    def test_source_archive_updates_files_and_preserves_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("old", encoding="utf-8")
            (root / "user-video.mkv").write_bytes(b"keep")
            archive_path = root / "payload.zip"
            names = set(updater.SOURCE_FILES) | {"tests/test_updater.py"}
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name in names:
                    archive.writestr(name, "4.4.0" if name == "VERSION" else "new")
            data = archive_path.read_bytes()
            manifest = {"version": "4.4.0", "source": {"url": updater.RAW_BASE + "source.zip",
                        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}

            def copy_asset(asset, destination):
                Path(destination).write_bytes(data)
                return Path(destination)

            with patch.object(updater, "download_verified", side_effect=copy_asset), \
                 patch.object(updater.subprocess, "run", return_value=Mock(returncode=0)):
                self.assertEqual(updater.update_source_folder(root, manifest), "4.4.0")
            self.assertEqual((root / "main.py").read_text(encoding="utf-8"), "new")
            self.assertEqual((root / "user-video.mkv").read_bytes(), b"keep")
            self.assertFalse((root / "run.bat").exists())

    def test_source_update_rolls_back_on_dependency_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("old", encoding="utf-8")
            names = set(updater.SOURCE_FILES)
            payload = root / "payload.zip"
            with zipfile.ZipFile(payload, "w") as archive:
                for name in names:
                    archive.writestr(name, "4.4.0" if name == "VERSION" else "new")
            data = payload.read_bytes()
            manifest = {"version": "4.4.0", "source": {"url": updater.RAW_BASE + "source.zip",
                        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}

            def copy_asset(asset, destination):
                Path(destination).write_bytes(data)
                return Path(destination)

            with patch.object(updater, "download_verified", side_effect=copy_asset), \
                 patch.object(updater.subprocess, "run", return_value=Mock(returncode=1, stderr="pip failed")):
                with self.assertRaisesRegex(RuntimeError, "pip failed"):
                    updater.update_source_folder(root, manifest)
            self.assertEqual((root / "main.py").read_text(encoding="utf-8"), "old")
            self.assertFalse((root / "VERSION").exists())


if __name__ == "__main__":
    unittest.main()
