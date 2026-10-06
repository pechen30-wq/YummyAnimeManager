"""Resume one episode of a pending task without clearing the other episodes."""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diagnostics import configure_logging
from main import (CHECKPOINT_FILE, DownloadCheckpoint, WorkThread,
                  decode_episode_matrix, find_ffmpeg, load_config)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("episode", type=float)
    args = parser.parse_args()
    checkpoint = DownloadCheckpoint.load(CHECKPOINT_FILE)
    if checkpoint is None:
        raise SystemExit("No pending download exists")
    matrix = decode_episode_matrix(checkpoint.payload["matrix"])
    if args.episode not in matrix:
        raise SystemExit("Episode is not in the pending task")
    if checkpoint.episode_complete(args.episode):
        raise SystemExit("Episode is already complete")

    backup = CHECKPOINT_FILE.with_name(
        CHECKPOINT_FILE.stem + ".before-benchmark-" +
        datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    shutil.copy2(CHECKPOINT_FILE, backup)
    print(f"Checkpoint backup: {backup}", flush=True)
    configure_logging(True)
    settings = dict(checkpoint.payload["settings"])
    settings["episode_items"] = {args.episode: matrix[args.episode]}
    settings["resolver_config"] = load_config()
    settings["ffmpeg_path"] = settings.get("ffmpeg_path") or find_ffmpeg()
    checkpoint.clear = lambda: None
    worker = WorkThread(**settings, checkpoint=checkpoint)
    outcome = []
    last = [-1]

    def progress(series, _overall, detail):
        bucket = series // 5
        if bucket != last[0]:
            last[0] = bucket
            print(f"{series}% {detail}", flush=True)

    worker.progress.connect(progress)
    worker.done.connect(lambda summary, errors: outcome.append((summary, errors)))
    worker.failed.connect(lambda error: outcome.append(("failed", [error])))
    worker.paused.connect(lambda: outcome.append(("paused", [])))
    worker.stopped.connect(lambda: outcome.append(("stopped", [])))
    worker.run()
    if not outcome:
        raise SystemExit("Worker returned without a result")
    print(outcome[0][0], flush=True)
    for error in outcome[0][1]:
        print(error, flush=True)
    if outcome[0][1] or outcome[0][0] in ("failed", "paused", "stopped"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
