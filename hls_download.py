"""Stage a complete finite HLS playlist before letting FFmpeg remux it."""

import base64
import hashlib
import re
import time
import urllib.parse
from pathlib import Path

import requests
from resilient_download import download_file


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


def media_playlist(url, headers):
    for _ in range(4):
        body, url = fetch_playlist(url, headers)
        if "#EXTINF:" in body:
            if "#EXT-X-ENDLIST" not in body:
                raise RuntimeError("Alloha вернул незавершённый список сегментов серии.")
            return body, url
        variants = [line.strip() for line in body.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        if not variants:
            raise RuntimeError("Alloha: в плейлисте нет видеосегментов.")
        url = urllib.parse.urljoin(url, variants[-1])
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


def stage_hls(url, cache, headers, progress, refresh=None):
    """Reuse only fully downloaded assets; .part files are never fed to FFmpeg."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    body, url = media_playlist(url, headers)
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
    total = sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in lines)
    index = 0

    def asset(uri, suffix):
        nonlocal refreshes
        target = urllib.parse.urljoin(url, uri)
        extension = Path(urllib.parse.urlsplit(asset_identity(target)).path).suffix.lower()
        if suffix == ".ts" and extension in (".m4s", ".mp4", ".aac", ".vtt"):
            suffix = extension
        name = f"asset-{original.index(target):05d}{suffix}"
        local = cache / name
        if not local.is_file():
            for attempt in range(19):
                try:
                    download_file(latest[target], local, dict(headers),
                                  lambda pct, detail: progress(
                                      min(89, int((index + pct/100) * 90 / max(1, total))),
                                      f"Сегменты {min(index+1,total)}/{total} · {detail}"))
                    break
                except requests.HTTPError as error:
                    status = error.response.status_code
                    if status in (401, 403) and refresh and refreshes < 16:
                        progress(int(index*90/max(1,total)), "Обновляю истёкшую ссылку Alloha…")
                        for renewal in range(3):
                            if refreshes >= 16:
                                raise error
                            refreshes += 1
                            try:
                                fresh_body, fresh_url = media_playlist(refresh(), headers)
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
                    time.sleep(attempt + 1)
        return name

    output = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            output.append(asset(stripped, ".ts"))
            index += 1
            progress(int(index * 90 / max(1, total)), f"Сегменты {index}/{total}")
        elif re.search(r'URI="[^"]+"', line):
            suffix = ".mp4" if stripped.startswith("#EXT-X-MAP:") else ".key"
            output.append(re.sub(r'URI="([^"]+)"',
                                 lambda match: f'URI="{asset(match.group(1), suffix)}"', line))
        else:
            output.append(line)
    playlist = cache / "complete.m3u8"
    playlist.write_text("\n".join(output) + "\n", encoding="utf-8")
    return playlist
