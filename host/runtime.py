"""호스트 운용 런타임.

지금까지 만든 네 조각을 **한 프로세스로 잇는다.**

    소켓 ─▶ 수신기 ─▶ 사건 ─▶ FSM ─▶ 지시 ─▶ 송신기 ─▶ 소켓
          (telemetry/receiver.py) (behavior/fsm.py) (common/protocol.py)

**실시간과 소켓을 만지는 곳은 `serve()` 하나다.** 그 위의 `ingest()`·`tick()` 은
바이트와 시각만 받으므로 로봇도 소켓도 없이 시험된다 — 조각들을 그렇게 만들어
놓은 이유가 여기서 값을 한다.

⚠️ **다른 개체의 텔레메트리는 링크 시각을 갱신하지 않는다.** 갱신하면 A 가 조용해진
것을 B 의 패킷이 가려서 페일세이프가 걸리지 않는다. 개체 판별은 `device_id` 로 하며
**보낸 IP 로 하지 않는다** (DR-17).

`mechdog_ip` 가 비어 있으면 **첫 텔레메트리를 보낸 곳을 상대로 삼는다.** 이것은
*"누가 보냈는가"* 를 IP 로 판별하는 것이 아니라 *"어디로 답을 보낼까"* 이며, 그
판별은 위의 `device_id` 검사가 이미 끝낸 뒤다.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import random
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from host.behavior.actions import PostureSequence, register_actions
from host.behavior.auth import Authenticator
from host.behavior.auth_judge import AuthJudge
from host.behavior.commander import Commander
from host.behavior.escalation import Escalation
from host.behavior.fall_monitor import FallMonitor
from host.behavior.fsm import STANDBY, Behavior, Event, behavior_from_config
from host.behavior.mission import Mission
from host.behavior.path_cause import PathCause
from host.behavior.patrol import PatrolController, controller_from_config, load_patrol_map
from host.behavior.ppe_judge import PpeJudge
from host.behavior.track_controller import TrackController
from host.behavior.voice_auth import VoiceAuthWindow
from host.behavior.zone_inspector import ZoneInspector
from host.behavior.zone_map import ZoneMap
from host.behavior.zone_policy import ZonePpePolicy
from host.behavior.zones import Zone, ZoneStore
from host.cloud import broadcast
from host.common.blackbox import BlackboxEntry, EventBlackbox
from host.common.config import ConfigError, load_config, telemetry_ids
from host.common.lidar_link import Scan
from host.common.logging_setup import (
    ERROR_ON_ENTER,
    EdgeTrigger,
    LogContext,
    PeriodicSummary,
    event_logger,
    setup_logging,
)
from host.common.protocol import CommandEncoder, system_clock_ms
from host.common.units import deg_to_rad, rad_to_deg
from host.dashboard.state import DashboardState
from host.report.situation import describe
from host.slam.occupancy import OccupancyGrid
from host.slam.pose_out import PoseOut
from host.slam.settings import maps_dir, require_lidar_track, validate_section
from host.telemetry.lidar_feed import open_lidar_feed
from host.telemetry.receiver import Ingested, TelemetryReceiver
from host.telemetry.ros2_relay import (
    OdomSender,
    forward_peer_of,
    odom_peer_of,
    open_odom_sender,
    send,
)
from host.telemetry.session_recorder import SessionRecorder
from host.vision.frame_collector import collector_from_config
from host.vision.vlm_reader import VlmReader
from host.vision.vlm_session import build_session_factory
from host.vision.vlm_worker import VlmWorker
from host.vision.worker import TickIntervals, VisionResult, VisionSource, build_worker

LOG = event_logger("mechadog.runtime")

#: 한 번에 받아들이는 최대 바이트. 텔레메트리 한 줄은 300 바이트를 넘지 않는다.
RECV_BYTES = 2048
WSAEMSGSIZE = 10040
SHUTDOWN_ESTOP_REPEATS = 3
SHUTDOWN_ESTOP_INTERVAL_S = 0.02
EYE_LED_REFRESH_MS = 1000
#: 관제 사건 목록에 올리는 FSM 전이 — 인증과 안전 래치 해제만. ⚠️ 순찰·추적 전이는
#: 싣지 않는다. `ALERT ⇄ TRACK` 은 초당 몇 번씩 왕복해 목록을 덮는다.
FEED_TRANSITIONS: dict[Event, str] = {
    Event.AUTH_REQUIRED: "auth_required",
    Event.AUTH_OK: "auth_granted",
    Event.AUTH_FAILED: "auth_failed",
    Event.RESET_CONFIRMED: "failsafe_cleared",
}


def _is_oversized_datagram(exc: OSError) -> bool:
    """Windows가 수신 버퍼보다 큰 UDP 전문에 붙이는 오류만 가려낸다."""
    return getattr(exc, "winerror", None) == WSAEMSGSIZE or exc.errno == WSAEMSGSIZE


# `SOUND` ACK 대기 — 실측 왕복은 30ms 안팎(`last_cmd_age_ms`)이라 세 주기면 넉넉하다.
SOUND_ACK_TIMEOUT_MS = 300
SOUND_RETRIES = 2

#: VLM 적재가 끝날 때까지 순찰 시작을 미루는 상한. 실측 적재는 ~15초 — 이 상한을
#: 넘는 적재는 멈춰 있다고 보고, 굶김 위험을 로그로 남기고 시작한다.
VLM_LOAD_WAIT_MS = 60_000


def _is_command_ack(raw: str | bytes) -> bool:
    """로봇이 명령마다 돌려주는 응답(펌웨어 `sendAck`)인가. **텔레메트리가 아니다.**

    명령을 텔레메트리 소켓에서 보내므로 응답도 그 소켓으로 돌아온다. 텔레메트리
    폐기로 세면 실기에서 폐기가 초당 10건씩 늘어 **진짜 손상을 가린다** — 목업은
    응답을 보내지 않아 시험에서 드러나지 않았다.
    """
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return isinstance(msg, dict) and "verdict" in msg and "applied" in msg


#: 보행 잠금(`--motion-lock`)에서 로봇으로 내보내는 전문 — 정지·상태 알림만.
MOTION_LOCK_TYPES = frozenset({"STOP", "ESTOP", "STATE", "LED"})


def _command_type(line: str) -> str:
    try:
        value = json.loads(line).get("type")
    except (ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""


def _peer_text(peer: tuple[str, int] | None) -> str:
    """로그에 실을 상대 표기. 튜플을 그대로 문자열로 만들면 읽기 어렵다."""
    return f"{peer[0]}:{peer[1]}" if peer else "학습 대기"


def open_socket(bind_port: int) -> socket.socket:
    """텔레메트리 수신 포트에 묶은 UDP 소켓. 송신도 이 소켓으로 한다.

    ⚠️ **Windows 전용 처리가 하나 있다.** 아직 아무도 듣지 않는 포트로 보내면 ICMP
    Port Unreachable 이 돌아오고 Windows 는 그것을 *다음 `recvfrom` 의*
    `ConnectionResetError` 로 돌려준다. UDP 에 연결이 없으므로 의미 없는 오류이며,
    로봇이 아직 안 켜진 것은 정상이다. 수신 루프가 `ConnectionResetError` 를 잡아
    넘긴다 (`SIO_UDP_CONNRESET` 은 CPython 에 없어 ioctl 로는 끌 수 없다).

    ⚠️ `SO_REUSEADDR` 를 쓰지 않는다 — Windows 에서 UDP 소켓 둘이 이 옵션으로 같은
    포트에 묶이면 둘째도 오류 없이 성공하고 패킷을 하나도 받지 못한다. 포트가 이미
    점유돼 있으면 `bind` 가 즉시 실패하는 쪽이 낫다.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", bind_port))
    return sock


@dataclass(slots=True)
class Stats:
    """운용 결과. **틱 수와 송신 수를 따로 센다** — 둘이 어긋나면 루프가 밀렸다."""

    ticks: int = 0
    sent: int = 0
    accepted: int = 0
    discarded: int = 0
    foreign: int = 0
    transitions: int = 0
    states: dict[str, int] = field(default_factory=dict)

    def note_state(self, state: str) -> None:
        self.states[state] = self.states.get(state, 0) + 1


