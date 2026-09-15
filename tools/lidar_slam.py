"""LiDAR SLAM 공간맵핑 — 지도를 만든다 (FR-6 · Phase 2 · 1단계).

    사용자가 로봇을 옮긴다 → 정지 → settle 대기 → 스캔 여러 장 → 정합 → 지도 갱신

**걸으면서 스캔하지 않는다** (ADR-7). 4족 보행은 매 스텝 피치·롤이 변하고 2D
LiDAR 는 스캔면이 수평이라고 가정하므로, 이동 중 스캔은 정합이 성립하지 않는다.
그래서 이 도구는 **로봇을 몰지 않는다** — 사람이 옮기고 이 도구는 받아적는다.
명령 소켓을 아예 열지 않는 이유가 그것이다.

    python tools/lidar_slam.py --simulate            # 가상 공간으로 알고리즘 확인
    python tools/lidar_slam.py --device mechdog-01 --lidar-device lidar-01

산출물은 `maps/` 로 간다 — `slam_map.npy` · `map_meta.json` · `slam_map.png`.
앞의 둘은 **한 쌍이다**: 메타 없이는 격자가 그냥 숫자 배열이다.
"""

from __future__ import annotations

import argparse
import contextlib
import random
import socket
import sys
import time
from pathlib import Path

# `python tools/lidar_slam.py` 로 직접 실행해도 host/ 를 찾게 한다.
# (pytest 는 pyproject 의 pythonpath 설정으로 해결된다)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.config import ConfigError
from host.common.console import survive_encoding_errors
from host.common.lidar_link import (
    Scan,
    ScanDecoder,
    points_from_wire,
    scan_of,
)
from host.common.logging_setup import event_logger, setup_logging
from host.common.protocol import system_clock_ms
from host.common.units import ms_to_s
from host.slam import settings, simulation, viz
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import (
    integrate_scan,
    match,
    merge_batch,
    predict,
    preprocess,
)
from host.slam.settings import match_params_from_config, range_from_config

LOG = event_logger("mechadog.tools.lidar_slam")

RECV_BYTES = 65536


