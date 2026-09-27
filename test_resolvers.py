from dataclasses import dataclass
from resolvers import provider_kind, choose_stream, StreamResult, _parse_hls_qualities

@dataclass
class Item:
    player: str
    iframe_url: str
    dubbing: str = "Test"
    number: str = "1"
    index: int = 1

samples = [
    (Item("Плеер CVH", "https://x/iframeCVH.html?anime_id=1"), "cvh"),
    (Item("Плеер Kodik", "https://kodik.info/seria/123/x"), "kodik"),
    (Item("Плеер Alloha", "https://alloha.tv/?id=x"), "alloha"),
    (Item("Aksor", "https://player.aksor.tv/video/abc"), "aksor"),
    (Item("Sibnet", "https://video.sibnet.ru/shell.php?videoid=1"), "sibnet"),
    (Item("Rutube", "https://rutube.ru/play/embed/0123456789abcdef0123456789abcdef"), "rutube"),
    (Item("VK", "https://vk.com/video_ext.php?oid=-1&id=2"), "vk"),
    (Item("Zedfilm", "https://zedfilm.ru/player/abc"), "zedfilm"),
]
for item, expected in samples:
    actual = provider_kind(item)
    assert actual == expected, (item, actual, expected)

master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2,RESOLUTION=1280x720
720/index.m3u8
"""
q = _parse_hls_qualities(master, "https://example.com/master.m3u8")
assert q["360p"] == "https://example.com/360/index.m3u8"
assert q["720p"] == "https://example.com/720/index.m3u8"

result = StreamResult("x", "test", {"480p": "a", "720p": "b"}, {})
assert choose_stream(result, "Лучшее") == ("720p", "b")
assert choose_stream(result, "480p") == ("480p", "a")
print("Offline resolver tests: OK")


from resolvers import _aksor_quality_map
_payload = {"qualities": {
    "q360": "https://cdn.example/360.mpd",
    "q720": "https://cdn.example/JAM CLUB/720.mpd",
    "q1080": None,
}}
_q = _aksor_quality_map(_payload)
assert _q["360p"].endswith("/360.mpd")
assert "%20" in _q["720p"]
assert "1080p" not in _q