class Runtime:
    """수신·판단·송신을 한 객체로 묶는다. **소켓은 여기 없다.**"""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        device_id: str,
        robot_ip: str | None = None,
        clock: Callable[[], int] = system_clock_ms,
        context: LogContext | None = None,
        vision: VisionSource | None = None,
        blackbox: EventBlackbox | None = None,
        event_publisher: Callable[[BlackboxEntry], None] | None = None,
        dashboard: DashboardState | None = None,
        mission: Mission | None = None,
        vlm_reader: VlmReader | None = None,
        announcer: Callable[[str], None] | None = None,
        navigator_factory: Callable[[Commander], PatrolController] | None = None,
        odom: OdomSender | None = None,
        pose_out: PoseOut | None = None,
        recorder: SessionRecorder | None = None,
        motion_lock: bool = False,
        record_frame_ms: int = 1000,
    ) -> None:
        network = config["network"]
        rate_hz = int(network["cmd_rate_hz"])
        if rate_hz <= 0:
            raise ConfigError("network.cmd_rate_hz 는 1 이상이어야 함")
        self._device_id = device_id
        self._dashboard = dashboard
        self._clock = clock
        # 운용 모드. **FSM·에스컬레이션과 직교하는 세 번째 축**이며 표는
        # 하나다 — 모드는 *어떤 사건이 생길 수 있는지*만 고른다 (FR-11 · ADR-33).
        # `main()` 이 `--mode` 를 반영해 만들어 넘기고, 없으면 설정에서 만든다.
        self._mission = mission if mission is not None else Mission(config)
        self._telemetry_received_at: int | None = None
        # 우리 로봇의 텔레메트리로 받아들이는 이름 — 펌웨어는 MAC 이름을 보낸다 (`telemetry_ids`).
        self._own_ids = telemetry_ids(dict(config), device_id)
        self._cmd_port = int(network["cmd_port"])
        self._telemetry_port = int(network["telemetry_port"])
        self._receiver = TelemetryReceiver()
        self._commander = Commander(
            CommandEncoder(clock=clock),
            period_ms=1000 // rate_hz,
            clock=clock,
        )
        self._behavior = behavior_from_config(self._commander, config)
        # ACK 를 기다리는 `SOUND` 하나 — (seq, track, 다시 보낼 시각, 남은 재전송) (`_resend_sound`).
        self._sound_wait: tuple[int, int, int, int] | None = None
        self._sound_retries_left = SOUND_RETRIES
        # 상태별 모션을 붙인다. 만들 수 없는 것은 등록하지 않고 이유를 남기며,
        # 등록되지 않은 상태는 `Behavior` 가 정지로 처리한다 — 안전측 기본값이다.
        self._actions = register_actions(self._behavior, config)
        self._normal_patrol = self._behavior.sequence_for("PATROL")
        self._behavior.register_sequence("PATROL", self._patrol_sequence)
        # LiDAR 길 찾기 (`--lidar-device`). 없으면 순찰은 고정 보행 시퀀스 그대로다.
        # ⚠️ **같은 `Commander` 를 쓴다.** 송신기가 둘이면 seq 가 둘로 갈라져 로봇이
        # 뒤처진 쪽을 통째로 버린다 (`_send_lock` 설명과 같은 이유).
        navigator = navigator_factory(self._commander) if navigator_factory else None
        self._navigator = navigator
        #: 대시보드가 알려준 «지금 이 구역» — 서버 스레드가 넣고 루프가 꺼낸다.
        self._locate_lock = threading.Lock()
        self._locate_asked: str | None = None
        #: 지도에서 찍은 목표 — 서버 스레드가 넣고 루프가 꺼낸다. 결과는 피드백으로.
        self._goto_asked: tuple[float, float] | None = None
        self._goal_feedback: dict[str, Any] | None = None
        #: 루프가 틱마다 만든 항법 상태 묶음 (서버는 참조만 읽는다).
        self._nav_snapshot: dict[str, Any] | None = None
        #: 사람이 순찰을 멈췄다(PATROL → IDLE) — 찍은 목표를 버린다 (루프에서).
        self._goal_cancel_asked = False
        #: ROS2 컨테이너로 가는 ODOM (`--lidar-device`, 보행 실측이 있는 기체만). 없으면 보내지 않는다.
        self._odom = odom
        #: 대시보드로 가는 측위 포즈 — 지도 폴더의 `pose_frame.json` 이 있을 때만
        #: 만든다 (`PoseOut.of`). 변환 없는 원시 순찰 좌표는 뷰어에서 엉뚱한 자리에
        #: 찍히므로 «모른다» 가 낫다.
        self._pose_out = pose_out
        #: 실기 동시 기록 (`--record-dir`). 없으면 기록하지 않는다.
        self._recorder = recorder
        #: 보행 잠금 (`--motion-lock`) — 로봇으로는 `MOTION_LOCK_TYPES` 만 나간다. 펌웨어는 명령을
        #: 하나라도 받아야 텔레메트리를 보내므로(`telemetry_publisher.observe_command`) «아무것도 안
        #: 보내는» 입력 검증은 성립하지 않는다 — 정지 명령만 보내 IMU·상태를 받는다.
        self._motion_lock = motion_lock
        self._record_frame_ms = record_frame_ms
        self._last_frame_saved_ms: int | None = None
        self._recorded_nav_phase: str | None = None
        #: 스캔 공급자 — `main()` 이 `LidarFeed.take` 를 붙인다 (`attach_scans`).
        self._take_scan: Callable[[], Scan | None] | None = None
        #: `PATROL` 에 다시 들어왔다 — 다음 순찰 시퀀스가 길 찾기를 다시 푼다 (`_patrol_sequence`).
        self._navigator_resume = False
        if navigator is not None:
            # 추적·경보·구역 점검에서 돌아오면 지금 자리에서 같은 목표로 다시 푼다.
            # ⚠️ **훅에서 바로 풀지 않는다.** 전이는 대시보드 스레드(`apply_external`)에서도
            # 일어나고, 그때 풀면 루프가 웨이포인트를 따라가는 중에 경로가 비워진다.
            self._behavior.fsm.on_enter("PATROL", self._mark_navigator_resume)
            self._behavior.fsm.on_exit("PATROL", self._mark_goal_cancel)
            # 실제 «순찰 정지» 는 PATROL → MANUAL → IDLE 이다 (Codex 검토 G P1).
            self._behavior.fsm.on_exit("MANUAL", self._mark_goal_cancel)
        self._normal_alert = cast("PostureSequence | None", self._behavior.sequence_for("ALERT"))
        self._behavior.register_sequence("ALERT", self._alert_sequence)
        # 판정 자세는 `ALERT` 를 떠날 때 푼다. 판정기는 `__init__` 끝에서 만들고 호출 때 찾는다.
        self._behavior.fsm.on_exit(
            "ALERT", lambda _previous, _target: self._ppe_judge.return_pose()
        )
        # ⚠️ `network` 절 안에 있다. 최상위에서 찾으면 프로파일에 주소를 적어도
        # 못 읽고, 첫 텔레메트리가 올 때까지 아무것도 보내지 않는 상태가 된다.
        host = robot_ip or network.get("mechdog_ip")
        self._peer: tuple[str, int] | None = (host, self._cmd_port) if host else None
        # 운용 루프의 송신 소켓 — 대시보드 명령(ESTOP 등)이 다음 틱을 기다리지
        # 않게 즉시 보내는 경로가 쓴다. `serve` 가 시작할 때 채워진다.
        self._sock: socket.socket | None = None
        # 전문의 seq 를 받는 인코딩과 그 전문의 송신을 한 덩어리로 묶는다. 틱(운용 루프)과
        # 관제 ESTOP(대시보드 스레드)이 같은 인코더를 쓰므로, 묶지 않으면 송신 순서가 seq
        # 순서와 뒤바뀌고 로봇은 뒤로 간 쪽을 ESTOP 까지 버린다.
        self._send_lock = threading.Lock()
        self._reset_pending = False
        # ⚠️ **`_reset_pending` 과 다른 것이다.** 저것은 *로봇이 래치를 풀었다고
        # 보고할 때까지 기다리는 중*이고, 이 둘은 *사람이 눌렀고 아직 틱이 처리하지
        # 않았다*는 뜻이다. 콘솔·대시보드는 운용 루프와 다른 스레드에 있으므로
        # 거기서 단계나 송신기를 직접 만지면 루프가 전문을 만드는 중간에 바뀐다.
        # 급한 것은 `ESTOP` 하나뿐이다 (DR-16).
        self._alarm_confirm_asked = False
        self._reset_asked = False
        #: 순찰을 예약했다. ⚠️ **리셋이 정착한 뒤에 시작해야 한다** — 아래 참고.
        self._patrol_asked = False
        #: 순찰 예약 시각 — VLM 적재 대기의 상한을 재는 데 쓴다.
        self._patrol_asked_ms: int | None = None
        self._session_open: str | None = None
        self._ignored_since_ms: int | None = None
        self._cmd_timeout_ms = int(config["safety"]["cmd_timeout_ms"])
        self._stats = Stats()
        # 로깅 컨텍스트와 샘플링. **매 수신마다 한 줄씩 찍으면 10Hz × 운용시간이 되고
        # 정작 중요한 전이가 묻힌다** (ENGINEERING_GUIDE 1.3).
        self._log = context if context is not None else LogContext(device_id=device_id)
        self._log.observe(state=self._behavior.state)
        self._edge = EdgeTrigger()
        self._summary = PeriodicSummary(interval_ms=1000)
        # ⚠️ **비전은 선택이다.** 카메라가 없어도 순찰·회피는 돌아야 하고(NFR-2.6),
        # 목업 검증도 비전 없이 해 왔다. 붙이지 않으면 이 아래는 전부 비활성이다.
        self._vision = vision
        self._collector = collector_from_config(config, device_id)
        if self._vision is not None and hasattr(self._vision, "set_ppe_enabled"):
            self._vision.set_ppe_enabled(self._mission.enables("ppe"))
        # 경비 추종 (FR-3.5 · ADR-40). 쓰러짐 감시는 아래에서 만들므로 의심 여부는 호출 때 묻는다.
        self._track_controller = TrackController(
            config,
            behavior=self._behavior,
            mission=self._mission,
            summary=self._summary,
            apply=self._apply,
            fall_suspected=lambda: self._fall.suspected,
        )
        # 구역 변화 감지 (FR-8). 구역 도착은 **지도 좌표 앵커**(`maps/zones.json`)와
        # 측위 위치로 판정한다 (FR-7.4) — ArUco 구역 마커는 쓰지 않는다. 점검은
        # `ZoneInspector` 가 맡고(아래), 앵커는 기동 로그 순서를 지키려고 여기서 읽는다.
        zone_ids = tuple(str(label) for label in config["zones"]["ids"])
        # ⚠️ **앵커 파일이 기동을 막으면 안 된다** — 없거나 깨졌으면 구역 점검만 쉰다.
        try:
            # 길 찾기가 쓰는 지도(`--maps`)의 구역을 그대로 쓴다 — 설정의 maps_dir 를 따로 읽으면
            # 다른 지도를 줬을 때 도착 판정(길 찾기)과 점검(카메라)이 서로 다른 자리를 본다.
            navigator_zones = getattr(self._navigator, "zones", None)
            anchors: tuple[Zone, ...] = (
                navigator_zones.as_tuple()
                if isinstance(navigator_zones, ZoneStore)
                else ZoneStore.load(maps_dir(config), zone_ids).as_tuple()
            )
        except (OSError, ValueError) as exc:
            LOG.error("zones_unreadable", error=f"{type(exc).__name__}: {exc}")
            anchors = ()
        self._zone_ppe = ZonePpePolicy(config, anchors)
        # 상황 판독 (FR-8 · ADR-35). 객체 목록 비교로는 COCO 어휘 밖의
        # «넘어진 소화기» 를 말할 수 없어 사진을 그대로 읽는 경로를 하나 둔다.
        #
        # ⚠️ **의존성이 없으면 팩토리가 `None` 이고 판독은 «없음» 으로 동작한다** —
        # `submit()` 은 늘 거짓을 돌려주고 변화 감지는 그대로 돈다. Tier 3 이므로
        # 이것이 정상 동작이다(ADR-35 결정 6). 설치 절차는 `models/README.md` ③.
        self._vlm = VlmWorker(
            vlm_reader
            if vlm_reader is not None
            else VlmReader(
                build_session_factory(config),
                budget_ms=int(config["vision"]["vlm"]["budget_ms"]),
            )
        )
        #: 판독기를 주입받았나 — `begin()` 의 적재 여부 판정에 쓴다.
        self._vlm_injected = vlm_reader is not None
        #: 쓰러짐 확정 기준. 기록에 함께 실어 **그때 무슨 기준이었는지**를 남긴다 —
        #: 설정을 고친 뒤 옛 기록을 보면 기준을 알 수 없다.
        self._fallen_confirm_ms = int(config["vision"]["fallen"]["confirm_ms"])
        # 시작값은 «쓰러짐 없음» 이다. 비우면 첫 프레임의 `False` 가 해제 로그로 남는다.
        self._edge.changed("fallen", False)
        # 저장소와 대시보드 송신자를 주입한다. 정식 CLI는 저장소를 항상 연결하고,
        # FastAPI/WebSocket 서버가 생기면 같은 항목을 publisher로 받는다.
        # 둘을 분리해야 디스크 기록 성공과 브라우저 연결 여부가 서로 발목을 잡지 않는다.
        self._blackbox = blackbox
        self._event_publisher = event_publisher
        #: 상황 서술 문장을 관제로 내보내는 방송기. 비동기·예외를 던지지
        #: 않는 계약이지만 `_record_scene` 에서 다시 한 번 감싼다 — 아직 없는 계약을
        #: 믿고 안 감싸면 방송기가 하나라도 어기는 순간 제어 틱이 죽는다.
        self._announcer = announcer
        self._last_telemetry: dict[str, Any] = {
            "device_id": device_id,
            "available": False,
        }
        # ⚠️ **정상값을 미리 심는다.** `EdgeTrigger` 는 첫 관측을 변화로 보므로, 심지
        # 않으면 기동할 때마다 `vision_worker_unhealthy` 와 `vision_recovered` 가
        # 거짓으로 찍힌다 — 실제로 그렇게 찍혔다. **거짓 경보는 진짜 경보를 묻는다.**
        self._edge.changed("vision_healthy", True)
        self._edge.changed("vision_stalled", False)
        self._edge.changed("person", False)  # 첫 관측이 변화로 잡히지 않게
        self._edge.changed("track_blocked", False)
        self._last_eye_led_ms: int | None = None
        self._blocked_since_ms: int | None = None
        # ⚠️ **순찰에 들어갈 때 사람 게이트를 재장전한다.** `PERSON_FOUND` 는 상승
        # 엣지로만 나가므로, 순찰을 시작하는 순간 **이미 사람이 보이고 있으면 엣지가
        # 없어 로봇이 그 사람을 그냥 지나친다** — 실기에서 그랬다. 게이트가
        # 02:07:16 에 켜진 뒤 02:07:18 에 순찰이 시작됐고, 검출이 초당 10~40건인데도
        # `ALERT` 로 한 번도 가지 않았다. 사람은 내내 보고 있었고 *"새로 나타났다"* 로
        # 쳐지지 않았을 뿐이다 (FR-3.2).
        #
        # `PERSON_FOUND` 를 받는 상태는 `PATROL`·`ZONE_INSPECT` 인데 `ZONE_INSPECT` 는
        # `PATROL` 에서만 들어가므로 여기만 재장전하면 된다.
        #
        # ⚠️ **임무 밖에서 들어올 때만이다** (`STANDBY` = 대기·수동). 모든 `PATROL`
        # 진입에서 재장전하면 **인증을 통과한 사람이 곧바로 다시 경보를 올린다** —
        # `AUTH_WAIT → PATROL` 복귀 틱에 그 사람이 아직 화면에 있기 때문이다.
        # 임무 중의 복귀(`SCAN`·`AUTH_WAIT`)는 게이트가 이미 살아 있으므로 건드리지
        # 않는다. 낡은 것은 **순찰을 시작하는 순간의 게이트**뿐이다.
        self._behavior.fsm.on_enter("PATROL", self._rearm_person_gate)
        # 직전 IMU 방위와 그 시각. 각속도는 차분이라 표본 하나를 들고 있어야 한다.
        self._last_yaw: tuple[float, int] | None = None
        # 틱 **간격**을 기록한다 — 개수만 세면 최악을 놓친다.
        # 상한을 `cmd_timeout_ms` 로 잡는 이유: 그것을 넘으면 로봇이 스스로 멈춘다.
        self._intervals = TickIntervals(limit_ms=self._cmd_timeout_ms)
        # 대응 강도 축. FSM 상태와 **직교한다** — 같은 `ALERT` 에서도 단계가
        # 다르면 눈 색깔과 음향이 다르다.
        self._escalation = Escalation(config)
        # ⚠️ **값을 넣지 않고 물어볼 대상을 넘긴다.** 단계는 사건·시간·확인 어느
        # 쪽으로도 바뀌므로 갱신 지점이 하나가 아니고, 복사해 두면 반드시 어긋난다.
        self._log.bind_escalation(lambda: self._escalation.level.value)
        # 같은 이유로 모드도 물어본다 (FR-11.5) — 관제 화면에서 바뀌므로 갱신 지점이
        # 하나가 아니다.
        self._log.bind_mode(lambda: self._mission.mode)
        # 음성 암구호의 인증 창 (FR-10.3 · ADR-37). 창을 열고 닫는 것은 `_apply` 다.
        self._voice_auth = VoiceAuthWindow(
            config,
            behavior=self._behavior,
            mission=self._mission,
            apply=self._apply,
            clock=clock,
            feed=self._feed_event if dashboard is not None else None,
        )
        # 사원증 인증 (FR-10 · ADR-28). **판정은 여기, 픽셀은 워커**다 — 마커 읽기는 프레임을
        # 쥔 쪽에서 하고 누구의 인증인지는 추적 결과를 함께 보는 판정기가 정한다.
        self._auth_judge = AuthJudge(
            config,
            behavior=self._behavior,
            mission=self._mission,
            escalation=self._escalation,
            voice=self._voice_auth,
            apply=self._apply,
        )
        # 쓰러짐 의심(L1)→확정(L3) (ADR-42). 판독 워커를 구역 판독과 나눠 쓰므로 구역
        # 판독이 기다리는 중인지를 물어본다. 점검기가 감시를 쥐므로 감시를 먼저 만들고
        # 점검기는 호출 때 찾는다.
        self._fall = FallMonitor(
            config,
            behavior=self._behavior,
            mission=self._mission,
            escalation=self._escalation,
            vlm=self._vlm,
            apply=self._apply,
            record=self._record_scene,
            zone_waiting=lambda: self._zone_inspector.waiting or self._path_cause.waiting,
        )
        # 구역 점검 (FR-8 · ADR-41). 같은 판독 워커를 쓰고, 판독 «예» 는 쓰러짐 의심으로 넘긴다.
        self._zone_inspector = ZoneInspector(
            config,
            behavior=self._behavior,
            mission=self._mission,
            vlm=self._vlm,
            fall=self._fall,
            anchors=anchors,
            apply=self._apply,
            record=self._record_scene,
            path_waiting=lambda: self._path_cause.waiting,
        )
        # LiDAR 막힘 확정 프레임의 원인 판독 (ADR-45). 같은 판독 워커를 쓰므로 쓰러짐·구역
        # 판독이 걸려 있으면 상한 안에서 빌 때를 기다린다.
        self._path_cause = PathCause(
            config,
            mission=self._mission,
            vlm=self._vlm,
            # 호출 때 찾는다 — 시험이 기록·방송을 대역으로 바꿔 끼운다.
            record=lambda kind, frame, judgement: self._record_scene(kind, frame, judgement),
            announce=lambda kind, judgement: self._announce_situation(kind, judgement),
            others_waiting=lambda: self._fall.waiting or self._zone_inspector.waiting,
        )
        # 보호구 판정 (FR-9 · ADR-42). 쓰러짐 의심 중에는 판정을 보류하므로 감시 뒤에 만든다.
        self._ppe_judge = PpeJudge(
            config,
            behavior=self._behavior,
            mission=self._mission,
            escalation=self._escalation,
            fall=self._fall,
            commander=self._commander,
            apply=self._apply,
            record=self._record_scene,
        )
        # 모든 POSE 에 들어가는 정적 roll 보정 (`posture.roll_offset_deg`). IMU 상시
        # 기울기 편향의 반대 부호 — 0 이면 기존 동작과 동일하다.
        self._roll_offset_deg = float(config["posture"].get("roll_offset_deg", 0.0))

    @property
    def behavior(self) -> Behavior:
        return self._behavior

    @property
    def auth(self) -> Authenticator:
        """인증 세션. 대시보드가 *"누가 인증됐나"* 를 보이는 데 쓴다 (FR-3.6.2)."""
        return self._auth_judge.authenticator

    @property
    def voice_auth(self) -> VoiceAuthWindow:
        """음성 암구호의 인증 창. 시험이 시도 횟수·창이 열린 시각을 본다."""
        return self._voice_auth

    @property
    def mission(self) -> Mission:
        """운용 모드 (FR-11). 대시보드가 표시·전환에 쓴다."""
        return self._mission

    def set_mode(self, target: str) -> str | None:
        """관제 화면에서 온 모드 전환. 성공이면 `None`, 거절이면 사유 (FR-11.3).

        ⚠️ **상태를 여기서 읽어 넘긴다.** `Mission` 이 `Behavior` 를 알면 모드 축을
        FSM 없이 단독으로 시험할 수 없다 — 두 축을 잇는 곳은 런타임 하나다.
        """
        reason = self._mission.refuse_reason(target, state=self._behavior.state)
        if reason is None and target == "factory" and self._vision is not None:
            try:
                self._vision.set_ppe_enabled(True)
            except (OSError, RuntimeError, ValueError) as exc:
                return f"PPE 판정기를 열 수 없다: {exc}"
        if reason is None:
            reason = self._mission.switch(target, state=self._behavior.state)
        if reason is None:
            if self._vision is not None and hasattr(self._vision, "set_ppe_enabled"):
                self._vision.set_ppe_enabled(self._mission.enables("ppe"))
            self._ppe_judge.reset()
        return reason

    @property
    def escalation(self) -> Escalation:
        """대응 단계. **대시보드와 확인 경로가 보는 정본이다** (아키텍처 3.1)."""
        return self._escalation

    @property
    def commander(self) -> Commander:
        return self._commander

    @property
    def stats(self) -> Stats:
        return self._stats

    @property
    def intervals(self) -> TickIntervals:
        """틱 **간격** 분포. 개수(`stats.ticks`)로는 최악을 알 수 없다."""
        return self._intervals

    @property
    def vision(self) -> VisionSource | None:
        """붙어 있는 추론 워커. 없으면 `None` (비전 없이도 운용된다)."""
        return self._vision

    @property
    def vlm(self) -> VlmWorker:
        """상황 판독 워커. **가중치가 없어도 객체는 있다** — 그때는 늘 거절한다."""
        return self._vlm

    @property
    def actions(self) -> dict[str, str]:
        """등록된 상태별 모션과, 못 만든 것의 이유."""
        return dict(self._actions)

    @property
    def context(self) -> LogContext:
        """로그에 실리는 공통 컨텍스트. 대시보드도 같은 값을 본다."""
        return self._log

    @property
    def peer(self) -> tuple[str, int] | None:
        """명령을 보낼 곳. 설정에 없으면 첫 텔레메트리로 배운다."""
        return self._peer

    @property
    def telemetry_port(self) -> int:
        return self._telemetry_port

    def ingest(self, raw: str | bytes, now_ms: int) -> Ingested:
        """텔레메트리 한 줄을 넣는다. 사건까지 적용하고 결과를 돌려준다."""
        out = self._receiver.ingest(raw)
        if out.reading is None:
            if _is_command_ack(raw):
                self._summary.count("acks")
                if (
                    self._sound_wait is not None
                    and json.loads(raw).get("seq") == self._sound_wait[0]
                ):
                    self._sound_wait = None
                return Ingested(discarded="명령 응답")
            self._stats.discarded += 1
            self._summary.count("discarded")
            # ⚠️ 폐기는 **요약으로만** 남긴다. 30% 손상 구간에서는 초당 3줄이 되고,
            # 그때 알고 싶은 것은 개별 사유가 아니라 *"얼마나 깨지고 있나"* 다.
            if out.warns and self._edge.changed("discard_reason", out.discarded):
                LOG.warning("telemetry_discarded", reason=out.discarded)
            return out

        if out.reading.device_id not in self._own_ids:
            # ⚠️ 링크 시각을 갱신하지 않고 사건도 적용하지 않는다. 남의 패킷으로
            # "살아 있음" 을 세면 우리 로봇의 침묵이 가려진다.
            self._stats.foreign += 1
            if self._edge.changed("foreign", out.reading.device_id):
                LOG.warning(
                    "foreign_device",
                    expected=sorted(self._own_ids),
                    received=out.reading.device_id,
                )
            return Ingested(discarded=f"다른 개체: {out.reading.device_id}")

        self._stats.accepted += 1
        if self._dashboard is not None:
            self._telemetry_received_at = self._dashboard.now()
        self._last_telemetry = {
            "device_id": out.reading.device_id,
            "available": True,
            "boot_id": out.reading.boot_id,
            "seq": out.reading.seq,
            "state": out.reading.state,
            "batt_v": out.reading.batt_v,
            "dist_cm": out.reading.dist_cm,
            "imu": {
                "pitch": out.reading.pitch,
                "roll": out.reading.roll,
                "yaw": out.reading.yaw,
            },
            "last_cmd_age_ms": out.reading.last_cmd_age_ms,
            "safety_latched": out.reading.safety_latched,
            "flags": {
                "tipped": out.reading.tipped,
                "lowbatt": out.reading.lowbatt,
                "link_ok": out.reading.link_ok,
                "obstacle": out.reading.obstacle,
                # 서비스 모드 — 대시보드 토글이 이 값으로 라벨을 맞춘다.
                # 여기서 빠뜨리면 화면이 켜진 서비스 모드를 "꺼짐"으로 표시한다.
                "service": out.reading.service,
            },
        }
        self._track_controller.note_distance(out.reading.dist_cm)
        if self._navigator is not None:
            # 온보드 장애물 플래그와 IMU yaw(측위의 회전 변화량)를 길 찾기에 넘긴다.
            self._navigator.observe_telemetry(out.reading, now_ms)
        if self._odom is not None:
            self._odom.note_telemetry(out.reading, now_ms)
        self._log.observe(seq=out.reading.seq)
        self._summary.count("accepted")
        if out.reading.batt_v is not None:
            self._summary.observe("batt_v", out.reading.batt_v)
        if out.reading.last_cmd_age_ms is not None:
            self._summary.observe("last_cmd_age_ms", float(out.reading.last_cmd_age_ms))
        self._observe_yaw_rate(out.reading.yaw, now_ms)
        if out.reading.pitch is not None:
            # ⚠️ **«명령이 나갔다» 와 «자세가 도착했다» 는 다르다.** `POSE` 는 ACK 로
            # 확인되지만 그것은 로봇이 받았다는 뜻일 뿐이고, 벤더 `set_pose` 가 `dur`
            # 동안 보간해 실제로 기울었는지는 IMU 로만 알 수 있다. 실기에서
            # 자세가 올라가려다 멈추는 것을 **운용자 눈으로** 잡았는데, 그때 로그에는
            # 근거가 없었다 — `yaw_rate` 때와 같은 자리다.
            self._summary.observe("pitch_deg", float(out.reading.pitch))
        self._watch_command_uptake(out.reading.last_cmd_age_ms, now_ms)
        self._behavior.note_telemetry(now_ms)
        self._behavior.note_onboard_state(out.reading.state)
        self._behavior.note_robot_latch(out.reading.safety_latched)
        self._settle_reset(out.reading.safety_latched, now_ms)
        self._watch_track_blocked(out.reading, now_ms)
        # 로봇이 보고하는 온보드 상태의 **변화만** 남긴다. 같은 값이 10Hz 로 오는 것이
        # 정상이므로 매번 찍으면 로그가 그것으로 덮인다.
        if self._edge.changed("robot_state", out.reading.state):
            LOG.info(
                "robot_state",
                reported=out.reading.state,
                latched=out.reading.safety_latched,
            )
        for event in out.events:
            self._apply(event, now_ms)
        return out

    def _poll_vision(self, now_ms: int) -> None:
        """검출 결과를 **무블로킹으로** 집어 온다. 없으면 그냥 지나간다.

        ⚠️ **여기서 절대 기다리지 않는다.** 프레임을 기다리면 카메라 사정이 명령
        주기를 흔들고, 그러면 로봇이 `cmd_timeout_ms` 를 넘겨 멈춘다.

        ⚠️ **판정은 여기서 하지 않는다.** `person` 필터와 시간 창 판정은 워커가
        **추론마다** 끝내 놓는다(ADR-25) — 이 틱(10Hz)에서 관측하면 25fps
        결과 중 10개만 보게 되고, 그러면 추론률을 올린 이유가 사라진다. 여기서는
        **이미 나온 판정을 읽어 사건으로 옮길 뿐**이다.
        """
        if self._vision is None:
            return
        required = self._zone_ppe.required(now_ms)
        self._ppe_judge.set_requirements(required)
        if hasattr(self._vision, "set_ppe_requirements"):
            self._vision.set_ppe_requirements(required)
        self._fall.watch(now_ms)
        self._ppe_judge.settle(now_ms)
        result = self._vision.latest()
        if result is not None and not self._edge.changed("vision_seq", result.frame_seq):
            result = None
        if result is not None and self._recorder is not None:
            self._record_vision(result, now_ms)
        if result is not None:
            self._collect_clear(result, now_ms)
            self._ppe_judge.forget_lost(result, now_ms)
            self._summary.count("detections", len(result.detections))
            self._behavior.note_vision(result.completed_ms)
            # 확정 여부와 별개로 마지막 실제 person 히트를 기록한다. 게이트 해제는
            # 300ms 판정이고, FSM의 TARGET_LOST는 마지막 검출 뒤 5초이므로 섞지 않는다.
            seen_ms = result.sighting.last_seen_ms if result.sighting.hits > 0 else None
            if seen_ms is not None:
                self._behavior.note_target(seen_ms)
            # 대응 단계는 확정과 실제 검출을 **둘 다** 본다 — 확정으로 L1 에 올라가고
            # 마지막 검출로 L1 을 내린다. 하나로 합치면 창이 빌 때마다 단계가 흔들린다.
            #
            # ⚠️ (잠정) **임무 밖(대기·수동)에서는 올리지 않는다** (`fsm.STANDBY`). 인증도
            # 보지 않는다 — 대기 중 모르는 사원증 2장이 `AUTH_FAILED` 로 L3 를 만든다.
            inspected = (
                self._mission.enables("ppe")
                and bool(result.tracks)
                and self._ppe_judge.is_done(
                    max(result.tracks, key=lambda track: track.height).track_id
                )
            )
            # ⚠️ **경비 추종에서는 고개를 든 뒤에만 L1 이다** (ADR-40). 접근·정렬 중에
            # 올리면 인증 요청까지의 10초를 걷는 데 다 쓴다. 마지막 검출 시각은 그대로 넣는다.
            #
            # ⚠️ **공장 모드는 사람으로 L1 을 올리지 않는다.** 멈춰서
            # 보호구를 볼 뿐이다 — 노란 눈은 쓰러짐 의심 전용이다.
            gated = (
                self._track_controller.tracks_person() and not self._track_controller.engaged
            ) or self._mission.enables("ppe")
            if not self._behavior.standby and not inspected:
                self._escalation.note_person(
                    present=result.sighting.present and not gated,
                    last_seen_ms=seen_ms,
                    now_ms=now_ms,
                )
                self._auth_judge.judge(result, now_ms)
        # ⚠️ **판정은 워커가 추론마다 했고, 여기서는 결과만 읽는다.** 게이트를 이 틱
        # (10Hz)에서 돌리면 25fps 결과 중 10개만 보게 되고 추론률을 올린 이유가 사라진다.
        #
        # ⚠️ **변화 여부는 게이트의 `changed` 가 아니라 우리 기준으로 본다.** 게이트는
        # 25fps 로 도니까 한 틱 사이에 확정→해제가 다 지나갈 수 있고, 그러면 그 순간의
        # `changed` 는 우리가 못 본 전이를 가리킨다.
        if result is not None and self._edge.changed("person", result.sighting.present):
            LOG.info(
                "person_gate",
                present=result.sighting.present,
                hits=result.sighting.hits,
                score=round(result.sighting.best_score, 3),
            )
            # `present=False`는 게이트 확정이 풀렸다는 뜻일 뿐, 5초 대상 상실 사건이
            # 아니다. TARGET_LOST는 Behavior의 마지막 검출 타이머가 발생시킨다.
            if result.sighting.present and not inspected:
                self._apply(Event.PERSON_FOUND, now_ms)
                self._record_person_event(result)
        if result is not None:
            self._fall.take_reading(now_ms)
            self._observe_fallen(result, now_ms)
            self._ppe_judge.judge(result, now_ms)
            self._track_controller.track(result, now_ms)
            self._zone_inspector.inspect(result, now_ms)
            self._fall.ask(result, now_ms)
        self._switch_hazard_detector()
        # ⚠️ **워커가 죽어도 로봇은 계속 걷는다 — 그것이 가장 위험하다.** 스레드에서
        # 예외가 새면 조용히 사라지므로, 살아 있는지와 결과가 낡지 않았는지를 본다.
        healthy = self._vision.healthy()
        if self._edge.changed("vision_healthy", healthy) and not healthy:
            LOG.error("vision_worker_unhealthy")
        stalled = self._vision.stalled(now_ms)
        if not healthy or stalled:
            self._behavior.note_vision_stalled()
        if self._edge.changed("vision_stalled", stalled):
            # 비전 단절은 **기능 저하**다 — 페일세이프로 가지 않고 인지만 끈다
            # (config `vision.stall_timeout_ms` 주석 · NFR-2.6).
            LOG.warning(
                "vision_stalled" if stalled else "vision_recovered",
                age_ms=self._vision.age_ms(now_ms),
            )

    def _record_vision(self, result: VisionResult, now_ms: int) -> None:
        """새 추론 결과 하나 — 모델 출력 그대로. 프레임은 `record_frame_ms` 간격으로만 파일로 둔다."""
        assert self._recorder is not None
        frame_file = None
        due = self._last_frame_saved_ms is None or (
            now_ms - self._last_frame_saved_ms >= self._record_frame_ms
        )
        if due and result.jpeg:
            frame_file = self._recorder.save_frame(
                result.jpeg, frame_seq=result.frame_seq, received_ms=result.frame_received_ms
            )
            self._last_frame_saved_ms = now_ms
        self._recorder.record(
            "vision",
            at_ms=now_ms,
            frame_seq=result.frame_seq,
            frame_received_ms=result.frame_received_ms,
            completed_ms=result.completed_ms,
            inference_ms=round(result.inference_ms, 2),
            size=[result.frame_width, result.frame_height],
            detections=[
                {"label": d.label, "score": round(d.score, 3), "box": [round(v, 1) for v in d.box]}
                for d in result.detections
            ],
            person_present=result.sighting.present,
            frame_file=frame_file,
        )

    def _switch_hazard_detector(self) -> None:
        """위험물 추론은 위험구역 점검 중에만 켠다 (ADR-43 대안 ⓐ 개정).

        ⚠️ **다른 구역·이동 중에는 끈다** — 거기서는 위험물이 보여도 경고하지 않는다는 결정이고,
        끄면 프레임마다 전체 추론 하나(약 8ms)를 아낀다. 바뀔 때만 워커에 알린다.
        """
        vision = self._vision
        if vision is None or not hasattr(vision, "set_hazard_enabled"):
            return
        self._zone_inspector.note_hazard_detector(bool(vision.hazard_available))
        watching = self._zone_inspector.watching_hazards
        if self._edge.changed("hazard_detector", watching):
            vision.set_hazard_enabled(watching)
            LOG.info("hazard_detector_switched", enabled=watching)

    def _patrol_sequence(self, commander: Commander, now_ms: int) -> None:
        if self._ppe_judge.halts_patrol(now_ms) or self._auth_judge.holds_patrol(now_ms):
            commander.halt()
        elif self._navigator is not None:
            if self._navigator_resume:
                self._navigator_resume = False
                self._navigator.resume()
            # 상태 알림·래치·링크는 FSM 이 쥔다 — 길 찾기만 맡긴다 (`PatrolController.steer`).
            self._navigator.steer(now_ms)
        elif self._normal_patrol is not None:
            self._normal_patrol(commander, now_ms)

    def _mark_goal_cancel(self, previous: str, target: str) -> None:
        """사람이 멈췄다 — `PATROL`→`MANUAL`/`IDLE`, `MANUAL`→`IDLE`. 경보·추적으로 잠시 나간 것은 아니다.

        수동 조종도 취소다 — 사람이 로봇을 옮긴 뒤 옛 목표로 걸어가면 안 된다. 표시만 하고 루프가 처리한다.
        """
        if (previous == "PATROL" and target in ("IDLE", "MANUAL")) or (
            previous == "MANUAL" and target == "IDLE"
        ):
            self._goal_cancel_asked = True

    def _mark_navigator_resume(self, _previous: str, _target: str) -> None:
        """`PATROL` 진입 훅. 표시만 한다 — 길 찾기 상태는 루프 스레드만 바꾼다."""
        self._navigator_resume = True

    def _alert_sequence(self, commander: Commander, now_ms: int) -> None:
        """Factory PPE owns ALERT posture; guard keeps the existing alert sequence."""
        if not self._mission.enables("ppe"):
            if self._normal_alert is not None:
                self._normal_alert(commander, now_ms)
            if self._normal_alert is None or self._normal_alert.sent:
                self._track_controller.engage()
            return
        self._ppe_judge.alert_sequence(commander, now_ms)

    def note_pose(self, pose: tuple[float, float, float], now_ms: int) -> None:
        """측위의 최신 위치 `(x m, y m, yaw rad)` 를 받는다 (`ZoneInspector.note_pose`).

        LiDAR 길 찾기(`--lidar-device`)의 스캔 정합이 `_observe_scan` 에서 부른다.
        그것이 없으면 아무도 부르지 않고 구역 도착이 일어나지 않는다.
        """
        self._zone_inspector.note_pose(pose, now_ms)
        if self._pose_out is not None:
            # 대시보드 실시간 위치 — 송신 실패는 순찰을 늦추지 않는다 (`PoseOut.send`).
            self._pose_out.send(
                pose,
                moving=self._behavior.state == "PATROL",
                score_frac=getattr(self._navigator, "match_frac", None),
                zone=getattr(self._navigator, "current_zone", None),
                verified=bool(getattr(self._navigator, "pose_verified", False)),
            )
        self._zone_ppe.note_pose(pose, now_ms)

    def attach_scans(self, take: Callable[[], Scan | None]) -> None:
        """최신 스캔 공급자를 붙인다 (`LidarFeed.take`). 길 찾기가 없으면 쓰지 않는다."""
        self._take_scan = take

    def _observe_scan(self, now_ms: int) -> None:
        """최신 스캔으로 측위하고, 자세가 갱신됐으면 구역 점검에 넘긴다.

        ⚠️ **루프 스레드에서만 길 찾기 상태를 바꾼다.** 수신 스레드는 스캔을 칸에
        넣고 전방 위험만 즉시 세운다 (`host/telemetry/lidar_feed.py`).
        """
        if self._navigator is None or self._take_scan is None:
            return
        scan = self._take_scan()
        if scan is not None:
            navigator = self._navigator
            _obs_started = time.perf_counter()
            navigator.observe_scan(scan, now_ms)
            _obs_ms = (time.perf_counter() - _obs_started) * 1000
            if _obs_ms > 300:
                LOG.warning("observe_scan_slow", ms=round(_obs_ms, 1), points=len(scan.points))
            updated = navigator.pose_ms == now_ms
            if updated:
                self.note_pose(navigator.pose, now_ms)
            elif (
                self._pose_out is not None
                and navigator.pose_ms is not None
                and navigator.pose_stale(now_ms)
            ):
                # 약한 정합을 조용히 삼키지 않는다 — 마지막 자세를 LOST 로 표시해
                # 대시보드가 낡은 위치를 신선한 것처럼 보여주지 않게 한다.
                self._pose_out.send(
                    navigator.pose,
                    moving=False,
                    lost=True,
                    score_frac=getattr(navigator, "match_frac", None),
                    zone=getattr(navigator, "current_zone", None),
                    verified=False,
                )
            if self._recorder is not None:
                x, y, yaw = navigator.pose
                self._recorder.record(
                    "localization",
                    at_ms=now_ms,
                    scan_seq=scan.seq,
                    scan_boot=scan.boot_id,
                    points=len(scan.points),
                    updated=updated,
                    score_frac=round(navigator.match_frac, 3),
                    lost=navigator.pose_stale(now_ms),
                    verified=navigator.pose_verified,
                    zone=navigator.current_zone,
                    pose=[round(x, 4), round(y, 4), round(yaw, 5)],
                    phase=navigator.phase.value,
                    target=navigator.target,
                )
        if self._recorder is not None and self._navigator.phase.value != self._recorded_nav_phase:
            self._recorded_nav_phase = self._navigator.phase.value
            self._recorder.record(
                "navigator_phase",
                at_ms=now_ms,
                phase=self._recorded_nav_phase,
                halt_reason=self._navigator.halt_reason,
                target=self._navigator.target,
            )
        for hit in self._navigator.take_new_obstacles():
            self._collect_blocked(hit, now_ms)
            self._record_path_blocked(hit, now_ms)

    def _collect_blocked(self, hit: tuple[float, float], now_ms: int) -> None:
        """LiDAR 막힘 확정 프레임을 VLM 학습용으로 모은다 (4.8.7 · 꺼져 있으면 아무 일 없다).

        프레임이 없어도 수집기에 알린다 — 막힘 사건 자체로 clear 보류 시간을 건다.
        """
        if not self._collector.enabled:
            return
        result = self._vision.latest() if self._vision is not None else None
        self._collector.note_blocked(
            now_ms,
            result.jpeg if result is not None else None,
            hit,
            cast(PatrolController, self._navigator).target,
            self._behavior.state,
            frame_ms=result.frame_received_ms if result is not None else None,
            frame_seq=result.frame_seq if result is not None else None,
        )

    def _collect_clear(self, result: VisionResult, now_ms: int) -> None:
        """막힘 없는 순찰 프레임을 VLM 학습용으로 모은다. 근거리 반사 정지·장애물 확인 중엔 거른다."""
        if not self._collector.enabled or self._navigator is None:
            return
        self._collector.note_clear(
            now_ms,
            result.jpeg,
            state=self._behavior.state,
            obstacle_active=self._navigator.safety.obstacle_active,
            pending=self._navigator.obstacle_pending,
            frame_ms=result.frame_received_ms,
            frame_seq=result.frame_seq,
        )

    def _record_path_blocked(self, hit: tuple[float, float], now_ms: int) -> None:
        """이동 경로가 새 장애물로 막혔다 — **가벼운 경고만** 남기고 순찰은 이어 간다.

        단계·FSM 은 바꾸지 않는다. 재계획(A*)이 돌아갈 길을 찾고, 못 찾으면 컨트롤러가
        재확인 뒤 그 구역을 이번 사이클에서 버린다 (`PatrolController._replan`).

        ⚠️ **순찰 중일 때만 기록한다.** 추적·경보 중에는 앞에 선 사람이 신규 장애물로
        확정되는데, 그것은 «경로가 막혔다» 가 아니다. 표시 자체는 남아 재계획이 피해 간다.

        기록은 `PathCause` 가 그 프레임의 원인 판독(«무너진 물건인가» · ADR-45)을 실어 한 번
        남긴다 — 판독을 걸 수 있으면 답이나 `vision.vlm.path_cause_wait_ms` 까지 미룬다.
        """
        judgement: dict[str, Any] = {
            "x": round(hit[0], 2),
            "y": round(hit[1], 2),
            "target": cast(PatrolController, self._navigator).target,
            "source": "lidar",
        }
        if self._behavior.state != "PATROL":
            LOG.info("path_obstacle_ignored", state=self._behavior.state, **judgement)
            return
        LOG.warning("path_blocked", **judgement)
        result = self._vision.latest() if self._vision is not None else None
        self._path_cause.blocked(judgement, result, now_ms)

    def _observe_fallen(self, result: VisionResult, now_ms: int) -> None:
        """누움 후보로 쓰러짐 의심에 든다.

        ⚠️ **판정은 워커가 추론마다 했고 여기서는 결과만 읽는다** — 게이트·추적과
        같은 이유다(10Hz 에서 재면 25fps 중 10개만 본다).

        ⚠️ **규칙 단독 `PERSON_DOWN` 은 없다.** 누움은 의심 진입에만 쓰고 확정에는
        세지 않는다 — 누운 사람을 거의 못 잡는다. 워커의 3초 정지
        확정(`fallen`)은 엣지 로그로만 남는다. 경비 모드는 예전처럼 그 엣지를 기록까지만 남긴다.
        """
        verdict = getattr(result, "fallen", None)
        if verdict is None:
            return
        if self._mission.enables("fallen") and verdict.candidate:
            self._fall.suspect("yolox", now_ms)
        # ⚠️ **엣지는 워커의 `changed` 가 아니라 우리 기준으로 본다** — `person` 과 같은
        # 이유다. 워커는 25fps 라 `changed` 가 실린 프레임이 이 틱(10Hz) 전에 덮어써진다.
        # 실기에서 워커 확정 6번 중 4번이 그렇게 사라졌다.
        if not self._edge.changed("fallen", verdict.fallen):
            return
        LOG.warning(
            "fallen_changed",
            fallen=verdict.fallen,
            aspect=verdict.aspect,
            still_ms=verdict.still_ms,
        )
        # 공장 모드의 기록은 확정(`FallMonitor`) 때 한 번 남긴다.
        if not verdict.fallen or self._mission.enables("fallen"):
            return
        self._record_scene(
            "person_fallen",
            result,
            {
                "fallen": True,
                "aspect": verdict.aspect,
                "still_ms": verdict.still_ms,
                "confirm_ms": self._fallen_confirm_ms,
            },
        )

    def _record_person_event(self, result: VisionResult) -> None:
        """확정 검출의 원본과 판단 근거를 한 번 저장하고 이벤트 채널에 넘긴다.

        게이트의 거짓→참 엣지에서만 호출되므로 10Hz 반복 저장은 일어나지 않는다.
        저장·대시보드 오류가 제어 루프를 죽이면 로깅이 안전보다 우선하는 꼴이 되므로
        두 실패는 각각 기록하고 제어는 계속한다.
        """
        self._record_scene("person_found", result)

    def _record_scene(
        self, event_type: str, result: VisionResult, judgement: dict[str, Any] | None = None
    ) -> None:
        """사진과 **그릴 수 없는 판단 근거**를 한자리에 남긴다.

        ⚠️ **검출 박스와 달리 이것들은 그림이 없다.** 쓰러짐 판정은 숫자이고
        VLM 판독은 문장이라, 사진 옆에 적어 두지 않으면 나중에 *"왜 그렇게
        판정했나"* 를 되짚을 방법이 없다.

        ⚠️ **문장 생성·방송은 기록이 없어도 나간다.** 관제가 그 순간 들어야
        할 경고이지 블랙박스 파일이 아니므로, 블랙박스가 없는 구성(`blackbox=None`)
        에서도 방송만은 막지 않는다.
        """
        sentence: str | None = None
        # 경비 모드의 쓰러짐은 기록만 남긴다 — 경보도 확인할 것도 없는 사건이라
        # 방송·자막 문장을 붙이지 않는다.
        if event_type != "person_fallen" or self._mission.enables("fallen"):
            sentence = self._announce_situation(event_type, judgement)
        if self._blackbox is None:
            return
        recorded_judgement = judgement
        if sentence is not None:
            recorded_judgement = {**(judgement or {}), "sentence": sentence}
        try:
            entry = self._blackbox.record(
                event_type,
                judgement=recorded_judgement,
                jpeg=result.jpeg,
                tracks=result.tracks,
                detections=result.detections,
                telemetry=self._last_telemetry,
                state=self._behavior.state,
                escalation=self._escalation.level.value,
                mode=self._mission.mode,
                now_ms=result.completed_ms,
            )
        except Exception as exc:  # noqa: BLE001 — 기록 실패가 10Hz 제어를 죽이면 안 된다
            LOG.error("blackbox_record_failed", error=f"{type(exc).__name__}: {exc}")
            return
        if self._event_publisher is None:
            return
        try:
            self._event_publisher(entry)
        except Exception as exc:  # noqa: BLE001 — 브라우저 단절은 제어 실패가 아니다
            LOG.error("dashboard_event_publish_failed", error=f"{type(exc).__name__}: {exc}")

    def _announce_situation(self, event_type: str, judgement: dict[str, Any] | None) -> str | None:
        """상황 문장을 만들어 방송한다. 만든 문장(없으면 `None`)을 돌려준다."""
        sentence: str | None = None
        try:
            sentence = describe(event_type, judgement)
        except Exception as exc:  # noqa: BLE001 — 문장 생성 실패가 10Hz 제어를 죽이면 안 된다
            LOG.error("situation_failed", error=f"{type(exc).__name__}: {exc}")
        if sentence is not None and self._announcer is not None:
            try:
                self._announcer(sentence)
            except Exception as exc:  # noqa: BLE001 — 방송 실패가 제어를 막으면 안 된다
                LOG.error("announce_failed", error=f"{type(exc).__name__}: {exc}")
        return sentence

    def _observe_yaw_rate(self, yaw: float | None, now_ms: int) -> None:
        """IMU 방위를 **각속도**로 바꿔 1초 요약에 싣는다 (좌우 대칭 근거).

        ⚠️ **yaw 를 그대로 평균 내면 안 된다.** 0~360 이라 경계에서 뒤집히고
        (359° 다음 1° 가 −358° 로 읽힌다), 좌우 대칭을 보려면 필요한 것은 방위가
        아니라 *"얼마나 빨리 도는가"* 다. 그래서 차분을 ±180 으로 접어 시간으로 나눈다.

        ⚠️ **부호를 살린다.** `track_dev_px` 는 좌우 진동을 감추지 않으려고 절대값을
        쓰지만 여기는 반대다 — **어느 쪽으로 돌았는지가 재려는 것 자체다.** 화면
        편차(px/s)는 대상까지의 거리에 종속돼 같은 런 안에서도 두 배씩 달라진다.
        """
        if yaw is None:
            return
        previous = self._last_yaw
        self._last_yaw = (yaw, now_ms)
        if previous is None:
            return
        prev_yaw, prev_ms = previous
        elapsed_ms = now_ms - prev_ms
        # 끊긴 구간을 가로질러 재지 않는다 — 텔레메트리가 10Hz 이므로 1초를 넘는
        # 간격은 공백이고, 그 사이의 회전은 이 두 표본으로 복원되지 않는다.
        if not 0 < elapsed_ms <= 1000:
            return
        delta_deg = (yaw - prev_yaw + 180.0) % 360.0 - 180.0
        self._summary.observe("yaw_rate_deg_s", delta_deg * 1000.0 / elapsed_ms)

    def _watch_command_uptake(self, age_ms: int | None, now_ms: int) -> None:
        """⚠️ **로봇이 우리 명령을 폐기하고 있는지 본다.**

        10Hz 로 보내는데 로봇이 보고하는 *마지막 수락 명령의 나이*가 명령 타임아웃을
        계속 넘으면, 패킷은 나가는데 로봇이 받아들이지 않는 것이다. 호스트 재시작으로
        seq 가 되돌아간 경우가 대표적이며 **조용하다** — 우리 통계에는 송신 성공으로
        잡히고 로봇만 폐기 카운터를 올린다. 그래서 여기서 큰 소리를 낸다.
        """
        if age_ms is None or self._session_open is not None:
            return
        if age_ms <= self._cmd_timeout_ms:
            self._ignored_since_ms = None
            return
        if self._ignored_since_ms is None:
            self._ignored_since_ms = now_ms
            return
        if now_ms - self._ignored_since_ms >= self._link_ignored_warn_ms:
            self._ignored_since_ms = now_ms
            LOG.warning(
                "commands_ignored",
                last_cmd_age_ms=age_ms,
                hint="세션 개시 실패 가능 (PROTOCOL 4절)",
            )

    def _log_transition(self, previous: str) -> None:
        """**최우선 로깅 지점** — 전 전이 + 트리거 (ENGINEERING_GUIDE 1.4).

        페일세이프 진입은 기능 상실이므로 `ERROR` 다 (레벨 정책 1.2). 상태 이름을
        여기 박지 않고 `ERROR_ON_ENTER` 표를 본다.
        """
        after = self._behavior.state
        self._log.observe(state=after)
        trigger = self._behavior.last_trigger
        if self._recorder is not None:
            self._recorder.record(
                "fsm",
                previous=previous,
                state=after,
                trigger=trigger.name if trigger is not None else None,
            )
        emit = LOG.error if after in ERROR_ON_ENTER else LOG.info
        emit(
            "fsm_transition",
            **{
                "from": previous,
                "to": after,
                "trigger": trigger.name if trigger is not None else None,
            },
        )

    def _emit_eye_led(self, now_ms: int) -> None:
        """단계 색을 눈 LED 로 내려보낸다 (FR-10.4).

        ⚠️ **색은 이미 계산돼 있었고 «보내는 곳» 만 없었다.** `escalation.presentation()`
        이 단계별 색과 점멸을 내는데, 그것이 **로그에만** 쓰이면 실기에서 눈이
        펌웨어 기본값(파랑)에 머문다 — 눈 LED 를 내려보내는 경로가 이것이다.

        ⚠️ **바뀌면 즉시, 그대로면 1초마다 보낸다.** UDP 한 건이 유실되거나 첫 전문이
        peer 학습 전에 소진돼도 복구해야 한다. 펌웨어는 같은 색이면 I2C 에 다시 쓰지 않는다.

        ⚠️ **페일세이프 흰색을 여기서 만들지 않는다.** 래치 중에는 온보드가 흰색으로
        덮는다. 링크가 끊겨 `F` 로 갔다면 호스트는 애초에 색을 보낼 수 없다.
        """
        seen = self._escalation.presentation()
        # 규약은 `blink_hz` 를 필수·음수 불가로 두고 **0 을 상시점등**으로 읽는다.
        # `None` 을 그대로 실으면 전문이 거부된다.
        blink = 0.0 if seen.blink_hz is None else float(seen.blink_hz)
        changed = self._edge.changed("eye_led", (seen.led, blink))
        if (
            not changed
            and self._last_eye_led_ms is not None
            and now_ms - self._last_eye_led_ms < EYE_LED_REFRESH_MS
        ):
            return
        self._commander.once("LED", color=seen.led, blink_hz=blink)
        self._last_eye_led_ms = now_ms

    def _announce_escalation(self, now_ms: int) -> None:
        """단계가 바뀐 **그 순간**을 사건으로 낸다 (FR-3.4).

        ⚠️ **상태가 아니라 단계에 건다.** `ALERT ⇄ TRACK` 왕복 체류가 0.2~0.6초로
        실측됐다. 상태 진입에 걸면 경고가 초당 몇 번씩 겹쳐 나간다.
        단계는 그 왕복에 영향받지 않으므로 엣지가 그대로 중복 억제가 된다.

        ⚠️ **블랙박스 사건에 얹지 않는다.** 그 사건은 사진을 저장할 때만 나가는데
        (`_record_scene` 네 곳), 미인증 10초로 조용히 L2 가 되는 경우에는 그 넷 중
        아무 일도 일어나지 않는다. 얹어 두면 **경고가 늦거나 아예 안 나간다** —
        음성 쪽은 한참 뒤 엉뚱한 사건에 얹혀 온 값을 보고서야 알아챈다.

        ⚠️ **읽을 문장을 여기서 실어 보낸다.** 단계와 문구가 한곳에 있어야 단계를
        고칠 때 문구가 남지 않는다. 음성 쪽은 받은 문장을 읽기만 한다.
        """
        if self._dashboard is None:
            return
        level = self._escalation.level.value
        # ⚠️ **래치 여부도 엣지다.** 보호구 경고(L3·래치 아님) 중에 쓰러짐이 확정되면 단계는
        # L3 그대로라, 단계만 보면 경보 문장이 나가지 않는다 — 음성은 이 사건의 문장만 읽는다.
        if not self._edge.changed("escalation_level", (level, self._escalation.latched)):
            return
        self._dashboard.record_event(
            {
                "event": "escalation_changed",
                "ts_ms": now_ms,
                "state": self._behavior.state,
                "escalation": level,
                "mode": self._mission.mode,
                "warning": self._escalation.presentation().warning,
                "reason": self._escalation.reason,
            }
        )

    def _announce_transition(self, event: Event, previous: str, now_ms: int) -> None:
        """인증·안전 전이를 관제 사건으로 낸다. 전이 로그(jsonl)에만 있던 것들이다."""
        if self._dashboard is None:
            return
        after = self._behavior.state
        name = (
            "failsafe_entered"
            if after == "FAILSAFE" and previous != "FAILSAFE"
            else FEED_TRANSITIONS.get(event)
        )
        if name is not None:
            self._feed_event(name, now_ms, previous=previous, trigger=event.name)

    def _feed_event(self, name: str, now_ms: int, **extra: Any) -> None:
        cast("DashboardState", self._dashboard).record_event(
            {
                "event": name,
                "ts_ms": now_ms,
                "state": self._behavior.state,
                "escalation": self._escalation.level.value,
                "mode": self._mission.mode,
                **extra,
            }
        )

    def _rearm_person_gate(self, previous: str, _target: str = "") -> None:
        """순찰을 **시작할 때** 사람 게이트를 재장전한다 (FR-3.2)."""
        self._track_controller.rearm()
        # 확인하지 않은 구역 경보를 들고 나온 순찰이다 — 다음 `ALERT` 는 사람 때문일 수 있다.
        self._zone_inspector.forget_alarm()
        if previous in STANDBY:
            self._edge.forget("person")
            # 대기(래치 해제·모드 전환)를 건너 계속 누운 사람도 다시 사건이 되게 한다.
            # FAILSAFE 중 확정은 L3 가 F 에 밀려 엣지만 소비된다.
            self._edge.changed("fallen", False)

    def _watch_track_blocked(self, reading: Any, now_ms: int) -> None:
        """추종 중에 온보드가 전진을 거부한 구간을 기록한다 (설계 규칙 ④ · FR-2.2).

        ⚠️ **판단은 아무것도 바꾸지 않는다 — 기록만 붙인다.** `AVOID` 는 `PATROL`
        에서만 열리는 것이 의도이고(추종 중에 회피를 돌리면 카메라가 대상을 놓쳐
        회피가 임무를 취소한다), 그래서 추종 중 막힘에는 **대응할 상태가 없다.**
        문제는 대응이 없는 것이 아니라 **일어난 줄도 몰랐다**는 것이다.

        실제로 일어나는 일 — 온보드가 **전진만** 거부하고 호 조향은 전진이
        있어야 돌므로 로봇은 **선 채로** 멈춘다. 호스트는 `TRACK` 을 유지하며 지시를
        계속 보내고, 대상이 계속 보이니 `TARGET_LOST` 도 돌지 않아 **그 자리에
        머문다.** 안전한 정지이지만 «왜 멈췄는지» 가 로그에 없다.

        ⚠️ **실기에서 운용자가 이것을 눈으로 봤다** — 순찰 중 사람을 발견한
        로봇이 **코앞(온보드 정지 거리)까지 와서** 섰다. 원인은 `FR-3.5.2`(거리 유지)가
        **미구현**이라 편차가 데드존 밖이면 거리와 무관하게 최대 보폭으로 전진하고,
        멈추는 것이 초음파 반사뿐이기 때문이다. **Tier 1 안전 반사가 거리 제어를
        대신하고 있다.** 이 로그가 그 빈도와 거리를 남겨 `FR-3.5.2` 의 목표값을 정할
        근거가 된다.

        ⚠️ **변화 때만 낸다.** 막힌 동안 10Hz 로 같은 줄을 쏟으면 그 로그가 런을 덮는다.
        """
        blocked = bool(reading.obstacle) and self._behavior.tracking
        if not self._edge.changed("track_blocked", blocked):
            return
        if blocked:
            self._blocked_since_ms = now_ms
            LOG.warning(
                "track_blocked",
                state=self._behavior.state,
                dist_cm=reading.dist_cm,
            )
            return
        since = self._blocked_since_ms
        self._blocked_since_ms = None
        LOG.info(
            "track_unblocked",
            state=self._behavior.state,
            blocked_ms=None if since is None else now_ms - since,
        )

    def _watch_sound(self, lines: list[str], now_ms: int) -> None:
        """이번 틱에 나간 `SOUND` 를 ACK 대기에 올린다. 새 것이 옛 것을 밀어낸다."""
        for line in lines:
            msg = json.loads(line)
            if msg["type"] == "SOUND":
                due = now_ms + SOUND_ACK_TIMEOUT_MS
                self._sound_wait = (msg["seq"], msg["track"], due, self._sound_retries_left)
                self._sound_retries_left = SOUND_RETRIES

    def _resend_sound(self, now_ms: int) -> None:
        """ACK 없이 마감이 지난 `SOUND` 를 **새 seq 로** 다시 싣는다.

        `SOUND` 는 한 번만 나가서 UDP 한 개가 빠지면 문장이 소리 없이 사라진다 —
        실기에서 명령의 약 1.2% 가 빠졌고 대체 문장 하나가 그렇게 나오지
        않았다. 같은 seq 는 펌웨어 순서 게이트가 거부하므로 `once` 로 다시 만든다.
        ⚠️ ACK 만 빠진 경우엔 같은 문장이 처음부터 다시 나온다 — 무음보다 낫다.
        """
        if self._sound_wait is None or now_ms < self._sound_wait[2]:
            return
        _seq, track, _due, left = self._sound_wait
        self._sound_wait = None
        # 더 새 문장이 이미 실려 있으면 옛 것은 버린다 — 뒤에 붙이면 새 문장을 덮는다.
        # 확인과 넣기는 한 덩어리다: 대시보드 스레드가 그 사이에 끼면 옛 것이 뒤에 붙었다.
        if left <= 0:
            if not self._commander.has_pending("SOUND"):
                LOG.warning("sound_unacked", track=track, retries=SOUND_RETRIES)
            return
        if self._commander.once_unless_pending("SOUND", track=track):
            self._sound_retries_left = left - 1

    def tick(self, now_ms: int) -> list[str]:
        """한 주기. 보낼 전문 목록을 돌려준다 (보내지는 않는다)."""
        # ⚠️ **이 함수의 소요가 명령 주기를 결정한다.** 운용 루프는 단일 스레드이고
        # (`serve`) 송신이 이 뒤에 붙으므로, 여기서 쓴 시간이 그대로 로봇이 느끼는
        # 명령 간격이 된다. 온보드 워치독은 600ms 라 여유가 여섯 주기다.
        tick_started = time.perf_counter()
        self._ppe_judge.note_time(now_ms)
        self._drain_confirmations(now_ms)
        # 측위를 비전보다 앞에 둔다 — 구역 점검(`_poll_vision`)이 이번 틱의 자세로 도착을 본다.
        self._observe_scan(now_ms)
        # 막힘 원인 판독을 쓰러짐·구역 판독보다 먼저 줍는다 — 같은 틱에 그쪽이 워커를 쓴다.
        self._path_cause.poll(now_ms)
        # (잠정) 임무 밖(대기·수동)에서는 래치되지 않은 단계를 내린다 (`fsm.STANDBY`).
        if self._behavior.standby:
            self._escalation.stand_down(now_ms)
        phase_started = time.perf_counter()
        self._poll_vision(now_ms)
        vision_ms = (time.perf_counter() - phase_started) * 1000
        # 단계의 시간 조건 — L1 해제(5초)와 L2 승격(10초). **전이와 무관하게 돈다.**
        #
        # ⚠️ **판단보다 앞에 둔다.** 뒤에 두면 이번 틱의 전문이 이전 단계에서 만들어져,
        # 눈 LED·음향이 한 주기씩 밀린다.
        self._escalation.tick(now_ms, require_auth=self._mission.enables("auth"))
        # 단계 색을 로봇에 내려보낸다 — 위 호출 바로 뒤가 제자리다.
        self._emit_eye_led(now_ms)
        self._auth_judge.request(now_ms)
        self._resend_sound(now_ms)
        before = self._behavior.state
        phase_started = time.perf_counter()
        lines = self._behavior.tick(now_ms)
        self._watch_sound(lines, now_ms)
        if self._navigator is not None:
            self._nav_snapshot = self._build_nav_snapshot(now_ms)
        behavior_ms = (time.perf_counter() - phase_started) * 1000
        if self._behavior.state != before:
            self._stats.transitions += 1
            # 감시자·상태 타이머가 만든 사건은 `_apply` 를 지나지 않는다 — 30초
            # 무응답이 내는 `AUTH_FAILED` 가 그렇다. 여기서 단계 축에 넣지 않으면
            # **경보로 올라갈 유일한 실제 경로가 빠진다.**
            trigger = self._behavior.last_trigger
            if trigger is not None:
                self._escalation.note_event(trigger.name, now_ms)
            self._log_transition(before)
        if lines:
            self._stats.ticks += 1
            self._stats.sent += len(lines)
            self._stats.note_state(self._behavior.state)
            self._summary.count("sent", len(lines))
            # 전문이 나온 회전만 센다 — 그것이 로봇이 체감하는 주기다.
            #
            # ⚠️ **초당 요약에도 싣는다.** `_intervals.digest()` 는 `runtime_stopped`
            # 에서만 나가므로 운용 중에는 간격이 벌어지는 것을 볼 수 없다. 그래서
            # 실기에서 로봇이 600ms 워치독으로 계속 래치하는데 호스트
            # 로그로는 원인을 못 찾고 USB 시리얼을 물려야 했다.
            gap_ms = self._intervals.note(now_ms)
            if gap_ms is not None:
                self._summary.maximum("cmd_gap_ms", gap_ms)
                if gap_ms > self._cmd_timeout_ms:
                    self._summary.count("cmd_gap_over_timeout")
        # 주기 요약 — fps·지연·카운터를 1초에 한 줄로 (1.3)
        digest = self._summary.drain(now_ms)
        if digest:
            LOG.info("telemetry_summary", **digest)
        phase_started = time.perf_counter()
        self._announce_escalation(now_ms)
        if self._dashboard is not None:
            self._dashboard.publish(
                telemetry=self._last_telemetry if self._last_telemetry["available"] else None,
                state=self._behavior.state,
                escalation=self._escalation.level.value,
                mode=self._mission.mode,
                received_at=self._telemetry_received_at,
            )
        dashboard_ms = (time.perf_counter() - phase_started) * 1000
        # ⚠️ **최댓값을 함께 낸다 — 평균은 꼬리를 숨긴다.** 위 `digest` 는 이 줄보다
        # 앞에서 비워지므로 여기 적은 값은 다음 요약에 실린다(한 틱 지연).
        total_ms = (time.perf_counter() - tick_started) * 1000
        self._summary.observe("tick_ms", total_ms)
        self._summary.maximum("tick_ms", total_ms)
        self._summary.maximum("tick_vision_ms", vision_ms)
        self._summary.maximum("tick_behavior_ms", behavior_ms)
        self._summary.maximum("tick_dashboard_ms", dashboard_ms)
        return lines

    def _apply(self, event: Event, now_ms: int) -> bool:
        """사건을 넣고 **전이했으면 반드시 남긴다.**

        ⚠️ 이 경로를 우회하면 로그의 `state` 가 실제와 어긋난다. 처음에는
        `start_patrol()` 만 직접 `behavior.event()` 를 불렀는데, 그 결과 15초 실행에서
        **앞 6초의 모든 레코드가 `IDLE` 로 찍혔다** — 로봇은 순찰 중이었다.
        로그가 거짓을 말하면 로그가 없는 것보다 나쁘다.
        """
        self._ppe_judge.note_time(now_ms)
        if self._motion_lock and event is Event.START_PATROL:
            # 보행 잠금 중에는 순찰을 시작하지 않는다 — MOVE 는 어차피 막히지만, FSM 이
            # PATROL 로 가면 화면·기록이 «걷는 중» 이라고 거짓말한다.
            if self._edge.changed("motion_lock_patrol", True):
                LOG.warning("motion_lock_refused_patrol")
            return False
        # ⚠️ **모드 게이트는 FSM 보다 앞이다** (FR-11.1). 뒤에 두면 전이는
        # 막아도 에스컬레이션이 올라가, 경비 모드에서 PPE 위반이 L3 경보를 만든다.
        # 여기가 사건이 지나는 유일한 지점이라 **한 곳에서 한 번만** 거른다.
        if not self._mission.allows(event.name):
            # ⚠️ `event=` 로 적으면 안 된다 — 로거의 위치 전용 인자와 부딪혀
            # `TypeError` 가 나고, 그 죽음은 이 게이트를 처음 타는 운용 중에 나온다.
            # 모드는 레코드 컨텍스트에 이미 실린다 (FR-11.5).
            LOG.debug("mission_gated", gated=event.name)
            return False
        before = self._behavior.state
        accepted = self._behavior.event(event, now_ms=now_ms)
        # ⚠️ **받아들여졌는지를 함께 넘긴다.** 올리는 것은 전이 여부와 무관하지만
        # (`ALERT` 의 PPE 위반) 내리는 것은 그렇지 않다 — 로봇 래치가 걸려 있으면
        # `RESET_CONFIRMED` 가 거부되고, 그때 F 를 풀면 호스트만 풀린다.
        self._escalation.note_event(event.name, now_ms, accepted=accepted)
        if not accepted:
            return False
        self._stats.transitions += 1
        # ⚠️ **`AUTH_WAIT` 에 들어올 때마다 시도를 0 으로 되돌린다.** 이 자리가
        # 사건이 지나는 유일한 지점이라 어느 경로로 들어왔든 한 번만 초기화된다.
        # 초기화하지 않으면 앞선 대기에서 쌓인 실패가 다음 사람에게 넘어가,
        # 처음 말하는 사람이 한 마디에 소진된다.
        if before != "AUTH_WAIT" and self._behavior.state == "AUTH_WAIT":
            self._voice_auth.open(now_ms)
            if self._voice_auth.require_both:
                self._auth_judge.reset()
        elif before == "AUTH_WAIT" and self._behavior.state != "AUTH_WAIT":
            self._voice_auth.close()
        self._log_transition(before)
        self._announce_transition(event, before, now_ms)
        return True

    def start_patrol(self, now_ms: int) -> bool:
        return self._apply(Event.START_PATROL, now_ms)

    #: 명령이 무시되는 상태가 이만큼 이어지면 경고한다. 한 번 튄 것으로 떠들지 않는다.
    _link_ignored_warn_ms = 1000

    def request_reset(self) -> None:
        """사람이 원인 해소를 확인했다 — `RESET_SAFE` 를 예약한다 (대시보드 진입점).

        **여기서 `RESET_CONFIRMED` 를 바로 넣지 않는다.** 패킷 수락과 실제 안전
        해제는 다르므로, 로봇이 `safety_latched=false` 를 보고할 때까지 기다린다
        (PROTOCOL 2절). 먼저 넣으면 로봇은 잠긴 채 호스트만 풀린다.

        **틱을 기다려 보낸다.** 틱을 앞지르는 것은 `ESTOP` 하나뿐이다 — 해제는
        급하지 않고, 급하게 만들 이유도 없다 (DR-16).
        """
        self._reset_pending = True
        self._commander.halt()
        self._commander.once("RESET_SAFE")
        LOG.info("reset_requested", waiting_for="safety_latched=false")

    def confirm_alarm(self, now_ms: int) -> bool:
        """사람이 **경보(L3)를 확인했다** — 유일한 L3 해제 경로다 (아키텍처 3.1).

        `request_reset()` 과 나눠 둔 이유가 있다. 저쪽은 *물리 상태*(넘어졌나·배터리·
        링크)를 확인하고 로봇의 래치가 풀릴 때까지 기다리는 일이고, 이쪽은 *상황
        판단*(침입자가 갔나·안전모·물건)이라 로봇에 보낼 것이 없다. 하나로 묶으면
        **비상정지를 눌렀다 푸는 것으로 경보를 지우는 길**이 생긴다.
        """
        released = self._escalation.confirm_alarm(now_ms)
        if not released:
            LOG.info("alarm_confirm_ignored", level=self._escalation.level.value)
        elif self._zone_inspector.alarm_alert:
            # 구역 변화의 `ALERT` 는 사람이 보이지 않으면 스스로 나갈 길이 없다 (FR-8.4).
            self._apply(Event.ZONE_ALARM_CONFIRMED, now_ms)
        elif self._fall.confirmed:
            # 확정한 쓰러짐도 확인하면 순찰로 돌아간다 (S4) — 누운 사람은 스스로 떠나지 않는다.
            self._fall.resolve(now_ms)
        return released

    def ask_alarm_confirm(self) -> None:
        """**다른 스레드에서 부른다** — 콘솔 키·대시보드 버튼의 진입점이다.

        여기서 곧바로 풀지 않는 이유는 스레드다. 운용 루프가 전문을 만드는 중간에
        단계가 바뀌면 그 틱의 명령이 어느 단계의 것인지 말할 수 없게 된다.
        """
        self._alarm_confirm_asked = True

    def ask_reset(self) -> None:
        """페일세이프(F) 해제 요청을 예약한다. **다른 스레드에서 부른다.**"""
        self._reset_asked = True

    def ask_goto(self, x: float, y: float) -> tuple[bool, str]:
        """지도에서 찍은 곳(순찰 좌표)을 예약한다. **다른 스레드에서 부른다.**"""
        navigator = self._navigator
        if navigator is None or not hasattr(navigator, "goto"):
            return False, "LiDAR 측위 순찰이 아니라 지도 이동을 할 수 없다"
        with self._locate_lock:
            self._goto_asked = (float(x), float(y))
            self._goal_feedback = None
        return True, "찍은 곳으로 갈 수 있는지 확인한다 — 결과는 지도에 표시된다"

    def nav_status(self) -> dict[str, Any]:
        """관제 지도가 그릴 자기 위치·신뢰·목표. **다른 스레드에서 부른다.**

        루프가 틱마다 만든 한 시점의 묶음을 돌려준다 — 서버 스레드가 컨트롤러 필드를 하나씩 읽으면
        옛 좌표에 새 신뢰·구역·경로가 섞인다 (Codex 검토 G P2).
        """
        snapshot = self._nav_snapshot
        if snapshot is None:
            return {"available": self._navigator is not None, "starting": True}
        return {**snapshot, "goal_feedback": self._goal_feedback}

    def _build_nav_snapshot(self, now_ms: int) -> dict[str, Any]:
        """루프 스레드에서 한 시점의 항법 상태를 묶는다."""
        navigator = self._navigator
        if navigator is None:
            return {"available": False}
        x, y, yaw = navigator.pose
        goal = getattr(navigator, "goal", None)
        hint = getattr(navigator, "_zone_hint", None)
        return {
            "available": True,
            "frame": "patrol",
            "pose": [round(x, 3), round(y, 3), round(rad_to_deg(yaw), 1)],
            "stale": bool(navigator.pose_stale(now_ms)),
            "verified": bool(getattr(navigator, "pose_verified", False)),
            "seeded": bool(getattr(navigator, "pose_seeded", False)),
            "phase": str(getattr(navigator, "phase", "")),
            "target": getattr(navigator, "target", None),
            "zone": getattr(navigator, "current_zone", None),
            "goal": None if goal is None else [round(goal[0], 3), round(goal[1], 3)],
            "holding_goal": bool(getattr(navigator, "holding_goal", False)),
            "goal_hold_reason": getattr(navigator, "goal_hold_reason", None),
            "zone_hint": None if hint is None else hint[0],
            "match_frac": round(float(getattr(navigator, "match_frac", 0.0)), 3),
            "path": [
                [round(px, 3), round(py, 3)]
                for px, py in getattr(getattr(navigator, "plan", None), "waypoints", ())
            ],
            "goal_feedback": self._goal_feedback,
            "fsm": self._behavior.state,
        }

    def ask_locate_zone(self, zone: str) -> tuple[bool, str]:
        """사람이 알려준 «지금 이 구역» 을 예약한다. **다른 스레드에서 부른다.**

        길 찾기·측위 상태는 루프 스레드만 바꾼다 — 적용은 다음 틱(`_drain_confirmations`).
        """
        navigator = self._navigator
        if navigator is None or not hasattr(navigator, "hint_zone"):
            return False, "LiDAR 측위 순찰이 아니라 위치를 알려줄 대상이 없다"
        known = navigator.locate_zone_ids()
        if zone not in known:
            return (
                False,
                f"구역 {zone!r} 이 없다 — 알려줄 수 있는 구역: {', '.join(known) or '없음'}",
            )
        with self._locate_lock:
            self._locate_asked = zone
        return True, f"구역 {zone} 안에서 위치를 다시 찾는다 — 찾을 때까지 로봇은 선다"

    def apply_external(self, event: Event) -> bool:
        """대시보드 명령이 FSM 사건을 넣는 진입점. **다른 스레드에서 부른다.**

        ⚠️ **`behavior.event()` 를 직접 부르는 대신 이것을 쓴다.** 직접 부르면 전이는
        일어나지만 `_apply()` 가 묶어 둔 **대응 단계 갱신과 전이 로그가 함께 빠진다.**
        실기에서 그렇게 드러났다 — E-Stop 이 로봇을 잠갔는데 단계가 `L0`
        (파랑) 에 머물러 **눈 LED 가 흰색으로 바뀌지 않았고**(FR-10.4), `MANUAL` 26.7초의
        전이도 로그에 한 줄도 남지 않았다. 같은 기록에서 온보드가 스스로 잠근 쪽은
        `F` 까지 정상으로 올라갔다 — **차이는 잠긴 이유가 아니라 어느 경로로 들어왔는가**였다.

        ⚠️ **예약이 아니라 즉시 적용이다.** `ask_reset()`·`ask_alarm_confirm()` 은 틱을
        기다리지만 이쪽은 그럴 수 없다 — `ESTOP` 은 틱 하나도 기다리면 안 되고
        (`estop()` 이 전문을 받은 자리에서 보내는 것과 같은 이유), `MANUAL_ON` 은
        결과를 그 자리에서 화면에 돌려줘야 한다.
        """
        return self._apply(event, self._clock())

    def note_voice_listening(self, captured_at_ms: int | None = None) -> tuple[bool, str]:
        """판정 대기 유예 (`VoiceAuthWindow.note_listening`). 대시보드 스레드가 부른다."""
        return self._voice_auth.note_listening(captured_at_ms)

    def note_voice_auth(self, ok: bool, captured_at_ms: int | None = None) -> tuple[bool, str]:
        """음성 암구호 판정 (`VoiceAuthWindow.note_verdict`). 대시보드 스레드가 부른다."""
        return self._voice_auth.note_verdict(ok, captured_at_ms)

    def ask_patrol(self) -> None:
        """순찰을 예약한다. **리셋이 정착한 뒤 `IDLE` 에서 시작한다.**

        ⚠️ **`start_patrol()` 을 바로 부르면 리셋과 순서가 어긋난다.** 실기에서
        이렇게 나타났다 — 기동 때 해제를 요청하고 곧바로 순찰을 시작하면, 로봇이
        `safety_latched=true` 를 보고해 호스트가 `FAILSAFE` 로 따라간 뒤 해제가
        정착하면서 `IDLE` 로 내려온다. 즉 **`--reset-on-start` 와 `--patrol` 을
        함께 주면 순찰이 조용히 취소된다.** 로그만 보면 순찰을 시작했다고 적혀
        있어서 더 나쁘다.

        그래서 의도를 세워 두고 **전이표가 허락할 때** 발행한다 — `START_PATROL`
        은 `IDLE` 에서만 전이를 만들므로 상태 이름이 여기 들어오지 않는다.
        """
        self._patrol_asked = True
        self._patrol_asked_ms = self._clock()

    def _drain_confirmations(self, now_ms: int) -> None:
        """사람이 누른 확인을 **판단보다 먼저** 처리한다.

        먼저 처리해야 이번 틱의 명령이 새 단계에 맞는다. 나중에 보면 한 주기 동안
        해제된 단계의 명령이 나간다 — 링크 감시를 지시 적용보다 앞에 둔 것과 같은
        이유다.
        """
        if self._alarm_confirm_asked:
            self._alarm_confirm_asked = False
            self.confirm_alarm(now_ms)
        if self._reset_asked:
            self._reset_asked = False
            self.request_reset()
        with self._locate_lock:
            zone, self._locate_asked = self._locate_asked, None
        if zone is not None and self._navigator is not None:
            self._navigator.hint_zone(zone, now_ms)
        with self._locate_lock:
            goto, self._goto_asked = self._goto_asked, None
        navigator = self._navigator
        if self._goal_cancel_asked:
            self._goal_cancel_asked = False
            if navigator is not None and hasattr(navigator, "cancel_goal"):
                navigator.cancel_goal("patrol_stopped")
            # 정지를 누르기 전에 들어온 이동 요청·그것이 건 순찰 시작도 버린다.
            if goto is not None or self._goal_feedback is not None:
                goto = None
                self._patrol_asked = False
        if goto is not None and navigator is not None:
            accepted, detail = navigator.goto(*goto)
            self._goal_feedback = {
                "accepted": accepted,
                "detail": detail,
                "goal": [round(goto[0], 3), round(goto[1], 3)],
                "at_ms": now_ms,
            }
            LOG.info("goto_requested", accepted=accepted, detail=detail)
            if accepted and self._behavior.state != "PATROL":
                # 순찰 중이 아니면 순찰을 시작해야 길 찾기가 돈다 — 같은 예약 경로(리셋 정착 대기 포함).
                self.ask_patrol()
        if (
            self._patrol_asked
            and navigator is not None
            and getattr(navigator, "holding_goal", False)
            and self._behavior.state == "PATROL"
        ):
            # 찍은 곳에 서 있다가 «순찰 시작» — 구역 순찰로 돌아간다.
            self._patrol_asked = False
            navigator.cancel_goal("patrol_restart")
        # ⚠️ **리셋을 기다린다.** 해제가 정착하기 전에 순찰을 시작하면 그 해제가
        # 순찰을 `IDLE` 로 되돌린다.
        if (
            self._patrol_asked
            and not self._reset_pending
            and self._behavior.fsm.can(Event.START_PATROL)
        ):
            # ⚠️ **VLM 적재가 끝날 때까지 보행을 미룬다.** 적재 스레드가 CPU·GIL 을
            # 잡아먹는 동안 걸으면 명령 공백이 로봇의 통신 감시를 넘긴다
            # (2026-10-02 실기: `cmd_gap 585ms` → `ONBOARD_FAILSAFE`).
            # 상한을 둔다 — 적재가 붙잡혀 있으면 대기가 끝이 없다.
            waited_ms = 0 if self._patrol_asked_ms is None else now_ms - self._patrol_asked_ms
            if self._vlm.loading and waited_ms < VLM_LOAD_WAIT_MS:
                if self._edge.changed("vlm_load_wait", True):
                    LOG.info("patrol_deferred_vlm_loading")
            else:
                if self._vlm.loading:
                    LOG.warning("patrol_starting_during_vlm_load", waited_ms=waited_ms)
                self._patrol_asked = False
                self.start_patrol(now_ms)

    def _settle_reset(self, latched: bool | None, now_ms: int) -> None:
        """로봇이 래치를 풀었다고 보고하면 그때 `RESET_CONFIRMED` 를 넣는다."""
        if not self._reset_pending or latched is not False:
            return
        self._reset_pending = False
        if self._apply(Event.RESET_CONFIRMED, now_ms):
            # FAILSAFE 중 거절된 자세 복귀를 래치 해제 직후 다시 보낸다.
            self._commander.once(
                "POSE",
                pitch=0.0,
                roll=self._roll_offset_deg,
                height=0.0,
                dur=self._ppe_judge.settle_ms,
            )

    def emergency_stop(self) -> str:
        """종료 전문. **틱을 기다리지 않는다.**

        정상 종료에도 보내는 이유 — 호스트가 사라진 뒤 로봇이 마지막 이동 명령을
        계속 실행하면 안 된다. 온보드 600ms 타임아웃이 어차피 잡지만 그것은
        *프로세스가 강제로 죽은 경우*를 위한 뒷받침이고, 스스로 끝낼 때는
        명시적으로 세우는 것이 맞다.
        """
        return self._commander.emergency_stop()

    def serve(
        self,
        sock: socket.socket,
        *,
        duration_s: float | None = None,
        clock: Callable[[], int] = system_clock_ms,
    ) -> Stats:
        """운용 루프. **이 함수만 소켓과 실시간을 만진다.**

        기다리는 시간을 송신기의 다음 마감에서 가져온다. 루프가 자기 마감을 따로
        세면 시계가 둘이 되고, 그때부터 어느 쪽이 진짜인지 알 수 없다.
        """
        self.begin(sock)
        started = clock()
        end_ms = started + int(duration_s * 1000) if duration_s is not None else None
        try:
            while end_ms is None or clock() < end_ms:
                now = clock()
                due = self.next_due_ms
                wait_ms = 0 if due is None else max(0, due - now)
                if end_ms is not None:
                    wait_ms = min(wait_ms, max(0, end_ms - now))
                sock.settimeout(wait_ms / 1000)
                try:
                    data, addr = sock.recvfrom(RECV_BYTES)
                except (TimeoutError, BlockingIOError):
                    # ⚠️ 둘 다 "지금은 받을 것이 없다" 다. `settimeout(0)` 은 소켓을
                    # **비차단으로 바꾸므로** TimeoutError 가 아니라 BlockingIOError
                    # (WinError 10035) 가 온다. 대기 시간이 0 인 것은 첫 틱에서
                    # 정상이므로 오류가 아니다.
                    pass
                except ConnectionResetError:
                    pass  # Windows ICMP — 위 open_socket 설명 참고
                except OSError as exc:
                    if not _is_oversized_datagram(exc):
                        raise
                    # 프로토콜 최대 크기를 넘는 한 건 때문에 운용 루프 전체가
                    # 끝나서는 안 된다. 다른 소켓 오류는 고장을 숨기지 않고 올린다.
                    self._stats.discarded += 1
                    LOG.warning(
                        "telemetry_datagram_too_large",
                        max_bytes=RECV_BYTES,
                        winerror=WSAEMSGSIZE,
                    )
                else:
                    self.receive(data, addr, clock())
                self.step(clock())
        finally:
            self._shutdown(sock)
        return self._stats

    # ── 운용 루프의 단계 — `serve` 와 여러 대를 한 소켓으로 돌리는 `host.fleet` 이 같이 쓴다 ──
    def begin(self, sock: socket.socket) -> None:
        """루프 전 준비. 비전 워커를 켜고 세션 개시 전문을 만든다."""
        # 세션 생성·워밍업은 운용 루프 전에 끝낸다. 루프 안에서 처음 열면 DirectML
        # 초기화가 600ms 명령 타임아웃을 넘겨 로봇을 멈출 수 있다.
        if self._vision is not None:
            self._vision.start()
        # VLM 을 모드와 상관없이 한 번 올린다 — ADR-35 결정 5 (상시 적재).
        # 모드를 바꿀 때 올리고 내리면 판독 시작과 해제가 겹쳐 세션을 닫거나 VRAM 이
        # 남았다. 경비 모드에 올라가 있어도 구역 점검이 없으니 판독은 생기지 않는다.
        # ⚠️ **생성자가 아니라 여기다.** 시험은 `Runtime` 을 수백 번 만들고, 거기서
        # 걸면 만들 때마다 적재가 돈다. `release()` 가 짝으로 내린다.
        # ⚠️ **`--no-vision` 이면 올리지 않는다.** 적재는 스레드에서 돌아도 4.1GB
        # 체크포인트를 읽는 동안 CPU·GIL 을 잡아먹어 제어 루프의 명령이 끊기고,
        # 실기에서 `cmd_gap 585ms` → `ONBOARD_FAILSAFE` 로 이어졌다 (2026-10-02).
        # 시험 주입(`vlm_reader`)은 가짜 세션이라 그대로 올린다.
        if self._vision is not None or self._vlm_injected:
            self._vlm.start()
        self._sock = sock
        # ⚠️ **루프보다 먼저 인코딩한다.** 이것이 프로세스의 seq=1 이어야 로봇이
        # 세션 개시로 인정한다. 틱이 한 번이라도 앞서면 seq 1 을 다른 명령이 쓴다.
        self._session_open = self._commander.open_session()
        LOG.info(
            "runtime_started",
            listen_port=self._telemetry_port,
            peer=_peer_text(self._peer),
            period_ms=self._commander.period_ms,
            actions=self._actions,
        )

    @property
    def next_due_ms(self) -> int | None:
        return self._commander.next_due_ms

    def owns(self, device_id: str) -> bool:
        """이 텔레메트리 개체 ID 가 우리 로봇인가 (설정 이름 또는 `telemetry_device_id`)."""
        return device_id in self._own_ids

    def receive(self, data: bytes, addr: tuple[str, int], now_ms: int) -> None:
        """받은 datagram 하나. 상대 주소를 모르면 첫 수락 텔레메트리에서 배운다."""
        out = self.ingest(data, now_ms)
        if self._recorder is not None:
            self._recorder.record_raw(
                "telemetry",
                data,
                at_ms=now_ms,
                src=f"{addr[0]}:{addr[1]}",
                accepted=out.reading is not None,
                discarded=out.discarded,
            )
        if self._peer is None and out.reading is not None:
            self._peer = (addr[0], self._cmd_port)
            LOG.info("peer_learned", peer=_peer_text(self._peer))

    def step(self, now_ms: int) -> None:
        """틱 하나 — 마감이 된 전문만 나간다 (마감은 송신기가 센다)."""
        with self._send_lock:
            self._send(cast("socket.socket", self._sock), self.tick(now_ms))
        if self._odom is not None:
            self._odom.publish(now_ms)

    def send_emergency_stop(self) -> str:
        """관제 ESTOP — 인코딩과 즉시 송신을 틱과 같은 락 안에서 한다.

        상대를 아직 모르면 보내지 않고 FSM 만 `halt` 로 내려간다(`send_immediate` 와 같다).
        세션 개시 전문이 아직 안 나갔으면 그것을 먼저 보낸다 — 첫 datagram 이어야 한다.
        """
        with self._send_lock:
            line = self._commander.emergency_stop()
            lines = [line] if self._session_open is None else [self._session_open, line]
            if self._sock is not None and self._peer is not None:
                self._session_open = None
                self._transmit(self._sock, self._peer, lines)
        return line

    def send_immediate(self, line: str) -> None:
        """다음 틱을 기다리지 않고 전문 한 줄을 즉시 보낸다.

        대시보드 `CommandService` 가 쓴다 — ESTOP 은 100ms 틱 하나도 기다리면
        안 된다. 상대를 아직 모르면(텔레메트리 0건) 조용히 버린다 — 보낼 곳이
        없는데 세우는 것보다, FSM 은 어차피 `halt` 로 내려간다.
        """
        sock, peer = self._sock, self._peer
        if sock is None or peer is None:
            return
        self._transmit(sock, peer, [line])

    def _transmit(self, sock: socket.socket, peer: tuple[str, int], lines: list[str]) -> list[str]:
        """로봇으로 나가는 **유일한** 길(틱·관제·즉시·ESTOP). 보행 잠금을 적용하고 기록한다."""
        if self._motion_lock:
            blocked = [line for line in lines if _command_type(line) not in MOTION_LOCK_TYPES]
            if blocked:
                lines = [line for line in lines if _command_type(line) in MOTION_LOCK_TYPES]
                if self._recorder is not None:
                    self._recorder.record("command_blocked", lines=blocked)
                if self._edge.changed("motion_lock_blocked", True):
                    LOG.warning(
                        "motion_lock_blocked", types=sorted({_command_type(b) for b in blocked})
                    )
        sent = send(sock, peer, lines)
        if self._recorder is not None and sent:
            self._recorder.record("command_sent", lines=sent, peer=_peer_text(peer))
        self._note_odom_sent(sent)
        if self._navigator is not None and sent:
            # ⚠️ 순찰기에도 **실제로 나간** 명령을 알린다 — 없으면 «정지 중» 판정(`_is_stationary`)이
            # AVOID·FAILSAFE 때 찍힌 정지 시각에 머물러, 걷는 중에도 감사가 돌고 표 독립성도
            # 못 본다(2026-10-03 Codex 검토: runtime 이 navigator.note_sent 를 부르지 않았다).
            self._navigator.note_sent(sent, self._clock())
        return sent

    def _note_odom_sent(self, sent: list[str]) -> None:
        """**실제로 나간** 명령만 오도메트리에 알린다. 송신 경로(틱·관제·즉시)가 모두 부른다."""
        if self._odom is not None:
            self._odom.note_sent(sent, self._clock())

    def _send(self, sock: socket.socket, lines: list[str]) -> None:
        if self._peer is None:
            return
        if self._session_open is not None:
            # 상대를 늦게 알았어도 **이 전문이 첫 datagram 이어야** 한다.
            lines = [self._session_open, *lines]
            self._session_open = None
        if not lines:
            return
        self._transmit(sock, self._peer, lines)

    def _shutdown(self, sock: socket.socket) -> None:
        # ⚠️ **`ESTOP` 을 먼저 보낸다.** 워커 정리를 기다리다 늦으면, 로봇을 멈추는
        # 신호가 스레드 조인 뒤로 밀린다. 순서가 안전을 결정한다.
        self.stop_robot(sock)
        self.release()
        # 이번 세션에 실측 스캔으로 자란 지도를 저장한다 — 다음 세션이 더 나은
        # 지도에서 시작하게. 처음 덮어쓸 때 원본은 `slam_map.orig.*` 로 남는다.
        navigator = self._navigator
        if isinstance(navigator, PatrolController) and navigator.maps_dir is not None:
            try:
                written = navigator.save_map()
            except OSError:
                LOG.warning("patrol_map_save_failed", directory=str(navigator.maps_dir))
            else:
                if written:
                    LOG.info("patrol_map_saved", directory=str(navigator.maps_dir))
                else:
                    LOG.info(
                        "patrol_map_not_saved",
                        reason="자세가 전역 확인된 적 없음 — 이 세션의 적분은 보류됐다",
                    )

    def stop_robot(self, sock: socket.socket) -> None:
        """종료 ESTOP 을 여러 번 보낸다. 여러 대면 **전부 먼저 세운 뒤** `release` 한다."""
        with self._send_lock:
            self._stop_robot_locked(sock)

    def _stop_robot_locked(self, sock: socket.socket) -> None:
        line = self.emergency_stop()
        if self._peer is not None:
            payload = line.encode("utf-8")
            # UDP 한 건의 유실로 600ms 온보드 타임아웃까지 마지막 동작이 남는 것을
            # 줄인다. 같은 seq의 중복은 멱등이고, 첫 건이 도착하면 나머지는 무시된다.
            if self._recorder is not None:
                self._recorder.record("command_sent", lines=[line], repeats=SHUTDOWN_ESTOP_REPEATS)
            for attempt in range(SHUTDOWN_ESTOP_REPEATS):
                with contextlib.suppress(OSError):
                    sock.sendto(payload, self._peer)
                if attempt + 1 < SHUTDOWN_ESTOP_REPEATS:
                    time.sleep(SHUTDOWN_ESTOP_INTERVAL_S)

    def release(self) -> None:
        """비전 워커를 멈추고 종료 요약을 남긴다. ESTOP 은 `stop_robot` 이 이미 보냈다."""
        if self._vision is not None:
            self._vision.stop()
        # VLM 판독을 기다린 뒤 모델을 내린다 — 운행 중에는 내리지 않고 여기서만 내린다.
        # ⚠️ **상한까지만 기다린다** — 적재(14.5초) 도중이면 내리지 않고 나간다. 스레드는
        # 데몬이라 프로세스와 함께 끝난다. `serve` 의 `finally` 에서 불리므로 `submit()` 과
        # 같은 스레드다.
        self._vlm.stop()
        LOG.info(
            "runtime_stopped",
            ticks=self._stats.ticks,
            sent=self._stats.sent,
            accepted=self._stats.accepted,
            discarded=self._stats.discarded,
            foreign=self._stats.foreign,
            transitions=self._stats.transitions,
            states=self._stats.states,
            # 개수만으로는 루프가 밀렸는지 알 수 없다 — 간격 분포를 함께 남긴다.
            tick_interval=self._intervals.digest(),
        )


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


