"""Verified updates from the project's public GitHub repository."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

from diagnostics import LOGGER
from resilient_download import download_file, download_ranges


RAW_BASE = "https://raw.githubusercontent.com/pechen30-wq/YummyAnimeManager/main/dist/"
MANIFEST_URL = RAW_BASE + "update.json"
CONFIG_FILE = Path.home() / ".yummy_anime_manager" / "config.json"
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

SOURCE_FILES = (
    "main.py", "resolvers.py", "alloha_runtime.py", "hls_download.py", "resilient_download.py", "chapters.py",
    "diagnostics.py", "process_utils.py", "url_history.py", "updater.py", "update_source.py",
    "requirements.txt", "VERSION", "README.md", "CHANGELOG.md",
    "NOTICE.md", "SECURITY.md", "build_exe.bat", "install.bat",
    "install_alloha_resolver.bat", "start_alloha_resolver.bat",
    "test_resolvers.py", "test_plexmatch.py", "run.bat",
    "scripts/build_update_payload.py", "USER_GUIDE.md", ".gitignore",
)


def version_tuple(value):
    match = VERSION_RE.fullmatch(str(value or ""))
    if not match:
        raise ValueError(f"Неверный номер версии: {value!r}")
    return tuple(map(int, match.groups()))


def newer_version(remote, local):
    return version_tuple(remote) > version_tuple(local)


def auto_update_enabled():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("auto_update", True) is not False
    except (OSError, ValueError, TypeError):
        return True


def validate_asset(asset, name):
    if not isinstance(asset, dict):
        raise ValueError(f"В update.json нет описания {name}.")
    url, digest, size = asset.get("url"), asset.get("sha256"), asset.get("size")
    if url != RAW_BASE + name or not SHA_RE.fullmatch(str(digest or "")):
        raise ValueError(f"Неверный URL или SHA-256 для {name}.")
    limit = 250 * 1024 * 1024 if name.endswith(".exe") else 10 * 1024 * 1024
    if not isinstance(size, int) or not (0 < size <= limit):
        raise ValueError(f"Неверный размер для {name}.")
    return asset


def fetch_manifest(get=requests.get):
    for attempt in range(3):
        try:
            response = get(MANIFEST_URL, params={"t": int(time.time())},
                           headers={"Cache-Control": "no-cache", "User-Agent": "YummyAnimeManager-Updater"},
                           timeout=(5, 12))
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                response.close()
            else:
                response.raise_for_status()
                break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
        time.sleep(0.6 * (attempt + 1))
    manifest = response.json()
    if not isinstance(manifest, dict):
        raise ValueError("Неверный формат update.json.")
    version_tuple(manifest.get("version"))
    validate_asset(manifest.get("exe"), "YummyAnimeManager.exe")
    validate_asset(manifest.get("source"), "source.zip")
    return manifest


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(asset, destination, progress_cb=lambda *_: None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.is_file() and destination.stat().st_size == asset["size"]:
        progress_cb(96, "Проверяю уже скачанное обновление…")
        if sha256_file(destination) == asset["sha256"]:
            progress_cb(100, "Обновление готово")
            return destination

    def report(percent, message):
        message = (message.replace("MP4 — ", "")
                          .replace("MP4 · ", "")
                          .replace("MP4 полностью скачан", "Файл скачан"))
        progress_cb(min(95, int(percent * 0.95)), message)

    # GitHub Raw supports byte ranges. Four independent connections are much
    # faster on high-latency routes and the verified block ledger lets a new
    # application process continue an interrupted update.
    if destination.suffix.lower() == ".exe" and asset["size"] >= 8 * 1024 * 1024:
        download_ranges(asset["url"], destination, {}, report,
                        block_size=4 * 1024 * 1024, attempts=5,
                        workers=4, resume=True)
    else:
        download_file(asset["url"], destination, {}, report,
                      attempts=5, resume=True)
    progress_cb(96, "Проверяю целостность обновления…")
    actual_size = destination.stat().st_size
    actual_hash = sha256_file(destination)
    if actual_size != asset["size"] or actual_hash != asset["sha256"]:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Скачанное обновление не прошло проверку размера и SHA-256.")
    LOGGER.info("Verified update asset %s size=%s",destination.name,actual_size)
    progress_cb(100, "Обновление готово")
    return destination


def validate_executable(path, *, timeout=30):
    """Run the packaged application's import-only smoke test before replacing it."""
    path = Path(path).resolve()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run([str(path), "--self-test"], cwd=path.parent,
                                capture_output=True, text=True, timeout=timeout,
                                creationflags=flags)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Проверка запуска обновления не завершилась вовремя.") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "неизвестная ошибка").strip()[-500:]
        raise RuntimeError(f"Обновление не прошло проверку запуска: {detail}")
    LOGGER.info("Update executable self-test passed: %s", path.name)


