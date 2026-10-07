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

    def test_large_executable_uses_resumable_parallel_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.exe"
            data = b"verified update"
            asset = {"url": updater.RAW_BASE + "YummyAnimeManager.exe",
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "size": 9 * 1024 * 1024}

            def download(_url, path, _headers, progress, **kwargs):
                Path(path).write_bytes(data)
                asset["size"] = len(data)
                progress(100, "MP4 полностью скачан")

            progress = []
            with patch.object(updater, "download_ranges", side_effect=download) as ranged, \
                 patch.object(updater, "download_file") as sequential:
                updater.download_verified(asset, destination,
                                           lambda *values: progress.append(values))
            self.assertTrue(ranged.call_args.kwargs["resume"])
            self.assertEqual(ranged.call_args.kwargs["workers"], 4)
            sequential.assert_not_called()
            self.assertEqual(progress[-1], (100, "Обновление готово"))

    def test_executable_self_test_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "update.exe"
            executable.write_bytes(b"exe")
            failed = Mock(returncode=1, stdout="", stderr="python DLL missing")
            with patch.object(updater.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "python DLL missing"):
                    updater.validate_executable(executable)

    def test_independent_environment_preserves_settings_without_mutating_parent(self):
        inherited = {"_PYI_APPLICATION_HOME_DIR": "removed-temp",
                     "PYINSTALLER_RESET_ENVIRONMENT": "0", "PATH": "user-path"}
        with patch.dict(updater.os.environ, inherited, clear=True):
            environment = updater.independent_executable_environment()
            self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertEqual(environment["PATH"], "user-path")
            self.assertEqual(dict(updater.os.environ), inherited)

    def test_executable_validation_requests_fresh_unpack(self):
        with patch.object(updater.subprocess, "run", return_value=Mock(returncode=0)) as run:
            updater.validate_executable("update.exe")
        self.assertEqual(run.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(run.call_args.args[0][-1], "--self-test")

    def test_replacement_helper_resets_environment_for_restart_and_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current, staged = root / "app.exe", root / "app.new.exe"
            staged.write_bytes(b"verified executable")
            with patch.object(updater, "CONFIG_FILE", root / "config.json"), \
                 patch.object(updater.subprocess, "Popen") as launch:
                updater.schedule_exe_replacement(current, staged, updater.sha256_file(staged))
            self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            helper = (root / "apply-update.ps1").read_text(encoding="utf-8")
            reset = helper.index("$env:PYINSTALLER_RESET_ENVIRONMENT = '1'")
            self.assertLess(reset, helper.index("Start-Process -FilePath $currentPath"))
            self.assertLess(reset, helper.index("Start-Process -FilePath $Current"))


if __name__ == "__main__":
    unittest.main()
