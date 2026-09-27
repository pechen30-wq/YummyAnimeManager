"""Bounded, resumable downloads for direct media URLs."""

import random
import re
import time
from pathlib import Path

import requests
from diagnostics import LOGGER, safe_url


RETRYABLE = (
    ConnectionResetError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)$", re.I)


def download_file(url, path, headers, progress_cb, *, attempts=4, sleep=time.sleep):
    """Download into a sidecar, resuming only when the server proves the offset."""
    path = Path(path)
    part = path.with_name(path.name + ".part")
    part.unlink(missing_ok=True)  # A previous run may have used a different URL.
    last_error = None
    last_pct = -1

    for attempt in range(1, attempts + 1):
        offset = part.stat().st_size if part.exists() else 0
        request_headers = dict(headers)
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        LOGGER.debug("Download request %s attempt=%s/%s offset=%s", safe_url(url), attempt, attempts, offset)
        try:
            with requests.get(url, headers=request_headers, stream=True,
                              timeout=(15, 60), allow_redirects=True) as response:
                if offset and response.status_code == 416:
                    part.unlink(missing_ok=True)
                    raise requests.exceptions.ConnectionError("Сервер отклонил Range; начинаю загрузку заново")
                response.raise_for_status()
                LOGGER.debug("Download response status=%s range=%s content_length=%s",
                             response.status_code, response.headers.get("Content-Range"),
                             response.headers.get("Content-Length"))
                append = False
                expected_end = 0
                if offset and response.status_code == 206:
                    match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
                    if not match or int(match.group(1)) != offset:
                        raise RuntimeError("Сервер вернул неверный Content-Range; загрузка остановлена во избежание повреждения файла.")
                    append = True
                    total = int(match.group(3)) if match.group(3) != "*" else 0
                    expected_end = int(match.group(2)) + 1
                elif response.status_code == 206:
                    match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
                    if not match or int(match.group(1)) != 0:
                        raise RuntimeError("Сервер вернул неожиданный Content-Range.")
                    total = int(match.group(3)) if match.group(3) != "*" else 0
                    expected_end = int(match.group(2)) + 1
                else:
                    # A 200 response to Range means the server ignored it.
                    offset = 0
                    try:
                        total = int(response.headers.get("Content-Length") or 0)
                    except (ValueError, TypeError):
                        total = 0

                with part.open("ab" if append else "wb") as output:
                    downloaded = offset
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = max(0, min(99, int(downloaded * 100 / total)))
                            if pct != last_pct:
                                progress_cb(pct, f"{pct}% · {downloaded/1024/1024:.1f}/{total/1024/1024:.1f} МБ")
                                last_pct = pct
                        else:
                            progress_cb(0, f"{downloaded/1024/1024:.1f} МБ (размер неизвестен)")
                if total and downloaded != total:
                    raise requests.exceptions.ConnectionError(
                        f"Получено {downloaded} из {total} байт")
                if expected_end and downloaded != expected_end:
                    raise requests.exceptions.ConnectionError(
                        f"Получено {downloaded} из {expected_end} байт диапазона")
                if not downloaded:
                    raise requests.exceptions.ConnectionError("Сервер вернул пустой видеофайл")
            part.replace(path)
            LOGGER.info("Download complete: %s bytes=%s", path, path.stat().st_size)
            progress_cb(100, "100%")
            return
        except RETRYABLE as error:
            last_error = error
            LOGGER.warning("Download interrupted attempt=%s/%s: %s: %s",
                           attempt, attempts, type(error).__name__, error)
            if attempt == attempts:
                break
            delay = min(8.0, 0.8 * 2 ** (attempt - 1)) * random.uniform(0.75, 1.25)
            progress_cb(0, f"Сеть прервана; повтор {attempt + 1}/{attempts} через {delay:.1f} с…")
            sleep(delay)

    raise RuntimeError(
        f"Не удалось скачать видео после {attempts} попыток: {last_error}"
    ) from last_error
