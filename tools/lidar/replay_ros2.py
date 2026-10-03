"""기록된 실기 세션(`events.jsonl`)을 ROS2 컨테이너로 리플레이한다.

스캔 데이터그램은 기록된 그대로 `scan_bridge`(5203)로 보내고, `command_sent` +
`telemetry` 이벤트로 `Odometry` 를 재구성해 ODOM 을 `odom_bridge`(5204)로 보낸다.
로봇은 전혀 움직이지 않는다 — 오프라인 검증(WBS 5.4.4) 전용 도구다.

세션 기록 시각(`t`, epoch ms)을 그대로 시간축으로 쓴다. `--speed` 로 배속 가능.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.odom_link import OdomEncoder
from host.slam import settings
from host.slam.odometry import Odometry, hold_of_reading, odom_params_from_config


def iter_events(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="기록 세션을 ROS2 브리지로 리플레이")
    parser.add_argument("session", type=Path, help="events.jsonl 이 있는 세션 폴더")
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--scan-port", type=int, default=5203)
    parser.add_argument("--odom-port", type=int, default=5204)
    parser.add_argument("--speed", type=float, default=1.0, help="재생 배속 (1=실시간)")
    parser.add_argument("--odom-period-ms", type=int, default=100)
    parser.add_argument("--max-ms", type=int, default=0, help="0 이면 전체, 아니면 처음 ms 만큼만")
    args = parser.parse_args()

    config = settings.load(args.device)
    odometry = Odometry(odom_params_from_config(config))
    encoder = OdomEncoder(args.device, "replay")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    scan_target = (args.host, args.scan_port)
    odom_target = (args.host, args.odom_port)

    events_path = args.session / "events.jsonl"
    counts = {"scan": 0, "telemetry": 0, "command_sent": 0, "odom": 0}
    t0_rec = t0_wall = None
    next_odom = 0

    for event in iter_events(events_path):
        t = int(event["t"])
        if t0_rec is None:
            t0_rec, t0_wall = t, time.monotonic()
            next_odom = t
        if args.max_ms and t - t0_rec > args.max_ms:
            break
        # 기록 케이던스 유지
        due = t0_wall + (t - t0_rec) / 1000.0 / args.speed
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        kind = event.get("kind")
        if kind == "scan":
            sock.sendto(event["raw"].encode(), scan_target)
            counts["scan"] += 1
        elif kind == "command_sent":
            odometry.note_sent(event.get("lines") or [], t)
            counts["command_sent"] += 1
        elif kind == "telemetry":
            tel = json.loads(event["raw"])
            flags = tel.get("flags") or {}
            odometry.note_hold(
                hold_of_reading(tel.get("safety_latched"), flags.get("obstacle")), t
            )
            imu = tel.get("imu") or {}
            if isinstance(imu.get("yaw"), int | float):
                odometry.note_imu(float(imu["yaw"]), t, tel.get("boot_id", ""))
            counts["telemetry"] += 1

        if t >= next_odom:
            pose = odometry.pose(t)
            sock.sendto(
                encoder.encode(
                    ts_ms=t, x_m=pose.x_m, y_m=pose.y_m,
                    yaw_rad=pose.yaw_rad, valid=pose.valid,
                ).encode(),
                odom_target,
            )
            counts["odom"] += 1
            next_odom = t + args.odom_period_ms

    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
