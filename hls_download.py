"""Stage a complete finite HLS playlist before letting FFmpeg remux it."""

import base64
import hashlib
import re
import time
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import requests
from resilient_download import download_file
from diagnostics import LOGGER


def fetch_playlist(url, headers):
    for attempt in range(3):
        try:
            with requests.get(url, headers=headers, timeout=(10, 40)) as response:
                response.raise_for_status()
                body = response.text
                if not body.lstrip().startswith("#EXTM3U"):
                    raise RuntimeError("Alloha вернул ответ вместо HLS-плейлиста.")
                return body, response.url
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as error:
            if isinstance(error, requests.HTTPError) and error.response.status_code < 500:
                raise
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def asset_identity(url):
    # Proxy signatures rotate, while their encoded upstream target stays stable.
    parsed = urllib.parse.urlsplit(url)
    encoded = urllib.parse.parse_qs(parsed.query).get("u")
    if encoded:
        try:
            value = encoded[0]
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        except (ValueError, UnicodeError):
            pass
    return url


def attributes(line):
    return {key: quoted if quoted else plain
            for key, quoted, plain in re.findall(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line)}


def playlist_variant(body, audio_only=False):
    variants = []
    pending = None
    tracks = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith('#EXT-X-MEDIA:'):
            track = attributes(line)
            if track.get('TYPE') == 'AUDIO' and track.get('URI'):
                tracks.append(track)
        elif line.startswith('#EXT-X-STREAM-INF:'):
            pending = attributes(line)
        elif line and not line.startswith('#'):
            variants.append((line, pending or {}))
            pending = None
    if not variants:
        raise RuntimeError("Alloha: в плейлисте нет видеосегментов.")
    if audio_only:
        group = variants[-1][1].get('AUDIO')
        matching = [track for track in tracks if not group or track.get('GROUP-ID') == group]
        if matching:
            return max(matching, key=lambda track: (track.get('DEFAULT') == 'YES',
                                                     track.get('AUTOSELECT') == 'YES'))['URI']
        # Muxed variants may carry different audio quality; keep the chosen stream.
    return variants[-1][0]


def media_playlist(url, headers, audio_only=False):
    for _ in range(4):
        body, url = fetch_playlist(url, headers)
        if "#EXTINF:" in body:
            if "#EXT-X-ENDLIST" not in body:
                raise RuntimeError("Alloha вернул незавершённый список сегментов серии.")
            return body, url
        url = urllib.parse.urljoin(url, playlist_variant(body, audio_only))
    raise RuntimeError("Alloha: слишком много вложенных плейлистов.")


def resources(body, url):
    result = []
    for line in body.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            result.append(urllib.parse.urljoin(url, line.strip()))
        else:
            result.extend(urllib.parse.urljoin(url, value) for value in re.findall(r'URI="([^"]+)"', line))
    return result


def layout(body):
    return [re.sub(r'URI="[^"]+"', 'URI="asset"', line) if line.startswith("#") else "segment"
            for line in body.splitlines() if line.strip() and not line.startswith("#EXT-X-PROGRAM-DATE-TIME")]


