"""자율 순찰 운용 루프 (FR-7 · Phase 2 · 3단계).

    LiDAR 소켓 ─▶ ScanDecoder ─▶ PatrolController ─▶ Commander ─▶ 명령 소켓
    텔레메트리 소켓 ─▶ TelemetryReceiver ─┘

**소켓과 실시각을 만지는 곳은 이 파일 하나다.** 그 위의 컨트롤러는 바이트와
시각만 받으므로 로봇도 LiDAR 도 없이 pytest 로 닫힌다 (ENGINEERING_GUIDE 2.1 ·
`host/runtime.py` 와 같은 구조).

규약에서 이 루프가 반드시 지켜야 하는 것 넷 —

    ① **첫 전문은 `STOP` seq=1** 이다 (`commander.open_session()`). 아니면 로봇이
      이전 세션의 최대 seq 를 넘을 때까지 우리 명령을 **통째로 폐기한다.**
    ② **변화가 없어도 10Hz 로 계속 보낸다.** 그것이 곧 링크 신호이고 별도
      하트비트가 없다 (PROTOCOL 1절).
    ③ `ESTOP` 은 **틱을 기다리지 않는다.** 즉시 보낸다.
    ④ `RESET_SAFE` 는 **사람이 확인했을 때만**, 그리고 보낸 뒤 텔레메트리의
      `state=IDLE` · `safety_latched=false` 로 해제를 확인한다.

    python tools/patrol_run.py --simulate --cycles 2
    python tools/patrol_run.py --device mechdog-01 --lidar-device lidar-01
"""

from __future__ import annotations

import argparse
import contextlib
import math
import random
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.behavior.commander import Commander
from host.behavior.patrol import (
    PatrolController,
    describe,
    drive_params_from_config,
)
from host.behavior.zones import ZoneStore
from host.common.config import ConfigError
from host.common.console import survive_encoding_errors
from host.common.lidar_link import (
    Scan,
    ScanDecoder,
    points_from_wire,
    scan_of,
)
from host.common.logging_setup import event_logger, setup_logging
from host.common.protocol import CommandEncoder, system_clock_ms
from host.common.units import deg_to_rad, ms_to_s
from host.slam import settings, simulation
from host.slam.occupancy import OccupancyGrid
from host.slam.settings import (
    match_params_from_config,
    plan_params_from_config,
    range_from_config,
)
from host.telemetry.receiver import TelemetryReceiver

LOG = event_logger("mechadog.tools.patrol_run")

RECV_BYTES = 65536

# 종료 신호도 UDP다. 한 번만 보내면 유실 시 온보드 타임아웃까지 마지막 MOVE가
# 남으므로 기본 런타임과 같은 횟수·간격으로 동일 ESTOP 전문을 반복한다.
SHUTDOWN_ESTOP_REPEATS = 3
SHUTDOWN_ESTOP_INTERVAL_S = 0.05

#: 시뮬레이션에서 `--reset-after-estop` 이 자동으로 풀어 줄 최대 횟수.
#: 실기에서는 **사람이 확인해야만** 풀리므로 (DR-16) 이것은 검증 편의이며,
#: 상한이 없으면 되풀이하는 실패를 통과로 만든다.
MAX_AUTO_RESETS = 5

#: 시뮬레이션 IMU 가 지도 좌표계와 갖는 **임의의 옵셋** (deg).
#:
#: ⚠️ **0 으로 두면 안 된다.** 실기에서 지도 좌표계는 매핑을 시작한 자리가
#: 정하고 IMU 의 0 은 부팅한 자리가 정하므로 둘 사이에는 임의의 옵셋이 있다.
#: 목업이 옵셋 0 을 보고하면 **IMU 를 절대값으로 잘못 쓰는 코드가 시뮬레이션에서
#: 멀쩡해 보인다** — 실제로 그 상태로 측위가 좌표계에 갇혀 있었고, 목업이 0 을
#: 보내서 그것이 드러나지 않았다 (docs/LIDAR_INTEGRATION.md 5절 ⑥).
SIM_IMU_OFFSET_DEG = 137.0

