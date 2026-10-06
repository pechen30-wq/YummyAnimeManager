"""Cold full-episode A/B benchmark; user checkpoints and completed media are read-only."""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]
MODES=("baseline","optimized","fast")


def save_json(path,value):
    temporary=path.with_name(path.name+".tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    temporary.replace(path)


def validate_media(path,dubbings):
    from process_utils import hidden_subprocess_kwargs
    ffprobe=shutil.which("ffprobe")
    ffmpeg=shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise RuntimeError("ffprobe and ffmpeg must be available for full media validation")
    probe=subprocess.run([ffprobe,"-v","error","-count_packets","-show_entries",
                          "stream=index,codec_type,codec_name,nb_read_packets:stream_tags=title,DURATION:format=duration,size",
                          "-of","json",str(path)],capture_output=True,text=True,encoding="utf-8",
                         timeout=600,**hidden_subprocess_kwargs())
    if probe.returncode or probe.stderr.strip():
        raise RuntimeError("Media packet inspection failed")
    media=json.loads(probe.stdout)
    audio=[stream for stream in media["streams"] if stream["codec_type"]=="audio"]
    video=[stream for stream in media["streams"] if stream["codec_type"]=="video"]
    if len(video)!=1 or [stream.get("tags",{}).get("title") for stream in audio]!=list(dubbings):
        raise RuntimeError("Output stream count, dubbing labels or order differs from selection")
    if any(int(stream.get("nb_read_packets",0))<=0 for stream in audio+video):
        raise RuntimeError("An output stream has no readable packets")
    decoded=subprocess.run([ffmpeg,"-hide_banner","-v","error","-i",str(path),
                            "-map","0:a","-f","null",os.devnull],capture_output=True,
                           timeout=900,**hidden_subprocess_kwargs())
    if decoded.returncode or decoded.stderr.strip():
        raise RuntimeError("Full audio decode failed")
    return {"video_tracks":len(video),"audio_tracks":len(audio),
            "duration":float(media["format"]["duration"]),"size":path.stat().st_size,
            "audio_decoded":True,"audio_streams":audio}


class Events(logging.Handler):
    def __init__(self):
        super().__init__()
        self.retries=0
        self.source_failures=0
        self.probes=0
        self.sources={}
    def emit(self,record):
        message=record.getMessage()
        if message.startswith(("Download interrupted","MP4 range interrupted")):
            self.retries+=1
        if message.startswith("Audio source failed:"):
            self.source_failures+=1
        if message.startswith("Audio source probe:"):
            self.probes+=1
        if message.startswith("Audio prefetched:"):
            provider=message.rsplit("provider=",1)[-1]
            self.sources[provider]=self.sources.get(provider,0)+1


def run_case(args):
    sys.path.insert(0,str(Path(args.code_dir).resolve()))
    import diagnostics
    from main import (APP_VERSION, DownloadCheckpoint, WorkThread,
                      decode_episode_matrix, find_ffmpeg, load_config)
    task=json.loads(Path(args.task_file).read_text(encoding="utf-8"))
    matrix=decode_episode_matrix(task["matrix"])
    if args.episode not in matrix:
        raise ValueError("Episode is not in the task snapshot")
    directory=Path(args.output_dir).resolve()/args.mode
    directory.mkdir(parents=True,exist_ok=False)
    diagnostics.LOG_DIR=directory/"logs"
    diagnostics.LOG_FILE=diagnostics.LOG_DIR/"application.log"
    diagnostics.configure_logging(True)
    events=Events(); diagnostics.LOGGER.addHandler(events)
    config=load_config()
    config["fast_kodik_audio"]=args.mode=="fast"
    config["probe_audio_sources"]=args.mode!="baseline"
    settings=dict(task["settings"])
    settings.update(base_dir=str(directory/"media"),keep_sources=False,
                    chapters_enabled=False,plexmatch_enabled=False)
    ffmpeg_path=settings.pop("ffmpeg_path","") or find_ffmpeg()
    payload={"schema":1,"settings":settings,"matrix":{str(args.episode):task["matrix"][str(args.episode)]},
             "files":{},"episodes":{}}
    checkpoint=DownloadCheckpoint(payload,directory/"checkpoint.json")
    checkpoint.save()
    checkpoint.clear=lambda:None
    worker=WorkThread(**settings,episode_items={args.episode:matrix[args.episode]},
                      resolver_config=config,ffmpeg_path=ffmpeg_path,
                      checkpoint=checkpoint)
    outcomes=[]
    worker.done.connect(lambda summary,errors:outcomes.append(("done",errors)))
    worker.failed.connect(lambda error:outcomes.append(("failed",[error])))
    worker.paused.connect(lambda:outcomes.append(("paused",[])))
    worker.stopped.connect(lambda:outcomes.append(("stopped",[])))
    started=time.monotonic()
    state={"mode":args.mode,"version":APP_VERSION,"status":"downloading","episode":args.episode,
           "dubbings":len(settings["dubbings"]),"progress":0}
    state_lock=threading.Lock()
    def progress(series,_overall,_detail):
        with state_lock: state["progress"]=series
    worker.progress.connect(progress)
    finished=threading.Event()
    def heartbeat():
        while not finished.is_set():
            with state_lock:
                state.update(seconds=round(time.monotonic()-started,2),
                             network_bytes=worker.control.transfer_stats()[2])
                snapshot=dict(state)
            save_json(directory/"progress.json",snapshot)
            print(json.dumps(snapshot,ensure_ascii=True),flush=True)
            finished.wait(30)
    pulse=threading.Thread(target=heartbeat,daemon=True); pulse.start()
    error=None
    try:
        worker.run()
        elapsed=time.monotonic()-started
        if not outcomes or outcomes[0][0]!="done" or outcomes[0][1]:
            raise RuntimeError("Episode download did not complete successfully; see private diagnostic log")
        record=checkpoint.payload["episodes"].get(str(args.episode))
        if not record:
            raise RuntimeError("No completed output was recorded")
        with state_lock: state["status"]="validating"
        validation=validate_media(Path(record["path"]),settings["dubbings"])
        result={"mode":args.mode,"version":APP_VERSION,"episode":args.episode,
                "seconds":round(elapsed,2),"network_bytes":worker.control.transfer_stats()[2],
                "network_metric":"application_metered_http","retries":events.retries,
                "source_failures":events.source_failures,"source_probes":events.probes,
                "sources":events.sources,"output":record["path"],"validation":validation,
                "snapshot_sha256":hashlib.sha256(Path(args.task_file).read_bytes()).hexdigest(),
                "fresh_download":True,"chapters_enabled":False}
        save_json(directory/"result.json",result)
        with state_lock: state["status"]="complete"
    except Exception as exc:
        error=exc
        with state_lock: state["status"]="failed"
        diagnostics.LOGGER.exception("Benchmark failed")
    finally:
        finished.set(); pulse.join(timeout=5)
        with state_lock: save_json(directory/"progress.json",state)
    if error:
        raise RuntimeError("Benchmark failed; see its isolated logs") from None


def run_suite(args):
    directory=Path(args.output_dir).resolve()
    directory.mkdir(parents=True,exist_ok=False)
    snapshot=directory/"input-task.json"
    shutil.copy2(args.task_file,snapshot)
    if shutil.disk_usage(directory).free<10*1024**3:
        raise RuntimeError("At least 10 GiB of free space is required")
    results=[]
    for mode in MODES:
        code_dir=args.baseline_dir if mode=="baseline" else args.code_dir
        command=[sys.executable,str(Path(__file__).resolve()),str(args.episode),"--mode",mode,
                 "--task-file",str(snapshot),"--output-dir",str(directory),"--code-dir",str(code_dir)]
        subprocess.run(command,check=True)
        results.append(json.loads((directory/mode/"result.json").read_text(encoding="utf-8")))
        save_json(directory/"comparison.json",results)
    baseline=results[0]
    lines=["# Full-episode download comparison", "",
           "Fresh runs of the same episode and selected dubbings, executed sequentially.",
           "Chapters disabled equally. Traffic measures application-tracked HTTP payloads.", "",
           "| Mode | Version | Seconds | HTTP bytes | Retries | Audio tracks | Time change | Traffic change |",
           "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for result in results:
        seconds_change=(result["seconds"]/baseline["seconds"]-1)*100
        traffic_change=(result["network_bytes"]/baseline["network_bytes"]-1)*100
        lines.append(f"| {result['mode']} | {result['version']} | {result['seconds']:.2f} | "
                     f"{result['network_bytes']} | {result['retries']} | "
                     f"{result['validation']['audio_tracks']} | {seconds_change:+.1f}% | {traffic_change:+.1f}% |")
    lines.extend(["", "All output audio streams were decoded in full. Fast mode may use a different audio bitrate.",
                  "A single sequential comparison is affected by changing CDN conditions; it does not prove a repeatable speedup."])
    (directory/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(f"Comparison report: {directory/'report.md'}",flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("episode",type=float)
    parser.add_argument("--task-file",default=str(Path.home()/".yummy_anime_manager"/"pending-download.json"))
    parser.add_argument("--output-dir",required=True)
    parser.add_argument("--code-dir",default=str(ROOT))
    parser.add_argument("--baseline-dir")
    parser.add_argument("--mode",choices=MODES)
    args=parser.parse_args()
    if args.mode:
        run_case(args)
    elif args.baseline_dir:
        run_suite(args)
    else:
        parser.error("Provide --mode or --baseline-dir")


if __name__=="__main__": main()
