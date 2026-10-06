"""Bounded, resumable downloads for direct media URLs."""

import random
import hashlib
import json
import re
import time
import threading
import contextlib
import urllib.parse
from collections import defaultdict, deque
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import requests
from diagnostics import LOGGER, safe_url


@dataclass(frozen=True)
class SourceProbe:
    latency: float
    bytes_per_second: float
    expected_bytes: int
    sampled_bytes: int
    host: str

    @property
    def estimated_seconds(self):
        return self.latency + (self.expected_bytes or 64*1024*1024)/max(1,self.bytes_per_second)


def probe_media(url,headers,control=None,*,sample_limit=256*1024,budget=12,session=None):
    """Read at most one small media range and bounded HLS manifests."""
    from hls_download import playlist_variant
    started=time.monotonic()
    deadline=started+budget
    owned=session is None
    session=session or requests.Session()
    sampled=0

    def check():
        if control: control.check()
        if time.monotonic()>=deadline:
            raise TimeoutError("Source sample time budget exceeded")

    def request(target,manifest=False):
        nonlocal sampled
        check()
        request_headers=dict(headers or {})
        request_headers["Accept-Encoding"]="identity"
        if not manifest:
            request_headers["Range"]=f"bytes=0-{sample_limit-1}"
        begin=time.monotonic()
        timeout=max(0.1,min(4,deadline-begin))
        with (control.transfer(target) if control else contextlib.nullcontext()), session.get(
                target,headers=request_headers,stream=True,timeout=(min(3,timeout),timeout)) as response:
            response.raise_for_status()
            response_headers=response.headers.copy()
            final_url=response.url
            body=bytearray()
            first_byte=None
            for chunk in response.iter_content(16384):
                check()
                if not chunk: continue
                if first_byte is None: first_byte=time.monotonic()
                sampled+=len(chunk)
                if control: control.record_bytes(len(chunk))
                body.extend(chunk[:sample_limit-len(body)])
                if len(body)>=sample_limit:
                    if manifest:
                        raise ValueError("Manifest exceeds source probe size budget")
                    break
            if not body:
                raise ValueError("Empty source sample")
        elapsed=max(0.001,time.monotonic()-begin)
        return bytes(body),response_headers,final_url,(first_byte or begin)-begin,len(body)/elapsed

    try:
        path=urllib.parse.urlsplit(url).path.lower()
        if path.endswith(".mpd"):
            return None
        duration=None
        total_duration=None
        if path.endswith(".m3u8"):
            for _ in range(4):
                data,_metadata,base,_latency,_speed=request(url,manifest=True)
                body=data.decode("utf-8-sig")
                if not body.lstrip().startswith("#EXTM3U"):
                    raise ValueError("Invalid HLS manifest")
                if "#EXTINF:" in body:
                    durations=[float(value) for value in re.findall(r"(?m)^#EXTINF:([0-9.]+)",body)]
                    segments=[urllib.parse.urljoin(base,line.strip()) for line in body.splitlines()
                              if line.strip() and not line.startswith("#")]
                    if len(segments)!=len(durations) or not segments or "#EXT-X-BYTERANGE:" in body:
                        raise ValueError("Unsupported HLS sample layout")
                    index=max(range(len(durations)),key=lambda index:durations[index])
                    url=segments[index]
                    duration=durations[index]
                    total_duration=sum(durations)
                    break
                url=urllib.parse.urljoin(base,playlist_variant(body,audio_only=True))
            else:
                raise ValueError("HLS nesting exceeds source probe budget")
        data,metadata,final_url,latency,speed=request(url)
        content_type=metadata.get("Content-Type","").lower()
        if "text/" in content_type or "json" in content_type or "xml" in content_type:
            raise ValueError("Source returned a document instead of media")
        match=re.search(r"/(\d+)$",metadata.get("Content-Range",""))
        total=int(match.group(1)) if match else int(metadata.get("Content-Length") or 0)
        if duration and total_duration:
            total=int(total*total_duration/duration)
        return SourceProbe(latency,speed,total,sampled,urllib.parse.urlsplit(final_url).hostname or "")
    finally:
        if owned: session.close()


RETRYABLE = (
    ConnectionResetError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)$", re.I)


def retry_delay(error, attempt):
    """Honor bounded Retry-After for throttling; otherwise use backoff."""
    response = getattr(error, "response", None)
    if response is not None and response.status_code == 429:
        value = response.headers.get("Retry-After", "").strip()
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 0
        if seconds > 0:
            return min(60.0, seconds)
    return min(8.0, 0.8 * 2 ** (attempt - 1)) * random.uniform(0.75, 1.25)


