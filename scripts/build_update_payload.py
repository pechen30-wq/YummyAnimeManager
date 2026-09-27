"""Build the versioned, hashed update manifest and source archive."""

import hashlib
import json
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from updater import RAW_BASE, SOURCE_FILES  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    exe = dist / "YummyAnimeManager.exe"
    if not exe.is_file():
        raise FileNotFoundError(exe)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    names = sorted(set(SOURCE_FILES) | {
        path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py")
    })
    archive_path = dist / "source.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            source = ROOT / name
            if not source.is_file():
                raise FileNotFoundError(source)
            archive.write(source, name)
    manifest = {"version": version}
    for key, path in (("exe", exe), ("source", archive_path)):
        manifest[key] = {"url": RAW_BASE + path.name,
                         "sha256": digest(path), "size": path.stat().st_size}
    (dist / "update.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (dist / "SHA256SUMS.txt").write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in (exe, archive_path)),
        encoding="utf-8",
    )


if __name__ == "__main__":
    build()