def _latest_jpeg(vision: VisionSource) -> Callable[[], bytes | None]:
    """대시보드 카메라 경로가 매번 호출하는 최신 프레임 공급자."""

    def grab() -> bytes | None:
        result = vision.latest()
        return result.jpeg if result is not None else None

    return grab


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


def dashboard_wiring(
    runtime: Runtime,
    config: Mapping[str, Any],
    *,
    vision: VisionSource | None,
    blackbox: EventBlackbox | None,
    broadcaster: broadcast.Broadcaster | None = None,
) -> dict[str, Any]:
    """관제 서버(`create_app`)에 넘길 명령·영상·사건 그림·정책 연결. 한 대·여러 대가 같이 쓴다."""
    from host.dashboard.commands import CommandService
    from host.dashboard.planning import PlanningService

    commands = CommandService(
        runtime.behavior,
        runtime.commander,
        runtime.send_immediate,
        emergency_stop=runtime.send_emergency_stop,
        request_reset=runtime.ask_reset,
        apply_event=runtime.apply_external,
        ask_patrol=runtime.ask_patrol,
        set_mode=runtime.set_mode,
        note_voice_auth=runtime.note_voice_auth,
        note_voice_listening=runtime.note_voice_listening,
        confirm_alarm=runtime.ask_alarm_confirm,
        locate_zone=runtime.ask_locate_zone,
        goto_point=getattr(runtime, "ask_goto", None),
        pose=(
            float(config["posture"]["pitch_up_deg"]),
            int(config["posture"]["settle_ms"]),
            float(config["posture"].get("roll_offset_deg", 0.0)),
        ),
    )
    return {
        "commands": commands,
        "camera": _latest_jpeg(vision) if vision is not None else None,
        # 박스와 그 박스를 계산한 JPEG 를 함께 보낸다.
        "vision": vision.latest if vision is not None else None,
        # 사건 전문에는 디렉터리 이름만 실으므로 그림은 여기서
        # 꺼낸다. 이름 검증은 저장 구조를 아는 블랙박스가 한다.
        "event_snapshot": None if blackbox is None else blackbox.snapshot_bytes,
        "policy": policy_view(config),
        # PC 스피커 방송 음량·무음 조절. 없으면(piper 없음 등) None —
        # 화면은 "방송 없음" 을 보여 준다.
        "broadcast": broadcaster,
        # 실제 집 지도·자기 위치 (LiDAR 측위 순찰일 때만).
        "map_view": _map_view(runtime),
        "nav_status": runtime.nav_status if _has_navigator(runtime) else None,
        "planning": PlanningService(dict(config), runtime.context.device_id),
    }


