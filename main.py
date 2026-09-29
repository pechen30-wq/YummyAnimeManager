
import sys
import re
import os
import json
import hashlib
import shutil
import subprocess
import tempfile
import time
import random
import urllib.parse
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict
from typing import Any

import requests
from resilient_download import download_file
from hls_download import stage_hls
from chapters import aniskip_points, inspect_media, remux_to_mkv
from diagnostics import LOGGER, LOG_DIR, configure_logging, install_exception_hooks, safe_url
from url_history import remember_url

from resolvers import (
    PlayerResolver, StreamResult, choose_stream, find_ffmpeg, provider_kind,
    provider_label, system_proxy_for, CHROME_UA,
    DIRECT_MEDIA_EXTS as RESOLVER_MEDIA_EXTS,
)
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer
from updater import (download_verified, fetch_manifest,
                     newer_version, schedule_exe_replacement)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QProgressBar, QComboBox, QDialog, QFormLayout,
    QDialogButtonBox, QCheckBox, QGroupBox, QSplitter
)

APP_NAME = "YummyAnime Manager"
APP_VERSION = "4.5.3"
YUMMY_API_BASE = "https://api.yani.tv"
CVH_API_BASE = "https://plapi.cdnvideohub.com/api/v1/player/sv"

CONFIG_DIR = Path.home() / ".yummy_anime_manager"
CONFIG_FILE = CONFIG_DIR / "config.json"

QUALITY_KEYS = {
    144: "mpegTinyUrl",
    240: "mpegLowestUrl",
    360: "mpegLowUrl",
    480: "mpegMediumUrl",
    720: "mpegHighUrl",
    1080: "mpegFullHdUrl",
    1440: "mpegQhdUrl",
    2048: "mpeg2kUrl",
    2160: "mpeg4kUrl",
}

DEFAULT_CVH_PUB = "747"
DEFAULT_CVH_AGGR = "mali"
DIRECT_MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi")


@dataclass
class VideoItem:
    video_id: int
    player: str
    dubbing: str
    number: str
    index: int
    iframe_url: str
    player_id: int | None = None
    duration: float | None = None
    season_hint: int | None = None

    @property
    def episode_key(self):
        raw = str(self.number or "").strip()
        m = re.search(r"\d+(?:\.\d+)?", raw)
        if m:
            try:
                return float(m.group())
            except Exception:
                pass
        return float(self.index or 0)

    @property
    def is_cvh(self):
        s = f"{self.player} {self.iframe_url}".lower()
        return "cvh" in s or "cdnvideohub" in s or "cdn-iframe" in s


@dataclass
class CvhRef:
    content_id: str
    season: int | None
    episode: float | None
    dubbing: str | None
    pub: str = DEFAULT_CVH_PUB
    aggr: str = DEFAULT_CVH_AGGR


def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    # v1/v2 migration
    if "public_token" not in data and data.get("app_token"):
        data["public_token"] = data["app_token"]
    return data


def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .") or "video"


def extract_slug(value):
    value = value.strip()
    if not value:
        raise ValueError("Введите ссылку YummyAnime или slug.")
    if "://" not in value:
        return value.strip("/")
    parsed = urllib.parse.urlparse(value)
    marker = "/catalog/item/"
    if marker in parsed.path:
        slug = parsed.path.split(marker, 1)[1].split("/", 1)[0]
        if slug:
            return slug
    raise ValueError("Ожидается ссылка вида https://.../catalog/item/<slug>")


def direct_media_url(url):
    try:
        p = urllib.parse.urlparse(url)
        return p.scheme in ("http", "https") and p.path.lower().endswith(DIRECT_MEDIA_EXTS)
    except Exception:
        return False


def norm_text(s):
    return re.sub(r"[^a-zа-яё0-9]+", "", (s or "").lower(), flags=re.I)


def same_episode(a, b):
    try:
        return abs(float(a) - float(b)) < 0.001
    except Exception:
        return str(a).strip() == str(b).strip()


def episode_label(value):
    try:
        f = float(value)
        if f.is_integer():
            return str(int(f))
        return str(f).rstrip("0").rstrip(".")
    except Exception:
        return str(value)



def infer_season_from_title(anime):
    """Best-effort Plex season number for a YummyAnime title."""
    anime = anime or {}
    values = []
    for key in ("title", "original", "name"):
        value = anime.get(key)
        if isinstance(value, dict):
            values.extend(str(x) for x in value.values() if x)
        elif value:
            values.append(str(value))
    values.extend(str(x) for x in (anime.get("other_titles") or []) if x)
    patterns = [
        r"(?i)\bseason\s*(\d{1,2})\b",
        r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\s+season\b",
        r"(?i)\bсезон\s*(\d{1,2})\b",
        r"(?i)\b(\d{1,2})\s*[-–—]?\s*(?:й|ый|ой)\s+сезон\b",
    ]
    for value in values:
        for pattern in patterns:
            m = re.search(pattern, value)
            if m:
                try: return max(0, int(m.group(1)))
                except Exception: pass
    return 1


def video_season_hint(raw_video, iframe_url, anime=None):
    """Use only explicit season metadata/URL markers; do not invent seasons."""
    raw_video = raw_video or {}
    raw = raw_video.get("season")
    try:
        if raw not in (None, ""): return int(float(raw))
    except Exception: pass
    try:
        parsed = urllib.parse.urlparse(urllib.parse.unquote(str(iframe_url or "")))
        q = urllib.parse.parse_qs(parsed.query)
        for name in ("season", "season_id", "seasonId"):
            vals = q.get(name)
            if vals:
                try: return int(float(vals[0]))
                except Exception: pass
        m = re.search(r"/cdn-iframe/\d+/(\d+)/\d+(?:\.\d+)?", parsed.path, re.I)
        if m: return int(m.group(1))
        m = re.search(r"/cdn-iframe/\d+/[^/]+/(\d+)/\d+(?:\.\d+)?", parsed.path, re.I)
        if m: return int(m.group(1))
    except Exception: pass
    return None

