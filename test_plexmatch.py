
from main import build_plexmatch

anime = {
    "title": "Парад смерти",
    "year": 2015,
    "remote_ids": {
        "myanimelist_id": 28223,
        "shikimori_id": 28223,
        "kp_id": 841914,
    },
}

s = build_plexmatch(anime)
assert "Title: Парад смерти" in s
assert "Year: 2015" in s
assert "# MyAnimeList ID: 28223" in s
assert "tmdbid:" not in s

anime2 = {
    "title": "Test",
    "year": 2020,
    "remote_ids": {"tmdb_id": 12345},
}
s2 = build_plexmatch(anime2)
assert "tmdbid: 12345" in s2
print("plexmatch tests: OK")
