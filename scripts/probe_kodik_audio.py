"""Check whether a pending episode offers an audio-equivalent smaller Kodik variant."""

import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import (CHECKPOINT_FILE, DownloadCheckpoint, WorkThread, decode_episode_matrix,
                  matching_audio_variant, numeric_hls_variants)
from hls_download import media_playlist
from process_utils import hidden_subprocess_kwargs
import urllib.parse
from resolvers import find_ffmpeg, provider_kind
from main import load_config


def sample_variants(stream,ffmpeg):
    """Check three segments per variant without downloading an episode."""
    variants=numeric_hls_variants(stream.qualities or {})
    if len(variants)<2:
        raise RuntimeError("Two numeric HLS variants are required")
    sizes=[]
    for _height,url,label in (variants[0],variants[-1]):
        body,base=media_playlist(url,stream.headers)
        segments=[urllib.parse.urljoin(base,line.strip()) for line in body.splitlines()
                  if line.strip() and not line.startswith("#")]
        if not segments:
            raise RuntimeError("No segments in playlist")
        durations=[float(value) for value in re.findall(r"(?m)^#EXTINF:([0-9.]+)",body)]
        if len(durations)!=len(segments):
            raise RuntimeError("Segment duration metadata is incomplete")
        full=[index for index,duration in enumerate(durations) if duration>=max(durations)*0.5]
        indexes=sorted({full[0],full[len(full)//2],full[-1]})
        def sample(segment):
            with requests.get(segment,headers=stream.headers,stream=True,timeout=(10,25)) as response:
                response.raise_for_status()
                data=bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data)>8*1024*1024:
                        raise RuntimeError("Sample exceeds the size budget")
            result=subprocess.run([ffmpeg,"-hide_banner","-loglevel","error","-i","pipe:0",
                                   "-map","0:a:0","-c:a","copy","-f","framehash","pipe:1"],
                                  input=bytes(data),capture_output=True,timeout=15,
                                  **hidden_subprocess_kwargs())
            packets=[line for line in result.stdout.splitlines() if line and not line.startswith(b"#")]
            if result.returncode or not packets:
                raise RuntimeError("Sample has no readable audio packets")
            return len(data),len(packets)
        with ThreadPoolExecutor(max_workers=3) as pool:
            samples=list(pool.map(sample,[segments[index] for index in indexes]))
        size=sum(size for size,_packets in samples)
        sizes.append(size)
        duration=sum(durations)
        print(f"Sample: quality={label} segments={len(segments)} duration={duration:.3f} "
              f"sample_duration={sum(durations[index] for index in indexes):.3f} "
              f"sample_bytes={size} audio_packets={[count for _size,count in samples]}",flush=True)
    print(f"Sample traffic reduction: {(1-sizes[0]/sizes[1])*100:.1f}%",flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("episode",type=float)
    parser.add_argument("--match",default="")
    parser.add_argument("--fast",action="store_true",help="Check fast selection and three audio samples per quality")
    args=parser.parse_args()
    checkpoint=DownloadCheckpoint.load(CHECKPOINT_FILE)
    matrix=decode_episode_matrix(checkpoint.payload["matrix"])
    episode=matrix[args.episode]
    candidates=[item for group in episode.get("__audio_candidates__",{}).values()
                for item in group if provider_kind(item)=="kodik"
                and args.match.casefold() in item.dubbing.casefold()]
    if not candidates:
        raise SystemExit("No Kodik audio candidate in this episode")
    config=load_config()
    config["fast_kodik_audio"]=args.fast
    worker=WorkThread("Kodik",[],{},"Лучшее",Path("."),"Probe",False,"",False,
                      resolver_config=config)
    resolver=worker.resolver
    started=time.monotonic()
    try:
        stream=worker.resolve_stream(candidates[0],audio_only=True) if args.fast else resolver.resolve(candidates[0])
        variant=(stream.quality,stream.url) if args.fast else matching_audio_variant(
            stream.qualities or {},stream.headers or {},find_ffmpeg())
        if args.fast:
            sample_variants(stream,find_ffmpeg())
        print(f"Kodik probe: dubbing={candidates[0].dubbing!r}, "
              f"variant={variant[0] if variant else 'original'}, "
              f"seconds={time.monotonic()-started:.1f}")
    finally:
        resolver.session.close()


if __name__=="__main__":
    main()