def find_mkvmerge():
    found = shutil.which("mkvmerge")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\MKVToolNix\mkvmerge.exe"),
        Path(r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


def anime_display_title(anime):
    title = (anime or {}).get("title") or (anime or {}).get("name") or (anime or {}).get("original") or "Anime"
    if isinstance(title, dict):
        title = title.get("ru") or title.get("en") or title.get("jp") or next(iter(title.values()), "Anime")
    return str(title)


def plex_compatible_ids(anime):
    """
    YummyAnime currently documents MAL/Shikimori/KP/etc.
    Plex .plexmatch directly supports TMDB/TVDB/IMDb.
    Keep this future-proof and also inspect top-level fields.
    """
    anime = anime or {}
    remote = anime.get("remote_ids") or {}
    result = {}

    candidates = {
        "tmdbid": ("tmdb_id", "tmdbid", "themoviedb_id"),
        "tvdbid": ("tvdb_id", "tvdbid", "thetvdb_id"),
        "imdbid": ("imdb_id", "imdbid"),
    }

    for plex_key, names in candidates.items():
        for source in (remote, anime):
            for name in names:
                value = source.get(name) if isinstance(source, dict) else None
                if value not in (None, "", 0):
                    result[plex_key] = str(value)
                    break
            if plex_key in result:
                break

    return result


def build_plexmatch(anime):
    anime = anime or {}
    title = anime_display_title(anime)
    year = anime.get("year")
    remote = anime.get("remote_ids") or {}
    exact_ids = plex_compatible_ids(anime)

    lines = [
        "# Generated by YummyAnime Manager",
        f"Title: {title}",
    ]

    if year not in (None, "", 0):
        try:
            lines.append(f"Year: {int(year)}")
        except Exception:
            lines.append(f"Year: {year}")

    # An exact Plex-compatible external ID takes precedence over Title/Year.
    for key in ("tmdbid", "tvdbid", "imdbid"):
        if exact_ids.get(key):
            value = exact_ids[key]
            if key == "imdbid" and value.isdigit():
                value = "tt" + value
            lines.append(f"{key}: {value}")
            break

    # Preserve useful anime IDs as comments. Plex ignores comment lines.
    if isinstance(remote, dict):
        if remote.get("myanimelist_id"):
            lines.append(f"# MyAnimeList ID: {remote['myanimelist_id']}")
        if remote.get("shikimori_id"):
            lines.append(f"# Shikimori ID: {remote['shikimori_id']}")
        if remote.get("kp_id"):
            lines.append(f"# Kinopoisk ID: {remote['kp_id']}")

    return "\n".join(lines) + "\n"


def write_plexmatch(show_dir, anime):
    """
    Create/update show-level .plexmatch without destroying user's episode/pattern hints.
    We replace only automatically managed series identity hints.
    """
    show_dir = Path(show_dir)
    show_dir.mkdir(parents=True, exist_ok=True)
    path = show_dir / ".plexmatch"

    managed = {"title", "show", "year", "tmdbid", "tvdbid", "imdbid"}
    preserved = []

    if path.exists():
        try:
            old = path.read_text(encoding="utf-8-sig")
            for line in old.splitlines():
                stripped = line.strip()
                if stripped.startswith("# Generated by YummyAnime Manager"):
                    continue
                if stripped.startswith("# MyAnimeList ID:"):
                    continue
                if stripped.startswith("# Shikimori ID:"):
                    continue
                if stripped.startswith("# Kinopoisk ID:"):
                    continue

                m = re.match(r"^\s*([A-Za-z]+)\s*:", line)
                if m and m.group(1).lower() in managed:
                    continue
                preserved.append(line)
        except Exception:
            preserved = []

    generated = build_plexmatch(anime).rstrip()
    extra = "\n".join(preserved).strip()

    content = generated
    if extra:
        content += "\n\n# Preserved custom hints\n" + extra
    content += "\n"

    path.write_text(content, encoding="utf-8")
    return path


class YummyApi:
    def __init__(self, public_token, lang="ru"):
        self.public_token = public_token.strip()
        self.lang = lang

    @property
    def headers(self):
        return {
            "X-Application": self.public_token,
            "Lang": self.lang,
            "Accept": "application/json,image/avif,image/webp",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }

    def get(self, path, params=None):
        if not self.public_token:
            raise RuntimeError("Не указан публичный X-Application token.")
        attempts = 4
        retryable_status = {429, 500, 502, 503, 504}
        last_error = None
        for attempt in range(1, attempts + 1):
            LOGGER.debug("YummyAnime API GET %s attempt=%s/%s", path, attempt, attempts)
            try:
                # requests.get opens a new connection on each attempt.
                response = requests.get(YUMMY_API_BASE + path, params=params,
                                        headers=self.headers, timeout=(5, 20))
                LOGGER.debug("YummyAnime API status=%s path=%s", response.status_code, path)
                if response.status_code == 401:
                    raise RuntimeError("YummyAnime отклонил публичный X-Application token (401).")
                if response.status_code == 404:
                    raise RuntimeError("Тайтл не найден (404).")
                if response.status_code in retryable_status:
                    last_error = requests.exceptions.HTTPError(f"HTTP {response.status_code}")
                    response.close()
                else:
                    response.raise_for_status()
                    data = response.json()
                    return data.get("response", data)
            except (ConnectionResetError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as error:
                last_error = error
            LOGGER.warning("YummyAnime API temporary error attempt=%s/%s path=%s: %s",
                           attempt, attempts, path, last_error)
            if attempt < attempts:
                time.sleep(min(8, 0.8 * 2 ** (attempt - 1)) * random.uniform(0.75, 1.25))
        raise RuntimeError(
            f"Не удалось получить данные YummyAnime после {attempts} попыток. "
            f"Проверьте подключение к api.yani.tv и повторите запрос. Причина: {last_error}"
        ) from last_error

    def anime(self, slug):
        return self.get(f"/anime/{urllib.parse.quote(slug, safe='')}", {"need_videos": "true"})

    def videos(self, anime_id):
        return self.get(f"/anime/{anime_id}/videos")


class CvhClient:
    HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://player.cdnvideohub.com/",
        "Origin": "https://player.cdnvideohub.com",
    }

    @staticmethod
    def parse_ref(url, fallback_episode=None, fallback_dubbing=None):
        if not url:
            return None
        p = urllib.parse.urlparse(urllib.parse.unquote(url))
        q = urllib.parse.parse_qs(p.query)

        def q1(*names):
            for n in names:
                vals = q.get(n)
                if vals and vals[0] != "":
                    return vals[0]
            return None

        pub = q1("pub", "publisher") or DEFAULT_CVH_PUB
        aggr = q1("aggr", "aggregator") or DEFAULT_CVH_AGGR
        dubbing = q1("dubbing", "voiceStudio", "studio") or fallback_dubbing
        content_id = q1("id", "anime_id", "animeId")
        season = q1("season")
        episode = q1("episode", "ep")

        try:
            season = int(float(season)) if season else None
        except Exception:
            season = None
        try:
            episode = float(episode) if episode else None
        except Exception:
            episode = None

        m = re.search(r"/cdn-iframe/(\d+)/(\d+)/(\d+(?:\.\d+)?)", p.path, re.I)
        if m:
            content_id = content_id or m.group(1)
            season = season or int(m.group(2))
            episode = episode if episode is not None else float(m.group(3))
        else:
            m = re.search(r"/cdn-iframe/(\d+)/([^/]+)/(\d+)/(\d+(?:\.\d+)?)", p.path, re.I)
            if m:
                content_id = content_id or m.group(1)
                dubbing = dubbing or urllib.parse.unquote(m.group(2))
                season = season or int(m.group(3))
                episode = episode if episode is not None else float(m.group(4))

        if not content_id and ("cdnvideohub" in p.netloc.lower() or "cvh" in p.netloc.lower()):
            nums = re.findall(r"/(\d{3,})(?:/|$)", p.path)
            if nums:
                content_id = nums[0]

        if not content_id:
            return None

        if episode is None and fallback_episode is not None:
            try:
                episode = float(fallback_episode)
            except Exception:
                pass

        return CvhRef(str(content_id), season, episode, dubbing, str(pub), str(aggr))

    def _get_json(self, url, params=None):
        r = requests.get(url, params=params, headers=self.HEADERS, timeout=25)
        r.raise_for_status()
        return r.json()

    def playlist(self, ref):
        return self._get_json(
            f"{CVH_API_BASE}/playlist",
            {"pub": ref.pub, "aggr": ref.aggr, "id": ref.content_id},
        )

    def video(self, vk_id):
        return self._get_json(f"{CVH_API_BASE}/video/{vk_id}")

    @staticmethod
    def items(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("items", "playlist", "data", "results"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return []

    @staticmethod
    def match_item(items, episode, dubbing, season=None):
        candidates = []
        for item in items:
            if episode is not None and not same_episode(item.get("episode"), episode):
                continue
            if season is not None and item.get("season") not in (None, season, str(season)):
                continue
            candidates.append(item)

        if not candidates:
            return None

        if dubbing:
            target = norm_text(dubbing)
            for item in candidates:
                studio = item.get("voiceStudio") or item.get("studio") or item.get("translation") or ""
                if norm_text(studio) == target:
                    return item
            for item in candidates:
                studio = item.get("voiceStudio") or item.get("studio") or item.get("translation") or ""
                a = norm_text(studio)
                if a and target and (a in target or target in a):
                    return item

        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def streams(payload):
        sources = {}
        if isinstance(payload, dict):
            sources = payload.get("sources") or {}
            if not sources and isinstance(payload.get("data"), dict):
                sources = payload["data"].get("sources") or {}
        return {q: sources[k] for q, k in QUALITY_KEYS.items() if sources.get(k)}

    def resolve(self, item):
        ref = self.parse_ref(
            item.iframe_url,
            fallback_episode=item.episode_key,
            fallback_dubbing=item.dubbing,
        )
        if not ref:
            raise RuntimeError("CVH ID не найден в iframe URL.")

        playlist = self.playlist(ref)
        match = self.match_item(
            self.items(playlist),
            ref.episode if ref.episode is not None else item.episode_key,
            ref.dubbing or item.dubbing,
            ref.season,
        )
        if not match:
            raise RuntimeError(
                f"CVH не смог однозначно сопоставить серию {episode_label(item.episode_key)} "
                f"и озвучку «{item.dubbing}»."
            )

        vk_id = match.get("vkId") or match.get("vk_id") or match.get("videoId") or match.get("id")
        if not vk_id:
            raise RuntimeError("CVH playlist не вернул vkId.")

        payload = self.video(vk_id)
        streams = self.streams(payload)
        if not streams:
            raise RuntimeError("CVH не вернул прямые MP4.")

        studio = match.get("voiceStudio") or match.get("studio") or item.dubbing
        return streams, studio


def pick_quality(streams, wanted):
    if not streams:
        return None, None
    if wanted == "Лучшее":
        q = max(streams)
        return q, streams[q]
    try:
        q = int(wanted.rstrip("p"))
    except Exception:
        return None, None
    return (q, streams[q]) if q in streams else (None, None)



def hidden_subprocess_kwargs():
    """Prevent ffmpeg/mkvmerge console windows from flashing on Windows."""
    if os.name != "nt": return {}
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    except Exception: pass
    return kwargs


def _parse_iso_duration(value):
    m = re.match(
        r"^P(?:(?P<d>\d+(?:\.\d+)?)D)?T?"
        r"(?:(?P<h>\d+(?:\.\d+)?)H)?"
        r"(?:(?P<m>\d+(?:\.\d+)?)M)?"
        r"(?:(?P<s>\d+(?:\.\d+)?)S)?$",
        str(value or ""), re.I,
    )
    if not m: return 0.0
    return (float(m.group("d") or 0)*86400 + float(m.group("h") or 0)*3600 +
            float(m.group("m") or 0)*60 + float(m.group("s") or 0))


def estimate_manifest_duration(result):
    """Best-effort duration for HLS/DASH when YummyAnime duration is absent."""
    try:
        headers={"User-Agent":CHROME_UA}; headers.update(result.headers or {})
        r=requests.get(result.url,headers=headers,timeout=20); r.raise_for_status()
        body=r.text; low=urllib.parse.urlparse(result.url).path.lower()
        if low.endswith(".m3u8"):
            vals=re.findall(r"#EXTINF:([0-9.]+)",body,re.I)
            if vals: return sum(float(x) for x in vals)
            pending=False; variants=[]
            for raw in body.splitlines():
                line=raw.strip()
                if line.upper().startswith("#EXT-X-STREAM-INF"):
                    pending=True; continue
                if pending and line and not line.startswith("#"):
                    variants.append(urllib.parse.urljoin(result.url,line)); pending=False
            if variants:
                rr=requests.get(variants[-1],headers=headers,timeout=20); rr.raise_for_status()
                vals=re.findall(r"#EXTINF:([0-9.]+)",rr.text,re.I)
                if vals: return sum(float(x) for x in vals)
        if low.endswith(".mpd"):
            m=re.search(r"mediaPresentationDuration=[\"']([^\"']+)[\"']",body,re.I)
            if m: return _parse_iso_duration(m.group(1))
    except Exception: pass
    return 0.0

def mkv_identify(mkvmerge, media_path):
    proc = subprocess.run(
        [mkvmerge, "-J", str(media_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **hidden_subprocess_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mkvmerge -J: {proc.stderr.strip() or 'ошибка'}")
    return json.loads(proc.stdout)


def audio_track_ids(mkvmerge, media_path):
    info = mkv_identify(mkvmerge, media_path)
    return [t["id"] for t in info.get("tracks", []) if t.get("type") == "audio"]


def merge_audio_tracks(mkvmerge, sources, output_path, progress_cb=None, video_source=None):
    if len(sources) < 2 and video_source is None:
        raise RuntimeError("Для объединения нужно минимум две озвучки.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd=[mkvmerge,"--ui-language","en","-o",str(output_path)]
    if video_source is not None:
        cmd += ["--no-audio", str(video_source)]
    first_file,first_name=sources[0]
    first_audio=audio_track_ids(mkvmerge,first_file)
    if not first_audio: raise RuntimeError(f"Нет аудиодорожки в {first_file.name}")
    for idx,tid in enumerate(first_audio):
        cmd += ["--track-name",f"{tid}:{first_name}","--language",f"{tid}:rus",
                "--default-track-flag",f"{tid}:{'yes' if idx==0 else 'no'}"]
    if video_source is not None:
        cmd += ["--no-video", "--no-subtitles", "--no-attachments", "--no-chapters"]
    cmd += [str(first_file)]
    for media_file,dub_name in sources[1:]:
        ids=audio_track_ids(mkvmerge,media_file)
        if not ids: raise RuntimeError(f"Нет аудиодорожки в {media_file.name}")
        cmd += ["--no-video","--no-subtitles","--no-attachments","--no-chapters"]
        for tid in ids:
            cmd += ["--track-name",f"{tid}:{dub_name}","--language",f"{tid}:rus",
                    "--default-track-flag",f"{tid}:no"]
        cmd += [str(media_file)]
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
        encoding="utf-8",errors="replace",bufsize=1,**hidden_subprocess_kwargs())
    lines=[]; last=-1
    for line in proc.stdout or []:
        lines.append(line.rstrip())
        LOGGER.debug("mkvmerge: %s", line.rstrip())
        pm=re.search(r"Progress:\s*(\d+)%",line,re.I)
        if pm:
            pct=max(0,min(100,int(pm.group(1))))
            if pct!=last and progress_cb: progress_cb(pct); last=pct
    code=proc.wait()
    if code>=2: raise RuntimeError("\n".join(lines[-20:]) or "Ошибка mkvmerge")
    if progress_cb: progress_cb(100)
    return "\n".join(lines)


class FetchThread(QThread):
    loaded = Signal(object, object)
    failed = Signal(str)

    def __init__(self, token, lang, slug):
        super().__init__()
        self.token, self.lang, self.slug = token, lang, slug

    def run(self):
        try:
            api = YummyApi(self.token, self.lang)
            anime = api.anime(self.slug)
            anime_id = anime.get("anime_id") or anime.get("id")
            if anime_id is None:
                raise RuntimeError("API не вернул anime_id.")
            self.loaded.emit(anime, api.videos(int(anime_id)))
        except Exception as e:
            LOGGER.exception("Anime metadata request failed for slug=%s", self.slug)
            self.failed.emit(str(e))


class QualityProbeThread(QThread):
    done = Signal(int, object, str)
    def __init__(self, serial, config, items):
        super().__init__(); self.serial=serial; self.config=dict(config or {}); self.items=list(items or [])
    def run(self):
        resolver=PlayerResolver(self.config); sets=[]; notes=[]; successes=0
        for item in self.items:
            stream=None
            try:
                stream=resolver.resolve(item)
                labels=set((stream.qualities or {}).keys())
                if not labels and stream.quality: labels.add(stream.quality)
                labels={str(x) for x in labels if x}
                if labels: sets.append(labels); successes+=1
            except Exception as e:
                LOGGER.exception("Quality probe failed for dubbing=%s", getattr(item, 'dubbing', ''))
                notes.append(f"{getattr(item,'dubbing','')}: {e}")
            finally:
                # Alloha shares one session per iframe with active downloads.
                # Probing must not close it; the server expires idle sessions.
                if stream is not None and stream.source != "alloha": PlayerResolver.release(stream)
        if sets:
            common=set.intersection(*sets)
            numeric=sorted([x for x in common if re.search(r"\d{3,4}",x)],
                key=lambda x:int(re.search(r"\d{3,4}",x).group()),reverse=True)
            qualities=numeric+(["auto"] if "auto" in common else [])
            note=f"Проверено по первой общей серии для {successes} озвуч." + (f" Ошибки: {'; '.join(notes[:2])}" if notes else "")
        else:
            qualities=[]; note="Не удалось автоматически определить качества. "+("; ".join(notes[:2]) if notes else "")
        self.done.emit(self.serial,qualities,note)


class WorkThread(QThread):
    progress=Signal(int,int,str)
    done=Signal(str,object)
    failed=Signal(str)
    def __init__(self,player,dubbings,episode_items,quality,base_dir,anime_title,merge_enabled,
                 mkvmerge_path,keep_sources,season_number=1,plex_structure=True,
                 plexmatch_enabled=True,anime_metadata=None,resolver_config=None,ffmpeg_path="",
                 chapters_enabled=True):
        super().__init__(); self.player=player; self.dubbings=dubbings; self.episode_items=episode_items
        self.quality=quality; self.base_dir=Path(base_dir); self.anime_title=anime_title
        self.merge_enabled=merge_enabled; self.mkvmerge=mkvmerge_path; self.keep_sources=keep_sources
        self.season_number=int(season_number or 1); self.plex_structure=bool(plex_structure)
        self.plexmatch_enabled=bool(plexmatch_enabled); self.anime_metadata=anime_metadata or {}
        self.resolver_config=resolver_config or {}; self.ffmpeg=ffmpeg_path or find_ffmpeg()
        self.chapters_enabled=chapters_enabled
        self.resolver=PlayerResolver(self.resolver_config)
    def emit_progress(self,ep_index,total_eps,series_pct,status):
        series_pct=max(0,min(100,int(series_pct)))
        overall=int((((ep_index-1)+series_pct/100.0)/max(1,total_eps))*100)
        self.progress.emit(series_pct,max(0,min(100,overall)),status)
    def resolve_stream(self,item, audio_only=False):
        result=self.resolver.resolve(item); label,url=choose_stream(result,"Лучшее" if audio_only else self.quality)
        if audio_only:
            # Preserve the master playlist's separate audio renditions.
            master = (result.qualities or {}).get("auto") or result.url
            if master and urllib.parse.urlparse(master).path.lower().endswith(".m3u8"):
                url = master
            result.audio_only = True
        result.url=url; result.quality=label; return result
    def ensure_chapters(self, media_path, item, episode, chapter_source=None):
        media_path=Path(media_path)
        if not self.chapters_enabled:
            return media_path
        if not self.ffmpeg or not Path(self.ffmpeg).exists():
            LOGGER.warning("Chapter inspection skipped: FFmpeg is unavailable")
            return media_path
        info=inspect_media(self.ffmpeg,media_path)
        LOGGER.info("Chapter inspection: %s existing=%s duration=%.3f",
                    media_path,info.chapters,info.duration)
        if info.chapters:
            if media_path.suffix.lower() != ".mkv":
                target=media_path.with_suffix(".mkv")
                remux_to_mkv(self.ffmpeg,media_path,target,keep_source=self.keep_sources)
                return target
            return media_path
        if chapter_source and Path(chapter_source) != media_path:
            source_info=inspect_media(self.ffmpeg,chapter_source)
            if source_info.chapters:
                remux_to_mkv(self.ffmpeg,media_path,media_path,chapter_source=chapter_source)
                LOGGER.info("Copied %s embedded chapters from first source",source_info.chapters)
                return media_path
        remote=self.anime_metadata.get("remote_ids") or {}
        mal_id=remote.get("myanimelist_id") if isinstance(remote,dict) else None
        duration=info.duration
        if duration<=0:
            try: duration=float(getattr(item,"duration",0) or 0)
            except (TypeError,ValueError): duration=0
        points=aniskip_points(mal_id,episode,duration)
        if not points:
            LOGGER.info("No matching AniSkip chapters: MAL=%s episode=%s duration=%.3f",
                        mal_id,episode,duration)
            return media_path
        target=media_path if media_path.suffix.lower()==".mkv" else media_path.with_suffix(".mkv")
        remux_to_mkv(self.ffmpeg,media_path,target,points=points,duration=duration,
                     keep_source=self.keep_sources)
        LOGGER.info("Added %s AniSkip chapters to %s",len(points),target)
        return target
    @staticmethod
    def extension_for(result):
        if result.audio_only: return ".mka"
        if result.is_manifest: return ".mkv"
        suffix=Path(urllib.parse.urlparse(result.url).path).suffix.lower()
        return suffix if suffix in RESOLVER_MEDIA_EXTS else ".mp4"
    def download_stream(self,result,path,item,progress_cb):
        path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
        if result.source != "local_hls" and (result.source == "alloha" or result.audio_only) and urllib.parse.urlparse(result.url).path.lower().endswith(".m3u8"):
            source_key=hashlib.sha256(str(getattr(item,"iframe_url",result.url)).encode()).hexdigest()[:12]
            cache = path.parent / (".hls_" + path.stem + "_" + source_key)
            headers={"User-Agent":CHROME_UA}; headers.update(result.headers or {})
            def refresh():
                PlayerResolver.release(result)
                fresh=self.resolver.resolve(item)
                label,url=choose_stream(fresh,result.quality)
                if result.audio_only:
                    master=(fresh.qualities or {}).get("auto") or fresh.url
                    if master and urllib.parse.urlparse(master).path.lower().endswith(".m3u8"):
                        url=master
                result.url=url; result.quality=label
                result.session=fresh.session; result.resolver_base=fresh.resolver_base
                result.headers=fresh.headers; result.qualities=fresh.qualities
                headers.clear(); headers.update({"User-Agent":CHROME_UA}); headers.update(fresh.headers or {})
                return url
            try:
                playlist = stage_hls(result.url, cache, headers, progress_cb,
                                     refresh=refresh if result.source == "alloha" else None,
                                     audio_only=result.audio_only)
            except requests.RequestException as error:
                raise RuntimeError(f"{result.source}: не удалось получить все сегменты серии.") from error
            local = StreamResult(str(playlist), "local_hls", {}, {})
            local.audio_only = result.audio_only
            self.download_stream(local, path, item,
                                 lambda pct, detail: progress_cb(90+int(pct/10), "Сборка видео · " + detail))
            if cache.resolve().parent == path.parent.resolve():
                shutil.rmtree(cache)
            return
        if result.is_manifest or result.audio_only:
            if not self.ffmpeg or not Path(self.ffmpeg).exists():
                raise RuntimeError("Для HLS/DASH нужен FFmpeg. Откройте Настройки и укажите ffmpeg.exe.")
            headers=dict(result.headers or {})
            cmd=[self.ffmpeg,"-y","-hide_banner","-loglevel",
                 "info" if LOGGER.isEnabledFor(10) else "error","-nostats","-progress","pipe:1"]
            ua=headers.pop("User-Agent",headers.pop("user-agent",CHROME_UA))
            if ua:
                cmd += ["-user_agent", ua]
            if headers:
                cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]

            # FFmpeg does not always inherit the Windows/system proxy.
            proxy = system_proxy_for(result.url)
            if proxy:
                cmd += ["-http_proxy", proxy]

            if urllib.parse.urlparse(result.url).path.lower().endswith(".m3u8"):
                cmd += ["-seg_max_retry", "3"]
                if result.source == "alloha":
                    cmd += ["-http_persistent", "0", "-http_multiple", "0"]

            # EOF is normal for finite HLS/DASH manifests and media segments.
            # Reconnecting there prevents manifest parsing from ever finishing.
            cmd += [
                "-rw_timeout", "30000000",
                "-reconnect", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "5xx",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-reconnect_max_retries", "5",
                "-reconnect_delay_total_max", "30",
                "-i", result.url,
                "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
                "-c", "copy", str(path),
            ]
            try: duration=float(getattr(item,"duration",0) or 0)
            except Exception: duration=0.0
            if duration<=0: duration=estimate_manifest_duration(result)
            if result.source == "local_hls":
                cmd=[self.ffmpeg,"-y","-hide_banner","-loglevel","error","-nostats",
                     "-progress","pipe:1","-protocol_whitelist","file,crypto,data",
                     "-allowed_extensions","ALL","-i",result.url,
                     "-map","0:v?","-map","0:a?","-map","0:s?","-c","copy",str(path)]
            if result.audio_only:
                input_position = cmd.index("-i")
                cmd[input_position:input_position] = ["-discard:v", "all"]
                input_end = cmd.index("-i") + 2
                cmd = cmd[:input_end] + ["-map", "0:a", "-vn", "-sn", "-dn",
                    "-map_metadata", "-1", "-map_chapters", "-1", "-c:a", "copy", str(path)]
            proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                encoding="utf-8",errors="replace",bufsize=1,**hidden_subprocess_kwargs())
            errors=[]; last=-1; downloaded_seconds=0.0; missing_segment=False
            for line in proc.stdout or []:
                line=line.strip(); seconds=None
                LOGGER.debug("FFmpeg: %s",line)
                if re.search(r"Segment .*failed too many times, skipping", line, re.I):
                    missing_segment=True
                    proc.kill()
                    break
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try: seconds=float(line.split("=",1)[1])/1_000_000.0
                    except Exception: pass
                elif line.startswith("out_time="):
                    try:
                        hh,mm,ss=line.split("=",1)[1].split(":"); seconds=int(hh)*3600+int(mm)*60+float(ss)
                    except Exception: pass
                elif line and "=" not in line: errors.append(line)
                if seconds is not None and duration>0:
                    downloaded_seconds=max(downloaded_seconds,seconds)
                    pct=max(0,min(99,int(seconds*100/duration)))
                    if pct!=last: progress_cb(pct,f"{pct}%"); last=pct
            code=proc.wait()
            if missing_segment:
                raise RuntimeError("Источник не отдал видеосегмент. Неполная серия не будет сохранена как готовая.")
            if code!=0: raise RuntimeError("\n".join(errors[-12:]) or "FFmpeg завершился с ошибкой.")
            if duration>0 and downloaded_seconds < duration-max(2.0,duration*0.02):
                raise RuntimeError(f"Видео скачано не полностью: {downloaded_seconds:.1f} из {duration:.1f} секунд.")
            progress_cb(100,"100%"); return
        headers={"User-Agent":CHROME_UA}; headers.update(result.headers or {})
        download_file(result.url, path, headers, progress_cb)
    def run(self):
        try:
            LOGGER.info("Download started: title=%s episodes=%s dubbings=%s quality=%s chapters=%s",
                        self.anime_title,len(self.episode_items),self.dubbings,self.quality,self.chapters_enabled)
            title_dir=self.base_dir/safe_name(self.anime_title)
            media_dir=title_dir/f"Season {self.season_number:02d}" if self.plex_structure else title_dir
            if self.plexmatch_enabled: write_plexmatch(title_dir,self.anime_metadata)
            errors=[]; completed=0; episodes=sorted(self.episode_items); total_eps=max(1,len(episodes))
            cross_source = any(
                items.get("__video__") is not None and
                any(items.get(dub) != items["__video__"] for dub in self.dubbings)
                for items in self.episode_items.values())
            use_merge=(self.merge_enabled and len(self.dubbings)>1) or cross_source
            if use_merge and (not self.mkvmerge or not Path(self.mkvmerge).exists()):
                raise RuntimeError("MKVToolNix не найден. Укажите путь к mkvmerge.exe в Настройках.")
            download_part=90.0 if use_merge else 100.0
            download_dubs = (["__video__"] if use_merge else []) + self.dubbings
            dub_span=download_part/max(1,len(download_dubs))
            for ep_index,ep in enumerate(episodes,1):
                LOGGER.info("Episode %s started",ep)
                source_files=[]; ep_label=episode_label(ep); temp_dir=title_dir/".tmp"/f"episode_{safe_name(ep_label)}"; failed=False
                self.emit_progress(ep_index,total_eps,0,f"Серия {ep_label} ({ep_index}/{len(episodes)}): подготовка…")
                for dub_index,dub in enumerate(download_dubs,1):
                    item=self.episode_items[ep].get(dub); dub_start=(dub_index-1)*dub_span
                    self.emit_progress(ep_index,total_eps,dub_start,f"Серия {ep_label} ({ep_index}/{len(episodes)}): {dub} — получение прямой ссылки…")
                    if not item: errors.append(f"Серия {ep_label}: нет озвучки «{dub}»."); failed=True; break
                    if use_merge and dub != "__video__" and item == self.episode_items[ep].get("__video__"):
                        source_files.append((source_files[0][0],dub))
                        continue
                    candidates = ([item] if dub == "__video__" else
                                  self.episode_items[ep].get("__audio_candidates__", {}).get(dub, [item]))
                    attempt_items = [candidate for candidate in candidates
                                     for _ in range(3 if provider_kind(candidate) in ("aksor", "alloha") else 2)]
                    max_attempts = len(attempt_items)
                    last_error = None

                    for attempt in range(1, max_attempts + 1):
                        item = attempt_items[attempt-1]
                        provider = provider_kind(item)
                        stream = None
                        dest = None
                        try:
                            if attempt > 1:
                                self.emit_progress(
                                    ep_index,
                                    total_eps,
                                    dub_start,
                                    f"Серия {ep_label} ({ep_index}/{len(episodes)}): "
                                    f"{dub} · {provider_label(item)} — обновляю источник "
                                    f"(попытка {attempt}/{max_attempts})…",
                                )
                                time.sleep(0.8 * (attempt - 1))

                            stream = self.resolve_stream(item, audio_only=use_merge and dub != "__video__")
                            LOGGER.info("Resolved episode=%s dubbing=%s provider=%s quality=%s URL=%s",
                                        ep,dub,stream.source,stream.quality,safe_url(stream.url))
                            ext = self.extension_for(stream)
                            qtag = safe_name(stream.quality or "auto")

                            if use_merge:
                                dest = temp_dir / f"{safe_name(dub)} [{qtag}]{ext}"
                            else:
                                try:
                                    plex_ep = f"S{self.season_number:02d}E{int(float(ep)):02d}"
                                except Exception:
                                    plex_ep = f"S{self.season_number:02d}E{safe_name(ep_label)}"
                                suffix = f" [{safe_name(dub)}]" if len(self.dubbings) > 1 else ""
                                dest = media_dir / f"{safe_name(self.anime_title)} - {plex_ep}{suffix}{ext}"

                            if attempt > 1 and dest.exists():
                                try:
                                    dest.unlink()
                                except Exception:
                                    pass

                            def fp(pct, detail, _start=dub_start, _span=dub_span):
                                sp = _start + _span * max(0, min(100, pct)) / 100.0
                                self.emit_progress(
                                    ep_index,
                                    total_eps,
                                    sp,
                                    f"Серия {ep_label} ({ep_index}/{len(episodes)}): "
                                    f"{dub} · {stream.quality} · {stream.source} — {detail}",
                                )

                            self.download_stream(stream, dest, item, fp)
                            if not use_merge:
                                try:
                                    dest=self.ensure_chapters(dest,item,ep)
                                except Exception as chapter_error:
                                    LOGGER.exception("Chapter processing failed for episode=%s dubbing=%s",ep,dub)
                                    errors.append(f"Серия {ep_label}, {dub}: главы не добавлены: {chapter_error}")
                            source_files.append((dest, dub))
                            last_error = None
                            break

                        except Exception as e:
                            last_error = e
                            LOGGER.exception("Download attempt failed: episode=%s dubbing=%s attempt=%s/%s",
                                             ep,dub,attempt,max_attempts)
                            if attempt >= max_attempts:
                                break
                        finally:
                            if stream is not None:
                                PlayerResolver.release(stream)

                    if last_error is not None:
                        errors.append(f"Серия {ep_label}, {dub}: {last_error}")
                        failed = True
                        break
                if failed:
                    self.emit_progress(ep_index,total_eps,100,f"Серия {ep_label}: пропущена из-за ошибки."); continue
                if use_merge:
                    try: plex_ep=f"S{self.season_number:02d}E{int(float(ep)):02d}"
                    except Exception: plex_ep=f"S{self.season_number:02d}E{safe_name(ep_label)}"
                    output=media_dir/f"{safe_name(self.anime_title)} - {plex_ep}.mkv"
                    self.emit_progress(ep_index,total_eps,90,f"Серия {ep_label} ({ep_index}/{len(episodes)}): объединение {len(source_files)} озвучек в MKV…")
                    try:
                        def mp(pct): self.emit_progress(ep_index,total_eps,90+pct*0.10,f"Серия {ep_label} ({ep_index}/{len(episodes)}): MKVToolNix — {pct}%")
                        merge_audio_tracks(self.mkvmerge,source_files[1:],output,progress_cb=mp,
                                           video_source=source_files[0][0]); completed+=1
                        try:
                            first_item=self.episode_items[ep].get("__video__") or self.episode_items[ep].get(self.dubbings[0])
                            self.ensure_chapters(output,first_item,ep,chapter_source=source_files[0][0])
                        except Exception as chapter_error:
                            LOGGER.exception("Chapter processing failed for merged episode=%s",ep)
                            errors.append(f"Серия {ep_label}: главы не добавлены: {chapter_error}")
                        if not self.keep_sources: shutil.rmtree(temp_dir,ignore_errors=True)
                    except Exception as e:
                        LOGGER.exception("MKV merge failed for episode=%s",ep)
                        errors.append(f"Серия {ep_label}: MKVToolNix: {e}")
                else: completed+=1
                self.emit_progress(ep_index,total_eps,100,f"Серия {ep_label} ({ep_index}/{len(episodes)}): готово.")
            if use_merge and not self.keep_sources:
                try: (title_dir/".tmp").rmdir()
                except Exception: pass
            self.progress.emit(100,100,"Готово")
            self.done.emit(f"Готово. Обработано серий: {completed}. Ошибок/пропусков: {len(errors)}.",errors)
        except Exception as e:
            LOGGER.exception("Download worker failed")
            self.failed.emit(str(e))


class UpdateCheckThread(QThread):
    available = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            manifest = fetch_manifest()
            if newer_version(manifest["version"], APP_VERSION):
                self.available.emit(manifest)
        except Exception as error:
            LOGGER.warning("Update check failed: %s", error)
            self.failed.emit(str(error))


class UpdateDownloadThread(QThread):
    progress = Signal(int, str)
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, asset, destination):
        super().__init__()
        self.asset = asset
        self.destination = destination

    def run(self):
        try:
            download_verified(self.asset, self.destination, self.progress.emit)
            self.ready.emit(str(self.destination))
        except Exception as error:
            LOGGER.exception("Update download failed")
            self.failed.emit(str(error))


class SettingsDialog(QDialog):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.resize(720, 390)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.public_token = QLineEdit(config.get("public_token", ""))
        self.public_token.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.public_token.setPlaceholderText("Публичный токен приложения YummyAnime")

        self.lang = QComboBox()
        self.lang.addItems(["ru", "en", "uk"])
        self.lang.setCurrentText(config.get("lang", "ru"))

        mkv_row = QWidget()
        mkv_layout = QHBoxLayout(mkv_row)
        mkv_layout.setContentsMargins(0, 0, 0, 0)
        self.mkv_path = QLineEdit(config.get("mkvmerge_path", "") or find_mkvmerge())
        browse = QPushButton("Обзор…")
        auto = QPushButton("Найти")
        browse.clicked.connect(self.browse_mkv)
        auto.clicked.connect(lambda: self.mkv_path.setText(find_mkvmerge()))
        mkv_layout.addWidget(self.mkv_path, 1)
        mkv_layout.addWidget(auto)
        mkv_layout.addWidget(browse)

        ff_row = QWidget()
        ff_layout = QHBoxLayout(ff_row)
        ff_layout.setContentsMargins(0, 0, 0, 0)
        self.ffmpeg_path = QLineEdit(config.get("ffmpeg_path", "") or find_ffmpeg())
        ff_browse = QPushButton("Обзор…")
        ff_auto = QPushButton("Найти")
        ff_browse.clicked.connect(self.browse_ffmpeg)
        ff_auto.clicked.connect(lambda: self.ffmpeg_path.setText(find_ffmpeg()))
        ff_layout.addWidget(self.ffmpeg_path, 1)
        ff_layout.addWidget(ff_auto)
        ff_layout.addWidget(ff_browse)

        self.alloha_resolver = QLineEdit(config.get("alloha_resolver_url", "http://127.0.0.1:8790"))
        self.alloha_resolver.setPlaceholderText("http://127.0.0.1:8790")
        self.diagnostic_log = QCheckBox("Подробный журнал для диагностики ошибок")
        self.diagnostic_log.setChecked(bool(config.get("diagnostic_logging", False)))
        self.auto_update = QCheckBox("Проверять обновления при запуске")
        self.auto_update.setChecked(bool(config.get("auto_update", True)))
        open_logs = QPushButton("Открыть папку логов")
        open_logs.clicked.connect(self.open_log_folder)

        form.addRow("Публичный токен (X-Application):", self.public_token)
        form.addRow("Язык:", self.lang)
        form.addRow("mkvmerge.exe:", mkv_row)
        form.addRow("ffmpeg.exe:", ff_row)
        form.addRow("Alloha resolver server:", self.alloha_resolver)
        form.addRow(self.diagnostic_log, open_logs)
        form.addRow(self.auto_update)
        layout.addLayout(form)

        token_note = QLabel(
            "Приватный токен YummyAnime не нужен. Kodik/CVH/Aksor/Sibnet/Rutube/VK/Zedfilm "
            "обрабатываются встроенными resolver-ами. Alloha требует официальный self-hosted "
            "YummyAnime resolver server. Локальный resolver устанавливается и запускается автоматически."
        )
        token_note.setWordWrap(True)
        layout.addWidget(token_note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def browse_mkv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите mkvmerge.exe", "", "mkvmerge (mkvmerge.exe);;Все файлы (*)"
        )
        if path:
            self.mkv_path.setText(path)

    def browse_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите ffmpeg.exe", "", "FFmpeg (ffmpeg.exe);;Все файлы (*)"
        )
        if path:
            self.ffmpeg_path.setText(path)

    def open_log_folder(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    def values(self):
        return {
            "public_token": self.public_token.text().strip(),
            "lang": self.lang.currentText(),
            "mkvmerge_path": self.mkv_path.text().strip(),
            "ffmpeg_path": self.ffmpeg_path.text().strip(),
            "alloha_resolver_url": self.alloha_resolver.text().strip().rstrip("/"),
            "diagnostic_logging": self.diagnostic_log.isChecked(),
            "auto_update": self.auto_update.isChecked(),
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1050, 760)

        self.config = load_config()
        configure_logging(bool(self.config.get("diagnostic_logging", False)))
        LOGGER.info("Starting %s %s", APP_NAME, APP_VERSION)
        if not self.config.get("mkvmerge_path"):
            self.config["mkvmerge_path"] = find_mkvmerge()
        if not self.config.get("ffmpeg_path"):
            self.config["ffmpeg_path"] = find_ffmpeg()
        if "alloha_resolver_url" not in self.config:
            self.config["alloha_resolver_url"] = "http://127.0.0.1:8790"

        self.anime = None
        self.videos = []
        self.player_map = {}
        self.dub_checks = {}
        self.fetch_thread = None
        self.work_thread = None
        self.quality_threads = []
        self.quality_probe_serial = 0
        self.update_check_thread = None
        self.update_download_thread = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        # URL row
        row = QHBoxLayout()
        self.url_edit = QComboBox()
        self.url_edit.setEditable(True)
        self.url_edit.setInsertPolicy(QComboBox.NoInsert)
        self.url_edit.addItems(self.config.get("url_history", [])[:10])
        self.url_edit.setCurrentIndex(-1)
        self.url_edit.lineEdit().setPlaceholderText("Вставьте ссылку на страницу аниме")
        self.load_btn = QPushButton("Загрузить")
        self.settings_btn = QPushButton("Настройки")
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.load_btn)
        row.addWidget(self.settings_btn)
        layout.addLayout(row)

        self.title_label = QLabel("Вставьте ссылку на аниме.")
        self.title_label.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(self.title_label)

        # Player + quality
        selector = QHBoxLayout()
        self.player_combo = QComboBox()
        self.player_combo.setMinimumWidth(320)
        self.quality_combo = QComboBox()
        self.quality_combo.setMinimumWidth(170)
        self.quality_combo.addItem("Загрузите аниме", None)
        self.quality_combo.setEnabled(False)
        self.season_combo = QComboBox()
        self.season_combo.setMinimumWidth(130)
        self.season_combo.addItem("Сезон 1", 1)
        selector.addWidget(QLabel("Источник видео:")); selector.addWidget(self.player_combo, 1); selector.addSpacing(20)
        selector.addWidget(QLabel("Качество:")); selector.addWidget(self.quality_combo); selector.addSpacing(20)
        selector.addWidget(QLabel("Сезон:")); selector.addWidget(self.season_combo)
        layout.addLayout(selector)
        self.quality_status = QLabel("")
        self.quality_status.setWordWrap(True)
        layout.addWidget(self.quality_status)

        self.source_status = QLabel("")
        self.source_status.setWordWrap(True)
        layout.addWidget(self.source_status)

        self.plex_structure = QCheckBox(
            "Plex-структура: Название аниме / Season XX / Название - SxxEyy.mkv"
        )
        self.plex_structure.setChecked(True)
        layout.addWidget(self.plex_structure)

        self.plexmatch_check = QCheckBox(
            "Создавать .plexmatch для Plex (название + год + поддерживаемый внешний ID)"
        )
        self.plexmatch_check.setChecked(True)
        self.plexmatch_check.setToolTip(
            "Файл создаётся в корне папки аниме и помогает Plex точнее сопоставить сериал."
        )
        layout.addWidget(self.plexmatch_check)

        splitter = QSplitter()

        # dubbings
        dub_box = QGroupBox("Озвучки")
        dub_layout = QVBoxLayout(dub_box)
        self.dub_list = QListWidget()
        dub_layout.addWidget(self.dub_list)
        splitter.addWidget(dub_box)

        # episodes
        ep_box = QGroupBox("Серии, доступные во всех выбранных озвучках")
        ep_layout = QVBoxLayout(ep_box)
        ep_buttons = QHBoxLayout()
        self.ep_all = QPushButton("Выбрать все")
        self.ep_none = QPushButton("Снять все")
        ep_buttons.addWidget(self.ep_all)
        ep_buttons.addWidget(self.ep_none)
        ep_buttons.addStretch()
        ep_layout.addLayout(ep_buttons)
        self.ep_list = QListWidget()
        ep_layout.addWidget(self.ep_list)
        splitter.addWidget(ep_box)
        splitter.setSizes([430, 570])

        layout.addWidget(splitter, 1)

        # merge
        merge_box = QGroupBox("MKV и озвучки")
        merge_layout = QVBoxLayout(merge_box)
        self.merge_check = QCheckBox("Собрать видео и выбранные озвучки в MKV")
        self.merge_check.setToolTip("Видео берётся из выбранного плеера, аудио — из любых плееров; нужен MKVToolNix.")
        self.merge_check.setChecked(True)
        self.keep_sources = QCheckBox("Сохранять исходные файлы после сборки MKV")
        self.chapters_check = QCheckBox("Добавлять главы из видео или AniSkip")
        self.chapters_check.setToolTip(
            "Сначала используются главы исходного видео. Если их нет, ищутся метки OP/ED/recap "
            "по MyAnimeList ID и длительности серии."
        )
        self.chapters_check.setChecked(bool(self.config.get("chapters_enabled", True)))
        self.mkv_status = QLabel("")
        merge_layout.addWidget(self.merge_check)
        merge_layout.addWidget(self.keep_sources)
        merge_layout.addWidget(self.chapters_check)
        merge_layout.addWidget(self.mkv_status)
        layout.addWidget(merge_box)

        # output
        out = QHBoxLayout()
        self.folder_edit = QLineEdit(
            self.config.get("download_dir", str(Path.home() / "Downloads" / "Anime"))
        )
        self.folder_btn = QPushButton("Папка…")
        self.download_btn = QPushButton("Скачать")
        out.addWidget(QLabel("Куда:"))
        out.addWidget(self.folder_edit, 1)
        out.addWidget(self.folder_btn)
        out.addWidget(self.download_btn)
        layout.addLayout(out)

        self.current_status_label = QLabel("Готово")
        self.current_status_label.setWordWrap(True)
        layout.addWidget(self.current_status_label)
        self.series_progress = QProgressBar(); self.series_progress.setRange(0,100); self.series_progress.setValue(0); self.series_progress.setFormat("Текущая серия: %p%")
        layout.addWidget(self.series_progress)
        self.overall_progress = QProgressBar(); self.overall_progress.setRange(0,100); self.overall_progress.setValue(0); self.overall_progress.setFormat("Общий прогресс: %p%")
        layout.addWidget(self.overall_progress)

        self.hint = QLabel(
            "Поддерживаются текущие источники YummyAnime/YummyTV: Kodik, CVH, Aksor, "
            "Sibnet, Rutube, VK, Zedfilm и прямые media URL. Alloha скачивается через "
            "официальный self-hosted YummyAnime resolver server."
        )
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.load_btn.clicked.connect(self.load_anime)
        self.settings_btn.clicked.connect(self.show_settings)
        self.player_combo.currentTextChanged.connect(self.on_player_changed)
        self.season_combo.currentIndexChanged.connect(self.on_season_changed)
        self.dub_list.itemChanged.connect(self.rebuild_episodes)
        self.ep_all.clicked.connect(lambda: self.set_episode_checks(Qt.Checked))
        self.ep_none.clicked.connect(lambda: self.set_episode_checks(Qt.Unchecked))
        self.folder_btn.clicked.connect(self.choose_folder)
        self.download_btn.clicked.connect(self.start_download)

        self.update_mkv_status()
        update_error = self.previous_update_error()
        if update_error:
            QTimer.singleShot(0, lambda: QMessageBox.warning(
                self, APP_NAME, f"Не удалось применить обновление: {update_error}"))
        if (not update_error and getattr(sys, "frozen", False)
                and self.config.get("auto_update", True)):
            QTimer.singleShot(0, self.check_for_updates)

    def previous_update_error(self):
        result_file = CONFIG_DIR / "update-result.txt"
        try:
            result = result_file.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return None
        result_file.unlink(missing_ok=True)
        return result.removeprefix("error: ") if result.startswith("error: ") else None

    def check_for_updates(self):
        self.update_check_thread = UpdateCheckThread(self)
        self.update_check_thread.available.connect(self.offer_update)
        self.update_check_thread.start()

    def offer_update(self, manifest):
        answer = QMessageBox.question(
            self, APP_NAME,
            f"Доступна версия {manifest['version']}. Скачать и перезапустить приложение?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        current = Path(sys.executable)
        destination = current.with_name(current.stem + ".new.exe")
        self.current_status_label.setText("Скачиваю обновление…")
        self.update_download_thread = UpdateDownloadThread(manifest["exe"], destination)
        self.update_download_thread.progress.connect(
            lambda percent, message: self.current_status_label.setText(f"Обновление: {message}"))
        self.update_download_thread.ready.connect(
            lambda path: self.apply_exe_update(path, manifest["exe"]["sha256"]))
        self.update_download_thread.failed.connect(
            lambda error: QMessageBox.warning(self, APP_NAME, f"Не удалось скачать обновление: {error}"))
        self.update_download_thread.start()

    def apply_exe_update(self, staged, digest):
        try:
            if self.update_download_thread is not None:
                self.update_download_thread.wait(5000)
            schedule_exe_replacement(sys.executable, staged, digest)
        except Exception as error:
            LOGGER.exception("Could not schedule update")
            QMessageBox.warning(self, APP_NAME, f"Не удалось применить обновление: {error}")
            return
        QApplication.quit()

    def current_season(self):
        try: return int(self.season_combo.currentData())
        except Exception: return infer_season_from_title(self.anime)
    def available_seasons_for_player(self):
        items=self.videos
        explicit=sorted({int(v.season_hint) for v in items if v.season_hint is not None})
        return explicit or [infer_season_from_title(self.anime)]
    def rebuild_seasons(self):
        prev=self.current_season() if self.season_combo.count() else None; seasons=self.available_seasons_for_player()
        self.season_combo.blockSignals(True); self.season_combo.clear()
        for s in seasons: self.season_combo.addItem("Спецвыпуски" if s==0 else f"Сезон {s}",s)
        if prev in seasons: self.season_combo.setCurrentIndex(seasons.index(prev))
        elif seasons: self.season_combo.setCurrentIndex(0)
        self.season_combo.blockSignals(False)
    def player_items_for_current_season(self):
        items=list(self.player_map.get(self.player_combo.currentText(),[])); explicit=[x for x in items if x.season_hint is not None]
        return [x for x in items if x.season_hint==self.current_season()] if explicit else items
    def all_items_for_current_season(self):
        return [v for v in self.videos if v.season_hint is None or v.season_hint == self.current_season()]
    def on_player_changed(self):
        self.update_source_status(); self.rebuild_episodes()
    def on_season_changed(self): self.rebuild_dubbings()
    def schedule_quality_probe(self):
        selected=self.selected_dubbings(); matrix=self.episode_matrix(); common=[ep for ep,dubs in matrix.items() if all(d in dubs for d in selected)]
        self.quality_probe_serial+=1; serial=self.quality_probe_serial
        self.quality_combo.clear(); self.quality_combo.addItem("Проверяю…",None); self.quality_combo.setEnabled(False)
        self.quality_status.setText("Проверяю реальные качества выбранного источника…")
        if not selected or not common:
            self.quality_combo.clear(); self.quality_combo.addItem("Нет доступных серий",None); self.quality_status.setText(""); return
        ep=sorted(common)[0]; items=[matrix[ep]["__video__"]]
        thread=QualityProbeThread(serial,self.config,items); self.quality_threads.append(thread); thread.done.connect(self.on_quality_probe_done)
        def cleanup():
            try: self.quality_threads.remove(thread)
            except ValueError: pass
        thread.finished.connect(cleanup); thread.start()
    def on_quality_probe_done(self,serial,qualities,note):
        if serial!=self.quality_probe_serial: return
        self.quality_combo.blockSignals(True); self.quality_combo.clear(); self.quality_combo.addItem("Лучшее доступное","Лучшее")
        for label in qualities: self.quality_combo.addItem("Авто (адаптивное)" if label=="auto" else label,label)
        self.quality_combo.setEnabled(True); self.quality_combo.setCurrentIndex(0); self.quality_combo.blockSignals(False)
        if qualities:
            shown=", ".join("Авто" if x=="auto" else x for x in qualities); self.quality_status.setText(f"Реально найдено: {shown}. {note}")
        else:
            self.quality_status.setText("Фиксированные разрешения определить заранее не удалось. «Лучшее доступное» будет определено при скачивании. "+note)

    def update_source_status(self):
        player = self.player_combo.currentText()
        sample = next(iter(self.player_map.get(player, [])), None)
        if not sample:
            self.source_status.setText("")
            return
        kind = provider_kind(sample)
        label = provider_label(sample)
        if kind == "alloha":
            base = self.config.get("alloha_resolver_url", "")
            self.source_status.setText(
                f"Источник: {label}. Скачивание через YummyAnime resolver server"
                + (f" ({base})" if base else " — адрес не настроен.")
            )
        elif kind == "unknown":
            self.source_status.setText(
                "Источник не распознан текущей версией resolver-а."
            )
        else:
            self.source_status.setText(f"Источник: {label}. Скачивание поддерживается.")

    def show_settings(self):
        dlg = SettingsDialog(self, self.config)
        if dlg.exec():
            self.config.update(dlg.values())
            self.config["download_dir"] = self.folder_edit.text()
            save_config(self.config)
            configure_logging(bool(self.config.get("diagnostic_logging", False)))
            LOGGER.info("Settings saved")
            self.update_mkv_status()
            self.update_source_status()

    def update_mkv_status(self):
        p = self.config.get("mkvmerge_path", "")
        ok = bool(p and Path(p).exists())
        self.mkv_status.setText(
            f"MKVToolNix: {'найден — ' + p if ok else 'не найден; укажите mkvmerge.exe в Настройках'}"
        )

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Папка для загрузок", self.folder_edit.text()
        )
        if folder:
            self.folder_edit.setText(folder)

    def load_anime(self):
        try:
            url = self.url_edit.currentText().strip()
            slug = extract_slug(url)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return

        if not self.config.get("public_token"):
            self.show_settings()
            if not self.config.get("public_token"):
                return

        self.config["url_history"] = remember_url(self.config.get("url_history"), url)
        self.url_edit.clear()
        self.url_edit.addItems(self.config["url_history"])
        self.url_edit.setCurrentIndex(0)
        save_config(self.config)
        LOGGER.info("Loading anime slug=%s URL=%s", slug, safe_url(url))

        self.load_btn.setEnabled(False)
        self.title_label.setText("Загрузка…")
        self.player_combo.clear(); self.dub_list.clear(); self.ep_list.clear(); self.season_combo.clear()
        self.quality_combo.clear(); self.quality_combo.addItem("Загрузка…",None); self.quality_combo.setEnabled(False); self.quality_status.setText("")
        self.statusBar().showMessage("Запрос YummyAnime API…")

        self.fetch_thread = FetchThread(
            self.config["public_token"],
            self.config.get("lang", "ru"),
            slug,
        )
        self.fetch_thread.loaded.connect(self.on_loaded)
        self.fetch_thread.failed.connect(self.on_load_failed)
        self.fetch_thread.start()

    def on_load_failed(self, error):
        LOGGER.error("Anime load failed: %s",error)
        self.load_btn.setEnabled(True)
        self.title_label.setText("Ошибка")
        QMessageBox.critical(self, APP_NAME, error)

    def on_loaded(self, anime, raw):
        self.load_btn.setEnabled(True)
        self.anime = anime
        LOGGER.info("Anime loaded: video_records=%s",len(raw) if isinstance(raw,list) else 0)

        title = anime.get("title") or anime.get("name") or "Без названия"
        if isinstance(title, dict):
            title = title.get("ru") or title.get("en") or next(iter(title.values()), "Без названия")
        self.title_label.setText(str(title))

        self.videos = []
        for v in raw if isinstance(raw, list) else []:
            data = v.get("data") or {}
            self.videos.append(VideoItem(
                video_id=int(v.get("video_id", 0) or 0),
                player=str(data.get("player") or "Неизвестный плеер"),
                dubbing=str(data.get("dubbing") or "Без названия"),
                number=str(v.get("number") or ""),
                index=int(v.get("index", 0) or 0),
                iframe_url=str(v.get("iframe_url") or ""),
                player_id=data.get("player_id"),
                duration=v.get("duration"),
                season_hint=video_season_hint(v, v.get("iframe_url"), anime),
            ))

        self.player_map = defaultdict(list)
        for v in self.videos:
            self.player_map[v.player].append(v)

        self.player_combo.blockSignals(True)
        self.player_combo.clear()
        self.player_combo.addItems(sorted(self.player_map, key=str.casefold))
        cvh_index = next((i for i in range(self.player_combo.count())
                          if "cvh" in self.player_combo.itemText(i).casefold()), -1)
        if cvh_index >= 0:
            self.player_combo.setCurrentIndex(cvh_index)
        self.player_combo.blockSignals(False)

        self.rebuild_seasons()
        self.rebuild_dubbings()
        self.statusBar().showMessage(
            f"Найдено {len(self.player_map)} плееров, {len(self.videos)} записей видео."
        )

    def rebuild_dubbings(self):
        self.update_source_status()
        self.dub_list.blockSignals(True)
        self.dub_list.clear()

        dubbings = sorted({v.dubbing for v in self.all_items_for_current_season()}, key=str.casefold)

        for i, dub in enumerate(dubbings):
            item = QListWidgetItem(dub)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if i == 0 else Qt.Unchecked)
            self.dub_list.addItem(item)

        self.dub_list.blockSignals(False)
        self.rebuild_episodes()

    def selected_dubbings(self):
        return [
            self.dub_list.item(i).text()
            for i in range(self.dub_list.count())
            if self.dub_list.item(i).checkState() == Qt.Checked
        ]

    def episode_matrix(self):
        selected = self.selected_dubbings()
        matrix = defaultdict(dict)
        priority = {"cvh": 0, "kodik": 1, "sibnet": 2, "alloha": 3, "aksor": 4}
        items = sorted(self.all_items_for_current_season(),
                       key=lambda v: (priority.get(provider_kind(v), 5), v.player, v.video_id))
        for v in items:
            if v.dubbing in selected:
                matrix[v.episode_key].setdefault(v.dubbing, v)
                matrix[v.episode_key].setdefault("__audio_candidates__", {}).setdefault(v.dubbing, []).append(v)
        for v in self.player_items_for_current_season():
            # Video is independent of which audio studios were selected.
            matrix[v.episode_key].setdefault("__video__", v)
            if selected and v.dubbing == selected[0]:
                matrix[v.episode_key]["__video__"] = v
                matrix[v.episode_key][selected[0]] = v
        for dubs in matrix.values():
            video = dubs.get("__video__")
            if video and video.dubbing in selected:
                dubs[video.dubbing] = video
                candidates = dubs["__audio_candidates__"][video.dubbing]
                dubs["__audio_candidates__"][video.dubbing] = [video] + [v for v in candidates if v != video]
        return {ep: dubs for ep, dubs in matrix.items() if "__video__" in dubs}

    def rebuild_episodes(self):
        self.ep_list.blockSignals(True)
        previous = {
            self.ep_list.item(i).data(Qt.UserRole)
            for i in range(self.ep_list.count())
            if self.ep_list.item(i).checkState() == Qt.Checked
        }
        self.ep_list.clear()

        selected = self.selected_dubbings()
        if not selected:
            self.ep_list.blockSignals(False); self.merge_check.setEnabled(False); self.quality_probe_serial += 1
            self.quality_combo.clear(); self.quality_combo.addItem("Выберите озвучку",None); self.quality_combo.setEnabled(False); self.quality_status.setText("")
            return

        matrix = self.episode_matrix()
        common = [
            ep for ep, dubs in matrix.items()
            if all(d in dubs for d in selected)
        ]

        for ep in sorted(common):
            item = QListWidgetItem(f"Серия {episode_label(ep)}")
            item.setData(Qt.UserRole, ep)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if not previous or ep in previous else Qt.Unchecked)
            self.ep_list.addItem(item)

        self.ep_list.blockSignals(False)
        requires_mux = any(
            any(matrix[ep][dub] != matrix[ep]["__video__"] for dub in selected)
            for ep in common)
        self.merge_check.setEnabled(len(selected) > 1 and not requires_mux)
        self.merge_check.setChecked(requires_mux or len(selected) > 1)

        self.statusBar().showMessage(
            f"Выбрано озвучек: {len(selected)} · общих серий: {len(common)}"
        )
        self.schedule_quality_probe()

    def set_episode_checks(self, state):
        for i in range(self.ep_list.count()):
            self.ep_list.item(i).setCheckState(state)

    def selected_episodes(self):
        return [
            self.ep_list.item(i).data(Qt.UserRole)
            for i in range(self.ep_list.count())
            if self.ep_list.item(i).checkState() == Qt.Checked
        ]

    def anime_title(self):
        return anime_display_title(self.anime)

    def start_download(self):
        player = self.player_combo.currentText()
        dubbings = self.selected_dubbings()
        episodes = self.selected_episodes()

        if not player:
            QMessageBox.information(self, APP_NAME, "Сначала загрузите тайтл.")
            return
        if not dubbings:
            QMessageBox.information(self, APP_NAME, "Выберите хотя бы одну озвучку.")
            return
        if not episodes:
            QMessageBox.information(self, APP_NAME, "Выберите хотя бы одну серию.")
            return

        matrix = self.episode_matrix()
        selected_matrix = {ep: matrix[ep] for ep in episodes if ep in matrix}

        do_merge = self.merge_check.isChecked() and len(dubbings) > 1
        do_merge = do_merge or any(
            any(items[dub] != items["__video__"] for dub in dubbings)
            for items in selected_matrix.values())
        if do_merge:
            mkv = self.config.get("mkvmerge_path", "")
            if not mkv or not Path(mkv).exists():
                QMessageBox.warning(
                    self, APP_NAME,
                    "Для сборки видео и аудиодорожек из разных источников нужен mkvmerge.exe.\n"
                    "Откройте Настройки и укажите путь к MKVToolNix."
                )
                return

        self.config["download_dir"] = self.folder_edit.text()
        self.config["chapters_enabled"] = self.chapters_check.isChecked()
        save_config(self.config)

        quality_value=self.quality_combo.currentData()
        if not quality_value:
            QMessageBox.information(self,APP_NAME,"Дождитесь определения доступных качеств или выберите доступное качество."); return
        self.download_btn.setEnabled(False); self.series_progress.setValue(0); self.overall_progress.setValue(0)
        self.current_status_label.setText("Подготовка загрузки…")
        LOGGER.info("Selection: player=%s dubbings=%s episodes=%s quality=%s merge=%s",
                    player,dubbings,episodes,quality_value,do_merge)

        self.work_thread = WorkThread(
            player=player,
            dubbings=dubbings,
            episode_items=selected_matrix,
            quality=quality_value,
            base_dir=self.folder_edit.text(),
            anime_title=self.anime_title(),
            merge_enabled=do_merge,
            mkvmerge_path=self.config.get("mkvmerge_path", ""),
            keep_sources=self.keep_sources.isChecked(),
            season_number=self.current_season(),
            plex_structure=self.plex_structure.isChecked(),
            plexmatch_enabled=self.plexmatch_check.isChecked(),
            anime_metadata=self.anime,
            resolver_config=self.config,
            ffmpeg_path=self.config.get("ffmpeg_path", "") or find_ffmpeg(),
            chapters_enabled=self.chapters_check.isChecked(),
        )
        self.work_thread.progress.connect(self.on_progress)
        self.work_thread.done.connect(self.on_done)
        self.work_thread.failed.connect(self.on_work_failed)
        self.work_thread.start()

    def on_progress(self, series_value, overall_value, text):
        self.series_progress.setValue(series_value); self.overall_progress.setValue(overall_value)
        self.current_status_label.setText(text); self.statusBar().showMessage(text)

    def on_done(self, summary, errors):
        LOGGER.info("Download finished: %s errors=%s",summary,len(errors))
        self.download_btn.setEnabled(True); self.series_progress.setValue(100); self.overall_progress.setValue(100)
        self.current_status_label.setText(summary)
        detail = ""
        if errors:
            detail = "\n\n" + "\n".join(errors[:12])
            if len(errors) > 12:
                detail += f"\n…и ещё {len(errors) - 12}"
        QMessageBox.information(self, APP_NAME, summary + detail)

    def on_work_failed(self, error):
        LOGGER.error("Download failed: %s",error)
        self.download_btn.setEnabled(True); self.current_status_label.setText(f"Ошибка: {error}")
        QMessageBox.critical(self, APP_NAME, error)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    install_exception_hooks()
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