def update_git_checkout(root):
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=root,
                            capture_output=True, text=True, timeout=30)
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        raise RuntimeError("Автообновление Git-копии поддерживается только на ветке main.")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=30)
    if status.returncode != 0 or status.stdout.strip():
        raise RuntimeError("Рабочая копия Git содержит изменения. Автообновление пропущено.")
    proc = subprocess.run(["git", "pull", "--ff-only", "origin", "main"], cwd=root,
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"Git pull не удался: {proc.stderr.strip()}")
    return proc.stdout.strip()


def update_source_folder(root, manifest, *, python=sys.executable):
    """Replace only project source files; preserve venv, downloads and settings."""
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix="yummy-update-", dir=root) as directory:
        temp = Path(directory)
        archive_path = download_verified(manifest["source"], temp / "source.zip")
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            expected = set(SOURCE_FILES)
            expected.update(p for p in names if re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", p))
            if names != expected or not {"main.py", "VERSION", "updater.py", "requirements.txt"} <= names:
                raise RuntimeError("Архив исходников содержит неожиданные или отсутствующие файлы.")
            if archive.read("VERSION").decode("utf-8").strip() != manifest["version"]:
                raise RuntimeError("Версия архива не совпадает с update.json.")
            stage, backup = temp / "stage", temp / "backup"
            stage.mkdir(); backup.mkdir()
            changed = []
            try:
                for name in sorted(names):
                    if name == "run.bat":  # This launcher is currently executing.
                        continue
                    target = root / name
                    staged = stage / name
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged.write_bytes(archive.read(name))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    existed = target.exists()
                    if existed:
                        saved = backup / name
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, saved)
                    os.replace(staged, target)
                    changed.append((target, backup / name if existed else None))
                proc = subprocess.run([python, "-m", "pip", "install", "-r", str(root / "requirements.txt")],
                                      capture_output=True, text=True, timeout=180)
                if proc.returncode != 0:
                    raise RuntimeError(f"Не удалось установить зависимости: {proc.stderr[-500:]}")
            except Exception:
                for target, saved in reversed(changed):
                    if saved is None:
                        target.unlink(missing_ok=True)
                    else:
                        os.replace(saved, target)
                raise
    return manifest["version"]


POWERSHELL_HELPER = r'''
param([int]$ParentPid, [string]$Current, [string]$Staged, [string]$ExpectedHash, [string]$ResultFile)
$ErrorActionPreference = 'Stop'
try {
    $currentPath = [IO.Path]::GetFullPath($Current)
    $stagedPath = [IO.Path]::GetFullPath($Staged)
    if ([IO.Path]::GetDirectoryName($currentPath) -ne [IO.Path]::GetDirectoryName($stagedPath)) {
        throw 'Update files must be in the same directory.'
    }
    for ($i = 0; $i -lt 90; $i++) {
        if (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Seconds 1
    }
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) { throw 'Application did not exit.' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $stagedPath).Hash.ToLowerInvariant() -ne $ExpectedHash) {
        throw 'SHA-256 mismatch.'
    }
    $backup = "$currentPath.previous"
    $replaced = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            [IO.File]::Replace($stagedPath, $currentPath, $backup)
            $replaced = $true
            break
        } catch [IO.IOException] { Start-Sleep -Seconds 1 }
          catch [UnauthorizedAccessException] { Start-Sleep -Seconds 1 }
    }
    if (-not $replaced) { throw 'Could not replace the running EXE.' }
    'success' | Set-Content -LiteralPath $ResultFile -Encoding UTF8
    Start-Process -FilePath $currentPath -WorkingDirectory ([IO.Path]::GetDirectoryName($currentPath))
} catch {
    "error: $($_.Exception.Message)" | Set-Content -LiteralPath $ResultFile -Encoding UTF8
    if (Test-Path -LiteralPath $Current) {
        Start-Process -FilePath $Current -WorkingDirectory ([IO.Path]::GetDirectoryName($Current))
    }
}
'''


def schedule_exe_replacement(current, staged, expected_hash):
    current, staged = Path(current).resolve(), Path(staged).resolve()
    if current.parent != staged.parent or current == staged:
        raise ValueError("Файл обновления должен находиться рядом с EXE.")
    if sha256_file(staged) != expected_hash:
        raise RuntimeError("SHA-256 обновления не совпадает.")
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    helper = CONFIG_FILE.parent / "apply-update.ps1"
    result_file = CONFIG_FILE.parent / "update-result.txt"
    result_file.unlink(missing_ok=True)
    helper.write_text(POWERSHELL_HELPER, encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                      "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(helper), "-ParentPid", str(os.getpid()),
                      "-Current", str(current), "-Staged", str(staged),
                      "-ExpectedHash", expected_hash, "-ResultFile", str(result_file)],
                     creationflags=flags, close_fds=True)
    return result_file
