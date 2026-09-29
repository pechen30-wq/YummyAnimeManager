"""Bounded, resumable downloads for direct media URLs."""

import random
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
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


class RangeUnsupportedError(RuntimeError):
    pass


class RangeDownloadError(RuntimeError):
    pass


def download_file(url, path, headers, progress_cb, *, attempts=4, sleep=time.sleep,
                  session=None, cancelled=None):
    """Download into a sidecar, resuming only when the server proves the offset."""
    path = Path(path)
    part = path.with_name(path.name + ".part")
    part.unlink(missing_ok=True)  # A previous run may have used a different URL.
    last_error = None
    last_pct = -1

    for attempt in range(1, attempts + 1):
        if cancelled and cancelled():
            raise RuntimeError("Загрузка фрагмента отменена.")
        offset = part.stat().st_size if part.exists() else 0
        request_headers = dict(headers)
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        LOGGER.debug("Download request %s attempt=%s/%s offset=%s", safe_url(url), attempt, attempts, offset)
        try:
            get = session.get if session is not None else requests.get
            with get(url, headers=request_headers, stream=True,
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
                        if cancelled and cancelled():
                            raise RuntimeError("Загрузка фрагмента отменена.")
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
            if session is not None:
                session.close()  # Discard a broken pool before reconnecting.
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


def download_ranges(url, path, headers, progress, *, block_size=1024 * 1024,
                    attempts=4, sleep=time.sleep, workers=4, require_ranges=False):
    """Commit only complete, contiguous, size-validated HTTP ranges."""
    if workers > 1:
        return download_ranges_parallel(url,path,headers,progress,block_size=block_size,
                                        attempts=attempts,sleep=sleep,workers=min(4,workers),
                                        require_ranges=require_ranges)
    started = time.monotonic()
    path = Path(path)
    part = path.with_name(path.name + ".range.part")
    part.unlink(missing_ok=True)
    offset, total, validator = 0, None, None
    with requests.Session() as session:
        while total is None or offset < total:
            requested_end = offset + block_size - 1
            if total is not None:
                requested_end = min(requested_end, total - 1)
            last_error = None
            for attempt in range(1, attempts + 1):
                request_headers = {**headers, "Accept-Encoding": "identity",
                                   "Range": f"bytes={offset}-{requested_end}"}
                if validator:
                    request_headers["If-Range"] = validator
                try:
                    with session.get(url, headers=request_headers, stream=True,
                                     timeout=(15, 60)) as response:
                        response.raise_for_status()
                        if response.status_code == 200:
                            if require_ranges:
                                raise RangeUnsupportedError("Источник не поддерживает загрузку диапазонами.")
                            if offset:
                                raise RuntimeError("Источник изменился или перестал поддерживать Range; сборка остановлена.")
                            try:
                                expected = int(response.headers.get("Content-Length") or 0)
                            except ValueError:
                                expected = 0
                            if expected <= 0:
                                raise RuntimeError("Сервер не поддерживает Range и не сообщает размер MP4.")
                            response.close()
                            # Some servers ignore Range. Use the validated full-file downloader.
                            download_file(url, path, headers, progress, session=session,
                                          attempts=attempts, sleep=sleep)
                            if path.stat().st_size != expected:
                                path.unlink(missing_ok=True)
                                raise RuntimeError("Размер MP4 изменился во время загрузки.")
                            return
                        match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
                        if response.status_code != 206 or not match or match.group(3) == "*":
                            raise RuntimeError("Сервер вернул неверный Content-Range.")
                        start, end, size = map(int, match.groups())
                        if start != offset or end < start or end > requested_end or end >= size:
                            raise RuntimeError("Сервер вернул неверные границы Content-Range.")
                        if total is not None and total != size:
                            raise RuntimeError("Размер исходного файла изменился; сборка остановлена.")
                        etag = response.headers.get("ETag", "")
                        if validator and etag and validator != etag:
                            raise RuntimeError("Исходный файл изменился; сборка остановлена.")
                        if not validator and etag and not etag.startswith("W/"):
                            validator = etag
                        total = size
                        block = bytearray()
                        for chunk in response.iter_content(chunk_size=65536):
                            block.extend(chunk)
                            if len(block) > end - start + 1:
                                raise RuntimeError("Размер блока превышает Content-Range.")
                        if len(block) != end - start + 1:
                            raise requests.ConnectionError("Сервер оборвал загрузку блока MP4.")
                    with part.open("ab") as output:
                        output.write(block)
                    offset += len(block)
                    progress(min(99, int(offset * 100 / total)),
                             f"MP4 блоками · {offset/1048576:.1f}/{total/1048576:.1f} МБ")
                    break
                except (*RETRYABLE, requests.HTTPError) as error:
                    if isinstance(error, requests.HTTPError) and error.response is not None:
                        if error.response.status_code != 429 and error.response.status_code < 500:
                            raise
                    last_error = error
                    session.close()
                    LOGGER.warning("MP4 range interrupted offset=%s attempt=%s/%s: %s",
                                   offset, attempt, attempts, type(error).__name__)
                    if attempt < attempts:
                        progress(int(offset * 100 / total) if total else 0,
                                 f"Повтор блока MP4 {attempt + 1}/{attempts}…")
                        sleep(min(8, 0.8 * 2 ** (attempt - 1)))
            else:
                raise RangeDownloadError(f"Не удалось скачать блок MP4 после {attempts} попыток: {last_error}") from last_error
    if not total or offset != total or part.stat().st_size != total:
        raise RuntimeError("MP4 скачан не полностью.")
    part.replace(path)
    LOGGER.info("MP4 range download complete: %s bytes=%s elapsed=%.2fs",
                path, total, time.monotonic()-started)
    progress(100, "MP4 полностью скачан")


def download_ranges_parallel(url, path, headers, progress, *, block_size=1048576,
                             attempts=4, sleep=time.sleep, workers=4, require_ranges=False):
    """Bound memory/connections; write verified, disjoint ranges at their offsets."""
    if block_size < 1 or attempts < 1:
        raise ValueError("Неверные параметры блочной загрузки.")
    path = Path(path)
    part = path.with_name(path.name + ".range.part")
    part.unlink(missing_ok=True)
    started = time.monotonic()
    stopped = threading.Event()
    local = threading.local()
    sessions = []
    lock = threading.Lock()
    workers = max(1, min(4, workers))

    def read_block(session, start, end, total=None, validator=None):
        for attempt in range(1, attempts + 1):
            if stopped.is_set():
                raise RuntimeError("Загрузка блока отменена.")
            request_headers = {**headers, "Accept-Encoding": "identity",
                               "Range": f"bytes={start}-{end}"}
            if validator:
                request_headers["If-Range"] = validator
            try:
                with session.get(url,headers=request_headers,stream=True,timeout=(15,60)) as response:
                    response.raise_for_status()
                    if response.status_code == 200 and total is None:
                        return None
                    match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
                    if response.status_code != 206 or not match or match.group(3) == "*":
                        raise RuntimeError("Сервер вернул неверный Content-Range.")
                    left, right, size = map(int, match.groups())
                    if size <= 0 or left != start or right != min(end,size-1) or right < left:
                        raise RuntimeError("Сервер вернул неверные границы Content-Range.")
                    if total is not None and size != total:
                        raise RuntimeError("Размер исходного файла изменился; сборка остановлена.")
                    etag = response.headers.get("ETag", "")
                    if validator and etag and validator != etag:
                        raise RuntimeError("Исходный файл изменился; сборка остановлена.")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise RuntimeError("Сервер сжал диапазон MP4; проверка размера невозможна.")
                    block = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        if stopped.is_set():
                            raise RuntimeError("Загрузка блока отменена.")
                        block.extend(chunk)
                        if len(block) > right-left+1:
                            raise RuntimeError("Размер блока превышает Content-Range.")
                    if len(block) != right-left+1:
                        raise requests.ConnectionError("Сервер оборвал загрузку блока MP4.")
                return block,size,etag if etag and not etag.startswith("W/") else None
            except (*RETRYABLE,requests.HTTPError) as error:
                if isinstance(error,requests.HTTPError) and error.response is not None:
                    if error.response.status_code != 429 and error.response.status_code < 500:
                        raise
                session.close()
                LOGGER.warning("MP4 range interrupted offset=%s attempt=%s/%s: %s",
                               start,attempt,attempts,type(error).__name__)
                if attempt == attempts:
                    raise RangeDownloadError(f"Не удалось скачать блок MP4 после {attempts} попыток: {error}") from error
                delay = min(8,0.8*2**(attempt-1))
                if sleep is time.sleep:
                    stopped.wait(delay)
                else:
                    sleep(delay)

    with requests.Session() as session:
        first = read_block(session,0,block_size-1)
    if first is None:
        if require_ranges:
            raise RangeUnsupportedError("Источник не поддерживает загрузку диапазонами.")
        # A server without Range must not receive concurrent full-file requests.
        return download_ranges(url,path,headers,progress,block_size=block_size,
                               attempts=attempts,sleep=sleep,workers=1)
    initial,total,validator = first
    downloaded = len(initial)
    progress(min(99,int(downloaded*100/total)),"MP4 — параллельная загрузка блоков…")

    def fetch(start):
        if not hasattr(local,"session"):
            local.session = requests.Session()
            with lock:
                sessions.append(local.session)
        block,_,_ = read_block(local.session,start,min(start+block_size-1,total-1),total,validator)
        return start,block

    offsets = iter(range(downloaded,total,block_size))
    pool = ThreadPoolExecutor(max_workers=workers,thread_name_prefix="mp4")
    active = set()
    last_pct = -1
    try:
        with part.open("wb") as output:
            output.write(initial)
            for _ in range(workers):
                start = next(offsets,None)
                if start is not None:
                    active.add(pool.submit(fetch,start))
            while active:
                done,active = wait(active,return_when=FIRST_COMPLETED)
                # Validate all completed futures before adding more work.
                blocks = [future.result() for future in done]
                for start,block in blocks:
                    output.seek(start)
                    output.write(block)
                    downloaded += len(block)
                pct = min(99,int(downloaded*100/total))
                if pct != last_pct:
                    last_pct = pct
                    progress(pct,f"MP4 · {downloaded/1048576:.1f}/{total/1048576:.1f} МБ · {workers} потока")
                for _ in done:
                    start = next(offsets,None)
                    if start is not None:
                        active.add(pool.submit(fetch,start))
    finally:
        stopped.set()
        pool.shutdown(wait=True,cancel_futures=True)
        for session in sessions:
            session.close()
    if downloaded != total or part.stat().st_size != total:
        raise RuntimeError("MP4 скачан не полностью.")
    part.replace(path)
    LOGGER.info("MP4 range download complete: %s workers=%s bytes=%s elapsed=%.2fs",
                path,workers,total,time.monotonic()-started)
    progress(100,"MP4 полностью скачан")
