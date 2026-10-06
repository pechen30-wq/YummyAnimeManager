
import sys
import re
import json
import hashlib
import shutil
import subprocess
import tempfile
import time
import threading
import random
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from resilient_download import (download_file, download_ranges, RangeUnsupportedError,
                                RangeDownloadError, DownloadControl, PauseDownload, StopDownload)
from hls_download import stage_hls, media_playlist
from chapters import aniskip_points, inspect_media, remux_to_mkv
from process_utils import hidden_subprocess_kwargs
from diagnostics import LOGGER, LOG_DIR, configure_logging, install_exception_hooks, safe_url
from url_history import remember_url

from resolvers import (
    PlayerResolver, StreamResult, choose_stream, find_ffmpeg, provider_kind,
    provider_label, system_proxy_for, CHROME_UA, SourceUnavailableError,
    DIRECT_MEDIA_EXTS as RESOLVER_MEDIA_EXTS,
)
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer
from updater import (download_verified, fetch_manifest,
                     newer_version, schedule_exe_replacement, validate_executable)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QProgressBar, QComboBox, QDialog, QFormLayout,
    QDialogButtonBox, QCheckBox, QGroupBox, QSplitter
)

APP_NAME = "YummyAnime Manager"
APP_VERSION = "4.8.4"
YUMMY_API_BASE = "https://api.yani.tv"
CVH_API_BASE = "https://plapi.cdnvideohub.com/api/v1/player/sv"

CONFIG_DIR = Path.home() / ".yummy_anime_manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
CHECKPOINT_FILE = CONFIG_DIR / "pending-download.json"

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
    views: int = 0

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


def encode_episode_matrix(matrix):
    def encode(value):
        if isinstance(value,VideoItem):
            return {"$video_item":asdict(value)}
        if isinstance(value,list):
            return [encode(item) for item in value]
        if isinstance(value,dict):
            return {str(key):encode(item) for key,item in value.items()}
        return value
    return {str(episode):encode(items) for episode,items in matrix.items()}


def decode_episode_matrix(data):
    def decode(value):
        if isinstance(value,dict) and "$video_item" in value:
            return VideoItem(**value["$video_item"])
        if isinstance(value,list):
            return [decode(item) for item in value]
        if isinstance(value,dict):
            return {key:decode(item) for key,item in value.items()}
        return value
    return {float(episode):decode(items) for episode,items in data.items()}


class DownloadCheckpoint:
    """Atomic task journal; completed files are reused only if unchanged."""
    def __init__(self,payload,path=CHECKPOINT_FILE):
        self.path=Path(path)
        self.payload=payload
        self.lock=threading.Lock()

    @classmethod
    def load(cls,path=CHECKPOINT_FILE):
        path=Path(path)
        if not path.is_file():
            return None
        payload=json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != 1 or not isinstance(payload.get("settings"),dict):
            raise ValueError("Неподдерживаемое состояние загрузки.")
        return cls(payload,path)

    def save(self):
        with self.lock:
            self._save_locked()

    def _save_locked(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+".tmp")
        temporary.write_text(json.dumps(self.payload,ensure_ascii=False,indent=2),encoding="utf-8")
        temporary.replace(self.path)

    def file(self,key):
        with self.lock:
            record=self.payload.get("files",{}).get(key)
        if not record:
            return None
        path=Path(record["path"])
        try:
            stat=path.stat()
            if stat.st_size == record["size"] and stat.st_mtime_ns == record["mtime_ns"]:
                return path, VideoItem(**record["item"])
        except (OSError,KeyError,TypeError,ValueError):
            pass
        return None

    def mark_file(self,key,path,item):
        path=Path(path)
        stat=path.stat()
        with self.lock:
            self.payload.setdefault("files",{})[key]={"path":str(path),"size":stat.st_size,
                                                       "mtime_ns":stat.st_mtime_ns,"item":asdict(item)}
            self._save_locked()

    def complete_episode(self,episode,path):
        path=Path(path)
        stat=path.stat()
        with self.lock:
            self.payload.setdefault("episodes",{})[str(episode)]={"path":str(path),
                "size":stat.st_size,"mtime_ns":stat.st_mtime_ns}
            self._save_locked()

    def episode_complete(self,episode):
        record=self.payload.get("episodes",{}).get(str(episode))
        if not record:
            return False
        try:
            stat=Path(record["path"]).stat()
            return stat.st_size == record["size"] and stat.st_mtime_ns == record["mtime_ns"]
        except (OSError,KeyError,TypeError,ValueError):
            return False

    def clear(self):
        self.path.unlink(missing_ok=True)

    def discard(self):
        """Remove staging files for this task after Stop."""
        try:
            settings=self.payload.get("settings",{})
            base=Path(settings.get("base_dir","")).resolve()
            grouped=bool(settings.get("plex_structure") and settings.get("series_title"))
            show_title=settings.get("series_title") if grouped else settings.get("anime_title", "")
            season=int(settings.get("season_number",1))
            title=base/safe_name(show_title)
            if title.resolve().parent == base and title.is_dir():
                staging=title/".tmp"
                if "series_title" in settings:
                    staging=staging/f"season_{season:02d}"
                if staging.resolve().parent == title.resolve() and staging.is_dir():
                    shutil.rmtree(staging)
                elif (staging.is_dir() and staging.resolve().parent == (title/".tmp").resolve()
                      and (title/".tmp").resolve().parent == title.resolve()):
                    shutil.rmtree(staging)
                for folder in (title,title/f"Season {season:02d}"):
                    if folder.is_dir() and folder.resolve().parent in (title.resolve(),base):
                        for entry in folder.iterdir():
                            prefix=f"{safe_name(show_title)} - S{season:02d}E"
                            if entry.name.startswith(prefix):
                                if entry.name.endswith((".part",".part.url",".range.part",".range.part.json")):
                                    entry.unlink(missing_ok=True)
                            elif entry.name.startswith(".hls_"+prefix) and entry.is_dir():
                                shutil.rmtree(entry)
        finally:
            self.clear()


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


def dubbing_stats(items):
    """Count unique episodes and site views without duplicating player mirrors."""
    by_dubbing = defaultdict(dict)
    for item in items:
        episode_views = by_dubbing[item.dubbing]
        episode_views[item.episode_key] = max(
            episode_views.get(item.episode_key, 0), max(0, item.views)
        )
    return {dub: (set(episodes), sum(episodes.values()))
            for dub, episodes in by_dubbing.items()}



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


def catalog_series_identity(anime):
    """Group TV sequel cards only when the site's order and title agree."""
    title = anime_display_title(anime)
    fallback = (title, infer_season_from_title(anime), anime)
    order = (anime or {}).get("viewing_order") or []
    if not isinstance(order, list) or not order:
        return fallback
    current_id = (anime or {}).get("anime_id")
    position = next((i for i, entry in enumerate(order)
                     if isinstance(entry, dict) and entry.get("anime_id") == current_id), None)
    if position is None:
        return fallback
    first, current = order[0], order[position]
    if not isinstance(first, dict) or not isinstance(current, dict):
        return fallback
    if any((entry.get("type") or {}).get("alias") != "tv"
           for entry in (first, current)):
        return fallback
    root_title = anime_display_title(first)
    season = position + 1
    if position:
        suffix = title[len(root_title):].strip(" -:()") if title.casefold().startswith(root_title.casefold()) else ""
        if not re.fullmatch(rf"(?:(?:season|сезон)\s*)?{season}", suffix, re.I):
            return fallback
    root_metadata = dict(anime)
    root_metadata["title"] = root_title
    root_metadata["year"] = first.get("year") or anime.get("year")
    if position:
        root_metadata["remote_ids"] = {}
    return root_title, season, root_metadata


