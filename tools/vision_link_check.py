"""Short camera-to-PC inference check. No robot commands or camera profile changes.

python tools/vision_link_check.py --camera-ip 192.168.0.42 --seconds 15 \
    --output-dir logs/vision-new-run

Measures PC arrival to completed detection/tracking/badge processing, not optical
capture latency or command response. Uses the existing model and queue policy.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.config import load_config  # noqa: E402
from host.common.logging_setup import JsonlFormatter  # noqa: E402
from host.vision.coco_labels import COCO_CLASSES  # noqa: E402
from host.vision.detector import Detector  # noqa: E402
from host.vision.stream_client import StreamEndpoints, StreamReader  # noqa: E402
from host.vision.worker import VisionWorker  # noqa: E402
from tools.camera_link_check import percentile  # noqa: E402


def short_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise argparse.ArgumentTypeError("seconds must be greater than 0 and at most 60")
    return seconds


def run(device: str, camera_ip: str, seconds: float, output: Path) -> dict:
    seconds = short_seconds(str(seconds))
    address = ipaddress.IPv4Address(camera_ip)
    config = load_config(device)
    endpoints = StreamEndpoints(control=f"http://{address}", stream=f"http://{address}:81/stream")
    detector = Detector(config, labels=COCO_CLASSES)
    reader = StreamReader(config, endpoints=endpoints)
    worker = VisionWorker(config, detector=detector, reader=reader)
    output.mkdir(parents=True, exist_ok=False)
    logger = logging.getLogger("mechadog.vision")
    previous_level = logger.level
    handler = logging.FileHandler(output / "events.jsonl", encoding="utf-8")
    handler.setFormatter(JsonlFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    rows = []
    previous = None
    error = None
    startup = time.perf_counter()
    started = None
    stopped = startup
    wait = threading.Event()
    try:
        worker.start()
        started = time.perf_counter()
        deadline = started + seconds
        while time.perf_counter() < deadline:
            result = worker.latest()
            if result is not None and result is not previous:
                previous = result
                if not rows:
                    (output / "first.jpg").write_bytes(result.jpeg)
                rows.append(
                    {
                        "frame_seq": result.frame_seq,
                        "received_ms": result.frame_received_ms,
                        "completed_ms": result.completed_ms,
                        "processing_ms": result.inference_ms,
                        "arrival_to_result_ms": result.completed_ms - result.frame_received_ms,
                        "detections": [asdict(d) for d in result.detections],
                        "tracks": len(result.tracks),
                        "markers": len(result.markers),
                    }
                )
            wait.wait(min(0.005, max(0.0, deadline - time.perf_counter())))
    except Exception as exc:  # noqa: BLE001 — preserve partial diagnostics
        error = f"{type(exc).__name__}: {exc}"
    finally:
        stopped = time.perf_counter()
        worker.stop()
        stop_ms = (time.perf_counter() - stopped) * 1000
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
    durations = [r["arrival_to_result_ms"] for r in rows]
    elapsed = stopped - started if started is not None else 0.0
    summary = {
        "requested_seconds": seconds,
        "elapsed_seconds": elapsed,
        "startup_seconds": (started or stopped) - startup,
        "stream": asdict(reader.stats),
        "worker": asdict(worker.stats),
        "observed_results": len(rows),
        "observed_result_fps": len(rows) / elapsed if elapsed else None,
        "arrival_to_result_p95_ms": percentile(durations),
        "arrival_to_result_max_ms": max(durations, default=None),
        "configured_inference_fps": config["vision"]["inference_fps"],
        "queue_dropped": worker.queue.dropped,
        "queue_max_depth": worker.queue.max_depth,
        "stop_ms": stop_ms,
        "threads_still_alive": [t.name for t in worker._threads if t.is_alive()],
        "error": error,
        "notes": [
            "Observed latest-result samples can miss overwritten results; worker totals are separate.",
            "Queue drops follow existing production policy; this tool adds no frame skipping.",
            "Includes PC queue/decode/detection/tracking/badge time; excludes camera/network/commands.",
            "No long-run stability or full end-to-end acceptance claimed.",
        ],
    }
    (output / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="mechdog-01")
    parser.add_argument("--camera-ip", required=True)
    parser.add_argument("--seconds", type=short_seconds, default=15.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = run(args.device, args.camera_ip, args.seconds, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(
        bool(
            summary["error"]
            or not summary["observed_results"]
            or summary["worker"]["errors"]
            or summary["threads_still_alive"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
