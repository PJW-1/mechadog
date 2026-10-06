"""Replay time-ordered pose/SCAN/photo events into a continuously saved map.

The tool opens no robot command socket. A real moving map requires measured
pose events from WBS 5.4.3; missing poses are rejected, never invented.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.lidar_link import encode_scan
from host.slam import settings, simulation
from host.slam.continuous_map import ContinuousMap, LidarPose
from host.slam.photo_map import PhotoRecorder
from host.vision.stream_client import Frame


def simulation_events(config: dict, device: str):
    """Known simulator poses test accumulation, not physical localization."""
    rng = random.Random(7)
    params = simulation.sim_params_from_config(config, settings.range_from_config(config)[1])
    stations = (
        (1.0, 1.0, 0.0),
        (2.0, 1.0, 0.0),
        (3.0, 1.0, 0.0),
        (4.0, 1.0, 0.0),
        (4.0, 2.0, 1.5707963267948966),
        (4.0, 3.0, 1.5707963267948966),
    )
    seq = 0
    for station, pose in enumerate(stations):
        for repeat in range(3):
            seq += 1
            received_ms = 1000 + station * 1000 + repeat * 100
            yield {
                "kind": "pose",
                "received_ms": received_ms,
                "x_m": pose[0],
                "y_m": pose[1],
                "yaw_rad": pose[2],
                "stationary": True,
                "level": True,
            }
            yield {
                "kind": "scan",
                "received_ms": received_ms,
                "payload": json.loads(
                    encode_scan(
                        seq=seq,
                        ts_ms=received_ms,
                        device_id=device,
                        boot_id="simulated-session",
                        points_wire=simulation.scan_world(
                            pose, simulation.DEFAULT_ROOM, params, rng
                        ),
                    )
                ),
            }


def replay(events, mapper: ContinuousMap, output: Path, photo_root: Path, save_every: int):
    pose = None
    frame = None
    last_ms = -1
    for event in events:
        received_ms = event["received_ms"]
        if type(received_ms) is not int or received_ms < last_ms:
            raise ValueError("이벤트 수신 시각은 증가하는 정수 밀리초여야 함")
        last_ms = received_ms
        kind = event["kind"]
        if kind == "pose":
            pose = LidarPose(
                received_ms,
                float(event["x_m"]),
                float(event["y_m"]),
                float(event["yaw_rad"]),
                event["stationary"] is True,
                event["level"] is True,
            )
        elif kind == "photo":
            path = Path(event["path"])
            if not path.is_absolute():
                path = photo_root / path
            frame = Frame(path.read_bytes(), received_ms, int(event.get("seq", 0)))
        elif kind == "scan":
            raw = json.dumps(event["payload"], ensure_ascii=False)
            updated = mapper.add(raw, received_ms, pose, frame, photo_dir=output)
            if updated and mapper.counts["mapped_revolutions"] % save_every == 0:
                mapper.save(output)
        else:
            raise ValueError(f"알 수 없는 이벤트: {kind!r}")
    mapper.save(output)
    return mapper.counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--lidar-device", default="lidar-b03fd35ee950")
    parser.add_argument("--input", help="시간순 JSONL 이벤트. '-'는 stdin")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--camera-url", help="사진 이벤트를 쓰는 실기 재생에서 필요")
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.simulate == (args.input is not None):
        parser.error("--simulate 또는 --input 중 하나만 지정")
    if args.save_every < 1:
        parser.error("--save-every 는 1 이상")
    output = Path(args.out)
    if output.exists() and any(output.iterdir()):
        parser.error(f"출력 폴더가 비어 있지 않음: {output}")
    config = settings.load(None if args.simulate else args.device)
    if args.simulate:
        config["lidar"] = dict(config["lidar"])
        # Simulator scan_world emits already robot-relative angles.
        config["lidar"]["mount_yaw_deg"] = 0
        config["lidar"]["angle_direction"] = 1
    photos = PhotoRecorder(config, args.camera_url) if args.camera_url else None
    mapper = ContinuousMap(
        config,
        "lidar-sim" if args.simulate else args.lidar_device,
        photos,
        source="simulation" if args.simulate else "external_pose",
    )
    try:
        if args.simulate:
            counts = replay(
                simulation_events(config, "lidar-sim"), mapper, output, Path.cwd(), args.save_every
            )
        elif args.input == "-":
            counts = replay(
                (json.loads(line) for line in sys.stdin if line.strip()),
                mapper,
                output,
                Path.cwd(),
                args.save_every,
            )
        else:
            source = Path(args.input)
            with source.open(encoding="utf-8") as file:
                counts = replay(
                    (json.loads(line) for line in file if line.strip()),
                    mapper,
                    output,
                    source.parent,
                    args.save_every,
                )
        print(json.dumps(counts, ensure_ascii=False))
        return 0 if counts["mapped_revolutions"] else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"지도 재생 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