def linked_tv_seasons(anime):
    """Return season number and card for verified TV sequels in viewing order."""
    root_title, current_season, _ = catalog_series_identity(anime)
    order = (anime or {}).get("viewing_order") or []
    if not isinstance(order, list) or len(order) < 2:
        return []
    current_id = anime.get("anime_id") or anime.get("id")
    current_entry = next((entry for entry in order if isinstance(entry, dict)
                          and entry.get("anime_id") == current_id), None)
    if current_entry is None or (current_entry.get("type") or {}).get("alias") != "tv":
        return []
    group_id = (current_entry.get("data") or {}).get("id")
    if not group_id or root_title != anime_display_title(order[0]):
        return []
    seasons = []
    for number, entry in enumerate(order, 1):
        if not isinstance(entry, dict) or (entry.get("type") or {}).get("alias") != "tv":
            continue
        if (entry.get("data") or {}).get("id") != group_id:
            continue
        title = anime_display_title(entry)
        suffix = title[len(root_title):].strip(" -:()") if title.casefold().startswith(root_title.casefold()) else ""
        if number == 1 and title.casefold() != root_title.casefold():
            continue
        if number > 1 and not re.fullmatch(rf"(?:(?:season|сезон)\s*)?{number}", suffix, re.I):
            continue
        if entry.get("anime_id"):
            seasons.append((number, entry))
    return seasons if any(entry.get("anime_id") == current_id for _, entry in seasons) else []


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
    if not result.is_manifest:
        return 0.0
    def read_manifest(url):
        # Metadata probing must never buffer an accidentally returned media file.
        with requests.get(url, headers=headers, timeout=(5, 10), stream=True) as response:
            response.raise_for_status()
            chunks=[]; size=0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("Manifest exceeds metadata probe limit")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8-sig")
    try:
        headers={"User-Agent":CHROME_UA}; headers.update(result.headers or {})
        body=read_manifest(result.url); low=urllib.parse.urlparse(result.url).path.lower()
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
                vals=re.findall(r"#EXTINF:([0-9.]+)",read_manifest(variants[-1]),re.I)
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


def merge_audio_tracks(mkvmerge, sources, output_path, progress_cb=None, video_source=None, control=None):
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
    if control: control.set_process(proc)
    lines=[]; last=-1
    try:
        for line in proc.stdout or []:
            if control: control.check()
            lines.append(line.rstrip())
            LOGGER.debug("mkvmerge: %s", line.rstrip())
            pm=re.search(r"Progress:\s*(\d+)%",line,re.I)
            if pm:
                pct=max(0,min(100,int(pm.group(1))))
                if pct!=last and progress_cb: progress_cb(pct); last=pct
        code=proc.wait()
        if control: control.check()
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()
        if control: control.clear_process(proc)
        close=getattr(proc.stdout,"close",None)
        if close: close()
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
            seasons = linked_tv_seasons(anime)
            if not seasons:
                self.loaded.emit(anime, api.videos(int(anime_id)))
                return
            records, season_anime = [], {}
            for number, entry in seasons:
                try:
                    if entry.get("anime_id") == anime_id:
                        card = anime
                    else:
                        slug = extract_slug(entry.get("anime_url") or "")
                        card = api.anime(slug) if slug else api.anime(str(entry["anime_id"]))
                    card_id = card.get("anime_id") or card.get("id")
                    if card_id != entry.get("anime_id"):
                        raise RuntimeError("Получена карточка другого сезона")
                    videos = api.videos(int(card_id))
                    if not isinstance(videos, list) or not videos:
                        continue
                    season_anime[number] = card
                    for video in videos:
                        if isinstance(video, dict):
                            records.append({**video, "_catalog_season": number})
                except Exception:
                    if entry.get("anime_id") == anime_id:
                        raise
                    LOGGER.exception("Could not load linked season %s", number)
            anime = dict(anime)
            anime["_catalog_seasons"] = season_anime
            self.loaded.emit(anime, records)
        except Exception as e:
            LOGGER.exception("Anime metadata request failed for slug=%s", self.slug)
            self.failed.emit(str(e))


class QualityProbeThread(QThread):
    done = Signal(int, object, str)
    def __init__(self, serial, config, items):
        super().__init__(); self.serial=serial; self.config=dict(config or {}); self.items=list(items or [])
    def run(self):
        resolver=PlayerResolver(self.config); sets=[]; notes=[]; successes=0; unavailable=False
        for item in self.items:
            stream=None
            try:
                stream=resolver.resolve(item)
                labels=set((stream.qualities or {}).keys())
                if not labels and stream.quality: labels.add(stream.quality)
                labels={str(x) for x in labels if x}
                if labels: sets.append(labels); successes+=1
            except Exception as e:
                unavailable = unavailable or isinstance(e, SourceUnavailableError)
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
            if unavailable:
                note="Источник недоступен: плеер отдаёт ссылки на отсутствующие файлы. Выберите другой плеер для видео; озвучки можно оставить выбранными."
        self.done.emit(self.serial,qualities,note)


def source_attempt_order(candidates):
    """Try other sources of the same voice before repeating a failing CDN."""
    limits = [3 if provider_kind(item) == "alloha" else
              1 if provider_kind(item) == "aksor" else 2 for item in candidates]
    return [item for round_index in range(max(limits, default=0))
            for item, limit in zip(candidates, limits) if round_index < limit]


