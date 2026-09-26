"""Read-only live LiDAR/camera map from an independently measured local pose feed.

No pose feed means no map updates. The program never sends a robot command.
"""

from __future__ import annotations

import argparse
import json
import math
import select
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.protocol import system_clock_ms
from host.slam import settings
from host.slam.continuous_map import ContinuousMap, LidarPose
from host.slam.photo_map import PhotoRecorder


def pose_from_datagram(raw: bytes, received_ms: int) -> LidarPose:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("pose 전문이 객체가 아님")
    if type(data.get("stationary")) is not bool or type(data.get("level")) is not bool:
        raise ValueError("stationary/level 은 실제 판정의 boolean 이어야 함")
    values = [data[key] for key in ("x_m", "y_m", "yaw_rad")]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("pose 좌표가 유한한 숫자가 아님")
    return LidarPose(received_ms, *map(float, values), data["stationary"], data["level"])


def run(args) -> dict:
    if not 1 <= args.seconds <= 3600:
        raise ValueError("수신 시간은 1~3600초")
    output = Path(args.out)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"출력 폴더가 비어 있지 않음: {output}")
    config = settings.load(args.device)
    photos = PhotoRecorder(config, args.camera_url)
    mapper = ContinuousMap(config, args.lidar_device, photos)
    pose = None
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as scans,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as poses,
    ):
        # No SO_REUSEADDR: a second Windows UDP bind can succeed but receive zero.
        scans.bind(("", int(config["lidar"]["scan_port"])))
        poses.bind(("127.0.0.1", args.pose_port))
        output.mkdir(parents=True, exist_ok=True)
        photos.start()
        deadline = time.monotonic() + args.seconds
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([poses, scans], [], [], 0.2)
                if poses in ready:
                    raw, _ = poses.recvfrom(4096)
                    try:
                        pose = pose_from_datagram(raw, system_clock_ms())
                    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                        mapper.counts.setdefault("invalid_pose", 0)
                        mapper.counts["invalid_pose"] += 1
                if scans in ready:
                    raw, _ = scans.recvfrom(65536)
                    received_ms = system_clock_ms()
                    frame = photos.sample(received_ms)
                    updated = mapper.add(raw, received_ms, pose, frame, photo_dir=output)
                    if updated and mapper.counts["mapped_revolutions"] % args.save_every == 0:
                        mapper.save(output)
        except KeyboardInterrupt:
            pass
        finally:
            photos.close()
            mapper.save(output)
    return mapper.counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--lidar-device", required=True)
    parser.add_argument("--camera-url", required=True)
    parser.add_argument("--pose-port", type=int, default=5202)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.pose_port <= 65535 or args.save_every < 1:
        parser.error("pose-port 또는 save-every 범위 오류")
    try:
        counts = run(args)
        print(json.dumps(counts, ensure_ascii=False))
        return 0 if counts["mapped_revolutions"] else 2
    except (OSError, ValueError) as exc:
        print(f"실시간 지도 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
