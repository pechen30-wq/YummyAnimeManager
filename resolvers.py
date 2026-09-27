import base64
import html
import json
import re
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import requests

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

DIRECT_MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi")

PROVIDER_LABELS = {
    "direct": "Прямой файл",
    "cvh": "CVH",
    "kodik": "Kodik",
    "aksor": "Aksor",
    "sibnet": "Sibnet",
    "rutube": "Rutube",
    "vk": "VK Video",
    "zedfilm": "Zedfilm",
    "alloha": "Alloha",
    "unknown": "Неизвестный iframe",
}


@dataclass
class StreamResult:
    url: str
    source: str
    qualities: dict
    headers: dict
    quality: str = "auto"
    session: str = ""
    resolver_base: str = ""

    @property
    def is_manifest(self):
        path = urllib.parse.urlparse(self.url).path.lower()
        return path.endswith(".m3u8") or path.endswith(".mpd")


def normalize_http_url(url):
    url = str(url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if re.match(r"^https?://", url, re.I):
        return url
    return "https://" + url.lstrip("/")


def direct_media_url(url):
    try:
        p = urllib.parse.urlparse(str(url or ""))
        return p.scheme in ("http", "https") and p.path.lower().endswith(DIRECT_MEDIA_EXTS)
    except Exception:
        return False


def provider_kind(item):
    value = f"{getattr(item, 'player', '')} {getattr(item, 'iframe_url', '')}".lower()
    url = getattr(item, "iframe_url", "")
    if direct_media_url(url):
        return "direct"
    if "alloha" in value:
        return "alloha"
    if "kodik" in value:
        return "kodik"
    if "iframecvh" in value or "cdnvideohub" in value or "cdn-iframe" in value or re.search(r"\bcvh\b", value):
        return "cvh"
    if "aksor.tv" in value or "aksor" in value:
        return "aksor"
    if "sibnet.ru" in value or "sibnet" in value:
        return "sibnet"
    if "rutube.ru" in value or "rutube" in value:
        return "rutube"
    if "iframevk" in value or "vkvideo" in value or "vk.com" in value or re.search(r"\bvk\b", value):
        return "vk"
    if "zedfilm.ru" in value or "hlamer.ru" in value or "zedfilm" in value:
        return "zedfilm"
    return "unknown"


def provider_label(item):
    return PROVIDER_LABELS.get(provider_kind(item), provider_kind(item))


def quality_number(label):
    m = re.search(r"(\d{3,4})", str(label or ""))
    return int(m.group(1)) if m else -1


def choose_stream(result, wanted):
    qualities = dict(result.qualities or {})
    if not qualities:
        return result.quality or "auto", result.url

    if wanted == "Лучшее":
        numeric = [(quality_number(k), k, v) for k, v in qualities.items() if quality_number(k) >= 0]
        if numeric:
            _, label, url = max(numeric)
            return label, url
        if "auto" in qualities:
            return "auto", qualities["auto"]
        label = next(reversed(qualities))
        return label, qualities[label]

    if wanted in qualities:
        return wanted, qualities[wanted]

    if len(qualities) == 1 and "auto" in qualities:
        return "auto", qualities["auto"]

    available = ", ".join(qualities)
    raise RuntimeError(f"нет качества {wanted}; доступны: {available}")


def find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return str(exe)
    except Exception:
        pass
    return ""


def _request(session, method, url, **kwargs):
    kwargs.setdefault("timeout", 30)
    r = session.request(method, url, **kwargs)
    r.raise_for_status()
    return r


def system_proxy_for(url="https://example.com/"):
    """Return system/environment HTTP proxy for FFmpeg, if configured."""
    try:
        proxies = urllib.request.getproxies() or {}
    except Exception:
        proxies = {}
    scheme = urllib.parse.urlparse(str(url or "")).scheme.lower()
    value = proxies.get(scheme) or proxies.get("https") or proxies.get("http") or ""
    value = str(value or "").strip()
    if value and "://" not in value:
        value = "http://" + value
    return value


def _stream_probe(session, url, headers=None, timeout=10):
    """Verify DNS/TLS/HTTP and first bytes of a media URL."""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", CHROME_UA)
    headers.setdefault("Range", "bytes=0-2047")
    try:
        response = session.get(
            url,
            headers=headers,
            stream=True,
            timeout=(6, timeout),
            allow_redirects=True,
        )
        if response.status_code not in (200, 206):
            msg = f"HTTP {response.status_code}"
            response.close()
            return False, url, msg
        final_url = response.url or url
        try:
            next(response.iter_content(chunk_size=2048), b"")
        finally:
            response.close()
        return True, final_url, ""
    except Exception as e:
        return False, url, str(e)


def _extract_meta_video_url(page):
    m = re.search(
        r'<meta[^>]+name=["\']video_url["\'][^>]+content=["\']([^"\']+)["\']',
        str(page or ""),
        re.I,
    )
    return str(m.group(1) or "").strip() if m else ""


def _aksor_quality_map(payload):
    raw = (payload or {}).get("qualities") or {}
    mapping = {
        "360p": "q360",
        "480p": "q480",
        "720p": "q720",
        "1080p": "q1080",
        "1440p": "q2k",
        "2160p": "q4k",
    }
    result = {}
    for label, key in mapping.items():
        value = str(raw.get(key) or "").strip().replace(" ", "%20")
        if value and value.lower() != "null":
            result[label] = value
    return result


def _decode_html_url(value):
    value = html.unescape(str(value or ""))
    replacements = {
        r"\/": "/",
        r"\u0026": "&",
        r"\u003a": ":",
        r"\u003d": "=",
        r"\u002f": "/",
        r"\u002d": "-",
        r"\x26": "&",
    }
    for old, new in replacements.items():
        value = re.sub(re.escape(old), new, value, flags=re.I)
    return value


def _parse_hls_qualities(master, base_url):
    found = {}
    pending = None
    for raw in str(master or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", line, re.I)
            pending = f"{m.group(1)}p" if m else "auto"
            continue
        if pending and not line.startswith("#"):
            found[pending] = urllib.parse.urljoin(base_url, line)
            pending = None
    return dict(sorted(found.items(), key=lambda kv: quality_number(kv[0])))


def _episode_number(item):
    raw = str(getattr(item, "number", "") or "").strip()
    m = re.search(r"\d+(?:\.\d+)?", raw)
    if m:
        try:
            return float(m.group())
        except Exception:
            pass
    try:
        return float(getattr(item, "index", 0) or 0)
    except Exception:
        return 0.0


def _same_episode(a, b):
    try:
        return abs(float(a) - float(b)) < 0.001
    except Exception:
        return str(a).strip() == str(b).strip()


def _norm_text(value):
    return re.sub(r"[^a-zа-яё0-9]+", "", str(value or "").lower(), flags=re.I)


class PlayerResolver:
    def __init__(self, config=None):
        self.config = config or {}
        self.session = requests.Session()

    def resolve(self, item):
        kind = provider_kind(item)
        if kind == "direct":
            url = normalize_http_url(item.iframe_url)
            return StreamResult(url, "direct", {"auto": url}, {"User-Agent": CHROME_UA})
        resolver = getattr(self, f"resolve_{kind}", None)
        if resolver:
            return resolver(item)
        raise RuntimeError("Для этого iframe не найден поддерживаемый resolver.")

    def resolve_cvh(self, item):
        full_url = normalize_http_url(item.iframe_url)
        parsed = urllib.parse.urlparse(full_url)
        q = urllib.parse.parse_qs(parsed.query)

        def q1(*names):
            for name in names:
                vals = q.get(name)
                if vals and vals[0] != "":
                    return vals[0]
            return None

        anime_id = q1("anime_id", "animeId", "id")
        episode = q1("episode", "ep")
        dubbing_code = q1("dubbing_code")
        dubbing_label = q1("dubbing", "voiceStudio", "studio") or getattr(item, "dubbing", "")

        m = re.search(r"/cdn-iframe/(\d+)/(\d+)/(\d+(?:\.\d+)?)", parsed.path, re.I)
        if m:
            anime_id = anime_id or m.group(1)
            episode = episode or m.group(3)
        if not anime_id:
            m = re.search(r"/(\d{3,})(?:/|$)", parsed.path)
            if m:
                anime_id = m.group(1)
        if not anime_id:
            raise RuntimeError("CVH: anime id не найден.")

        try:
            episode = float(episode) if episode is not None else _episode_number(item)
        except Exception:
            episode = _episode_number(item)

        headers = {
            "Referer": "https://ru.yummyani.me/",
            "User-Agent": CHROME_UA,
            "Accept": "application/json",
        }
        playlist_url = "https://plapi.cdnvideohub.com/api/v1/player/sv/playlist"
        payload = _request(
            self.session,
            "GET",
            playlist_url,
            params={"pub": "745", "id": str(anime_id), "aggr": "mali"},
            headers=headers,
        ).json()
        items = payload.get("items") if isinstance(payload, dict) else None
        items = items if isinstance(items, list) else []
        serial = not (isinstance(payload, dict) and payload.get("isSerial") is False)
        candidates = [x for x in items if not serial or x.get("episode") is None or _same_episode(x.get("episode"), episode)]
        if not candidates:
            raise RuntimeError("CVH: серия не найдена.")

        expected = _norm_text(dubbing_code or dubbing_label or getattr(item, "dubbing", ""))
        selected = None
        if expected:
            for cand in candidates:
                studio = _norm_text(cand.get("voiceStudio") or "")
                voice_type = _norm_text(" ".join(filter(None, [cand.get("voiceType"), cand.get("voiceStudio")])))
                if studio == expected or voice_type == expected or (studio and (studio in expected or expected in studio)):
                    selected = cand
                    break
        selected = selected or candidates[0]
        vk_id = selected.get("vkId") or selected.get("vk_id") or selected.get("videoId") or selected.get("id")
        if not vk_id:
            raise RuntimeError("CVH: vkId не найден.")

        video = _request(
            self.session,
            "GET",
            f"https://plapi.cdnvideohub.com/api/v1/player/sv/video/{urllib.parse.quote(str(vk_id))}",
            headers=headers,
        ).json()
        sources = video.get("sources") or {}
        failover_host = str(video.get("failoverHost") or "").strip()
        mapping = {
            "240p": "mpegLowestUrl",
            "360p": "mpegLowUrl",
            "480p": "mpegMediumUrl",
            "720p": "mpegHighUrl",
            "1080p": "mpegFullHdUrl",
        }
        qualities = {}
        for label, key in mapping.items():
            url = str(sources.get(key) or "").strip()
            if not url:
                continue
            if failover_host:
                try:
                    p = urllib.parse.urlparse(url)
                    if p.scheme == "https" and re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", p.hostname or ""):
                        url = p._replace(netloc=failover_host + ((":" + str(p.port)) if p.port else "")).geturl()
                except Exception:
                    pass
            qualities[label] = url
        if not qualities:
            raise RuntimeError("CVH: прямые MP4 не найдены.")
        label = max(qualities, key=quality_number)
        return StreamResult(qualities[label], "cvh", qualities, headers, label)

    def resolve_kodik(self, item):
        full_url = normalize_http_url(item.iframe_url)
        headers = {"User-Agent": CHROME_UA, "Referer": "https://yani.tv/", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
        page = _request(self.session, "GET", full_url, headers=headers).text.replace("\n", "").replace("\r", "")

        def first(pattern):
            m = re.search(pattern, page, re.I)
            return m.group(1) if m else ""

        url_params_raw = first(r"\burlParams\s*=\s*'([^']+)'")
        p_type = first(r"\b(?:videoInfo|vInfo)\.type\s*=\s*'([^']+)'")
        p_hash = first(r"\b(?:videoInfo|vInfo)\.hash\s*=\s*'([^']+)'")
        p_id = first(r"\b(?:videoInfo|vInfo)\.id\s*=\s*'([^']+)'")
        player_src = first(r'src="((?:(?:https?:)?//[^"]+)?/assets/js/app\.player_single[^"]+)"')
        if not all([url_params_raw, p_type, p_hash, p_id, player_src]):
            raise RuntimeError("Kodik: данные плеера не найдены.")
        try:
            url_params = json.loads(url_params_raw)
        except Exception as e:
            raise RuntimeError(f"Kodik: urlParams не разобраны: {e}")

        parsed = urllib.parse.urlparse(full_url)
        iframe_origin = f"{parsed.scheme}://{parsed.netloc}"
        player_script_url = urllib.parse.urljoin(iframe_origin + "/", player_src)
        script = _request(self.session, "GET", player_script_url, headers={"User-Agent": CHROME_UA, "Referer": full_url}).text
        endpoint_path = self._kodik_endpoint(script)
        player_origin = player_script_url.split("/assets/js/")[0]
        endpoint_url = urllib.parse.urljoin(player_origin + "/", endpoint_path.lstrip("/"))

        data = {
            "d": url_params.get("d", ""),
            "d_sign": url_params.get("d_sign", ""),
            "pd": url_params.get("pd", ""),
            "pd_sign": url_params.get("pd_sign", ""),
            "ref": url_params.get("ref", ""),
            "ref_sign": url_params.get("ref_sign", ""),
            "bad_user": "true",
            "cdn_is_working": "true",
            "type": p_type,
            "hash": p_hash,
            "id": p_id,
            "info": "{}",
        }
        payload = _request(
            self.session,
            "POST",
            endpoint_url,
            data=data,
            headers={
                "User-Agent": CHROME_UA,
                "Referer": full_url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).json()
        links = payload.get("links") or {}
        qualities = {}
        for q in (240, 360, 480, 720, 1080):
            rows = links.get(str(q))
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                continue
            decoded = self._decode_kodik_src(rows[0].get("src"))
            if decoded:
                actual, decoded = self._kodik_quality_url(decoded, q)
                qualities[f"{actual}p"] = decoded
        if not qualities:
            raise RuntimeError("Kodik: HLS-ссылки не найдены.")
        label = max(qualities, key=quality_number)
        return StreamResult(qualities[label], "kodik", qualities, {"User-Agent": CHROME_UA, "Referer": full_url}, label)

    @staticmethod
    def _kodik_endpoint(script):
        for encoded in re.findall(r'atob\("([A-Za-z0-9+/=]+)"\)', script or ""):
            try:
                decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
            except Exception:
                continue
            if decoded.startswith("/") and not decoded.startswith("//") and len(decoded) <= 10:
                return decoded
        return "/ftor"

    @staticmethod
    def _decode_kodik_src(src):
        if not src:
            return ""
        if "//" in str(src):
            return normalize_http_url(src)
        out = []
        for ch in str(src):
            if "a" <= ch <= "z":
                out.append(chr((ord(ch) - 97 + 18) % 26 + 97))
            elif "A" <= ch <= "Z":
                out.append(chr((ord(ch) - 65 + 18) % 26 + 65))
            else:
                out.append(ch)
        value = "".join(out)
        value += "=" * ((4 - len(value) % 4) % 4)
        try:
            return normalize_http_url(base64.b64decode(value).decode("utf-8", errors="replace"))
        except Exception:
            return ""

    def _kodik_quality_url(self, url, expected):
        m = re.search(r"/(\d+)\.mp4:hls:manifest\.m3u8(?=$|[?#])", url, re.I)
        actual = int(m.group(1)) if m else 0
        if not actual or actual == expected or expected <= actual:
            return actual or expected, url
        repaired = re.sub(r"/(\d+)\.mp4:hls:manifest\.m3u8(?=$|[?#])", f"/{expected}.mp4:hls:manifest.m3u8", url, flags=re.I)
        try:
            r = self.session.head(repaired, headers={"User-Agent": CHROME_UA}, timeout=7, allow_redirects=True)
            if r.ok:
                return expected, repaired
        except Exception:
            pass
        return actual, url

    def resolve_aksor(self, item):
        """
        Aksor can return a dead CDN edge for one episode while neighbouring
        episodes work. Validate the returned URLs, refresh the API, then use
        the same player-page fallback strategy as YummyTV.
        """
        full_url = normalize_http_url(item.iframe_url)
        parts = [x for x in urllib.parse.urlparse(full_url).path.split("/") if x]

        video_hash = ""
        if "video" in parts:
            idx = parts.index("video")
            if idx + 1 < len(parts):
                video_hash = parts[idx + 1]
        if not video_hash and parts:
            video_hash = parts[-1]
        if not video_hash:
            raise RuntimeError("Aksor: hash видео не найден.")

        api_headers = {
            "User-Agent": CHROME_UA,
            "Referer": full_url,
            "Accept": "application/json",
        }
        stream_headers = {
            "User-Agent": CHROME_UA,
            "Referer": full_url,
        }

        raw_candidates = {}
        probe_errors = []

        # Try a fresh API response up to three times. The timestamp avoids a
        # cached response that keeps pointing at the same dead CDN edge.
        for attempt in range(3):
            try:
                response = _request(
                    self.session,
                    "GET",
                    f"https://player.aksor.tv/api/video/{urllib.parse.quote(video_hash)}",
                    params={"_": int(time.time() * 1000)},
                    headers=api_headers,
                )
                candidate_map = _aksor_quality_map(response.json())
                raw_candidates.update(candidate_map)

                reachable = {}
                for label, url in candidate_map.items():
                    ok, final_url, err = _stream_probe(
                        self.session,
                        url,
                        headers=stream_headers,
                        timeout=12,
                    )
                    if ok:
                        reachable[label] = final_url
                    else:
                        probe_errors.append(f"{label}: {err}")

                if reachable:
                    label = max(reachable, key=quality_number)
                    return StreamResult(
                        reachable[label],
                        "aksor",
                        reachable,
                        stream_headers,
                        label,
                    )
            except Exception as e:
                probe_errors.append(f"API {attempt + 1}: {e}")

            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))

        # Fallback 1: inspect player HTML for a direct video_url.
        page = ""
        try:
            page = _request(
                self.session,
                "GET",
                full_url,
                headers={
                    "Referer": "https://yani.tv/",
                    "User-Agent": CHROME_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            ).text

            meta_url = _extract_meta_video_url(page)
            if meta_url and "{{" not in meta_url:
                meta_url = normalize_http_url(meta_url.replace(" ", "%20"))
                ok, final_url, err = _stream_probe(
                    self.session,
                    meta_url,
                    headers=stream_headers,
                    timeout=12,
                )
                if ok:
                    return StreamResult(
                        final_url,
                        "aksor",
                        {"auto": final_url},
                        stream_headers,
                        "auto",
                    )
                probe_errors.append(f"video_url: {err}")
        except Exception as e:
            probe_errors.append(f"player page: {e}")

        # Fallback 2: discover a changed /api path from the player scripts.
        if page:
            script_urls = []
            for sm in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', page, re.I):
                script_urls.append(
                    urllib.parse.urljoin("https://player.aksor.tv/", sm.group(1))
                )

            for script_url in script_urls:
                try:
                    script = _request(
                        self.session,
                        "GET",
                        script_url,
                        headers={
                            "Referer": full_url,
                            "User-Agent": CHROME_UA,
                            "Accept": "*/*",
                        },
                    ).text
                    pm = re.search(r'["\']([^"\']*/api)["\']', script)
                    if not pm:
                        continue

                    api_path = pm.group(1)
                    if api_path.startswith("http"):
                        alt_api = api_path.rstrip("/") + f"/video/{urllib.parse.quote(video_hash)}"
                    else:
                        alt_api = urllib.parse.urljoin(
                            "https://player.aksor.tv/",
                            api_path.lstrip("/") + f"/video/{urllib.parse.quote(video_hash)}",
                        )

                    payload = _request(
                        self.session,
                        "GET",
                        alt_api,
                        params={"_": int(time.time() * 1000)},
                        headers=api_headers,
                    ).json()

                    alt_map = _aksor_quality_map(payload)
                    reachable = {}
                    for label, url in alt_map.items():
                        ok, final_url, err = _stream_probe(
                            self.session,
                            url,
                            headers=stream_headers,
                            timeout=12,
                        )
                        if ok:
                            reachable[label] = final_url
                        else:
                            probe_errors.append(f"fallback {label}: {err}")

                    if reachable:
                        label = max(reachable, key=quality_number)
                        return StreamResult(
                            reachable[label],
                            "aksor",
                            reachable,
                            stream_headers,
                            label,
                        )
                except Exception as e:
                    probe_errors.append(f"fallback script: {e}")

        hosts = sorted({
            urllib.parse.urlparse(url).hostname or ""
            for url in raw_candidates.values()
            if url
        })
        hosts_text = ", ".join(x for x in hosts if x) or "Aksor CDN"
        detail = "; ".join(probe_errors[-4:])

        raise RuntimeError(
            "Aksor: CDN для этой серии сейчас недоступен "
            f"({hosts_text}). Приложение трижды обновило ссылку и проверило "
            "fallback плеера, но рабочего потока нет."
            + (f" Последняя ошибка: {detail}" if detail else "")
        )

    def resolve_sibnet(self, item):
        full_url = normalize_http_url(item.iframe_url)
        page = _request(
            self.session,
            "GET",
            full_url,
            headers={
                "Referer": "https://yani.tv/",
                "User-Agent": CHROME_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ).text
        stream = ""
        for pat in (
            r'player\.src\s*\(\s*\[\s*\{\s*src\s*:\s*["\']([^"\']+)["\']',
            r'<source[^>]+src\s*=\s*["\']([^"\']+)["\']',
        ):
            m = re.search(pat, page, re.I)
            if m:
                stream = m.group(1)
                break
        stream = urllib.parse.urljoin(full_url, _decode_html_url(stream))
        if not stream:
            raise RuntimeError("Sibnet: MP4 не найден.")
        headers = {"Referer": full_url, "Origin": "https://video.sibnet.ru", "User-Agent": CHROME_UA}
        try:
            rr = self.session.get(stream, headers={**headers, "Range": "bytes=0-1"}, stream=True, timeout=15, allow_redirects=True)
            if rr.ok or rr.status_code == 206:
                stream = rr.url or stream
            rr.close()
        except Exception:
            pass
        return StreamResult(stream, "sibnet", {"auto": stream}, headers, "auto")

    def resolve_rutube(self, item):
        full_url = normalize_http_url(item.iframe_url)
        m = re.search(r"([a-f0-9]{32})", full_url, re.I)
        if not m:
            raise RuntimeError("Rutube: video id не найден.")
        headers = {"Referer": full_url, "Origin": "https://rutube.ru", "User-Agent": CHROME_UA, "Accept": "*/*"}
        payload = _request(
            self.session,
            "GET",
            f"https://rutube.ru/api/play/options/{m.group(1)}/?no_404=true",
            headers=headers,
        ).json()
        balancer = payload.get("video_balancer") or {}
        master_url = str(balancer.get("m3u8") or balancer.get("default") or "").strip()
        if not master_url:
            raise RuntimeError("Rutube: HLS не найден.")
        qualities = {}
        try:
            qualities = _parse_hls_qualities(_request(self.session, "GET", master_url, headers=headers).text, master_url)
        except Exception:
            pass
        if not qualities:
            qualities = {"auto": master_url}
        numeric = [k for k in qualities if quality_number(k) >= 0]
        label = max(numeric, key=quality_number) if numeric else "auto"
        return StreamResult(qualities[label], "rutube", qualities, headers, label)

    @staticmethod
    def _vk_pair(url):
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        combined = (q.get("id") or [""])[0]
        m = re.match(r"^(-?\d+)_(\d+)$", combined)
        if m:
            return m.group(1), m.group(2)
        oid = (q.get("oid") or [""])[0]
        vid = (q.get("id") or [""])[0]
        if re.match(r"^-?\d+$", oid) and re.match(r"^\d+$", vid):
            return oid, vid
        m = re.search(r"(?:video|clip)(-?\d+)_(\d+)", url, re.I)
        return (m.group(1), m.group(2)) if m else None

    @staticmethod
    def _vk_qualities(page, base_url):
        decoded = _decode_html_url(page)
        qualities = {}
        for m in re.finditer(r'["\']?(?:url|mp4_)(2160|1440|1080|720|480|360|240)["\']?\s*[:=]\s*["\']([^"\']+)["\']', decoded, re.I):
            url = urllib.parse.urljoin(base_url, _decode_html_url(m.group(2)))
            if url.startswith("http"):
                qualities[f"{m.group(1)}p"] = url
        for m in re.finditer(r'["\']?(?:hls_ondemand|hls_fmp4|hls|url_hls|url)["\']?\s*[:=]\s*["\']([^"\']+)["\']', decoded, re.I):
            url = urllib.parse.urljoin(base_url, _decode_html_url(m.group(1)))
            if ".m3u8" in url:
                qualities.setdefault("auto", url)
        if not qualities:
            for m in re.finditer(r'https?://[^\s"\'<>\\]+\.(?:m3u8|mp4)(?:\?[^\s"\'<>\\]*)?', decoded, re.I):
                qualities.setdefault("auto", _decode_html_url(m.group(0)))
        return qualities

    def resolve_vk(self, item):
        full_url = normalize_http_url(item.iframe_url)
        pair = self._vk_pair(full_url)
        if pair:
            player_url = f"https://vk.com/video_ext.php?oid={urllib.parse.quote(pair[0])}&id={urllib.parse.quote(pair[1])}&hd=1"
        elif "video_ext.php" in full_url.lower():
            player_url = full_url
        else:
            wrapper = _request(self.session, "GET", full_url, headers={"User-Agent": CHROME_UA, "Referer": full_url}).text
            pair = self._vk_pair(_decode_html_url(wrapper))
            player_url = (
                f"https://vk.com/video_ext.php?oid={urllib.parse.quote(pair[0])}&id={urllib.parse.quote(pair[1])}&hd=1"
                if pair else full_url
            )
        headers = {"User-Agent": CHROME_UA, "Referer": full_url, "Origin": "https://vk.com", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        page = _request(self.session, "GET", player_url, headers=headers).text
        if re.search(r"embedErrorCallback\s*\?\.?\s*\(\s*8\s*\)", page, re.I):
            raise RuntimeError("VK: видео недоступно.")
        qualities = self._vk_qualities(page, player_url)
        auto = qualities.get("auto")
        if auto and ".m3u8" in auto.lower():
            try:
                variants = _parse_hls_qualities(_request(self.session, "GET", auto, headers=headers).text, auto)
                for k, v in variants.items():
                    qualities.setdefault(k, v)
            except Exception:
                pass
        if not qualities:
            raise RuntimeError("VK: потоки не найдены.")
        numeric = [k for k in qualities if quality_number(k) >= 0]
        label = max(numeric, key=quality_number) if numeric else "auto"
        return StreamResult(qualities[label], "vk", qualities, headers, label)

    def resolve_zedfilm(self, item):
        full_url = normalize_http_url(item.iframe_url)
        headers = {
            "Referer": "https://yani.tv/",
            "Origin": "https://hlamer.ru",
            "User-Agent": CHROME_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r = _request(self.session, "GET", full_url, headers=headers)
        try:
            page = r.content.decode("cp1251")
        except Exception:
            page = r.text
        candidates = {}
        m = re.search(r"video_Init\(\s*['\"]([^'\"]+)['\"]", page, re.I)
        if m:
            try:
                payload = json.loads(base64.b64decode(m.group(1)).decode("utf-8", errors="replace"))
                for key in ("url", "url2"):
                    u = str(payload.get(key) or "").strip()
                    if u and u.lower() != "null":
                        u = urllib.parse.urljoin(full_url, _decode_html_url(u))
                        if re.search(r"\.(?:mpd|m3u8|mp4)(?:[?#]|$)", u, re.I):
                            qm = re.search(r"(?<!\d)(144|240|360|480|720|1080|1440|2160)p?", u, re.I)
                            candidates[f"{qm.group(1)}p" if qm else "auto"] = u
            except Exception:
                pass
        if not candidates:
            normalized = _decode_html_url(page)
            patterns = [
                r'https?://[^"\'\s<>]+\.(?:mpd|m3u8|mp4)[^"\'\s<>]*',
                r'//[^"\'\s<>]+\.(?:mpd|m3u8|mp4)[^"\'\s<>]*',
                r'\b(?:file|src|source|url|hls|dash)\b\s*[:=]\s*[\'"]([^\'"]+\.(?:mpd|m3u8|mp4)[^\'"]*)[\'"]',
            ]
            for pat in patterns:
                for mm in re.finditer(pat, normalized, re.I):
                    raw = mm.group(1) if mm.lastindex else mm.group(0)
                    u = urllib.parse.urljoin(full_url, _decode_html_url(raw))
                    if any(x in u.lower() for x in ("doubleclick", "yandex", "/ads", "ima")):
                        continue
                    qm = re.search(r"(?<!\d)(144|240|360|480|720|1080|1440|2160)p?", raw, re.I)
                    candidates[f"{qm.group(1)}p" if qm else "auto"] = u
        if not candidates:
            raise RuntimeError("Zedfilm: статический поток не найден; этот embed может требовать WebView.")
        numeric = [k for k in candidates if quality_number(k) >= 0]
        label = max(numeric, key=quality_number) if numeric else "auto"
        return StreamResult(candidates[label], "zedfilm", candidates, {"Referer": full_url, "Origin": "https://hlamer.ru", "User-Agent": CHROME_UA}, label)

    def resolve_alloha(self, item):
        base = str(self.config.get("alloha_resolver_url") or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("Alloha требует YummyAnime resolver server. Укажите адрес в Настройках.")
        try:
            r = requests.get(base + "/resolve", params={"url": item.iframe_url}, timeout=45)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            raise RuntimeError(f"Alloha resolver недоступен ({base}): {e}")
        if payload.get("error"):
            raise RuntimeError(f"Alloha resolver: {payload['error']}")
        url = str(payload.get("url") or "").strip()
        if not url:
            raise RuntimeError("Alloha resolver не вернул поток.")
        qualities = payload.get("qualities") or {payload.get("quality") or "auto": url}
        return StreamResult(
            url,
            "alloha",
            qualities,
            payload.get("headers") or {},
            payload.get("quality") or "auto",
            payload.get("session") or "",
            base,
        )

    @staticmethod
    def release(result):
        if result and result.session and result.resolver_base:
            try:
                requests.get(result.resolver_base.rstrip("/") + "/release", params={"session": result.session}, timeout=10)
            except Exception:
                pass