def _has_navigator(runtime: Any) -> bool:
    return getattr(runtime, "_navigator", None) is not None and hasattr(runtime, "nav_status")


def _map_view(runtime: Any) -> Callable[[], tuple[bytes, dict[str, Any]]] | None:
    """항법 지도·구역을 관제 웹에 그릴 함수. 처음 부를 때 한 번 그린다."""
    if not _has_navigator(runtime):
        return None
    from host.dashboard.live_map import MapView, PoseFrame, render

    navigator = runtime._navigator
    view = MapView(
        lambda: render(
            navigator.grid,
            zones=navigator.zones,
            zone_map=navigator.zone_map,
            frame=PoseFrame.load(navigator.maps_dir),
            occ_thresh=navigator.plan_params.occ_thresh,
            free_thresh=navigator.plan_params.free_thresh,
        )
    )
    return view.get


def _publish_event(dashboard: DashboardState) -> Callable[[BlackboxEntry], None]:
    """블랙박스 기록 하나를 관제 화면이 읽을 형태로 바꿔 넘긴다.

    ⚠️ **JPEG 바이트를 보내지 않는다.** 사건 채널은 상태 전문과 같은 JSON 소켓이고,
    프레임 하나가 수십 KB 다 — 거기 실으면 사건 하나가 텔레메트리를 밀어낸다.
    그림은 블랙박스가 디스크에 갖고 있으므로 **가리키는 이름만** 보낸다.

    ⚠️ **절대 경로를 브라우저에 보내지 않는다.** 화면에 쓸 일이 없고 PC 의 폴더
    구조를 드러낸다. 기록 디렉터리 이름이면 되돌아 찾을 수 있다.
    """

    def publish(entry: BlackboxEntry) -> None:
        dashboard.record_event(
            {
                "event": entry.event_type,
                "ts_ms": entry.ts_ms,
                "state": entry.state,
                "escalation": entry.escalation,
                "mode": entry.mode,
                # 비주 대상도 함께 남긴다 (FR-3.8.4) — 주 대상만 보내면 옆에 있던
                # 사람이 기록에서 사라진다.
                "tracks": entry.tracks,
                "detections": entry.detections,
                "telemetry": entry.telemetry,
                "entry": entry.meta_path.parent.name,
                "snapshot": entry.jpeg_path.name if entry.jpeg_path is not None else None,
                # 그릴 수 없는 판단 근거 — PPE 판정·쓰러짐 수치·VLM 판독 (B2).
                "judgement": entry.judgement,
            }
        )

    return publish