#: IMU 표류 (deg/틱). 자력계가 없어 적분값이 서서히 어긋난다.
SIM_IMU_DRIFT_DEG_PER_TICK = 0.02


def open_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        with contextlib.suppress(OSError):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    sock.setblocking(False)
    return sock


def build_controller(config: dict, maps: Path, seed: int | None) -> PatrolController:
    grid = OccupancyGrid.load(maps)
    labels = tuple(str(label) for label in config["zones"]["ids"])
    zones = ZoneStore.load(maps, labels)
    if not len(zones):
        raise ConfigError(
            f"구역 좌표가 없다: {maps / 'zones.json'} — tools/zone_select.py 를 먼저 실행한다"
        )
    lidar = config["lidar"]
    # 주기는 설정에서 온다 (`network.cmd_rate_hz: 10`). 코드에 100ms 를 박으면
    # 설정을 고쳐도 안 바뀐다.
    period_ms = round(1000 / float(config["network"]["cmd_rate_hz"]))
    return PatrolController(
        commander=Commander(CommandEncoder(), period_ms=period_ms),
        grid=grid,
        zones=zones,
        drive=drive_params_from_config(config),
        plan_params=plan_params_from_config(config),
        match_params=match_params_from_config(config),
        range_m=range_from_config(config),
        new_obstacle_margin_m=float(lidar["new_obstacle_margin_mm"]) / 1000.0,
        new_obstacle_check_radius_m=float(lidar["new_obstacle_check_radius_mm"]) / 1000.0,
        new_obstacle_confirmations=int(lidar["new_obstacle_confirmations"]),
        obstacle_mark_radius_m=float(lidar["obstacle_mark_radius_mm"]) / 1000.0,
        forward_fan_rad=deg_to_rad(float(lidar["forward_fan_deg"])),
        # 회피 시퀀스와 **같은 값을 쓴다** — 갇힌 상황을 몇 번까지
        # 스스로 풀어 보고 사람에게 넘길지의 값이다 (FR-2.3).
        max_reverify_attempts=int(config["fsm"]["avoid_attempts"]),
        random_after_first_cycle=bool(config["zones"]["random_after_first_cycle"]),
        rng=random.Random(seed),
    )


def send(
    sock: socket.socket | None,
    peer: tuple[str, int] | None,
    lines: list[str] | tuple[str, ...],
) -> None:
    if sock is None or peer is None:
        return
    for line in lines:
        with contextlib.suppress(OSError):
            # Windows 는 상대가 없으면 ICMP 로 예외를 낸다. UDP 는 도달을 보장하지
            # 않으므로 여기서 재시도하지 않는다 — 다음 틱이 100ms 뒤에 온다.
            sock.sendto(line.encode("utf-8"), peer)


def stop_for_shutdown(
    controller: PatrolController,
    sock: socket.socket,
    peer: tuple[str, int] | None,
) -> None:
    """프로세스 종료 전에 같은 ESTOP 데이터그램을 세 번 보낸다."""
    line = controller.emergency_stop("순찰 프로세스 종료")
    if peer is None:
        return
    payload = line.encode("utf-8")
    for attempt in range(SHUTDOWN_ESTOP_REPEATS):
        with contextlib.suppress(OSError):
            sock.sendto(payload, peer)
        if attempt + 1 < SHUTDOWN_ESTOP_REPEATS:
            time.sleep(SHUTDOWN_ESTOP_INTERVAL_S)