def open_scan_socket(port: int, timeout_s: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        # Windows — 상대가 없을 때 ICMP 로 인해 recvfrom 이 예외를 던지는 것을 막는다
        # (`host/runtime.py open_socket` 과 같은 처리).
        with contextlib.suppress(OSError):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    sock.settimeout(timeout_s)
    return sock


def prepare_real_capture(sock: socket.socket, step: int, settle_s: float) -> None:
    """사용자 이동이 끝난 뒤 안정된 최신 스캔만 남긴다.

    중계 노드는 계속 송신하므로 단순히 대기한 뒤 읽으면 이동 중 패킷이 소켓
    버퍼에 남아 있다. Enter로 정지를 확인하고 안정화 시간을 기다린 다음,
    그동안 쌓인 패킷을 비워 다음 수신부터 새 배치를 만든다.
    """
    input(f"[SLAM] 위치 {step + 1}: 로봇을 옮겨 완전히 세운 뒤 Enter를 누르세요. ")
    time.sleep(settle_s)
    timeout = sock.gettimeout()
    sock.setblocking(False)
    try:
        while True:
            sock.recvfrom(RECV_BYTES)
    except (BlockingIOError, OSError):
        pass
    finally:
        sock.settimeout(timeout)


def collect_real(
    sock: socket.socket,
    decoder: ScanDecoder,
    batch_size: int,
    expected_device_id: str | None = None,
) -> list[Scan]:
    """정지 상태에서 스캔 여러 장을 모은다. **폐기된 패킷은 세지 않는다.**"""
    batch: list[Scan] = []
    while len(batch) < batch_size:
        try:
            raw, _ = sock.recvfrom(RECV_BYTES)
        except TimeoutError:
            LOG.warning("scan_timeout", have=len(batch), want=batch_size)
            return batch
        result = decoder.decode(raw)
        if result.warns:
            LOG.warning("scan_unknown_type", reason=result.reason)
            continue
        if not result.accepted:
            # 규칙 ③ — 폐기된 패킷은 링크가 살아 있다는 신호가 아니다.
            LOG.debug("scan_discarded", reason=result.reason)
            continue
        scan = scan_of(result)
        if scan is not None:
            if expected_device_id is not None and scan.device_id != expected_device_id:
                LOG.warning(
                    "foreign_lidar_scan",
                    expected=expected_device_id,
                    received=scan.device_id,
                )
                continue
            if scan.dropped:
                LOG.debug("scan_points_dropped", dropped=scan.dropped, kept=len(scan.points))
            batch.append(scan)
    return batch


def run(args: argparse.Namespace, config: dict) -> int:
    settings.require_lidar_track(config, simulation=args.simulate)
    lidar = config["lidar"]
    range_m = range_from_config(config)
    match_params = match_params_from_config(config)
    maps = Path(args.out) if args.out else settings.maps_dir(config)

    grid = OccupancyGrid.blank(
        resolution=lidar["resolution_mm"] / 1000.0,
        span_cells=int(lidar["initial_span_cells"]),
    )
    settle_s = ms_to_s(config["localization"]["settle_delay_ms"])
    pad = int(lidar["expand_pad_cells"])
    batch_size = int(lidar["scan_batch"])

    rng = random.Random(args.seed)
    sim_params = simulation.sim_params_from_config(config, range_m[1])
    decoder = ScanDecoder()
    sock: socket.socket | None = None
    if not args.simulate:
        sock = open_scan_socket(int(lidar["scan_port"]), ms_to_s(lidar["scan_stall_timeout_ms"]))
        LOG.info("scan_listen", port=int(lidar["scan_port"]))

    # 가상 매핑에서 로봇이 지나갈 경로. 실기에서는 사람이 옮긴다.
    sim_route = [(1.0, 1.0), (4.5, 1.0), (4.5, 4.0), (1.0, 4.0), (1.0, 1.0)]
    true_pose = (sim_route[0][0], sim_route[0][1], 0.0)
    route_index = 1

    pose = (0.0, 0.0, 0.0) if not args.simulate else true_pose
    previous_pose = pose
    trail = [(pose[0], pose[1])]
    live = viz.LiveMap(grid, title="LiDAR SLAM", enabled=args.plot)

    step = 0
    try:
        while args.steps <= 0 or step < args.steps:
            if args.simulate:
                # ⚠️ **전선 형식을 거쳐서 넣는다.** 목업이 내부 단위로 바로
                # 넘기면 단위 변환과 점 검증을 건너뛴 채 알고리즘만 시험하게
                # 되고, 실기에서 처음 그 경로를 지나게 된다.
                batch = [
                    Scan(
                        "lidar-sim",
                        "0" * 16,
                        step * batch_size + index + 1,
                        system_clock_ms(),
                        points_from_wire(
                            simulation.scan_world(
                                true_pose, simulation.DEFAULT_ROOM, sim_params, rng
                            )
                        ),
                    )
                    for index in range(batch_size)
                ]
            else:
                assert sock is not None
                prepare_real_capture(sock, step, settle_s)
                batch = collect_real(sock, decoder, batch_size, args.lidar_device)
                if not batch:
                    continue

            merged = merge_batch([scan.points for scan in batch])
            points_robot = preprocess(merged, *range_m)
            if points_robot.size == 0:
                LOG.warning("scan_empty_after_filter")
                continue

            center = predict(previous_pose, pose)
            result = match(grid, points_robot, center, match_params)
            if not result.skipped and result.score == 0:
                # 정합 실패 자세로 누적하면 한 번의 실패가 영구적인 가짜 벽이 된다.
                LOG.warning("scan_match_failed", step=step)
                continue
            previous_pose = pose
            pose = result.pose if not result.skipped else center
            integrate_scan(
                grid,
                pose,
                points_robot,
                hit=float(lidar["hit_logodds"]),
                miss=float(lidar["miss_logodds"]),
                pad_cells=pad,
            )
            trail.append((pose[0], pose[1]))
            live.update(pose, trail)

            if args.simulate:
                true_pose = simulation.waypoint_walk(
                    true_pose,
                    sim_route[route_index],
                    step_m=config["localization"]["move_increment_mm"] / 1000.0 * 0.4,
                    max_yaw_step=0.1,
                )
                if (
                    abs(true_pose[0] - sim_route[route_index][0]) < 1e-6
                    and abs(true_pose[1] - sim_route[route_index][1]) < 1e-6
                ):
                    route_index = (route_index + 1) % len(sim_route)
            step += 1
            if step % 20 == 0:
                LOG.info(
                    "mapping",
                    step=step,
                    known_cells=grid.known_cells(),
                    score=result.score,
                )
    except KeyboardInterrupt:
        LOG.info("interrupted", step=step)
    finally:
        live.close()
        if sock is not None:
            sock.close()
        written = grid.save(maps)
        with contextlib.suppress(ImportError):
            written["png"] = viz.save_png(grid, maps / "slam_map.png")
        LOG.info("map_saved", **{k: str(v) for k, v in written.items()})
        print(f"[SLAM] 지도 저장: {maps}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LiDAR SLAM 공간맵핑 — 지도를 만들어 maps/ 에 저장한다",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device", default=None, help="개체 프로파일 (실기 운용 시 필수)")
    parser.add_argument(
        "--lidar-device",
        default=None,
        help="LiDAR 중계 노드 device_id (실기 운용 시 필수)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="가상 LiDAR 로 알고리즘만 확인 (하드웨어 불요)",
    )
    parser.add_argument("--steps", type=int, default=160, help="매핑 사이클 수. 0 이면 Ctrl+C 까지")
    parser.add_argument("--seed", type=int, default=None, help="시뮬레이션 재현용 시드")
    parser.add_argument("--out", default=None, help="지도 저장 경로. 기본은 maps/")
    parser.add_argument("--plot", action="store_true", help="진행 상황을 창으로 본다 (matplotlib)")
    return parser


def main(argv: list[str] | None = None) -> int:
    # ⚠️ **인자 처리보다 앞이다** — cp949 콘솔에서 `--help` 조차 죽었다
    # (CONTRIBUTING 8절). 도움말은 `argparse` 가 stdout 에 쓴다.
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    if not args.simulate and (not args.device or not args.lidar_device):
        print(
            "[SLAM] 실기는 --device <unit-id>와 --lidar-device <relay-id>가 모두 필요하다; "
            "알고리즘만 확인하려면 --simulate를 쓴다",
            file=sys.stderr,
        )
        return 2
    try:
        config = settings.load(None if args.simulate else args.device)
        setup_logging(config, device_id=args.device or "lidar-sim")
        return run(args, config)
    except ConfigError as exc:
        print(f"[SLAM] 설정 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
