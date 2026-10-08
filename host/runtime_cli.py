"""호스트 운용 런타임의 명령줄 진입점 — `python -m host.runtime`.

인자를 읽고 설정·로거·비전·기록기·이력·관제 서버를 만들어 `Runtime` 하나에 잇는다.
판단은 전부 `host.runtime.Runtime` 에 있고, 여기는 **조립과 기동 순서**만 맡는다.
`host.runtime.main` 이 이 모듈의 `main` 으로 넘긴다 (`mechadog-runtime` 스크립트도 같다).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import random
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from host.behavior.commander import Commander
from host.behavior.mission import Mission
from host.behavior.patrol import PatrolController, controller_from_config, load_patrol_map
from host.behavior.zone_map import ZoneMap
from host.behavior.zones import ZoneStore
from host.cloud import broadcast
from host.common.blackbox import EventBlackbox
from host.common.config import ConfigError, load_config
from host.common.history import open_history
from host.common.logging_setup import event_logger, setup_logging
from host.common.trace import config_sha256, new_session_id
from host.common.units import deg_to_rad
from host.dashboard.state import DashboardState
from host.dashboard.wiring import _publish_event, dashboard_wiring
from host.runtime import Runtime, open_socket
from host.slam.occupancy import OccupancyGrid
from host.slam.pose_out import PoseOut
from host.slam.settings import maps_dir, require_lidar_track, validate_section
from host.telemetry.lidar_feed import open_lidar_feed
from host.telemetry.ros2_relay import forward_peer_of, odom_peer_of, open_odom_sender
from host.telemetry.session_recorder import SessionRecorder
from host.vision.worker import build_worker

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


#: 콘솔 확인 키. **경보 해제와 페일세이프 해제는 다른 키다** (아키텍처 3.1).
#:
#: 확인해야 하는 것이 다르기 때문이다 — F 는 물리 상태(넘어졌나·배터리·링크),
#: L3 는 상황 판단(침입자가 갔나·안전모·물건). 하나로 묶으면 비상정지를 풀는 조작이
#: 경보까지 지운다.
#: ⚠️ **여기에는 ASCII 와 한글만 쓴다.** 개발 콘솔은 cp949 이고 `print()` 가
#: `—`(U+2014) 같은 문자를 만나면 `UnicodeEncodeError` 로 **기동 자체가 실패한다** —
#: 실제로 이 배너에 붙였다가 그렇게 됐다 (CONTRIBUTING 8절).
CONSOLE_HELP = """관리자 확인 (키를 누르고 Enter)
  c   경보(L3) 확인: 상황을 확인했다
  r   페일세이프(F) 해제 요청: 원인을 확인했다"""


def watch_console(runtime: Runtime, stream: Any = None) -> None:
    """확인 키를 읽어 **요청만 넣는다.** 처리는 운용 루프가 한다.

    ⚠️ **한 글자 즉시 입력이 아니라 Enter 를 받는다.** 확인은 되돌릴 수 없는 조작이
    아니지만 *"경보를 껐다"* 는 기록을 남기므로, 지나가다 키가 눌리는 것으로
    일어나지 않는 편이 낫다. `teleop` 이 즉시 입력을 쓰는 것은 조종이 연속 동작이기
    때문이고 여기는 아니다.
    """
    for line in stream if stream is not None else sys.stdin:
        key = line.strip().lower()
        if key == "c":
            runtime.ask_alarm_confirm()
        elif key == "r":
            runtime.ask_reset()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m host.runtime",
        description="MechDog 호스트 운용 런타임",
    )
    parser.add_argument("--device", required=True, help="개체 id — config/devices/<id>.yaml")
    parser.add_argument("--robot-ip", default=None, help="설정의 mechdog_ip 를 덮어쓴다")
    parser.add_argument("--xiao-ip", default=None, help="설정의 xiao_ip 를 덮어쓴다")
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="진단용: 카메라·검출 워커 없이 모션 런타임만 실행한다",
    )
    parser.add_argument("--duration", type=float, default=None, help="N초 후 종료 (기본 무한)")
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        help="PC 로컬 관제 서버 포트 — 텔레메트리·검출 방송과 명령 API (기본 비활성, 예: 8000)",
    )
    parser.add_argument(
        "--mode",
        default=None,
        # ⚠️ **`choices` 를 두지 않는다.** argparse 가 막으면 *"고를 수는 있지만 선행
        # 기능이 없다"* 와 *"그런 모드가 없다"* 가 같은 오류로 뭉개진다. 판정은
        # `Mission` 이 하고 사유를 문장으로 돌려준다 (FR-11.7).
        help="운용 모드 — guard | factory (config 의 mission.mode 를 덮어쓴다)",
    )
    parser.add_argument("--patrol", action="store_true", help="기동 직후 순찰을 시작한다")
    parser.add_argument(
        "--ppe-test", action="store_true", help="PPE 시험: 공장 모드 표시·기록, 보행 자동 잠금"
    )
    parser.add_argument(
        "--ppe-down-reference",
        action="store_true",
        help="로컬 v12 person_down 참고 의견 (다운로드 없음)",
    )
    parser.add_argument(
        "--lidar-device",
        default=None,
        help="LiDAR 중계 노드 device_id — 주면 순찰을 LiDAR 측위·A* 경로로 돈다 (기본 비활성)",
    )
    parser.add_argument(
        "--reset-on-start",
        action="store_true",
        help="기동 시 사람이 원인 해소를 확인한 것으로 보고 RESET_SAFE 를 보낸다",
    )
    parser.add_argument(
        "--log-level", default=None, help="config.logging.level 을 덮어쓴다 (DEBUG 실행용)"
    )
    parser.add_argument(
        "--maps",
        default=None,
        help="지도·구역 폴더 — config 의 lidar.maps_dir 를 덮어쓴다 (patrol_run --maps 와 같다)",
    )
    parser.add_argument(
        "--pose-seed",
        default=None,
        metavar="X,Y,YAW_DEG",
        help="시작 자세 시드 (지도 좌표, m·deg) — 로봇을 원점이 아닌 곳에 놓을 때 첫 정합의 중심",
    )
    parser.add_argument(
        "--record-dir",
        default=None,
        help="실기 동시 기록 폴더 — 원본 SCAN·TELEMETRY·송신 명령·측위·영상 인식을 한 시간축 JSONL 로",
    )
    parser.add_argument(
        "--record-frame-ms",
        type=int,
        default=1000,
        help="기록할 카메라 프레임 간격 (기본 1000ms — 추론 결과는 매번 기록)",
    )
    parser.add_argument(
        "--motion-lock",
        action="store_true",
        help="보행 잠금 — 로봇에는 STOP·ESTOP·STATE·LED 만 보낸다 (입력 확인용, --patrol 과 함께 못 쓴다)",
    )
    return parser


#: 방송 합성기 적재를 기다리는 한도(초). 이 PC 실측 적재는 약 1.5초다.
BROADCAST_READY_TIMEOUT_S = 10.0


def _broadcaster(config: dict[str, Any]) -> broadcast.Broadcaster | None:
    """`config.broadcast` 가 켜져 있으면 관제 방송기를 돌려준다.

    방송기 자체를 돌려준다 — 호출부가 `Runtime` 에는 `.say` 를 넘기고, 대시보드에는
    방송기 자체를 넘겨 음량·무음 조절 API 가 붙게 한다.

    ⚠️ 설정 오기(`length_scale: "빠르게"`)는 방송만 끈다 — 방송은 런타임 기동을
    막지 않는다는 원칙이 설정 읽기에도 걸린다.

    ⚠️ **합성기 적재를 여기서 끝낸다** (비전 워밍업과 같은 이유, `Runtime.begin`).
    적재가 운용 루프와 겹치면 GIL 을 1.3~1.7초 쥐어 명령 간격이 600ms 를 넘고, 로봇이
    기동 직후 페일세이프에 다시 걸린다. 제한 시간을 넘기면 기다림만 그만두고 기동한다.
    """
    try:
        broadcaster = broadcast.from_config(config)
    except (TypeError, ValueError) as exc:
        LOG.warning("broadcast_config_invalid", error=f"{type(exc).__name__}: {exc}")
        return None
    if broadcaster is not None:
        broadcaster.wait_ready(BROADCAST_READY_TIMEOUT_S)
    return broadcaster


def _open_recorder(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    argv: list[str] | None,
    *,
    session_id: str,
    models: list[dict[str, Any]],
) -> SessionRecorder:
    """기록 폴더와 manifest. 비밀값을 넣지 않는다 — 설정은 해시만, 인자는 그대로.

    `session_id` 는 블랙박스 meta.json 과 같은 값이고, `models` 는 설정된 가중치의 sha256·메타다.
    """
    import subprocess

    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = ""
    return SessionRecorder(
        Path(args.record_dir),
        manifest={
            "session_id": session_id,
            "argv": sys.argv[1:] if argv is None else argv,
            "device": args.device,
            "lidar_device": args.lidar_device,
            "maps": args.maps,
            "motion_lock": args.motion_lock,
            "git_revision": revision,
            "config_sha256": config_sha256(config),
            "models": models,
            "mount_yaw_deg": (config.get("lidar") or {}).get("mount_yaw_deg"),
            "angle_direction": (config.get("lidar") or {}).get("angle_direction"),
        },
    )


def _pose_out(config: dict[str, Any], maps: Path) -> PoseOut | None:
    """대시보드 포즈 송신기 — 지도 폴더에 `pose_frame.json` 이 있을 때만 만든다.

    순찰 좌표 → 표시(평면) 좌표 변환은 지도가 가진다 (`PoseOut.of` 문서). 송신
    목적지는 `lidar.pose_out_host` · `pose_out_port` — 대시보드 포즈 수신 포트다.
    """
    lidar = config.get("lidar") or {}
    return PoseOut.of(
        maps,
        host=str(lidar.get("pose_out_host", "127.0.0.1")),
        port=int(lidar.get("pose_out_port", 5300)),
    )


def _seeded_controller(
    config: dict[str, Any],
    commander: Commander,
    patrol_map: tuple[OccupancyGrid, ZoneStore],
    pose_seed: str | None,
    maps_dir: Path | None = None,
) -> PatrolController:
    """순찰 컨트롤러를 만들고 `--pose-seed` 가 있으면 첫 정합의 탐색 중심을 심는다.

    `observe_map_pose` 가 아니라 `pose` 필드를 직접 쓴다 — 측위 완료 시각
    (`_last_pose_ms`)을 찍지 않아야 첫 정합 전용 넓은 창(`first_match_params`)이
    배치 오차를 흡수할 수 있다.
    """
    controller = controller_from_config(config, commander, *patrol_map, random.Random())
    # 걸으며 자란 지도를 다음 세션도 쓰게 한다 — 종료 시 `save_map` 이 이 폴더에 쓴다.
    controller.maps_dir = maps_dir
    if maps_dir is not None:
        # 구역 영역 지도 — 대시보드와 같은 파일이라 «지금 어느 방인가» 의 정의가 하나다.
        controller.zone_map = ZoneMap.load(maps_dir)
        LOG.info(
            "zone_map_loaded" if controller.zone_map else "zone_map_absent", maps=str(maps_dir)
        )
        # 측위 전용 지도 — 세션 SLAM 병합(가구 다리 랜드마크)을 담은 `slam_map_loc.npy` 가
        # 있으면 정합은 그것으로 하고, 경로 계획은 항법용 `slam_map.npy` 그대로다.
        if (maps_dir / "slam_map_loc.npy").is_file():
            controller.loc_grid = OccupancyGrid.load(maps_dir, stem="slam_map_loc")
            LOG.info("loc_map_loaded", maps=str(maps_dir))
    if pose_seed:
        try:
            x_s, y_s, yaw_s = (float(part) for part in pose_seed.split(","))
        except ValueError:
            raise ConfigError(
                f"--pose-seed 형식은 'x,y,yaw_deg' 다 — 받은 값: {pose_seed!r}"
            ) from None
        controller.pose = (x_s, y_s, deg_to_rad(yaw_s))
        controller.pose_seeded = True
        LOG.info("pose_seeded", x=x_s, y=y_s, yaw_deg=yaw_s)
    return controller


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # 인자 오류는 `parser.error` — 종료 코드 2 (`SystemExit(str)` 은 1 이라 설정 거부 rc 2 와 갈린다).
    if args.dashboard_port is not None and not 1 <= args.dashboard_port <= 65535:
        parser.error("dashboard-port must be between 1 and 65535")
    if args.motion_lock and (args.patrol or args.reset_on_start):
        parser.error("--motion-lock 은 --patrol · --reset-on-start 와 함께 쓸 수 없다")
    if args.record_frame_ms <= 0:
        parser.error("--record-frame-ms 는 1 이상이어야 한다")
    # ⚠️ 설정을 읽기 전에는 우리 로거가 없다. 여기서만 표준 출력을 쓴다.
    try:
        config = load_config(args.device)
        # ⚠️ **모드는 여기서 정해진다** (FR-11.2). 로거를 세우기 전에
        # 만드는 이유는 **모르는 모드나 선행 기능 없는 모드로는 기동 자체를 거부**
        # 하기 때문이다 (FR-11.7) — `ModeError` 가 `ConfigError` 라서 이 절이 잡는다.
        mission = Mission(config, mode=args.mode)
        if args.ppe_test:
            if not mission.enables("ppe") or args.no_vision or args.patrol or args.reset_on_start:
                raise ConfigError(
                    "--ppe-test는 factory 비전 모드이며 --patrol/--reset-on-start와 함께 쓸 수 없다"
                )
            args.motion_lock = True
            config = dict(config)
            config["vision"] = dict(config["vision"])
            config["vision"]["ppe"] = dict(
                config["vision"]["ppe"], test_mode=True, down_reference=args.ppe_down_reference
            )
        elif args.ppe_down_reference:
            raise ConfigError("--ppe-down-reference에는 --ppe-test가 필요하다")
        if mission.enables("ppe") and args.no_vision:
            raise ConfigError("factory 모드는 PPE 비전 없이 시작할 수 없다")
        if args.lidar_device:
            # `patrol_run` 과 같은 관문 — `lidar` 절 전수 검사와 `localization.track == lidar`
            # (ADR-18). 빠진 키·0 주기가 뒤에서 traceback 으로 새지 않는다.
            lidar = config.get("lidar")
            if not isinstance(lidar, dict):
                raise ConfigError("--lidar-device 에는 config.yaml 의 lidar 절이 필요하다")
            validate_section(lidar)
            require_lidar_track(config, simulation=False)
            # ROS2 컨테이너(전달·ODOM) 목적지 이름도 여기서 확인한다 — 못 풀면 기동 거부다.
            forward_peer_of(lidar)
            odom_peer_of(lidar)
        # 지도·구역이 없으면 기동을 거부한다 — 길 찾기를 달라고 했는데 고정 보행으로
        # 조용히 내려가면 운용자는 LiDAR 로 돈다고 믿는다.
        patrol_maps = Path(args.maps) if args.maps else maps_dir(config)
        patrol_map = load_patrol_map(config, patrol_maps) if args.lidar_device else None
    except (OSError, ValueError) as exc:  # `ConfigError` 와 깨진 지도·구역 파일(`json`·`np.load`)
        logging.basicConfig(level="ERROR")
        logging.getLogger("mechadog.runtime").error("설정을 읽을 수 없다 — %s", exc)
        return 2

    if args.log_level:  # CLI 가 `config.logging.level` 을 덮어쓴다 (DEBUG 실행용)
        config = dict(config)
        config["logging"] = dict(config["logging"], level=args.log_level.upper())
    if args.xiao_ip:
        config = dict(config)
        config["network"] = dict(config["network"], xiao_ip=args.xiao_ip)
    context = setup_logging(config, device_id=args.device)
    # 로거를 세운 뒤에 연다 — 실측이 없는 기체의 `odometry_unavailable` 이 JSONL 에 남는다. 그런 기체는 `None`.
    odom = open_odom_sender(config, args.device) if args.lidar_device else None

    session_id = new_session_id()
    vision = None if args.no_vision else build_worker(config)
    # 가중치 sha256 은 여기서 한 번 계산된다 (검출기가 들고 있다가 로드 때 다시 쓴다).
    recorder = (
        None
        if args.record_dir is None
        else _open_recorder(
            args,
            config,
            argv,
            session_id=session_id,
            models=[] if vision is None else vision.model_files(),
        )
    )
    blackbox = EventBlackbox(config)
    # 비었거나 열 수 없으면 `None` — 이력 없이 돈다 (ADR-46). 닫는 것은 아래 `finally` 다.
    history = open_history(config)
    dashboard = (
        DashboardState(args.device, stale_after_ms=int(config["safety"]["link_loss_failsafe_ms"]))
        if args.dashboard_port is not None
        else None
    )
    # 방송기 자체를 쥔다 — Runtime 에는 `.say` 만 넘기고, 대시보드에는 방송기 자체를
    # 넘겨 음량·무음 조절 API 가 붙게 한다.
    broadcaster = _broadcaster(config)
    runtime = Runtime(
        config,
        device_id=args.device,
        robot_ip=args.robot_ip,
        context=context,
        vision=vision,
        blackbox=blackbox,
        dashboard=dashboard,
        mission=mission,
        # 사건을 관제 화면으로 밀어 준다. ⚠️ **저장만으로는 완료가
        # 아니다** — 블랙박스는 디스크에 남기고 사람은 화면을 본다. 이 연결이
        # 없으면 기록은 쌓이는데 아무도 모른다.
        event_publisher=(None if dashboard is None else _publish_event(dashboard)),
        # 사건 문장을 Host PC 스피커로 읽는다. 워커가 데몬 스레드라
        # 따로 닫지 않는다.
        announcer=(None if broadcaster is None else broadcaster.say),
        navigator_factory=(
            None
            if patrol_map is None
            else lambda commander: _seeded_controller(
                config, commander, patrol_map, args.pose_seed, maps_dir=patrol_maps
            )
        ),
        odom=odom,
        pose_out=_pose_out(config, patrol_maps) if args.lidar_device else None,
        recorder=recorder,
        history=history,
        motion_lock=args.motion_lock,
        record_frame_ms=args.record_frame_ms,
        session_id=session_id,
    )
    sock = open_socket(runtime.telemetry_port)
    # ⚠️ **tty 일 때만 붙인다.** 서비스·CI 로 돌리면 stdin 이 즉시 EOF 라 스레드가
    # 바로 끝나고, 파이프로 돌리면 남의 출력을 키로 읽는다.
    if sys.stdin is not None and sys.stdin.isatty():
        threading.Thread(target=watch_console, args=(runtime,), daemon=True).start()
        print(CONSOLE_HELP)
    try:
        if args.reset_on_start:
            runtime.request_reset()
        if args.patrol:
            # ⚠️ **예약한다. 바로 시작하지 않는다.** 해제가 정착하면서 `IDLE` 로
            # 내려오므로, 먼저 시작한 순찰은 조용히 취소된다 (`ask_patrol` 참고).
            runtime.ask_patrol()
        with contextlib.ExitStack() as stack:
            if recorder is not None:
                # 가장 먼저 등록 → 가장 나중에 닫힌다. 종료 ESTOP·수신 정지까지 기록한다.
                stack.callback(recorder.close)
                recorder.start()
            if odom is not None:
                stack.callback(odom.close)
            if args.lidar_device:
                feed = open_lidar_feed(
                    config,
                    args.lidar_device,
                    # 순찰로 걷는 동안만 LiDAR 전방 거리로 세운다 (`LidarFeed.handle`).
                    armed=lambda: runtime.behavior.state == "PATROL",
                    on_danger=runtime.send_emergency_stop,
                    on_raw=(
                        None
                        if recorder is None
                        else lambda raw, at: recorder.record_raw("scan", raw, at_ms=at)
                    ),
                )
                # 서비스 종료 ESTOP(`serve` 의 `finally`) 뒤에 닫힌다.
                stack.callback(feed.stop)
                runtime.attach_scans(feed.take)
                feed.start()
            if dashboard is not None:
                from host.dashboard.server import running_server

                stack.enter_context(
                    running_server(
                        dashboard,
                        args.dashboard_port,
                        **dashboard_wiring(
                            runtime,
                            config,
                            vision=vision,
                            blackbox=blackbox,
                            broadcaster=broadcaster,
                        ),
                    )
                )
            runtime.serve(sock, duration_s=args.duration)
    except KeyboardInterrupt:
        LOG.info("interrupted", action="ESTOP 송신 후 종료")
    finally:
        sock.close()
        if history is not None:
            history.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
