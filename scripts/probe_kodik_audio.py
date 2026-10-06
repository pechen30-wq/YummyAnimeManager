"""Check whether a pending episode offers an audio-equivalent smaller Kodik variant."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import CHECKPOINT_FILE, DownloadCheckpoint, decode_episode_matrix, matching_audio_variant
from resolvers import PlayerResolver, find_ffmpeg, provider_kind
from main import load_config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("episode",type=float)
    parser.add_argument("--match",default="")
    args=parser.parse_args()
    checkpoint=DownloadCheckpoint.load(CHECKPOINT_FILE)
    matrix=decode_episode_matrix(checkpoint.payload["matrix"])
    episode=matrix[args.episode]
    candidates=[item for group in episode.get("__audio_candidates__",{}).values()
                for item in group if provider_kind(item)=="kodik"
                and args.match.casefold() in item.dubbing.casefold()]
    if not candidates:
        raise SystemExit("No Kodik audio candidate in this episode")
    resolver=PlayerResolver(load_config())
    started=time.monotonic()
    try:
        stream=resolver.resolve(candidates[0])
        variant=matching_audio_variant(stream.qualities or {},stream.headers or {},find_ffmpeg())
        print(f"Kodik probe: dubbing={candidates[0].dubbing!r}, "
              f"variant={variant[0] if variant else 'original'}, "
              f"seconds={time.monotonic()-started:.1f}")
    finally:
        resolver.session.close()


if __name__=="__main__":
    main()