def serve_real(args: argparse.Namespace, config: dict, controller: PatrolController) -> int:
    """실기 운용. 두 소켓을 논블로킹으로 훑고 마감에 맞춰 전문을 낸다."""
    network = config["network"]
    lidar = config["lidar"]
    scan_sock = open_socket(int(lidar["scan_port"]))
    tlm_sock = open_socket(int(network["telemetry_port"]))
    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_ip = args.robot or network.get("mechdog_ip")
    peer = (peer_ip, int(network["cmd_port"])) if peer_ip else None

    scan_decoder = ScanDecoder()
    telemetry = TelemetryReceiver()

    # ① 세션 개시 — **가장 먼저 인코딩해야 한다** (`Commander.open_session` 주석).
    session_line = controller.commander.open_session()
    send(cmd_sock, peer, [session_line])
    LOG.info("session_opened", peer=str(peer))
    controller.start()

    try:
        while True:
            now_ms = system_clock_ms()

            # ── 텔레메트리 ──
            while True:
                try:
                    raw, sender = tlm_sock.recvfrom(RECV_BYTES)
                except (BlockingIOError, OSError):
                    break
                ingested = telemetry.ingest(raw)
                if ingested.warns:
                    LOG.warning("telemetry_unknown_state")
                if not ingested.accepted or ingested.reading is None:
                    continue
                if ingested.reading.device_id != args.device:
                    # 세 로봇이 같은 포트로 말한다. 다른 개체의 안전 상태와 IMU가
                    # 섞이면 엉뚱한 로봇의 자세로 경로를 계산한다 (DR-17).
                    LOG.warning(
                        "foreign_telemetry",
                        expected=args.device,
                        received=ingested.reading.device_id,
                    )
                    continue
                if peer is None:
                    # `mechdog_ip` 가 비어 있으면 첫 텔레메트리를 보낸 곳을
                    # 상대로 삼는다. 개체 판별은 `device_id` 가 이미 끝냈고
                    # 이것은 "어디로 답을 보낼까" 다 (runtime.py 머리말 · DR-17).
                    peer = (sender[0], int(network["cmd_port"]))
                    LOG.info("peer_learned", peer=str(peer))
                    send(cmd_sock, peer, [session_line])
                controller.observe_telemetry(ingested.reading, now_ms)

            # ── LiDAR 스캔 ──
            while True:
                try:
                    raw, _ = scan_sock.recvfrom(RECV_BYTES)
                except (BlockingIOError, OSError):
                    break
                result = scan_decoder.decode(raw)
                if result.warns:
                    LOG.warning("scan_unknown_type", reason=result.reason)
                    continue
                scan = scan_of(result)
                if scan is None:
                    continue
                if scan.device_id != args.lidar_device:
                    LOG.warning(
                        "foreign_lidar_scan",
                        expected=args.lidar_device,
                        received=scan.device_id,
                    )
                    continue
                # ③ 위험은 즉시 나간다.
                urgent = controller.guard_scan(scan)
                if urgent:
                    send(cmd_sock, peer, [urgent])
                controller.observe_scan(scan, now_ms)

            # ── 판단 ──
            send(cmd_sock, peer, controller.step(now_ms))
            # ② 변화가 없어도 10Hz 로 계속 보낸다.
            send(cmd_sock, peer, controller.commander.tick(now_ms))

            if args.cycles > 0 and controller.stats.cycles >= args.cycles:
                LOG.info("cycles_done", cycles=controller.stats.cycles)
                break

            due = controller.commander.next_due_ms
            sleep_s = 0.002 if due is None else max(0.0, ms_to_s(due - system_clock_ms()))
            time.sleep(min(sleep_s, 0.05))
    except KeyboardInterrupt:
        LOG.info("interrupted")
    finally:
        stop_for_shutdown(controller, cmd_sock, peer)
        scan_sock.close()
        tlm_sock.close()
        cmd_sock.close()
    return 0


