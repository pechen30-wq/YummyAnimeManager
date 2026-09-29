"""Install and start the official resolver in the user's writable data folder."""

import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import requests
from diagnostics import LOGGER

ROOT = Path.home() / ".yummy_anime_manager" / "runtime"
REVISION = "8a9f358865ce5dde309bf68341c22ce7f8782056"
LOCK = threading.Lock()
PROCESS = None


def healthy(base):
    try:
        response = requests.get(base + "/health", timeout=2)
        return response.status_code == 200 and response.json().get("ok") is True
    except (requests.RequestException, ValueError):
        return False


def download(url, path):
    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=(10, 90)) as response:
                response.raise_for_status()
                with path.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
            return
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def extract(archive, target):
    with zipfile.ZipFile(archive) as source:
        for name in source.namelist():
            resolved = (target / name).resolve()
            if not resolved.is_relative_to(target.resolve()):
                raise RuntimeError("Небезопасный путь в архиве resolver-а.")
        source.extractall(target)


def node_runtime(root):
    node = shutil.which("node")
    if node:
        cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        result = subprocess.run([node, "--version"], capture_output=True, text=True,
                                timeout=10, **hidden())
        if result.returncode == 0 and int(result.stdout.strip().lstrip("v").split(".")[0]) >= 18 and cli.is_file():
            return Path(node), cli
    if os.name != "nt":
        raise RuntimeError("Установите Node.js 18+ с npm для Alloha resolver.")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    local = root / "node" / "node.exe"
    cli = local.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if local.is_file() and cli.is_file():
        return local, cli
    LOGGER.info("Alloha: installing portable Node.js LTS")
    response = requests.get("https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt", timeout=30)
    response.raise_for_status()
    entry = next(line.split() for line in response.text.splitlines()
                 if line.endswith(f"-win-{arch}.zip"))
    digest, name = entry
    with tempfile.TemporaryDirectory(dir=root) as directory:
        temp = Path(directory)
        archive = temp / "node.zip"
        download("https://nodejs.org/dist/latest-v22.x/" + name, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Не совпала контрольная сумма Node.js.")
        extract(archive, temp / "unpacked")
        shutil.copytree(temp / "unpacked" / name.removesuffix(".zip"), local.parent, dirs_exist_ok=True)
    return local, cli


def hidden():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def ensure_resolver(base):
    """Remote URLs are user-managed; only plain HTTP loopback is auto-started."""
    global PROCESS
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path not in ("", "/"):
        return
    if healthy(base):
        return
    with LOCK:
        if healthy(base):
            return
        ROOT.mkdir(parents=True, exist_ok=True)
        server = ROOT / "alloha-resolver"
        node, npm_cli = node_runtime(ROOT)
        env = os.environ.copy()
        env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "browsers")
        env["PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT"] = "120000"
        proxy = urllib.request.getproxies().get("https")
        if proxy:
            env.setdefault("HTTPS_PROXY", proxy)
        env["YANI_RESOLVER_HOST"] = "127.0.0.1"
        env["YANI_RESOLVER_PORT"] = str(parsed.port or 80)
        marker = server / ".installed"
        if not marker.is_file():
            LOGGER.info("Alloha: installing official resolver and Chromium (first run)")
            with tempfile.TemporaryDirectory(dir=ROOT) as directory:
                temp = Path(directory)
                archive = temp / "resolver.zip"
                download(f"https://codeload.github.com/yummyanime/yummy-lampa-plugin/zip/{REVISION}", archive)
                extract(archive, temp / "unpacked")
                source = temp / "unpacked" / f"yummy-lampa-plugin-{REVISION}" / "server"
                shutil.copytree(source, server, dirs_exist_ok=True)
            log_dir = ROOT.parent / "logs"
            log_dir.mkdir(exist_ok=True)
            with (log_dir / "alloha-install.log").open("w", encoding="utf-8") as log:
                result = subprocess.run([str(node), str(npm_cli), "install", "--ignore-scripts", "--no-audit", "--no-fund"],
                                        cwd=server, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=300, **hidden())
                if result.returncode == 0:
                    for attempt in range(3):
                        result = subprocess.run(
                            [str(node), str(server / "node_modules" / "playwright" / "cli.js"), "install", "chromium"],
                            cwd=server, env=env, stdout=log, stderr=subprocess.STDOUT,
                            timeout=900, **hidden())
                        if result.returncode == 0:
                            break
            if result.returncode:
                raise RuntimeError(f"Не удалось установить Alloha resolver. Подробности: {log_dir / 'alloha-install.log'}")
            marker.write_text(REVISION, encoding="utf-8")
        log_dir = ROOT.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        with (log_dir / "alloha-resolver.log").open("a", encoding="utf-8") as log:
            PROCESS = subprocess.Popen([str(node), "index.js"], cwd=server, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, **hidden())
        for _ in range(60):
            if healthy(base):
                LOGGER.info("Alloha: resolver ready")
                return
            if PROCESS.poll() is not None:
                break
            time.sleep(0.5)
        raise RuntimeError(f"Alloha resolver не запустился. Подробности: {log_dir / 'alloha-resolver.log'}")
