"""Preserve embedded chapters or add verified AniSkip chapter markers."""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from diagnostics import LOGGER


ANISKIP_URL = "https://api.aniskip.com/v2/skip-times/{mal_id}/{episode}"
SKIP_TYPES = ("op", "ed", "mixed-op", "mixed-ed", "recap")
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    chapters: int


def inspect_media(ffmpeg, path, subprocess_run=subprocess.run):
    """FFmpeg's ffmetadata output exposes chapters without a separate ffprobe binary."""
    proc = subprocess_run(
        [str(ffmpeg), "-hide_banner", "-i", str(path), "-f", "ffmetadata", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Не удалось проверить главы: {proc.stderr[-500:]}")
    match = DURATION_RE.search(proc.stderr)
    duration = (int(match.group(1)) * 3600 + int(match.group(2)) * 60 +
                float(match.group(3))) if match else 0.0
    return MediaInfo(duration, proc.stdout.count("[CHAPTER]"))


def aniskip_points(mal_id, episode, duration, get=requests.get):
    """Return chapter starts only when the episode and runtime match AniSkip."""
    try:
        mal_id = int(mal_id)
        episode = float(episode)
        duration = float(duration)
    except (TypeError, ValueError):
        return []
    if mal_id <= 0 or episode < 1 or not episode.is_integer() or duration <= 0:
        return []
    params = [("types", kind) for kind in SKIP_TYPES]
    params.append(("episodeLength", f"{duration:.3f}"))
    response = get(ANISKIP_URL.format(mal_id=mal_id, episode=int(episode)),
                   params=params, timeout=(5, 12), headers={"User-Agent": "YummyAnimeManager/4.3"})
    if response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("found"):
        return []

    candidates = {"op": [], "ed": [], "recap": []}
    for result in payload.get("results") or []:
        try:
            kind = result["skipType"]
            interval = result["interval"]
            start, end = float(interval["startTime"]), float(interval["endTime"])
            recorded_length = float(result["episodeLength"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind not in SKIP_TYPES or not (0 <= start < end <= duration + 1):
            continue
        if abs(recorded_length - duration) > max(20, duration * .03):
            continue
        category = kind.removeprefix("mixed-")
        candidates[category].append((kind.startswith("mixed-"),
                                     abs(recorded_length - duration), start, end))

    # AniSkip can return both ordinary and "mixed" proposals for one segment.
    # They are alternative timings, not additional chapters.
    selected = []
    for category in ("op", "ed", "recap"):
        if not candidates[category]:
            continue
        _, _, start, end = min(candidates[category])
        if any(start < other_end and end > other_start
               for _, other_start, other_end in selected):
            continue
        selected.append((category, start, end))

    markers = {0: "Episode"}
    for kind, start, end in sorted(selected, key=lambda item: item[1]):
        start_ms, end_ms = round(start * 1000), round(end * 1000)
        if kind == "recap":
            markers[start_ms] = "Recap"
            after = "Episode"
        elif kind == "op":
            markers[start_ms] = "Opening"
            after = "Episode"
        else:
            markers[start_ms] = "Ending"
            after = "After credits"
        if end < duration - .5:
            markers[end_ms] = after
    return sorted(markers.items()) if len(markers) > 1 else []


def write_ffmetadata(points, duration, path):
    duration_ms = round(float(duration) * 1000)
    lines = [";FFMETADATA1"]
    for index, (start, title) in enumerate(points):
        end = points[index + 1][0] if index + 1 < len(points) else duration_ms
        if start >= end:
            continue
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}",
                      f"END={end}", f"title={title}"])
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def remux_to_mkv(ffmpeg, source, target, *, chapter_source=None, points=None,
                 duration=0, keep_source=True, subprocess_run=subprocess.run):
    """Copy media streams and chapters into a temporary MKV, then replace atomically."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp_dir:
        temp = Path(temp_dir)
        staged = temp / "output.mkv"
        cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
        chapter_input = 0
        if points:
            metadata = temp / "chapters.ffmeta"
            write_ffmetadata(points, duration, metadata)
            cmd += ["-f", "ffmetadata", "-i", str(metadata)]
            chapter_input = 1
        elif chapter_source and Path(chapter_source) != source:
            cmd += ["-i", str(chapter_source)]
            chapter_input = 1
        cmd += ["-map", "0", "-map_metadata", "0", "-map_chapters",
                str(chapter_input), "-c", "copy", str(staged)]
        LOGGER.debug("Remuxing %s to %s with chapter input %s", source, target, chapter_input)
        proc = subprocess_run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"Не удалось добавить главы в MKV: {proc.stderr[-1000:]}")
        staged.replace(target)
    if source != target and not keep_source:
        source.unlink()