def serve_simulated(args: argparse.Namespace, config: dict, controller: PatrolController) -> int:
    """시뮬레이션 — 소켓 없이 같은 판단 경로를 돌린다.

    ⚠️ **컨트롤러를 우회하지 않는다.** 스캔은 전선 형식으로 만들어 같은 변환을
    거치고, 명령은 `commander.tick` 이 만든 **실제 전문을 파싱해서** 자세에
    반영한다. 그래야 규약을 지나는 경로가 시뮬레이션에서도 검증된다 — 의도를
    직접 읽어 쓰면 인코더·클램핑·seq 를 건너뛴다.
    """
    import json

    lidar = config["lidar"]
    range_m = range_from_config(config)
    sim_params = simulation.sim_params_from_config(config, range_m[1])
    rng = random.Random(args.seed)
    period_s = ms_to_s(controller.commander.period_ms)

    true_pose = (args.start_x, args.start_y, 0.0)
    controller.pose = true_pose
    lines = [controller.commander.open_session()]
    controller.start()

    now_ms = system_clock_ms()
    seq = 0
    tick = 0
    # ⚠️ **자동 리셋에 상한을 둔다.** 상한이 없으면 "장애물로 들어가 E-STOP,
    # 리셋, 다시 들어가기"를 되풀이해도 검증이 통과한다 — 실제로 한 번의
    # 순찰에서 2538회를 세고서야 알아챘다. 사람은 그렇게 리셋하지 않는다.
    auto_resets = 0
    while args.cycles <= 0 or controller.stats.cycles < args.cycles:
        tick += 1
        if args.ticks > 0 and tick > args.ticks:
            LOG.warning("tick_budget_exhausted", ticks=tick)
            break

        segments = list(simulation.DEFAULT_ROOM)
        if args.inject_obstacle_at and tick >= args.inject_obstacle_at:
            segments += list(simulation.LATE_OBSTACLE)

        seq += 1
        scan = Scan(
            "lidar-sim",
            "0" * 16,
            seq,
            now_ms,
            points_from_wire(simulation.scan_world(true_pose, segments, sim_params, rng)),
        )
        # 목업 텔레메트리 — 온보드가 보고할 만한 최소 관측. `flags.obstacle` 을
        # 참으로 두면 컨트롤러가 온보드 판정을 따라 멈추는지 볼 수 있다.
        # **옵셋과 표류가 있는 IMU yaw 를 보고한다.** 컨트롤러는 변화량만
        # 쓰므로 옵셋에 영향받지 않아야 한다 — 그것이 이 값의 요점이다.
        imu_yaw = (
            math.degrees(true_pose[2]) + SIM_IMU_OFFSET_DEG + SIM_IMU_DRIFT_DEG_PER_TICK * tick
        )
        controller.observe_telemetry(
            _FakeReading(
                state="PATROL",
                safety_latched=False,
                obstacle=False,
                dist_cm=100,
                imu={"pitch": 0.0, "roll": 0.0, "yaw": imu_yaw},
                last_cmd_age_ms=20,
            ),
            now_ms,
        )
        urgent = controller.guard_scan(scan)
        if urgent:
            lines.append(urgent)
            LOG.info("sim_estop_sent")
            if args.reset_after_estop and auto_resets < MAX_AUTO_RESETS:
                auto_resets += 1
                lines.append(controller.request_reset())
                controller.observe_telemetry(
                    _FakeReading(
                        state="IDLE",
                        safety_latched=False,
                        obstacle=False,
                        dist_cm=100,
                        imu={"pitch": 0.0, "roll": 0.0, "yaw": imu_yaw},
                        last_cmd_age_ms=20,
                    ),
                    now_ms,
                )
        controller.observe_scan(scan, now_ms)

        lines.extend(controller.step(now_ms))
        emitted = controller.commander.tick(now_ms)
        lines.extend(emitted)

        # **실제 전문에서 보행을 읽어 자세에 반영한다.**
        for line in emitted:
            message = json.loads(line)
            if message["type"] == "MOVE":
                true_pose = simulation.apply_move(
                    true_pose, message["step"], message["angle"], period_s, sim_params
                )
        controller.pose = true_pose
        now_ms += controller.commander.period_ms

        if args.verbose:
            print(f"tick={tick:4d} {describe(controller)}")

    if auto_resets >= MAX_AUTO_RESETS:
        LOG.error("auto_reset_budget_exhausted", limit=MAX_AUTO_RESETS)

    stats = controller.stats
    print(
        f"[Patrol] 사이클 {stats.cycles} · 구역 {stats.zones_visited} · 재계획 {stats.replans} "
        f"· E-STOP {stats.estops} · 측위상실 {stats.lost} · 자동리셋 {auto_resets} "
        f"· 전문 {len(lines)}"
    )
    del lidar
    return 0