def stage_hls(url, cache, headers, progress, refresh=None, audio_only=False, workers=4):
    """Reuse only fully downloaded assets; .part files are never fed to FFmpeg."""
    started = time.monotonic()
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    body, url = media_playlist(url, headers, audio_only)
    signature = hashlib.sha256(repr(layout(body)).encode()).hexdigest()
    marker = cache / "layout.txt"
    if marker.is_file() and marker.read_text(encoding="ascii") != signature:
        cache = cache / ("layout-" + signature[:16])
        cache.mkdir(parents=True, exist_ok=True)
        marker = cache / "layout.txt"
    marker.write_text(signature, encoding="ascii")
    original = resources(body, url)
    latest = dict(zip(original, original))
    refreshes = 0
    lines = body.splitlines()
    # One job per resource: repeated keys/maps must never share a .part writer.
    names = {}
    output = []
    def local_name(uri, suffix):
        target = urllib.parse.urljoin(url, uri)
        if target not in names:
            extension = Path(urllib.parse.urlsplit(asset_identity(target)).path).suffix.lower()
            if suffix == ".ts" and extension in (".m4s", ".mp4", ".aac", ".vtt"):
                suffix = extension
            names[target] = f"asset-{original.index(target):05d}{suffix}"
        return names[target]

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            output.append(local_name(stripped, ".ts"))
        elif re.search(r'URI="[^\"]+"', line):
            suffix = ".mp4" if stripped.startswith("#EXT-X-MAP:") else ".key"
            output.append(re.sub(r'URI="([^\"]+)"',
                                 lambda match: f'URI="{local_name(match.group(1), suffix)}"', line))
        else:
            output.append(line)

    playlist = cache / "complete.m3u8"
    playlist.unlink(missing_ok=True)
    stopped = threading.Event()
    state_lock = threading.Lock()
    refresh_lock = threading.Lock()
    thread_state = threading.local()
    sessions = []
    fractions = {target: 1.0 if (cache / name).is_file() else 0.0
                 for target, name in names.items()}
    cached = sum(value == 1 for value in fractions.values())
    last_report = 0.0
    last_pct = 0

    def report(target, pct, detail):
        nonlocal last_report, last_pct
        with state_lock:
            if stopped.is_set():
                return
            fractions[target] = max(fractions[target], pct / 100)
            now = time.monotonic()
            if pct != 100 and now - last_report < 0.2:
                return
            last_report = now
            last_pct = max(last_pct, min(90, int(sum(fractions.values()) * 90 / max(1, len(names)))))
            completed = sum(value == 1 for value in fractions.values())
            progress(last_pct, f"Фрагменты {completed}/{len(names)} · {detail}")

    def download(target):
        nonlocal refreshes
        if not hasattr(thread_state, "session"):
            thread_state.session = requests.Session()
            with state_lock:
                sessions.append(thread_state.session)
        session = thread_state.session
        for attempt in range(19):
            if stopped.is_set():
                return
            with refresh_lock:
                request_url = latest[target]
                request_headers = dict(headers)
            try:
                download_file(request_url, cache / names[target], request_headers,
                              lambda pct, detail: report(target, pct, detail),
                              session=session, attempts=2, cancelled=stopped.is_set,
                              sleep=stopped.wait)
                report(target, 100, "Готово")
                return
            except requests.HTTPError as error:
                status = error.response.status_code
                if status in (401, 403) and refresh:
                    # Other workers may already have renewed this expired URL.
                    with refresh_lock:
                        if latest[target] != request_url:
                            continue
                        if refreshes >= 16 or stopped.is_set():
                            raise
                        report(target, 0, "Обновляю истёкшую ссылку Alloha…")
                        for renewal in range(3):
                            if refreshes >= 16:
                                raise error
                            refreshes += 1
                            try:
                                fresh_body, fresh_url = media_playlist(refresh(), headers, audio_only)
                                break
                            except requests.HTTPError as expired:
                                if expired.response.status_code not in (401, 403) or renewal == 2:
                                    raise
                                time.sleep(2 * (renewal+1))
                        fresh_resources = resources(fresh_body, fresh_url)
                        if layout(fresh_body) != layout(body) or len(fresh_resources) != len(original):
                            raise RuntimeError("После обновления Alloha изменился состав серии; сборка остановлена.")
                        latest.update(zip(original, fresh_resources))
                    continue
                if status < 500 or attempt >= 2:
                    raise
                if stopped.wait(attempt + 1):
                    return
        raise RuntimeError("Не удалось обновить ссылку на фрагмент серии.")

    pending = iter(target for target in names if fractions[target] < 1)
    pool = ThreadPoolExecutor(max_workers=max(1, min(4, workers)), thread_name_prefix="hls")
    active = set()
    try:
        # Bound both active work and queued work, so a failed CDN stops promptly.
        for _ in range(max(1, min(4, workers))):
            target = next(pending, None)
            if target is not None:
                active.add(pool.submit(download, target))
        while active:
            done, active = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
            for _ in done:
                target = next(pending, None)
                if target is not None:
                    active.add(pool.submit(download, target))
    finally:
        stopped.set()
        pool.shutdown(wait=True, cancel_futures=True)
        for session in sessions:
            session.close()

    # FFmpeg sees the ordered manifest only after every resource is complete.
    playlist.write_text("\n".join(output) + "\n", encoding="utf-8")
    elapsed = time.monotonic() - started
    size = sum((cache / name).stat().st_size for name in names.values())
    LOGGER.info("HLS complete: workers=%s assets=%s cached=%s bytes=%s elapsed=%.2fs",
                max(1, min(4, workers)), len(names), cached, size, elapsed)
    progress(90, f"Фрагменты {len(names)}/{len(names)}")
    return playlist
