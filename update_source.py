"""Startup updater for run.bat. The application still starts if GitHub is down."""

import os
import sys
from pathlib import Path
import json

from diagnostics import LOGGER, configure_logging
from updater import (auto_update_enabled, fetch_manifest, newer_version,
                     update_git_checkout, update_source_folder)


def main():
    if os.environ.get("YUMMY_SKIP_UPDATE") == "1" or not auto_update_enabled():
        return 0
    root = Path(__file__).resolve().parent
    try:
        try:
            diagnostic = bool(json.loads((Path.home() / ".yummy_anime_manager" / "config.json")
                                         .read_text(encoding="utf-8")).get("diagnostic_logging", False))
        except (OSError, ValueError, TypeError):
            diagnostic = False
        configure_logging(diagnostic)
        local_version = (root / "VERSION").read_text(encoding="utf-8").strip()
        manifest = fetch_manifest()
        if not newer_version(manifest["version"], local_version):
            return 0
        print(f"YummyAnime Manager: обновление {local_version} → {manifest['version']}…")
        if (root / ".git").exists():
            print(update_git_checkout(root))
        else:
            update_source_folder(root, manifest, python=sys.executable)
        print(f"Исходники обновлены до {manifest['version']}.")
        return 0
    except Exception as error:
        LOGGER.exception("Source auto-update failed")
        print(f"Не удалось обновить исходники: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