def matching_audio_variant(qualities, headers, ffmpeg, control=None):
    """Use a smaller Kodik variant only when sampled audio packets are identical."""
    numeric = sorted((int(match.group(1)), url, label)
                     for label, url in qualities.items()
                     if (match := re.fullmatch(r"(\d+)p", str(label)))
                     and urllib.parse.urlsplit(url).path.lower().endswith(".m3u8"))
    if len(numeric) < 2 or not ffmpeg:
        return None
    low, high = numeric[0], numeric[-1]
    deadline=time.monotonic()+30

    def check():
        if control: control.check()
        if time.monotonic()>deadline:
            raise TimeoutError("audio comparison time budget exceeded")

    def segments(entry):
        check()
        body, base = media_playlist(entry[1], headers)
        if any(tag in body for tag in ("#EXT-X-KEY:", "#EXT-X-BYTERANGE:", "#EXT-X-MAP:")):
            return None
        urls=[urllib.parse.urljoin(base, line.strip()) for line in body.splitlines()
              if line.strip() and not line.startswith("#")]
        durations=[round(float(value), 3) for value in
                   re.findall(r"(?m)^#EXTINF:([0-9.]+)", body)]
        return (urls,durations) if len(urls)==len(durations) else None

    def audio_hash(url):
        check()
        with requests.get(url, headers=headers, stream=True, timeout=(10, 25)) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(65536):
                check()
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    return None
        proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                               "-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-hash",
                               "sha256", "pipe:1"], input=bytes(data), capture_output=True,
                              timeout=15, **hidden_subprocess_kwargs())
        return proc.stdout.strip() if proc.returncode == 0 else None

    try:
        small, large = segments(low), segments(high)
        if not small or not large or len(small[0]) != len(large[0]) or small[1] != large[1]:
            return None
        for index in sorted({0, len(small[0]) // 2, len(small[0]) - 1}):
            smaller, larger = audio_hash(small[0][index]), audio_hash(large[0][index])
            if not smaller or smaller != larger:
                return None
        LOGGER.info("Equivalent Kodik audio verified at %s and %s; choosing %s",
                    high[2], low[2], low[2])
        return low[2], low[1]
    except (OSError, requests.RequestException, subprocess.TimeoutExpired, ValueError, RuntimeError) as error:
        LOGGER.debug("Audio variant comparison unavailable: %s", error)
        return None


class WorkThread(QThread):
    progress=Signal(int,int,str)
    done=Signal(str,object)
    failed=Signal(str)
    paused=Signal()
    stopped=Signal()
    def __init__(self,player,dubbings,episode_items,quality,base_dir,anime_title,merge_enabled,
                 mkvmerge_path,keep_sources,season_number=1,plex_structure=True,
                 plexmatch_enabled=True,anime_metadata=None,resolver_config=None,ffmpeg_path="",
                 chapters_enabled=True,checkpoint=None,series_title=None):
        super().__init__(); self.player=player; self.dubbings=dubbings; self.episode_items=episode_items
        self.quality=quality; self.base_dir=Path(base_dir); self.anime_title=anime_title
        self.series_title=series_title or anime_title
        self.merge_enabled=merge_enabled; self.mkvmerge=mkvmerge_path; self.keep_sources=keep_sources
        self.season_number=int(season_number or 1); self.plex_structure=bool(plex_structure)
        self.plexmatch_enabled=bool(plexmatch_enabled); self.anime_metadata=anime_metadata or {}
        self.resolver_config=resolver_config or {}; self.ffmpeg=ffmpeg_path or find_ffmpeg()
        self.chapters_enabled=chapters_enabled
        self.resolver=PlayerResolver(self.resolver_config)
        self._resolver_local=threading.local()
        self._progress_lock=threading.Lock()
        self._progress_episode=None
        self._progress_peak=0
        self._audio_pool=None
        self.checkpoint=checkpoint
        self.control=DownloadControl()
    def emit_progress(self,ep_index,total_eps,series_pct,status):
        self.control.check()
        series_pct=max(0,min(100,int(series_pct)))
        with self._progress_lock:
            if self._progress_episode != ep_index:
                self._progress_episode=ep_index
                self._progress_peak=0
            self._progress_peak=max(self._progress_peak,series_pct)
            series_pct=self._progress_peak
        overall=int((((ep_index-1)+series_pct/100.0)/max(1,total_eps))*100)
        self.progress.emit(series_pct,max(0,min(100,overall)),status)
    def resolve_stream(self,item, audio_only=False):
        resolver=getattr(self._resolver_local,"resolver",self.resolver)
        result=resolver.resolve(item); label,url=choose_stream(result,"Лучшее" if audio_only else self.quality)
        if audio_only:
            # Preserve the master playlist's separate audio renditions.
            master = (result.qualities or {}).get("auto") or result.url
            if master and urllib.parse.urlparse(master).path.lower().endswith(".m3u8"):
                url = master
            if result.source == "kodik" and url == result.url:
                equivalent=matching_audio_variant(result.qualities or {}, result.headers or {},
                                                  self.ffmpeg,self.control)
                if equivalent:
                    label,url=equivalent
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
    def download_mp4_audio(self,result,path,item,progress_cb, *, require_ranges=False):
        if self.checkpoint:
            key=hashlib.sha256(str(getattr(item,"iframe_url",result.url)).encode()).hexdigest()[:12]
            directory=Path(path).parent/f".mp4_audio_{safe_name(Path(path).stem)}_{key}"
            directory.mkdir(parents=True,exist_ok=True)
        else:
            directory=Path(tempfile.mkdtemp(prefix=".mp4_audio_",dir=Path(path).parent))
        finished=False
        try:
            video=Path(directory)/"source.mp4"
            headers={"User-Agent":CHROME_UA}; headers.update(result.headers or {})
            if not video.is_file():
                download_ranges(result.url,video,headers,
                                lambda pct,detail:progress_cb(int(pct*0.9),detail),
                                attempts=2 if require_ranges else 4,require_ranges=require_ranges,
                                control=self.control,resume=bool(self.checkpoint))
            self.control.check()
            local=StreamResult(str(video),"local_file",{},{})
            local.audio_only=True
            self.download_stream(local,path,item,
                                 lambda pct,detail:progress_cb(90+int(pct/10),"Извлечение аудио · "+detail))
            finished=True
        finally:
            if (finished or not self.checkpoint) and directory.resolve().parent == Path(path).parent.resolve():
                shutil.rmtree(directory,ignore_errors=True)

    def download_stream(self,result,path,item,progress_cb):
        self.control.check()
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
                                     audio_only=result.audio_only,control=self.control)
            except requests.RequestException as error:
                raise RuntimeError(f"{result.source}: не удалось получить все сегменты серии.") from error
            local = StreamResult(str(playlist), "local_hls", {}, {})
            local.audio_only = result.audio_only
            self.download_stream(local, path, item,
                                 lambda pct, detail: progress_cb(90+int(pct/10), "Сборка видео · " + detail))
            if cache.resolve().parent == path.parent.resolve():
                shutil.rmtree(cache)
            return
        if result.audio_only and result.source in ("cvh","sibnet") and not result.is_manifest:
            progress_cb(0,"Аудио — загружаю MP4 в четыре потока…")
            try:
                return self.download_mp4_audio(result,path,item,progress_cb,require_ranges=True)
            except (RangeUnsupportedError,RangeDownloadError,requests.RequestException) as error:
                LOGGER.warning("Parallel audio transport unavailable; using FFmpeg: provider=%s reason=%s",
                               result.source,type(error).__name__)
                progress_cb(0,"Источник не принял параллельную загрузку — открываю аудиопоток…")
        if result.is_manifest or result.audio_only:
            progress_cb(0, "Аудио — открываю поток…" if result.audio_only else "Открываю видеопоток…")
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

            if result.audio_only and not result.is_manifest:
                cmd += ["-multiple_requests","1","-short_seek_size","1048576"]

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
            if result.source in ("local_hls", "local_file"):
                cmd=[self.ffmpeg,"-y","-hide_banner","-loglevel","error","-nostats",
                     "-progress","pipe:1","-protocol_whitelist","file,crypto,data"]
                if result.source == "local_hls":
                    cmd += ["-allowed_extensions","ALL"]
                cmd += ["-i",result.url,"-map","0:v?","-map","0:a?","-map","0:s?","-c","copy",str(path)]
            if result.audio_only:
                input_position = cmd.index("-i")
                cmd[input_position:input_position] = ["-discard:v", "all"]
                input_end = cmd.index("-i") + 2
                cmd = cmd[:input_end] + ["-map", "0:a", "-vn", "-sn", "-dn",
                    "-map_metadata", "-1", "-map_chapters", "-1", "-c:a", "copy", str(path)]
            proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                encoding="utf-8",errors="replace",bufsize=1,**hidden_subprocess_kwargs())
            self.control.set_process(proc)
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
                if seconds is not None:
                    downloaded_seconds=max(downloaded_seconds,seconds)
                    if duration>0:
                        pct=max(0,min(99,int(seconds*100/duration)))
                        if pct!=last: progress_cb(pct,f"{pct}%"); last=pct
                    elif seconds>=0:
                        progress_cb(0, f"Получено {'аудио' if result.audio_only else 'видео'}: {int(seconds)//60}:{int(seconds)%60:02d}")
            code=proc.wait()
            close=getattr(proc.stdout,"close",None)
            if close: close()
            self.control.clear_process(proc)
            self.control.check()
            if missing_segment:
                raise RuntimeError("Источник не отдал видеосегмент. Неполная серия не будет сохранена как готовая.")
            network_failure = any(re.search(
                r"Stream ends prematurely|I/O error|End of file|Connection.*failed|Connection.*reset|timed out",
                error, re.I) for error in errors)
            short = duration>0 and downloaded_seconds < duration-max(2.0,duration*0.02)
            if (code!=0 or short) and network_failure and result.audio_only and not result.is_manifest and result.source != "local_file":
                LOGGER.warning("Direct audio interrupted; switching to validated MP4 ranges: provider=%s",result.source)
                progress_cb(0,"Аудиопоток оборван — обновляю ссылку для загрузки MP4 блоками…")
                fresh=self.resolve_stream(item,audio_only=True)
                try:
                    if fresh.is_manifest:
                        return self.download_stream(fresh,path,item,progress_cb)
                    self.download_mp4_audio(fresh,path,item,progress_cb)
                    return
                finally:
                    PlayerResolver.release(fresh)
            if code!=0: raise RuntimeError("\n".join(errors[-12:]) or "FFmpeg завершился с ошибкой.")
            if duration>0 and downloaded_seconds < duration-max(2.0,duration*0.02):
                raise RuntimeError(f"Видео скачано не полностью: {downloaded_seconds:.1f} из {duration:.1f} секунд.")
            progress_cb(100,"100%"); return
        headers={"User-Agent":CHROME_UA}; headers.update(result.headers or {})
        if result.source in ("cvh","sibnet"):
            try:
                download_ranges(result.url, path, headers, progress_cb,
                                control=self.control,resume=bool(self.checkpoint))
            except RangeDownloadError:
                LOGGER.warning("Parallel video transport interrupted; retrying ranges sequentially: provider=%s",result.source)
                progress_cb(0,"Источник прервал параллельную загрузку — повторяю последовательно…")
                download_ranges(result.url,path,headers,progress_cb,workers=1,
                                control=self.control,resume=bool(self.checkpoint))
        else:
            download_file(result.url, path, headers, progress_cb,
                          control=self.control,resume=bool(self.checkpoint))

    def prefetch_audio(self, ep, dub, temp_dir):
        """Prepare one additional track while the preceding track is processed."""
        resolver=PlayerResolver(self.resolver_config)
        self._resolver_local.resolver=resolver
        stream=None
        try:
            self.control.check()
            item=self.episode_items[ep].get("__audio_candidates__",{}).get(dub,[self.episode_items[ep][dub]])[0]
            stream=self.resolve_stream(item,audio_only=True)
            qtag=safe_name(stream.quality or "auto")
            dest=temp_dir/f"{safe_name(dub)} [{qtag}]{self.extension_for(stream)}"
            temp_dir.mkdir(parents=True,exist_ok=True)
            self.download_stream(stream,dest,item,lambda *_: None)
            self.control.check()
            if self.checkpoint: self.checkpoint.mark_file(f"{ep}:{dub}",dest,item)
            LOGGER.info("Audio prefetched: episode=%s dubbing=%s provider=%s",
                        ep,dub,stream.source)
            return dest,item
        except (PauseDownload,StopDownload):
            raise
        except Exception as error:
            LOGGER.warning("Audio prefetch failed: episode=%s dubbing=%s error=%s",
                           ep,dub,error)
            return False
        finally:
            if stream is not None: PlayerResolver.release(stream)
            resolver.session.close()
            del self._resolver_local.resolver

    def run(self):
        try:
            LOGGER.info("Download started: title=%s episodes=%s dubbings=%s quality=%s chapters=%s",
                        self.anime_title,len(self.episode_items),self.dubbings,self.quality,self.chapters_enabled)
            show_title=self.series_title if self.plex_structure else self.anime_title
            title_dir=self.base_dir/safe_name(show_title)
            media_dir=title_dir/f"Season {self.season_number:02d}" if self.plex_structure else title_dir
            if self.plexmatch_enabled:
                metadata = self.anime_metadata
                if show_title != self.anime_title:
                    _, _, metadata = catalog_series_identity(metadata)
                if show_title == self.anime_title or not (title_dir/".plexmatch").exists():
                    write_plexmatch(title_dir,metadata)
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
                self.control.check()
                if self.checkpoint and self.checkpoint.episode_complete(ep):
                    completed+=1
                    self.emit_progress(ep_index,total_eps,100,f"Серия {episode_label(ep)}: уже скачана.")
                    continue
                LOGGER.info("Episode %s started",ep)
                source_files=[]; ep_label=episode_label(ep)
                staging=title_dir/".tmp"
                if self.checkpoint and "series_title" in self.checkpoint.payload.get("settings",{}):
                    staging=staging/f"season_{self.season_number:02d}"
                temp_dir=staging/f"episode_{safe_name(ep_label)}"; failed=False
                prefetch={}
                self.emit_progress(ep_index,total_eps,0,f"Серия {ep_label} ({ep_index}/{len(episodes)}): подготовка…")
                for dub_index,dub in enumerate(download_dubs,1):
                    item=self.episode_items[ep].get(dub); dub_start=(dub_index-1)*dub_span
                    self.emit_progress(ep_index,total_eps,dub_start,f"Серия {ep_label} ({ep_index}/{len(episodes)}): {dub} — получение прямой ссылки…")
                    if not item: errors.append(f"Серия {ep_label}: нет озвучки «{dub}»."); failed=True; break
                    prefetched=None
                    if dub in prefetch:
                        future=prefetch[dub]
                        if future is not None: prefetched=future.result()
                        # Keep at most two active audio jobs. The next job is
                        # queued only after the current result is consumed.
                        remaining=[name for name in self.dubbings if name not in prefetch
                                   and name != self.episode_items[ep]["__video__"].dubbing]
                        if remaining:
                            name=remaining[0]
                            if not (self.checkpoint and self.checkpoint.file(f"{ep}:{name}")):
                                prefetch[name]=self._audio_pool.submit(self.prefetch_audio,ep,name,temp_dir)
                            else:
                                prefetch[name]=None
                    cached=self.checkpoint.file(f"{ep}:{dub}") if self.checkpoint else None
                    if not cached and prefetched and Path(prefetched[0]).is_file():
                        cached=prefetched
                    if cached:
                        cached_path,item=cached
                        if dub == "__video__": self.episode_items[ep]["__video__"]=item
                        source_files.append((cached_path,dub))
                        if dub == "__video__" and use_merge and len(self.dubbings)>1:
                            pending=[name for name in self.dubbings if name != item.dubbing
                                     and not (self.checkpoint and self.checkpoint.file(f"{ep}:{name}"))]
                            if pending:
                                self._audio_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="audio")
                                for name in pending[:2]:
                                    prefetch[name]=self._audio_pool.submit(self.prefetch_audio,ep,name,temp_dir)
                        self.emit_progress(ep_index,total_eps,dub_start+dub_span,
                                           f"Серия {ep_label}: {dub} — уже скачано")
                        continue
                    video_item = self.episode_items[ep].get("__video__")
                    if use_merge and dub != "__video__" and video_item and dub == video_item.dubbing:
                        LOGGER.info("Audio reused from downloaded video: episode=%s dubbing=%s provider=%s",
                                    ep,dub,provider_kind(video_item))
                        self.emit_progress(ep_index,total_eps,dub_start+dub_span,
                                           f"Серия {ep_label}: {dub} — звук из скачанного видео")
                        source_files.append((source_files[0][0],dub))
                        continue
                    if dub == "__video__" or not use_merge:
                        candidates = self.episode_items[ep].get("__video_candidates__", [item])
                        if not use_merge:
                            candidates = [v for v in candidates if v.dubbing == dub] or [item]
                    else:
                        candidates = self.episode_items[ep].get("__audio_candidates__", {}).get(dub, [item])
                    attempt_items = source_attempt_order(candidates)
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
                                self.control.wait(0.8 * (attempt - 1))

                            stream = self.resolve_stream(item, audio_only=use_merge and dub != "__video__")
                            self.control.check()
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
                                dest = media_dir / f"{safe_name(show_title)} - {plex_ep}{suffix}{ext}"

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
                            if dub == "__video__":
                                self.episode_items[ep]["__video__"] = item
                            if not use_merge:
                                try:
                                    dest=self.ensure_chapters(dest,item,ep)
                                except Exception as chapter_error:
                                    LOGGER.exception("Chapter processing failed for episode=%s dubbing=%s",ep,dub)
                                    errors.append(f"Серия {ep_label}, {dub}: главы не добавлены: {chapter_error}")
                            if self.checkpoint: self.checkpoint.mark_file(f"{ep}:{dub}",dest,item)
                            source_files.append((dest, dub))
                            if dub == "__video__" and use_merge and len(self.dubbings)>1:
                                pending=[name for name in self.dubbings if name != item.dubbing
                                         and not (self.checkpoint and self.checkpoint.file(f"{ep}:{name}"))]
                                if pending:
                                    self._audio_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="audio")
                                    for name in pending[:2]:
                                        prefetch[name]=self._audio_pool.submit(self.prefetch_audio,ep,name,temp_dir)
                            last_error = None
                            break

                        except (PauseDownload,StopDownload):
                            raise
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
                if self._audio_pool is not None:
                    self._audio_pool.shutdown(wait=True,cancel_futures=failed)
                    self._audio_pool=None
                if failed:
                    self.emit_progress(ep_index,total_eps,100,f"Серия {ep_label}: пропущена из-за ошибки."); continue
                if use_merge:
                    try: plex_ep=f"S{self.season_number:02d}E{int(float(ep)):02d}"
                    except Exception: plex_ep=f"S{self.season_number:02d}E{safe_name(ep_label)}"
                    output=media_dir/f"{safe_name(show_title)} - {plex_ep}.mkv"
                    self.emit_progress(ep_index,total_eps,90,f"Серия {ep_label} ({ep_index}/{len(episodes)}): объединение {len(source_files)} озвучек в MKV…")
                    try:
                        def mp(pct): self.emit_progress(ep_index,total_eps,90+pct*0.10,f"Серия {ep_label} ({ep_index}/{len(episodes)}): MKVToolNix — {pct}%")
                        merge_audio_tracks(self.mkvmerge,source_files[1:],output,progress_cb=mp,
                                           video_source=source_files[0][0],control=self.control); completed+=1
                        try:
                            first_item=self.episode_items[ep].get("__video__") or self.episode_items[ep].get(self.dubbings[0])
                            self.ensure_chapters(output,first_item,ep,chapter_source=source_files[0][0])
                        except Exception as chapter_error:
                            LOGGER.exception("Chapter processing failed for merged episode=%s",ep)
                            errors.append(f"Серия {ep_label}: главы не добавлены: {chapter_error}")
                        self.control.check()
                        if self.checkpoint: self.checkpoint.complete_episode(ep,output)
                        if not self.keep_sources: shutil.rmtree(temp_dir,ignore_errors=True)
                    except (PauseDownload,StopDownload):
                        raise
                    except Exception as e:
                        LOGGER.exception("MKV merge failed for episode=%s",ep)
                        errors.append(f"Серия {ep_label}: MKVToolNix: {e}")
                else:
                    completed+=1
                    if self.checkpoint and source_files:
                        self.checkpoint.complete_episode(ep,source_files[0][0])
                self.emit_progress(ep_index,total_eps,100,f"Серия {ep_label} ({ep_index}/{len(episodes)}): готово.")
            if use_merge and not self.keep_sources:
                if self.checkpoint and "series_title" in self.checkpoint.payload.get("settings",{}):
                    try: (title_dir/".tmp"/f"season_{self.season_number:02d}").rmdir()
                    except OSError: pass
                try: (title_dir/".tmp").rmdir()
                except OSError: pass
            self.progress.emit(100,100,"Готово")
            if self.checkpoint and not errors: self.checkpoint.clear()
            self.done.emit(f"Готово. Обработано серий: {completed}. Ошибок/пропусков: {len(errors)}.",errors)
        except PauseDownload:
            LOGGER.info("Download paused: %s",self.anime_title)
            self.paused.emit()
        except StopDownload:
            LOGGER.info("Download stopped: %s",self.anime_title)
            self.stopped.emit()
        except Exception as e:
            LOGGER.exception("Download worker failed")
            self.failed.emit(str(e))
        finally:
            if self._audio_pool is not None:
                self._audio_pool.shutdown(wait=True,cancel_futures=True)
                self._audio_pool=None


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
            self.progress.emit(99, "Проверяю запуск обновления…")
            validate_executable(self.destination)
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
        self.season_anime = {}
        self.series_title = None
        self.series_number = 1
        self.videos = []
        self.player_map = {}
        self.dub_checks = {}
        self._episodes_initialized = False
        self.fetch_thread = None
        self.work_thread = None
        self.close_after_pause = False
        try:
            self.checkpoint = DownloadCheckpoint.load(CONFIG_DIR / "pending-download.json")
        except (OSError,ValueError,KeyError,TypeError) as error:
            LOGGER.warning("Cannot read pending download: %s", error)
            self.checkpoint = None
        self.quality_threads = []
        self.quality_probe_serial = 0
        self.quality_probe_timer = QTimer(self)
        self.quality_probe_timer.setSingleShot(True)
        self.quality_probe_timer.setInterval(250)
        self.quality_probe_timer.timeout.connect(self.start_quality_probe)
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
        self.series_label = QLabel("")
        layout.addWidget(self.series_label)

        # Keep the everyday choice prominent; source overrides are optional.
        selector = QHBoxLayout()
        self.player_combo = QComboBox()
        self.player_combo.setMinimumWidth(260)
        self.quality_combo = QComboBox()
        self.quality_combo.setMinimumWidth(170)
        self.quality_combo.addItem("Загрузите аниме", None)
        self.quality_combo.setEnabled(False)
        self.season_combo = QComboBox()
        self.season_combo.setMinimumWidth(130)
        self.season_combo.addItem("Сезон 1", 1)
        selector.addWidget(QLabel("Качество видео:")); selector.addWidget(self.quality_combo); selector.addSpacing(20)
        selector.addWidget(QLabel("Сезон:")); selector.addWidget(self.season_combo)
        selector.addStretch()
        layout.addLayout(selector)
        self.quality_status = QLabel("")
        self.quality_status.setWordWrap(True)
        layout.addWidget(self.quality_status)

        self.source_toggle = QPushButton("Источник видео · изменить ▸")
        self.source_toggle.setCheckable(True)
        self.source_toggle.setFlat(True)
        self.source_toggle.setEnabled(False)
        self.source_toggle.setToolTip("Выбор другого плеера, если автоматически выбранный источник недоступен.")
        source_toggle_row = QHBoxLayout()
        source_toggle_row.addWidget(self.source_toggle)
        source_toggle_row.addStretch()
        layout.addLayout(source_toggle_row)
        self.source_options = QWidget()
        source_layout = QVBoxLayout(self.source_options)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Источник видео:"))
        source_row.addWidget(self.player_combo, 1)
        source_layout.addLayout(source_row)
        self.source_status = QLabel("")
        self.source_status.setWordWrap(True)
        source_layout.addWidget(self.source_status)
        self.source_options.setVisible(False)
        layout.addWidget(self.source_options)

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
        dub_buttons = QHBoxLayout()
        self.dub_all = QPushButton("Выбрать все")
        self.dub_none = QPushButton("Снять все")
        dub_buttons.addWidget(self.dub_all)
        dub_buttons.addWidget(self.dub_none)
        dub_buttons.addStretch()
        dub_layout.addLayout(dub_buttons)
        self.dub_list = QListWidget()
        dub_layout.addWidget(self.dub_list)
        splitter.addWidget(dub_box)

        # episodes
        ep_box = QGroupBox("Серии")
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
        self.pause_btn = QPushButton("Пауза")
        self.resume_btn = QPushButton("Продолжить")
        self.stop_btn = QPushButton("Остановить")
        out.addWidget(QLabel("Куда:"))
        out.addWidget(self.folder_edit, 1)
        out.addWidget(self.folder_btn)
        out.addWidget(self.download_btn)
        out.addWidget(self.pause_btn)
        out.addWidget(self.resume_btn)
        out.addWidget(self.stop_btn)
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
        self.source_toggle.toggled.connect(self.on_source_toggle)
        self.season_combo.currentIndexChanged.connect(self.on_season_changed)
        self.dub_list.itemChanged.connect(self.update_selection_state)
        self.ep_list.itemChanged.connect(self.on_episode_selection_changed)
        self.dub_all.clicked.connect(lambda: self.set_dubbing_checks(Qt.Checked))
        self.dub_none.clicked.connect(lambda: self.set_dubbing_checks(Qt.Unchecked))
        self.ep_all.clicked.connect(lambda: self.set_episode_checks(Qt.Checked))
        self.ep_none.clicked.connect(lambda: self.set_episode_checks(Qt.Unchecked))
        self.folder_btn.clicked.connect(self.choose_folder)
        self.download_btn.clicked.connect(self.start_download)
        self.pause_btn.clicked.connect(self.pause_download)
        self.resume_btn.clicked.connect(self.resume_download)
        self.stop_btn.clicked.connect(self.stop_download)
        self.set_download_buttons()
        if self.checkpoint:
            settings=self.checkpoint.payload["settings"]
            self.current_status_label.setText(
                f"Есть незавершённая загрузка: {settings.get('anime_title','аниме')}. Нажмите «Продолжить».")

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
        if self.season_anime:
            return sorted(self.season_anime)
        if self.series_title and self.series_title != anime_display_title(self.anime):
            return [self.series_number]
        items=self.videos
        explicit=sorted({int(v.season_hint) for v in items if v.season_hint is not None})
        return explicit or [self.series_number]
    def rebuild_seasons(self):
        prev=self.current_season() if self.season_combo.count() else None; seasons=self.available_seasons_for_player()
        self.season_combo.blockSignals(True); self.season_combo.clear()
        for s in seasons: self.season_combo.addItem("Спецвыпуски" if s==0 else f"Сезон {s}",s)
        if prev in seasons: self.season_combo.setCurrentIndex(seasons.index(prev))
        elif self.series_number in seasons: self.season_combo.setCurrentIndex(seasons.index(self.series_number))
        elif seasons: self.season_combo.setCurrentIndex(0)
        self.season_combo.blockSignals(False)
    def player_items_for_current_season(self):
        items=list(self.player_map.get(self.player_combo.currentText(),[])); explicit=[x for x in items if x.season_hint is not None]
        if self.season_anime:
            return [x for x in items if x.season_hint == self.current_season()]
        if self.series_title and self.series_title != anime_display_title(self.anime):
            return items
        return [x for x in items if x.season_hint==self.current_season()] if explicit else items
    def all_items_for_current_season(self):
        if self.season_anime:
            return [v for v in self.videos if v.season_hint == self.current_season()]
        if self.series_title and self.series_title != anime_display_title(self.anime):
            return list(self.videos)
        return [v for v in self.videos if v.season_hint is None or v.season_hint == self.current_season()]
    def on_player_changed(self):
        self.update_source_toggle()
        self.rebuild_episodes()
        self.rebuild_dubbings()
    def on_source_toggle(self, expanded):
        self.source_options.setVisible(expanded)
        self.update_source_toggle()
    def update_source_toggle(self):
        source = self.player_combo.currentText()
        arrow = "▾" if self.source_toggle.isChecked() else "▸"
        self.source_toggle.setText(
            f"Источник видео: {source} · изменить {arrow}" if source
            else f"Источник видео · изменить {arrow}")
    def on_season_changed(self):
        self._episodes_initialized = False
        self.update_season_header()
        self.rebuild_dubbings()
    def current_anime_metadata(self):
        return self.season_anime.get(self.current_season(), self.anime)
    def update_season_header(self):
        metadata = self.current_anime_metadata()
        self.title_label.setText(anime_display_title(metadata))
        if self.season_anime:
            self.series_label.setText(
                f"{self.series_title} · сезон {self.current_season()} из {len(self.season_anime)} доступных")
        elif self.anime and len(self.anime.get("viewing_order") or []) > 1:
            self.series_label.setText(f"Для Plex: {self.series_title} / Season {self.current_season():02d}")
        else:
            self.series_label.setText("")
    def schedule_quality_probe(self):
        self.quality_probe_serial += 1
        self.quality_probe_timer.start()
    def start_quality_probe(self):
        if self.quality_threads:
            self.quality_probe_timer.start()
            return
        selected=self.selected_dubbings(); matrix=self.episode_matrix()
        common=[ep for ep in self.selected_episodes()
                if ep in matrix and all(d in matrix[ep] for d in selected)]
        serial=self.quality_probe_serial
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
            self.quality_status.setText(note if note.startswith("Источник недоступен:") else
                "Фиксированные разрешения определить заранее не удалось. «Лучшее доступное» будет определено при скачивании. "+note)

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
        self.series_label.setText("")
        self.player_combo.clear(); self.dub_list.clear(); self.ep_list.clear(); self.season_combo.clear()
        self.source_toggle.setEnabled(False)
        self.update_source_toggle()
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
        self.series_title, self.series_number, _ = catalog_series_identity(anime)
        self.season_anime = {int(number): card for number, card in
                             (anime.get("_catalog_seasons") or {}).items()}
        LOGGER.info("Anime loaded: video_records=%s",len(raw) if isinstance(raw,list) else 0)

        self.videos = []
        self._episodes_initialized = False
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
                season_hint=v.get("_catalog_season") or video_season_hint(v, v.get("iframe_url"), anime),
                views=max(0, int(v.get("views") or 0)),
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
        self.source_toggle.setEnabled(bool(self.player_combo.count()))
        self.update_source_toggle()

        self.season_combo.blockSignals(True)
        self.season_combo.clear()
        self.rebuild_seasons()
        self.update_season_header()
        self.rebuild_dubbings()
        self.statusBar().showMessage(
            f"Найдено сезонов: {len(self.season_anime) or 1}; "
            f"плееров: {len(self.player_map)}; записей видео: {len(self.videos)}."
        )

    def rebuild_dubbings(self):
        self.update_source_status()
        previous = set(self.selected_dubbings())
        had_rows = self.dub_list.count() > 0
        chosen_episodes = set(self.selected_episodes()) if self._episodes_initialized else set()
        stats = dubbing_stats(self.all_items_for_current_season())
        dubbings = sorted(
            (dub for dub, (episodes, _) in stats.items()
             if chosen_episodes <= episodes),
            key=lambda dub: (-stats[dub][1], dub.casefold()),
        )
        checked = previous & set(dubbings)
        if not checked and (previous or not had_rows) and dubbings:
            checked = {dubbings[0]}
        self.dub_list.blockSignals(True)
        self.dub_list.clear()
        for dub in dubbings:
            item = QListWidgetItem(dub)
            item.setData(Qt.UserRole, dub)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if dub in checked else Qt.Unchecked)
            self.dub_list.addItem(item)
        self.dub_list.blockSignals(False)
        self.refresh_dubbing_sources()
        self.rebuild_episodes()

    def refresh_dubbing_sources(self):
        items = self.all_items_for_current_season()
        stats = dubbing_stats(items)
        player = self.player_combo.currentText()
        self.dub_list.blockSignals(True)
        try:
            for index in range(self.dub_list.count()):
                row = self.dub_list.item(index)
                dub = row.data(Qt.UserRole)
                sources = sorted({provider_label(v) for v in items if v.dubbing == dub},key=str.casefold)
                episodes, views = stats[dub]
                row.setText(f"{dub} · серий: {len(episodes)} · просмотров: {views:,}\nПлееры: {', '.join(sources)}")
                available = any(v.dubbing == dub and v.player == player for v in items)
                row.setToolTip("Источники: " + ", ".join(sources) +
                               (". Доступна в выбранном плеере; если выбрана для видео, её звук используется без повторной загрузки."
                                if available else ". Аудио будет получено из другого плеера."))
        finally:
            self.dub_list.blockSignals(False)

    def selected_dubbings(self):
        return [
            self.dub_list.item(i).data(Qt.UserRole)
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
            matrix[v.episode_key].setdefault("__video_candidates__", []).append(v)
            matrix[v.episode_key].setdefault("__video__", v)
        for dubs in matrix.values():
            # Prefer any selected voice available in the chosen video player,
            # even when the first checked voice exists only in another player.
            candidates = dubs.get("__video_candidates__", [])
            if not candidates:
                continue
            rank = {dub:index for index,dub in enumerate(selected)}
            dubs["__video__"] = min(candidates,key=lambda v:rank.get(v.dubbing,len(selected)))
            video = dubs.get("__video__")
            if video:
                candidates=dubs["__video_candidates__"]
                dubs["__video_candidates__"] = [video] + [v for v in candidates if v != video]
            if video and video.dubbing in selected:
                dubs[video.dubbing] = video
                candidates = dubs["__audio_candidates__"][video.dubbing]
                dubs["__audio_candidates__"][video.dubbing] = [video] + [v for v in candidates if v != video]
        return {ep: dubs for ep, dubs in matrix.items() if "__video__" in dubs}

    def rebuild_episodes(self):
        was_initialized = self._episodes_initialized
        self.ep_list.blockSignals(True)
        previous = set(self.selected_episodes()) if self._episodes_initialized else set()
        self.ep_list.clear()
        selected = self.selected_dubbings()
        available = {item.episode_key for item in self.player_items_for_current_season()}
        if not self._episodes_initialized and selected:
            stats = dubbing_stats(self.all_items_for_current_season())
            previous = available & stats[selected[0]][0]
        for ep in sorted(available):
            item = QListWidgetItem(f"Серия {episode_label(ep)}")
            item.setData(Qt.UserRole, ep)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if ep in previous else Qt.Unchecked)
            self.ep_list.addItem(item)
        self.ep_list.blockSignals(False)
        self._episodes_initialized = True
        if not was_initialized:
            self.rebuild_dubbings()
            return
        self.update_selection_state()

    def on_episode_selection_changed(self, _item):
        self.rebuild_dubbings()

    def update_selection_state(self, _item=None):
        selected = self.selected_dubbings()
        episodes = self.selected_episodes()
        matrix = self.episode_matrix() if selected else {}
        ready = [ep for ep in episodes if ep in matrix and all(d in matrix[ep] for d in selected)]
        requires_mux = any(
            any(matrix[ep][dub] != matrix[ep]["__video__"] for dub in selected)
            for ep in ready)
        self.merge_check.setEnabled(len(selected) > 1 and not requires_mux)
        self.merge_check.setChecked(requires_mux or len(selected) > 1)
        self.statusBar().showMessage(
            f"Выбрано озвучек: {len(selected)} · серий: {len(ready)}"
        )
        if not selected or not ready:
            self.quality_probe_serial += 1
            self.quality_combo.clear()
            self.quality_combo.addItem("Выберите озвучку и серии", None)
            self.quality_combo.setEnabled(False)
            self.quality_status.setText("")
            return
        self.schedule_quality_probe()

    def set_episode_checks(self, state):
        self.ep_list.blockSignals(True)
        for i in range(self.ep_list.count()):
            self.ep_list.item(i).setCheckState(state)
        self.ep_list.blockSignals(False)
        self.on_episode_selection_changed(None)

    def set_dubbing_checks(self, state):
        self.dub_list.blockSignals(True)
        for i in range(self.dub_list.count()):
            self.dub_list.item(i).setCheckState(state)
        self.dub_list.blockSignals(False)
        self.update_selection_state()

    def selected_episodes(self):
        return [
            self.ep_list.item(i).data(Qt.UserRole)
            for i in range(self.ep_list.count())
            if self.ep_list.item(i).checkState() == Qt.Checked
        ]

    def anime_title(self):
        return anime_display_title(self.current_anime_metadata())

    def start_download(self):
        if self.checkpoint:
            self.current_status_label.setText("Сначала продолжите или остановите незавершённую загрузку.")
            return
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
        if len(selected_matrix) != len(episodes) or any(
                any(dub not in items for dub in dubbings)
                for items in selected_matrix.values()):
            QMessageBox.warning(self, APP_NAME,
                "Не все выбранные озвучки доступны для отмеченных серий. Обновите выбор.")
            return

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
        self.series_progress.setValue(0); self.overall_progress.setValue(0)
        self.current_status_label.setText("Подготовка загрузки…")
        self.statusBar().clearMessage()
        LOGGER.info("Selection: player=%s dubbings=%s episodes=%s quality=%s merge=%s",
                    player,dubbings,episodes,quality_value,do_merge)

        settings=dict(
            player=player,
            dubbings=dubbings,
            quality=quality_value,
            base_dir=self.folder_edit.text(),
            anime_title=self.anime_title(),
            series_title=self.series_title or self.anime_title(),
            merge_enabled=do_merge,
            mkvmerge_path=self.config.get("mkvmerge_path", ""),
            keep_sources=self.keep_sources.isChecked(),
            season_number=self.current_season(),
            plex_structure=self.plex_structure.isChecked(),
            plexmatch_enabled=self.plexmatch_check.isChecked(),
            anime_metadata=self.current_anime_metadata(),
            ffmpeg_path=self.config.get("ffmpeg_path", "") or find_ffmpeg(),
            chapters_enabled=self.chapters_check.isChecked(),
        )
        payload={"schema":1,"settings":settings,"matrix":encode_episode_matrix(selected_matrix),
                 "url":self.url_edit.currentText(),"files":{},"episodes":{}}
        self.checkpoint=DownloadCheckpoint(payload,CONFIG_DIR / "pending-download.json")
        try:
            self.checkpoint.save()
        except OSError as error:
            self.checkpoint=None
            QMessageBox.critical(self,APP_NAME,f"Не удалось сохранить состояние загрузки: {error}")
            return
        self.launch_download(selected_matrix)

    def launch_download(self, matrix):
        settings=dict(self.checkpoint.payload["settings"])
        settings["episode_items"]=matrix
        settings["resolver_config"]=self.config
        settings["ffmpeg_path"]=self.config.get("ffmpeg_path") or find_ffmpeg()
        self.work_thread=WorkThread(**settings,checkpoint=self.checkpoint)
        self.work_thread.progress.connect(self.on_progress)
        self.work_thread.done.connect(self.on_done)
        self.work_thread.failed.connect(self.on_work_failed)
        self.work_thread.paused.connect(self.on_paused)
        self.work_thread.stopped.connect(self.on_stopped)
        self.work_thread.finished.connect(self.on_worker_finished)
        self.work_thread.start()
        self.set_download_buttons()

    def set_download_buttons(self):
        active=self.work_thread is not None and self.work_thread.isRunning()
        pending=self.checkpoint is not None
        self.download_btn.setEnabled(not active and not pending)
        self.pause_btn.setEnabled(active)
        self.resume_btn.setEnabled(pending and not active)
        self.stop_btn.setEnabled(pending)

    def pause_download(self):
        if self.work_thread and self.work_thread.isRunning():
            self.work_thread.control.request("paused")
            self.pause_btn.setEnabled(False)
            self.current_status_label.setText("Приостанавливаю загрузку…")

    def resume_download(self):
        if not self.checkpoint or (self.work_thread and self.work_thread.isRunning()): return
        try:
            matrix=decode_episode_matrix(self.checkpoint.payload["matrix"])
        except (KeyError,ValueError,TypeError) as error:
            QMessageBox.critical(self,APP_NAME,f"Не удалось восстановить загрузку: {error}")
            return
        settings=self.checkpoint.payload["settings"]
        self.url_edit.setCurrentText(self.checkpoint.payload.get("url",""))
        self.title_label.setText(settings["anime_title"])
        self.folder_edit.setText(settings["base_dir"])
        self.current_status_label.setText("Возобновляю загрузку…")
        self.launch_download(matrix)

    def stop_download(self):
        if self.work_thread and self.work_thread.isRunning():
            self.work_thread.control.request("stopped")
            self.pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.current_status_label.setText("Останавливаю загрузку…")
        else:
            self.on_stopped()

    def on_paused(self):
        self.current_status_label.setText("Загрузка приостановлена. Её можно продолжить после запуска приложения.")

    def on_stopped(self):
        if self.checkpoint:
            try: self.checkpoint.discard()
            except (OSError,ValueError,TypeError) as error:
                LOGGER.warning("Cannot remove stopped download staging: %s",error)
        self.checkpoint=None
        self.set_download_buttons()
        self.current_status_label.setText("Загрузка остановлена.")

    def on_worker_finished(self):
        if self.work_thread: self.work_thread.wait()
        self.work_thread=None
        self.set_download_buttons()
        if self.close_after_pause:
            QTimer.singleShot(0,self.close)

    def closeEvent(self,event):
        if self.work_thread and self.work_thread.isRunning():
            self.close_after_pause=True
            self.pause_download()
            event.ignore()
            return
        super().closeEvent(event)

    def on_progress(self, series_value, overall_value, text):
        self.series_progress.setValue(series_value); self.overall_progress.setValue(overall_value)
        self.current_status_label.setText(text)

    def on_done(self, summary, errors):
        LOGGER.info("Download finished: %s errors=%s",summary,len(errors))
        if not errors: self.checkpoint=None
        self.set_download_buttons()
        self.series_progress.setValue(100); self.overall_progress.setValue(100)
        self.current_status_label.setText(
            summary + (" Нажмите «Продолжить», чтобы повторить пропущенные серии." if errors else ""))
        detail = ""
        if errors:
            detail = "\n\n" + "\n".join(errors[:12])
            if len(errors) > 12:
                detail += f"\n…и ещё {len(errors) - 12}"
            detail += "\n\nНажмите «Продолжить», чтобы повторить пропущенные серии."
        QMessageBox.information(self, APP_NAME, summary + detail)

    def on_work_failed(self, error):
        LOGGER.error("Download failed: %s",error)
        self.set_download_buttons()
        self.current_status_label.setText(f"Ошибка: {error}. Нажмите «Продолжить», чтобы повторить загрузку.")
        QMessageBox.critical(self, APP_NAME,
            error + "\n\nЗагрузка сохранена. Нажмите «Продолжить», чтобы повторить пропущенные серии.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    if "--self-test" in sys.argv:
        sys.exit(0)
    install_exception_hooks()
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