def policy_view(config: Mapping[str, Any]) -> dict[str, Any]:
    """설정 화면의 대응 단계 표가 읽는 값 (B7). **읽는 키는 판정 코드와 같다.**

    화면에 숫자를 따로 적어 두면 config 를 고쳐도 화면이 옛 값을 말한다.
    """
    esc, auth, vision = config["escalation"], config["auth"], config["vision"]
    return {
        "detect_window_ms": int(vision["detect_window_ms"]),
        "detect_hits_required": int(vision["detect_hits_required"]),
        "l1_to_l2_hold_s": int(esc["l1_to_l2_hold_s"]),
        "target_lost_timeout_s": int(config["fsm"]["target_lost_timeout_s"]),
        "auth_timeout_s": int(auth["timeout_s"]),
        "auth_max_attempts": int(auth["max_attempts"]),
        "auth_session_valid_s": int(auth["session_valid_s"]),
        "auth_verdict_grace_s": auth.get("verdict_grace_s"),
        "auth_require_both": bool(auth.get("require_both", False)),
        "l3_warning": (esc.get("sound") or {}).get("l3_warning"),
        "led": {key: value for key, value in esc["led"].items() if key != "l3_blink_hz"},
        # «위치 알려주기» 버튼 목록 — 순찰 구역 id (방 이름이 아니라 구역 기호).
        "patrol_zones": [str(zone) for zone in config["zones"]["ids"]],
    }


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
    args: argparse.Namespace, config: Mapping[str, Any], argv: list[str] | None
) -> SessionRecorder:
    """기록 폴더와 manifest. 비밀값을 넣지 않는다 — 설정은 해시만, 인자는 그대로."""
    import hashlib
    import subprocess

    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = ""
    config_text = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return SessionRecorder(
        Path(args.record_dir),
        manifest={
            "argv": sys.argv[1:] if argv is None else argv,
            "device": args.device,
            "lidar_device": args.lidar_device,
            "maps": args.maps,
            "motion_lock": args.motion_lock,
            "git_revision": revision,
            "config_sha256": hashlib.sha256(config_text.encode("utf-8")).hexdigest(),
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

    recorder = None if args.record_dir is None else _open_recorder(args, config, argv)
    vision = None if args.no_vision else build_worker(config)
    blackbox = EventBlackbox(config)
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
        motion_lock=args.motion_lock,
        record_frame_ms=args.record_frame_ms,
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
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
