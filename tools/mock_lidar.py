"""가상 LiDAR 중계 노드 — 통신 상대방으로서의 DevKit (WBS 6.1 · Phase 2).

`tools/mock_mechdog.py` 와 같은 자리에 서는 도구다. 흉내내는 것은 *중계 노드가
보내는 스캔 데이터그램*뿐이고, UART 타이밍도 모터 간섭도 재현하지 않는다.

왜 필요한가 — **LiDAR 는 아직 제품이 확정되지 않았다** (ADR-18 · 재선정 중).
호스트측 SLAM·측위·순찰을 실물 없이 검증할 방법이 없으면, 장비가 도착하는 날
처음 통합을 시작하게 된다.

    python tools/mock_lidar.py --host 127.0.0.1
    python tools/mock_lidar.py --drop-rate 0.1 --corrupt-rate 0.05 --seed 42

⚠️ **이 목업은 규약 검증의 대상이지 근거가 아니다.** 여기서 만드는 전문이
`docs/PROTOCOL_LIDAR.md` 와 어긋나면 목업이 틀린 것이다 — 픽스처
(`tests/fixtures/lidar_samples.jsonl`)가 둘 사이의 기준이다.
"""

from __future__ import annotations

import argparse
import contextlib
import random
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.console import survive_encoding_errors
from host.common.lidar_link import encode_scan
from host.common.protocol import system_clock_ms
from host.common.units import ms_to_s
from host.slam import settings, simulation


def new_boot_id(rng: random.Random) -> str:
    """부팅마다 새 불투명 문자열. 난수 64비트의 16자리 hex (규약 5절 권장)."""
    return f"{rng.getrandbits(64):016x}"


def run(args: argparse.Namespace) -> int:
    config = settings.load(None)
    lidar = config["lidar"]
    range_max_m = float(lidar["range_max_mm"]) / 1000.0
    rng = random.Random(args.seed)
    sim_params = simulation.sim_params_from_config(config, range_max_m)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer = (args.host, args.port or int(lidar["scan_port"]))
    boot_id = new_boot_id(rng)
    seq = 0
    period_s = ms_to_s(args.period_ms)

    # 목업이 지나갈 경로. 실기에서는 사람이 로봇을 옮긴다.
    route = [(1.0, 1.0), (4.5, 1.0), (4.5, 4.0), (1.0, 4.0)]
    pose = (route[0][0], route[0][1], 0.0)
    index = 1

    print(f"[mock-lidar] {peer[0]}:{peer[1]} 로 송신 · device={args.device} boot={boot_id}")
    try:
        while True:
            seq += 1
            points = simulation.scan_world(pose, simulation.DEFAULT_ROOM, sim_params, rng)
            line = encode_scan(
                seq=seq,
                ts_ms=system_clock_ms(),
                device_id=args.device,
                boot_id=boot_id,
                points_wire=points,
            )
            if rng.random() < args.corrupt_rate:
                # 깨진 패킷 — 호스트가 규칙 ③ 대로 **링크 카운터를 갱신하지
                # 않는지** 보는 데 쓴다.
                line = line[: len(line) // 2]
            if rng.random() >= args.drop_rate:
                with contextlib.suppress(OSError):
                    sock.sendto(line.encode("utf-8"), peer)

            if args.walk:
                pose = simulation.waypoint_walk(pose, route[index], 0.1, 0.15)
                if abs(pose[0] - route[index][0]) < 1e-6 and abs(pose[1] - route[index][1]) < 1e-6:
                    index = (index + 1) % len(route)
            if args.reboot_at and seq == args.reboot_at:
                # 재부팅 — 새 `boot_id` 로 `seq` 가 1 로 돌아간다. 호스트가 이것을
                # 폐기하지 않고 수락하는지가 규칙 ① 의 요점이다.
                boot_id, seq = new_boot_id(rng), 0
                print(f"[mock-lidar] 재부팅 — boot={boot_id}")
            time.sleep(period_s)
    except KeyboardInterrupt:
        print(f"[mock-lidar] 종료 — {seq} 스캔 송신")
    finally:
        sock.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="가상 LiDAR 중계 노드 — 스캔을 UDP 로 보낸다")
    parser.add_argument("--host", default="127.0.0.1", help="호스트 주소")
    parser.add_argument("--port", type=int, default=None, help="기본은 lidar.scan_port")
    parser.add_argument("--device", default="lidar-mock", help="device_id")
    parser.add_argument("--period-ms", type=int, default=200, help="스캔 주기")
    parser.add_argument("--walk", action="store_true", help="목업이 경로를 따라 움직인다")

    faults = parser.add_argument_group("장애 주입")
    faults.add_argument("--drop-rate", type=float, default=0.0, help="송신 유실률 0.0~1.0")
    faults.add_argument("--corrupt-rate", type=float, default=0.0, help="깨진 패킷 송신률")
    faults.add_argument("--reboot-at", type=int, default=0, help="이 seq 에서 재부팅 (새 boot_id)")
    faults.add_argument("--seed", type=int, default=None, help="재현용 시드")
    return parser


def main(argv: list[str] | None = None) -> int:
    # ⚠️ **인자 처리보다 앞이다** — cp949 콘솔에서 `--help` 조차 죽었다
    # (CONTRIBUTING 8절). 도움말은 `argparse` 가 stdout 에 쓴다.
    survive_encoding_errors()
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