class _FakeReading:
    """시뮬레이션용 텔레메트리. `Reading` 과 **같은 필드 이름**을 쓴다.

    `Reading` 을 직접 만들지 않는 이유는 그것이 규약 검증을 통과한 레코드를
    뜻하기 때문이다. 여기서 만드는 것은 검증을 지나지 않았으므로 다른 타입인
    것이 맞다 — 목업이 진짜와 구분되지 않아야 하는 것은 `mock_mechdog.py`
    쪽이고 그것은 실제로 UDP 로 말한다.
    """

    __slots__ = (
        "dist_cm",
        "imu",
        "last_cmd_age_ms",
        "obstacle",
        "safety_latched",
        "state",
    )

    def __init__(self, **fields: object) -> None:
        for name in self.__slots__:
            setattr(self, name, fields.get(name))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="자율 순찰 — A* 경로로 구역을 돈다")
    parser.add_argument("--device", default=None, help="개체 프로파일 (실기 운용 시 필수)")
    parser.add_argument(
        "--lidar-device",
        default=None,
        help="LiDAR 중계 노드 device_id (실기 운용 시 필수)",
    )
    parser.add_argument("--simulate", action="store_true", help="소켓 없이 알고리즘만 돌린다")
    parser.add_argument("--robot", default=None, help="로봇 IP. 없으면 첫 텔레메트리 송신자")
    parser.add_argument("--maps", default=None, help="지도·구역 경로. 기본은 maps/")
    parser.add_argument(
        "--cycles", type=int, default=0, help="이 사이클 수를 돌면 종료. 0 이면 무한"
    )
    parser.add_argument("--seed", type=int, default=None, help="무작위 시작 구역 재현용 시드")

    sim = parser.add_argument_group("시뮬레이션")
    sim.add_argument("--ticks", type=int, default=4000, help="최대 틱 (무한 루프 방지)")
    sim.add_argument("--start-x", type=float, default=1.0)
    sim.add_argument("--start-y", type=float, default=1.0)
    sim.add_argument(
        "--inject-obstacle-at",
        type=int,
        default=0,
        help="이 틱부터 지도에 없던 장애물 등장",
    )
    sim.add_argument(
        "--reset-after-estop",
        action="store_true",
        help="E-STOP 뒤 자동으로 RESET_SAFE (검증용)",
    )
    sim.add_argument("--verbose", action="store_true", help="틱마다 상태를 찍는다")
    return parser


def main(argv: list[str] | None = None) -> int:
    # ⚠️ **가장 먼저 부른다.** cp949 콘솔에서는 `--help` 조차 `—` 때문에 죽었다 —
    # `argparse` 가 도움말을 stdout 에 쓰는 순간이라 인자 처리보다 앞이어야 한다.
    # 같은 함정을 저장소에서 네 번째로 밟았다 (CONTRIBUTING 8절).
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    if not args.simulate and (not args.device or not args.lidar_device):
        print(
            "[Patrol] 실기는 --device <unit-id>와 --lidar-device <relay-id>가 모두 필요하다; "
            "알고리즘만 확인하려면 --simulate를 쓴다",
            file=sys.stderr,
        )
        return 2
    try:
        config = settings.load(None if args.simulate else args.device)
        settings.require_lidar_track(config, simulation=args.simulate)
        setup_logging(config, device_id=args.device or "patrol-sim", console=args.verbose)
        maps = Path(args.maps) if args.maps else settings.maps_dir(config)
        controller = build_controller(config, maps, args.seed)
    except ConfigError as exc:
        print(f"[Patrol] 설정 오류: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"[Patrol] {exc}", file=sys.stderr)
        return 2

    if args.simulate:
        return serve_simulated(args, config, controller)
    return serve_real(args, config, controller)


if __name__ == "__main__":
    raise SystemExit(main())
