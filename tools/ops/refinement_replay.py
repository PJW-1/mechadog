"""Replay recorded observation events into a candidate map, with no live I/O.

The pose origin is explicitly the laser centre. Existing base_link MAP_POSE
messages require a measured transform in the recorder; this tool cannot infer it.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from host.slam import settings
from host.slam.continuous_map import LidarPose
from host.slam.map_refinement import MapRefinement
from tools.ops.refinement_prepare import file_hash, load_grid, read_mapping


def replay(events: Iterable[dict[str, Any]], mapper: MapRefinement) -> dict[str, Any]:
    pose: LidarPose | None = None
    context: dict[str, Any] = {}
    last_ms = -1
    updates: list[dict[str, Any]] = []
    for event in events:
        stamp = event.get("received_ms")
        if type(stamp) is not int or stamp < 0 or stamp < last_ms:
            raise ValueError("Events need monotonic nonnegative integer received_ms")
        last_ms = stamp
        kind = event.get("kind")
        if kind == "pose":
            context = event
            pose = None
            if event.get("pose_origin") == "laser" and event.get("yaw_frame") == "base_link":
                # bool conversion is deliberately not used: strings/1 are not observations.
                pose = LidarPose(
                    stamp,
                    float(event["x_m"]),
                    float(event["y_m"]),
                    float(event["yaw_rad"]),
                    event.get("stationary") is True,
                    event.get("level") is True,
                )
        elif kind == "scan":
            raw = event.get("payload")
            if isinstance(raw, dict):
                raw = json.dumps(raw, ensure_ascii=False)
            if not isinstance(raw, str):
                raise ValueError("Scan payload must be original SCAN JSON")
            result = mapper.submit(
                raw,
                stamp,
                pose,
                pose_frame_id=str(context.get("frame_id", "")),
                localization_valid=context.get("localization_valid") is True,
                laser_extrinsics_verified=context.get("laser_extrinsics_verified") is True,
                stationary_verified=context.get("stationary") is True,
                level_verified=context.get("level") is True,
            )
            updates.append({"received_ms": stamp, **asdict(result)})
        elif kind == "localization_lost":
            # Invalidate even if a recently received pose was good before loss.
            context = {}
            pose = None
            # submit rejects this event and clears any partially collected scan.
            result = mapper.submit(
                "{}",
                stamp,
                None,
                pose_frame_id="",
                localization_valid=False,
                laser_extrinsics_verified=False,
                stationary_verified=False,
                level_verified=False,
            )
            updates.append({"received_ms": stamp, "event": "localization_lost", **asdict(result)})
        else:
            raise ValueError(f"Unsupported replay event kind: {kind!r}")
    return {
        "schema": 1,
        "mode": "offline_recording_replay",
        "commands_enabled": False,
        "physical_commands_sent": 0,
        "updates": updates,
        "committed_revolutions": sum(item["accepted"] for item in updates),
        "map": mapper.metadata(),
        "navigation_ready": False,
    }


def audit_saved_observations(path: Path) -> dict[str, Any]:
    """Snapshot medians are evidence, not fresh packets with measured global poses."""
    source = read_mapping(path)
    records = source.get("records", [])
    if not isinstance(records, list):
        raise ValueError("Observation records must be a list")
    return {
        "schema": 1,
        "mode": "historical_snapshot_eligibility_audit",
        "source_sha256": file_hash(path),
        "records": len(records),
        "actual_saved_points": sum(len(record.get("points", [])) for record in records),
        "committed_revolutions": 0,
        "physical_commands_sent": 0,
        "navigation_ready": False,
        "note": "Actual local rays retained for alignment. No synthetic packet repetition or invented pose/IMU.",
        "rejections": [
            {
                "id": record["id"],
                "eligible_for_online_map_update": False,
                "reason": "historical_snapshot_missing_synchronized_pose_scan_imu_contract",
                "missing": [
                    "original packet seq/boot and received_ms",
                    "verified same-frame laser pose",
                    "measured base-to-laser transform",
                    "synchronized observed stationary/level",
                ],
                "point_count": len(record.get("points", [])),
            }
            for record in records
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input", type=Path, help="Recorded pose/SCAN JSONL")
    inputs.add_argument("--observations", type=Path, help="Audit historical local snapshot records")
    parser.add_argument("--map-dir", type=Path)
    parser.add_argument("--stem", default="slam_map")
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--lidar-device")
    parser.add_argument("--frame-id")
    parser.add_argument(
        "--no-go", type=Path, help="JSON object with rectangles_m in this map frame"
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("Replay output directory must be empty; existing revisions are preserved")
    if args.observations:
        result = audit_saved_observations(args.observations)
    else:
        if not all((args.map_dir, args.lidar_device, args.frame_id)):
            parser.error("--input requires --map-dir, --lidar-device and --frame-id")
        config = settings.load(args.device)
        rectangles = read_mapping(args.no_go).get("rectangles_m", []) if args.no_go else []
        mapper = MapRefinement(
            config,
            args.lidar_device,
            load_grid(args.map_dir, args.stem),
            args.frame_id,
            no_go_rectangles=rectangles,
        )
        with args.input.open(encoding="utf-8-sig") as stream:
            result = replay((json.loads(line) for line in stream if line.strip()), mapper)
        args.out.mkdir(parents=True, exist_ok=True)
        mapper.snapshot().save(args.out, stem="refinement_candidate")
        result["source_sha256"] = file_hash(args.input)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "replay_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "committed_revolutions": result["committed_revolutions"],
                "physical_commands_sent": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