class RangeUnsupportedError(RuntimeError):
    pass


class RangeDownloadError(RuntimeError):
    pass


class PauseDownload(Exception):
    pass


class StopDownload(Exception):
    pass


class DownloadControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._mode = "running"
        self._processes = set()
        self._global_slots = threading.BoundedSemaphore(12)
        self._host_slots = defaultdict(lambda: threading.BoundedSemaphore(8))
        self._active_transfers = 0
        self._received_bytes = 0
        self._recent_bytes = deque()
        self._host_errors = defaultdict(deque)
        self._host_cooldown = {}

    def report_host_error(self, url, error):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host:
            return
        now = time.monotonic()
        with self._lock:
            errors = self._host_errors[host]
            errors.append(now)
            while errors and now-errors[0] > 60:
                errors.popleft()
            if len(errors) >= 3:
                self._host_cooldown[host] = now + 60
                LOGGER.warning("CDN temporarily bypassed after repeated errors: host=%s error=%s",
                               host,type(error).__name__)

    @contextlib.contextmanager
    def transfer(self, url):
        """Limit the total and per-host number of simultaneous HTTP streams."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        with self._lock:
            host_slot = self._host_slots[host]
        while True:
            self.check()
            with self._lock:
                if self._host_cooldown.get(host,0) > time.monotonic():
                    raise RuntimeError("Сервер CDN временно исключён после повторных ошибок.")
            if host_slot.acquire(timeout=0.2):
                break
        try:
            while True:
                self.check()
                if self._global_slots.acquire(timeout=0.2):
                    break
            with self._lock:
                self._active_transfers += 1
            try:
                yield
            finally:
                with self._lock:
                    self._active_transfers -= 1
                self._global_slots.release()
        finally:
            host_slot.release()

    def record_bytes(self, count):
        if count <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self._received_bytes += count
            self._recent_bytes.append((now, count))
            while self._recent_bytes and now-self._recent_bytes[0][0] > 10:
                self._recent_bytes.popleft()

    def transfer_stats(self):
        with self._lock:
            now = time.monotonic()
            while self._recent_bytes and now-self._recent_bytes[0][0] > 10:
                self._recent_bytes.popleft()
            span = max(1.0, min(10.0, now-self._recent_bytes[0][0])) if self._recent_bytes else 1.0
            return self._active_transfers, sum(n for _,n in self._recent_bytes)/span, self._received_bytes

    def request(self, mode):
        if mode not in ("paused", "stopped"):
            raise ValueError(mode)
        with self._lock:
            self._mode = mode
            self._event.set()
            processes = tuple(self._processes)
        for process in processes:
            try:
                if process.poll() is None:
                    process.kill()
            except (OSError, AttributeError):
                pass

    def check(self):
        if self._event.is_set():
            with self._lock:
                mode = self._mode
            if mode == "stopped":
                raise StopDownload()
            raise PauseDownload()

    def wait(self, seconds):
        self._event.wait(seconds)
        self.check()

    def set_process(self, process):
        with self._lock:
            self._processes.add(process)
        try:
            self.check()
        except (PauseDownload, StopDownload):
            try:
                if process.poll() is None:
                    process.kill()
                process.wait()
            finally:
                self.clear_process(process)
                close = getattr(process.stdout, "close", None)
                if close:
                    close()
            raise

    def clear_process(self, process):
        with self._lock:
            self._processes.discard(process)


def download_file(url, path, headers, progress_cb, *, attempts=4, sleep=time.sleep,
                  session=None, cancelled=None, control=None, resume=False):
    """Download into a sidecar, resuming only when the server proves the offset."""
    path = Path(path)
    part = path.with_name(path.name + ".part")
    identity = part.with_name(part.name + ".url")
    fingerprint = hashlib.sha256(url.encode()).hexdigest()
    if not resume or not identity.is_file() or identity.read_text(encoding="ascii") != fingerprint:
        part.unlink(missing_ok=True)
    if resume:
        identity.write_text(fingerprint,encoding="ascii")
    last_error = None
    last_pct = -1

    for attempt in range(1, attempts + 1):
        if control:
            control.check()
        if cancelled and cancelled():
            raise RuntimeError("Загрузка фрагмента отменена.")
        offset = part.stat().st_size if part.exists() else 0
        request_headers = dict(headers)
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        LOGGER.debug("Download request %s attempt=%s/%s offset=%s", safe_url(url), attempt, attempts, offset)
        try:
            get = session.get if session is not None else requests.get
            with (control.transfer(url) if control else contextlib.nullcontext()), get(url, headers=request_headers, stream=True,
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
                        if control:
                            control.check()
                        if cancelled and cancelled():
                            raise RuntimeError("Загрузка фрагмента отменена.")
                        if not chunk:
                            continue
                        output.write(chunk)
                        if control: control.record_bytes(len(chunk))
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
            identity.unlink(missing_ok=True)
            LOGGER.info("Download complete: %s bytes=%s", path, path.stat().st_size)
            progress_cb(100, "100%")
            return
        except (*RETRYABLE, requests.HTTPError) as error:
            if isinstance(error, requests.HTTPError) and error.response is not None:
                if error.response.status_code != 429 and error.response.status_code < 500:
                    raise
            if control: control.report_host_error(url,error)
            if session is not None:
                session.close()  # Discard a broken pool before reconnecting.
            last_error = error
            LOGGER.warning("Download interrupted attempt=%s/%s: %s: %s",
                           attempt, attempts, type(error).__name__, error)
            if attempt == attempts:
                break
            delay = retry_delay(error, attempt)
            progress_cb(0, f"Сеть прервана; повтор {attempt + 1}/{attempts} через {delay:.1f} с…")
            sleep(delay)
            if control:
                control.check()

    raise RuntimeError(
        f"Не удалось скачать видео после {attempts} попыток: {last_error}"
    ) from last_error


def download_ranges(url, path, headers, progress, *, block_size=1024 * 1024,
                    attempts=4, sleep=time.sleep, workers=4, require_ranges=False,
                    control=None, resume=False, force_sequential=False):
    """Commit only complete, contiguous, size-validated HTTP ranges."""
    if workers > 1 or (resume and not force_sequential):
        return download_ranges_parallel(url,path,headers,progress,block_size=block_size,
                                        attempts=attempts,sleep=sleep,workers=min(4,workers),
                                        require_ranges=require_ranges,control=control,resume=resume)
    started = time.monotonic()
    path = Path(path)
    part = path.with_name(path.name + ".range.part")
    part.unlink(missing_ok=True)
    offset, total, validator = 0, None, None
    with requests.Session() as session:
        while total is None or offset < total:
            if control:
                control.check()
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
                    with (control.transfer(url) if control else contextlib.nullcontext()), session.get(url, headers=request_headers, stream=True,
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
                                          attempts=attempts, sleep=sleep,control=control,resume=resume)
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
                            if control:
                                control.check()
                            block.extend(chunk)
                            if control: control.record_bytes(len(chunk))
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
                    if control: control.report_host_error(url,error)
                    last_error = error
                    session.close()
                    LOGGER.warning("MP4 range interrupted offset=%s attempt=%s/%s: %s",
                                   offset, attempt, attempts, type(error).__name__)
                    if attempt < attempts:
                        progress(int(offset * 100 / total) if total else 0,
                                 f"Повтор блока MP4 {attempt + 1}/{attempts}…")
                        sleep(retry_delay(error, attempt))
                        if control:
                            control.check()
            else:
                raise RangeDownloadError(f"Не удалось скачать блок MP4 после {attempts} попыток: {last_error}") from last_error
    if not total or offset != total or part.stat().st_size != total:
        raise RuntimeError("MP4 скачан не полностью.")
    part.replace(path)
    LOGGER.info("MP4 range download complete: %s bytes=%s elapsed=%.2fs",
                path, total, time.monotonic()-started)
    progress(100, "MP4 полностью скачан")


def download_ranges_parallel(url, path, headers, progress, *, block_size=1048576,
                             attempts=4, sleep=time.sleep, workers=4, require_ranges=False,
                             control=None, resume=False):
    """Bound memory/connections; write verified, disjoint ranges at their offsets."""
    if block_size < 1 or attempts < 1:
        raise ValueError("Неверные параметры блочной загрузки.")
    path = Path(path)
    part = path.with_name(path.name + ".range.part")
    ledger_path = part.with_name(part.name + ".json")
    saved = None
    if resume and part.is_file() and ledger_path.is_file():
        try:
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError,ValueError,TypeError):
            saved = None
    if not saved:
        part.unlink(missing_ok=True)
        ledger_path.unlink(missing_ok=True)
    started = time.monotonic()
    stopped = threading.Event()
    local = threading.local()
    sessions = []
    lock = threading.Lock()
    workers = max(1, min(4, workers))

    def read_block(session, start, end, total=None, validator=None):
        for attempt in range(1, attempts + 1):
            if control:
                control.check()
            if stopped.is_set():
                raise RuntimeError("Загрузка блока отменена.")
            request_headers = {**headers, "Accept-Encoding": "identity",
                               "Range": f"bytes={start}-{end}"}
            if validator:
                request_headers["If-Range"] = validator
            try:
                with (control.transfer(url) if control else contextlib.nullcontext()), session.get(url,headers=request_headers,stream=True,timeout=(15,60)) as response:
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
                        if control:
                            control.check()
                        if stopped.is_set():
                            raise RuntimeError("Загрузка блока отменена.")
                        block.extend(chunk)
                        if control: control.record_bytes(len(chunk))
                        if len(block) > right-left+1:
                            raise RuntimeError("Размер блока превышает Content-Range.")
                    if len(block) != right-left+1:
                        raise requests.ConnectionError("Сервер оборвал загрузку блока MP4.")
                return block,size,etag if etag and not etag.startswith("W/") else None
            except (*RETRYABLE,requests.HTTPError) as error:
                if isinstance(error,requests.HTTPError) and error.response is not None:
                    if error.response.status_code != 429 and error.response.status_code < 500:
                        raise
                if control: control.report_host_error(url,error)
                session.close()
                LOGGER.warning("MP4 range interrupted offset=%s attempt=%s/%s: %s",
                               start,attempt,attempts,type(error).__name__)
                if attempt == attempts:
                    raise RangeDownloadError(f"Не удалось скачать блок MP4 после {attempts} попыток: {error}") from error
                delay = retry_delay(error, attempt)
                if sleep is time.sleep:
                    if control:
                        control.wait(delay)
                    else:
                        stopped.wait(delay)
                else:
                    sleep(delay)

    if control:
        control.check()
    with requests.Session() as session:
        first = read_block(session,0,block_size-1)
    if first is None:
        if require_ranges:
            raise RangeUnsupportedError("Источник не поддерживает загрузку диапазонами.")
        # A server without Range must not receive concurrent full-file requests.
        return download_ranges(url,path,headers,progress,block_size=block_size,
                               attempts=attempts,sleep=sleep,workers=1,control=control,
                               resume=resume,force_sequential=True)
    initial,total,validator = first
    first_hash = hashlib.sha256(initial).hexdigest()
    blocks = {"0":first_hash}
    if (saved and saved.get("total") == total and saved.get("block_size") == block_size
            and saved.get("blocks",{}).get("0") == first_hash
            and (not saved.get("validator") or not validator or saved["validator"] == validator)):
        with part.open("rb") as source:
            for key, expected in saved.get("blocks",{}).items():
                try:
                    start = int(key)
                    if start < 0 or start >= total or start % block_size:
                        continue
                    source.seek(start)
                    content = source.read(min(block_size,total-start))
                    if hashlib.sha256(content).hexdigest() == expected:
                        blocks[key] = expected
                except (OSError,ValueError,TypeError):
                    continue
    else:
        part.unlink(missing_ok=True)
    downloaded = sum(min(block_size,total-int(start)) for start in blocks)

    def save_ledger():
        state = {"total":total,"block_size":block_size,"validator":validator,"blocks":blocks}
        temporary = ledger_path.with_name(ledger_path.name + ".tmp")
        temporary.write_text(json.dumps(state,separators=(",", ":")),encoding="utf-8")
        temporary.replace(ledger_path)
    progress(min(99,int(downloaded*100/total)),"MP4 — параллельная загрузка блоков…")

    def fetch(start):
        if not hasattr(local,"session"):
            local.session = requests.Session()
            with lock:
                sessions.append(local.session)
        block,_,_ = read_block(local.session,start,min(start+block_size-1,total-1),total,validator)
        return start,block

    offsets = iter(start for start in range(block_size,total,block_size) if str(start) not in blocks)
    pool = ThreadPoolExecutor(max_workers=workers,thread_name_prefix="mp4")
    active = set()
    last_pct = -1
    try:
        with part.open("r+b" if part.exists() else "w+b") as output:
            output.seek(0)
            output.write(initial)
            output.flush()
            save_ledger()
            for _ in range(workers):
                start = next(offsets,None)
                if start is not None:
                    active.add(pool.submit(fetch,start))
            while active:
                done,active = wait(active,timeout=0.25 if control else None,
                                   return_when=FIRST_COMPLETED)
                if control: control.check()
                # Validate all completed futures before adding more work.
                completed_blocks = [future.result() for future in done]
                for start,block in completed_blocks:
                    output.seek(start)
                    output.write(block)
                    output.flush()
                    blocks[str(start)] = hashlib.sha256(block).hexdigest()
                    save_ledger()
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
    ledger_path.unlink(missing_ok=True)
    LOGGER.info("MP4 range download complete: %s workers=%s bytes=%s elapsed=%.2fs",
                path,workers,total,time.monotonic()-started)
    progress(100,"MP4 полностью скачан")
