"""호스트 운용 런타임 (WBS 4.3.7).

지금까지 만든 네 조각을 **한 프로세스로 잇는다.**

    소켓 ─▶ 수신기 ─▶ 사건 ─▶ FSM ─▶ 지시 ─▶ 송신기 ─▶ 소켓
            (4.3.6)          (3.4)          (4.3.2)

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
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from host.behavior.actions import register_actions
from host.behavior.auth import Authenticator, Outcome
from host.behavior.change_detect import BaselineStore, ChangeConfirmer, classify_changes
from host.behavior.commander import Commander
from host.behavior.escalation import Escalation, Level
from host.behavior.fsm import STANDBY, Behavior, Event, behavior_from_config
from host.behavior.mission import Mission
from host.behavior.tracker import LockOnTracker
from host.common.blackbox import BlackboxEntry, EventBlackbox
from host.common.config import ConfigError, load_config, telemetry_ids
from host.common.logging_setup import (
    ERROR_ON_ENTER,
    EdgeTrigger,
    LogContext,
    PeriodicSummary,
    event_logger,
    setup_logging,
)
from host.common.protocol import CommandEncoder, system_clock_ms
from host.dashboard.state import DashboardState
from host.telemetry.receiver import Ingested, TelemetryReceiver
from host.vision.worker import TickIntervals, VisionWorker, build_worker

LOG = event_logger("mechadog.runtime")

#: 한 번에 받아들이는 최대 바이트. 텔레메트리 한 줄은 300 바이트를 넘지 않는다.
RECV_BYTES = 2048
WSAEMSGSIZE = 10040
SHUTDOWN_ESTOP_REPEATS = 3
SHUTDOWN_ESTOP_INTERVAL_S = 0.02


def _is_oversized_datagram(exc: OSError) -> bool:
    """Windows가 수신 버퍼보다 큰 UDP 전문에 붙이는 오류만 가려낸다."""
    return getattr(exc, "winerror", None) == WSAEMSGSIZE or exc.errno == WSAEMSGSIZE


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
        vision: Any = None,
        blackbox: EventBlackbox | None = None,
        event_publisher: Callable[[BlackboxEntry], None] | None = None,
        dashboard: DashboardState | None = None,
        mission: Mission | None = None,
    ) -> None:
        network = config["network"]
        rate_hz = int(network["cmd_rate_hz"])
        if rate_hz <= 0:
            raise ConfigError("network.cmd_rate_hz 는 1 이상이어야 함")
        self._device_id = device_id
        self._dashboard = dashboard
        self._clock = clock
        # 운용 모드 (3.4.4). **FSM·에스컬레이션과 직교하는 세 번째 축**이며 표는
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
        # 상태별 모션을 붙인다 (3.5.1). 만들 수 없는 것은 등록하지 않고 이유를 남기며,
        # 등록되지 않은 상태는 `Behavior` 가 정지로 처리한다 — 안전측 기본값이다.
        self._actions = register_actions(self._behavior, config)
        # ⚠️ `network` 절 안에 있다. 최상위에서 찾으면 프로파일에 주소를 적어도
        # 못 읽고, 첫 텔레메트리가 올 때까지 아무것도 보내지 않는 상태가 된다.
        host = robot_ip or network.get("mechdog_ip")
        self._peer: tuple[str, int] | None = (host, self._cmd_port) if host else None
        # 운용 루프의 송신 소켓 — 대시보드 명령(ESTOP 등)이 다음 틱을 기다리지
        # 않게 즉시 보내는 경로가 쓴다. `serve` 가 시작할 때 채워진다.
        self._sock: socket.socket | None = None
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
        # 추종(`3.5.4`)은 **검출이 들어오는 자리에서** 계산한다 — 비전은 25fps 로
        # 오고 명령은 10Hz 로 나가므로, 명령 쪽에서 계산하면 프레임을 버리게 된다.
        # 계산 결과는 `TRACK` 시퀀스에 넘기고 그쪽이 명령 주기로 옮긴다.
        self._tracker = LockOnTracker(config)
        self._track_sequence = self._behavior.sequence_for("TRACK")
        # 구역 변화 감지 (FR-8 · `3.6.x`). 구역 "식별" 은 ArUco 마커로 하며 측위와
        # 무관하다 — 바코드를 읽는 것과 같다(config `zones` 주석). 그래서 Phase 1
        # 에서도 쓸 수 있다.
        self._baselines = BaselineStore(config)
        self._confirmer = ChangeConfirmer(config)
        zone_markers = (config.get("zones") or {}).get("marker_map") or {}
        self._zone_markers = {int(key): str(value) for key, value in zone_markers.items()}
        self._watch_classes = tuple(config["vision"]["coco"]["change_watch_classes"])
        self._zone: str | None = None
        #: 이번 점검에서 관찰한 프레임 수. **시간이 아니라 사이클을 센다** —
        #: `ChangeConfirmer` 가 사이클 단위이므로 같은 축으로 세야 어긋나지 않는다.
        self._zone_cycles = 0
        # 저장소와 대시보드 송신자를 주입한다. 정식 CLI는 저장소를 항상 연결하고,
        # FastAPI/WebSocket 서버(4.5.1)가 생기면 같은 항목을 publisher로 받는다.
        # 둘을 분리해야 디스크 기록 성공과 브라우저 연결 여부가 서로 발목을 잡지 않는다.
        self._blackbox = blackbox
        self._event_publisher = event_publisher
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
        self._blocked_since_ms: int | None = None
        # ⚠️ **순찰에 들어갈 때 사람 게이트를 재장전한다.** `PERSON_FOUND` 는 상승
        # 엣지로만 나가므로, 순찰을 시작하는 순간 **이미 사람이 보이고 있으면 엣지가
        # 없어 로봇이 그 사람을 그냥 지나친다** — 2026-09-19 실기에서 그랬다. 게이트가
        # 02:07:16 에 켜진 뒤 02:07:18 에 순찰이 시작됐고, 검출이 초당 10~40건인데도
        # `ALERT` 로 한 번도 가지 않았다. 사람은 내내 보고 있었고 *"새로 나타났다"* 로
        # 쳐지지 않았을 뿐이다 (FR-3.2).
        #
        # `PERSON_FOUND` 를 받는 상태는 `PATROL` 하나뿐이라 여기만 재장전하면 된다.
        #
        # ⚠️ **임무 밖에서 들어올 때만이다** (`STANDBY` = 대기·수동). 모든 `PATROL`
        # 진입에서 재장전하면 **인증을 통과한 사람이 곧바로 다시 경보를 올린다** —
        # `AUTH_WAIT → PATROL` 복귀 틱에 그 사람이 아직 화면에 있기 때문이다.
        # 임무 중의 복귀(`SCAN`·`AUTH_WAIT`)는 게이트가 이미 살아 있으므로 건드리지
        # 않는다. 낡은 것은 **순찰을 시작하는 순간의 게이트**뿐이다.
        self._behavior.fsm.on_enter("PATROL", self._rearm_person_gate)
        # 추종 **진입** 임계. 이탈은 조향 데드존이 정한다 — 둘을 갈라 두는 이유는
        # `_track` 주석과 `config.yaml` 의 `track_engage_px` 항목에 있다.
        self._track_engage_px = float(config["fsm"]["track_engage_px"])
        # 직전 IMU 방위와 그 시각. 각속도는 차분이라 표본 하나를 들고 있어야 한다.
        self._last_yaw: tuple[float, int] | None = None
        # 직전 추종 지시 시각. 공백 길이를 재는 데 쓴다 (`track_gap_ms`).
        self._last_track_ms: int | None = None
        # 틱 **간격**을 기록한다 — 개수만 세면 최악을 놓친다 (3.3.2 DoD).
        # 상한을 `cmd_timeout_ms` 로 잡는 이유: 그것을 넘으면 로봇이 스스로 멈춘다.
        self._intervals = TickIntervals(limit_ms=self._cmd_timeout_ms)
        # 대응 강도 축 (3.8.3). FSM 상태와 **직교한다** — 같은 `ALERT` 에서도 단계가
        # 다르면 눈 색깔과 음향이 다르다.
        self._escalation = Escalation(config)
        # ⚠️ **값을 넣지 않고 물어볼 대상을 넘긴다.** 단계는 사건·시간·확인 어느
        # 쪽으로도 바뀌므로 갱신 지점이 하나가 아니고, 복사해 두면 반드시 어긋난다.
        self._log.bind_escalation(lambda: self._escalation.level.value)
        # 같은 이유로 모드도 물어본다 (FR-11.5) — 관제 화면에서 바뀌므로 갱신 지점이
        # 하나가 아니다.
        self._log.bind_mode(lambda: self._mission.mode)
        # 사원증 인증 (3.8.1). **판정은 여기, 픽셀은 워커**다 — 마커 읽기는 프레임을
        # 쥔 쪽에서 하고 누구의 인증인지는 추적 결과를 함께 보는 여기서 정한다.
        self._auth = Authenticator(config)

    @property
    def behavior(self) -> Behavior:
        return self._behavior

    @property
    def auth(self) -> Authenticator:
        """인증 세션. 대시보드가 *"누가 인증됐나"* 를 보이는 데 쓴다 (FR-3.6.2)."""
        return self._auth

    @property
    def mission(self) -> Mission:
        """운용 모드 (FR-11). 대시보드가 표시·전환에 쓴다."""
        return self._mission

    def set_mode(self, target: str) -> str | None:
        """관제 화면에서 온 모드 전환. 성공이면 `None`, 거절이면 사유 (FR-11.3).

        ⚠️ **상태를 여기서 읽어 넘긴다.** `Mission` 이 `Behavior` 를 알면 모드 축을
        FSM 없이 단독으로 시험할 수 없다 — 두 축을 잇는 곳은 런타임 하나다.
        """
        return self._mission.switch(target, state=self._behavior.state)

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
    def vision(self) -> Any:
        """붙어 있는 추론 워커. 없으면 `None` (비전 없이도 운용된다)."""
        return self._vision

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
            # 동안 보간해 실제로 기울었는지는 IMU 로만 알 수 있다. 2026-09-18 실기에서
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
        **추론마다** 끝내 놓는다(`3.3.3` · ADR-25) — 이 틱(10Hz)에서 관측하면 25fps
        결과 중 10개만 보게 되고, 그러면 추론률을 올린 이유가 사라진다. 여기서는
        **이미 나온 판정을 읽어 사건으로 옮길 뿐**이다.
        """
        if self._vision is None:
            return
        result = self._vision.latest()
        fresh = result is not None and self._edge.changed("vision_seq", result.frame_seq)
        if fresh:
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
            if not self._behavior.standby:
                self._escalation.note_person(
                    present=result.sighting.present, last_seen_ms=seen_ms, now_ms=now_ms
                )
                self._judge_auth(result, now_ms)
        # ⚠️ **판정은 워커가 추론마다 했고, 여기서는 결과만 읽는다.** 게이트를 이 틱
        # (10Hz)에서 돌리면 25fps 결과 중 10개만 보게 되고 추론률을 올린 이유가 사라진다.
        #
        # ⚠️ **변화 여부는 게이트의 `changed` 가 아니라 우리 기준으로 본다.** 게이트는
        # 25fps 로 도니까 한 틱 사이에 확정→해제가 다 지나갈 수 있고, 그러면 그 순간의
        # `changed` 는 우리가 못 본 전이를 가리킨다.
        if fresh and self._edge.changed("person", result.sighting.present):
            LOG.info(
                "person_gate",
                present=result.sighting.present,
                hits=result.sighting.hits,
                score=round(result.sighting.best_score, 3),
            )
            # `present=False`는 게이트 확정이 풀렸다는 뜻일 뿐, 5초 대상 상실 사건이
            # 아니다. TARGET_LOST는 Behavior의 마지막 검출 타이머가 발생시킨다.
            if result.sighting.present:
                self._apply(Event.PERSON_FOUND, now_ms)
                self._record_person_event(result)
        if fresh:
            self._track(result, now_ms)
            self._inspect_zone(result, now_ms)
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

    def _track(self, result: Any, now_ms: int) -> None:
        """대표 박스의 x편차를 추종 지시로 바꿔 사건과 시퀀스에 넘긴다 (`3.5.4` · FR-3.5).

        ⚠️ **`ALERT`·`TRACK` 에서만 돈다.** 순찰 중에 사람이 스쳐도 여기서 각도를
        만들면 순찰 경로가 흔들린다 — 추종으로 들어갈지는 `PERSON_FOUND` 가 정하고
        전이표가 지킨다. 여기는 *"얼마나 돌지"* 만 맡는다.

        ⚠️ **박스가 없으면 아무 사건도 내지 않는다.** 대상이 사라진 판단은
        `FR-3.7` 의 5초 타이머 소관이고, 여기서 `TARGET_CENTERED` 를 내면
        **대상이 없어졌는데 중앙에 들어왔다고 보고하는 꼴**이 되어 `TRACK` 이
        `ALERT` 로 되돌아간다.

        ⚠️ **현장지원 모드에서는 돌지 않는다** (FR-11.1 · ADR-34 규칙 2). 사건만
        막으면 조향각은 매 프레임 계산돼 `TRACK` 시퀀스에 그대로 실린다 — 전이는
        없는데 로봇이 사람을 따라 도는, 가장 설명하기 어려운 모양이 된다.
        """
        if not self._mission.enables("track"):
            return
        if not self._behavior.tracking:
            # ⚠️ **추종 구간을 벗어나면 엣지 기억을 지운다.** 남겨 두면 다시 `ALERT` 로
            # 들어왔을 때 첫 판정이 *"변화 없음"* 으로 삼켜져 `TARGET_OFF_CENTER` 가
            # 나가지 않는다. 그러면 `3.5.3` 이 미착수라 시퀀스가 없는 `ALERT` 에
            # 정지한 채 갇힌다 — 2026-09-18 실기에서 편차 84px 대상을 앞에 두고
            # 33초를 서 있었다.
            self._edge.forget("track_centered")
            self._last_track_ms = None
            return
        box = result.sighting.box
        if box is None:
            return
        width = int(getattr(result, "frame_width", 0) or 0)
        if width <= 0:
            return
        center_x = (float(box[0]) + float(box[2])) / 2.0
        box_height = float(box[3]) - float(box[1])
        try:
            command = self._tracker.update(center_x, width, box_height)
        except ValueError as exc:
            # 데드존이 화면 반폭 이상이면 추종이 성립하지 않는다. 설정 오류이며
            # 이 프레임을 버리고 다음으로 간다 — 여기서 죽으면 순찰까지 멈춘다.
            if self._edge.changed("track_config_bad", True):
                LOG.error("track_unusable", reason=str(exc))
            return
        self._edge.changed("track_config_bad", False)
        # 실기 검증 근거 — 1초 요약(`telemetry_summary`)에 추종 지표를 같이 싣는다.
        # ⚠️ **절대값을 넣는다.** 부호를 그대로 평균 내면 좌우로 떠는 것이 0 으로
        # 상쇄돼 미세진동이 감춰진다 — DoD 가 확인하라는 바로 그것이다 (`3.5.4`).
        self._summary.observe("track_dev_px", abs(command.deviation_px))
        self._summary.observe("track_angle_deg", abs(command.angle))
        # ⚠️ **거리 유지(`FR-3.5.2`)의 목표값을 정하려면 이 숫자부터 있어야 한다.**
        # `fsm.track_target_height_px` 는 화각·장착 높이·사람 키가 섞여 계산으로
        # 세울 수 없다 — 목표 거리(약 1.0m)에 서서 여기 찍히는 값을 읽어 채운다.
        self._summary.observe("track_box_h_px", box_height)
        self._summary.observe("track_step_mm", command.step)
        # ⚠️ **공백의 «길이» 를 남긴다.** 요약은 개수만 세므로 지시가 몇 번 나갔는지는
        # 알아도 **얼마나 오래 비었는지**를 알 수 없었고, 그래서 `track_coast_ms` 를
        # 한 번의 관측(중앙값 956ms)으로 어림해야 했다. 이 값이 쌓이면 상한이 맞는지
        # 숫자로 판정된다 — 보행 흔들림이 만드는 공백은 기체·바닥마다 다르다.
        if self._last_track_ms is not None:
            self._summary.observe("track_gap_ms", float(now_ms - self._last_track_ms))
        self._last_track_ms = now_ms
        if not command.centered:
            self._summary.count("track_off_center")
        # ⚠️ **전이를 먼저, 지시는 그다음이다.** `TRACK` 진입 훅이 지난 추종의
        # 잔상을 지우므로(`TrackSequence.forget`), 순서를 뒤집으면 방금 넣은 지시가
        # 함께 지워져 **추종 첫 주기가 통째로 정지로 나간다.**
        #
        # 같은 판정이 이어지는 동안은 사건을 내지 않는다 — 25fps 로 같은 전이를
        # 수백 번 넣으면 로그가 전이로 뒤덮이고 단계 축도 흔들린다.
        #
        # ⚠️ **전이 임계는 조향 데드존보다 넓다 (히스테리시스).** `command.centered` 는
        # *"명령을 낼 것인가"* 를 40px 로 정하고 그대로 쓰지만, **`TRACK` 으로 들어가는
        # 판단만** `track_engage_px`(50px)로 늦춘다. 하나로 두면 경계에서 bbox 가
        # ±5px 떨리는 것이 그대로 왕복 전이가 된다 — 2026-09-18 실기에서 한 초에
        # 3왕복했다(`35.4 → 41.2 → 37.2 → 44.3px`). 나오는 쪽은 좁은 임계 그대로여야
        # 중앙에 든 대상을 늦게 놓지 않는다.
        centered = (
            command.centered
            if self._behavior.state == "TRACK"
            else abs(command.deviation_px) <= self._track_engage_px
        )
        if self._edge.changed("track_centered", centered):
            LOG.info(
                "track_command",
                centered=centered,
                deviation_px=round(command.deviation_px, 1),
                angle=round(command.angle, 2),
                step=round(command.step, 1),
            )
            self._apply(Event.TARGET_CENTERED if centered else Event.TARGET_OFF_CENTER, now_ms)
        if self._track_sequence is not None:
            self._track_sequence.note(command.step, command.angle, now_ms)

    def _zone_of(self, result: Any) -> str | None:
        """이번 프레임에 보이는 구역 마커. 없으면 `None`.

        ⚠️ **둘 이상 보이면 아무것도 고르지 않는다.** 구역 경계에 서면 두 장이
        같이 잡히는데, 아무 쪽이나 고르면 **엉뚱한 구역의 기준과 견주어** 물건이
        통째로 사라졌다고 보고한다. 한 장만 보일 때까지 기다린다.
        """
        seen = {
            self._zone_markers[marker.marker_id]
            for marker in result.markers
            if marker.marker_id in self._zone_markers
        }
        return next(iter(seen)) if len(seen) == 1 else None

    def _inspect_zone(self, result: Any, now_ms: int) -> None:
        """구역 마커를 보고 기준과 견준다 (WBS 3.6.x · FR-8).

        ⚠️ **픽셀을 보지 않는다** (FR-8.2 필수 제약). 비교는 `classify_changes` 가
        객체 목록으로만 하고 이미지는 기준 스냅샷 보관용으로만 쓴다.

        ⚠️ **사람 대응이 우선이다.** `ALERT`·`TRACK` 에서는 돌지 않는다 — 사람을
        보고 있는데 물건 목록을 견주면 대응이 한 박자 늦는다.

        ⚠️ **공장 모드에서만 돈다** (FR-11.1 · ADR-33 개정). 사건을 막는 것만으로는
        모자라다 — 기준이 없는 구역에서 이 함수는 **그 자리에서 기준을 등록**하고,
        그것은 사건이 아니라 디스크에 남는 상태다. 경비 순찰이 지나가며 남긴 기준을
        다음 공장 순찰이 정본으로 쓰게 된다.
        """
        if not self._mission.enables("change_detect"):
            return
        state = self._behavior.state
        if state == "PATROL":
            zone = self._zone_of(result)
            if zone is None:
                # ⚠️ **마커가 안 보이면 그 구역을 떠난 것이다.** 비우지 않으면 다음
                # 순회에 같은 구역을 다시 점검하지 못한다 — 순찰은 도는 것이므로
                # 같은 마커를 몇 번이고 다시 만난다.
                self._zone = None
                return
            if zone != self._zone and self._apply(Event.ZONE_ARRIVED, now_ms):
                self._zone = zone
                self._zone_cycles = 0
                # 지난 점검의 누적을 끌고 들어가지 않는다 — 다른 시점의 관찰이
                # 이번 사이클 수를 채우면 한 번 보고 확정하는 꼴이 된다.
                self._confirmer.forget(zone)
                LOG.info("zone_arrived", zone=zone)
            return
        if state != "ZONE_INSPECT" or self._zone is None:
            return

        zone = self._zone
        width = int(getattr(result, "frame_width", 0) or 0)
        height = int(getattr(result, "frame_height", 0) or 0)
        if width <= 0 or height <= 0:
            return
        self._zone_cycles += 1

        baseline = self._baselines.load(zone)
        if baseline is None:
            # FR-8.1 — 기준이 없으면 **이번 것이 기준이다.** 기준 없이 견주면
            # 처음 보는 물건이 전부 반입으로 잡혀 첫 순찰이 경보로 뒤덮인다.
            self._baselines.register(
                zone,
                result.detections,
                frame_size=(width, height),
                now_ms=now_ms,
                jpeg=result.jpeg,
            )
            LOG.info("zone_baseline_registered", zone=zone, objects=len(result.detections))
            self._leave_zone(now_ms)
            return

        changes = classify_changes(
            baseline,
            result.detections,
            frame_size=(width, height),
            watch_classes=self._watch_classes,
        )
        confirmed = self._confirmer.observe(zone, changes)
        if confirmed:
            LOG.warning(
                "zone_changed",
                zone=zone,
                changes=[change.as_dict() for change in confirmed],
            )
            # 전이가 에스컬레이션을 L3 로 올린다 (`escalation` 표 · FR-8.4).
            self._apply(Event.ZONE_CHANGED, now_ms)
            self._zone_cycles = 0
            return
        # ⚠️ **영원히 서 있지 않는다.** 확정에 필요한 사이클을 다 보고도 아무것도
        # 안 나오면 순찰로 돌아간다. 여기서 나가지 않으면 카메라가 흔들리는 동안
        # 로봇이 구역 앞에 멈춘 채로 남는다.
        if self._zone_cycles >= self._confirmer.confirm_cycles:
            LOG.info("zone_clear", zone=zone, cycles=self._zone_cycles)
            self._leave_zone(now_ms)

    def _leave_zone(self, now_ms: int) -> None:
        self._zone_cycles = 0
        self._apply(Event.ZONE_CLEAR, now_ms)

    def _judge_auth(self, result: Any, now_ms: int) -> None:
        """사원증을 판정하고 **사건으로 옮긴다** (FR-10.1 · `3.8.1`).

        ⚠️ **인증 표시는 사건이 아니라 상태에서 가져온다.** `AUTH_OK` 사건만 보면
        60초 유효 시간이 지난 것(FR-10.2.4)과 미인증자가 새로 합류한 것(FR-3.8.1)을
        놓친다 — 둘 다 사건이 없는 변화다. 그래서 매번 *"보이는 전원이 인증됐나"*
        를 물어 에스컬레이션에 반영한다.

        ⚠️ **경비 모드에서만 돈다** (FR-11.1). `_apply` 게이트만으로는 모자라다 —
        아래 `note_authenticated()` 는 사건이 아니라 **직접 호출**이라 그 게이트를
        지나지 않고, 공장·현장지원 모드에서 사원증이 스쳐도 단계를 건드린다.
        """
        if not self._mission.enables("auth"):
            return
        self._auth.note_tracks(result.tracks)
        outcome = self._auth.observe(result.markers, result.tracks, now_ms)
        if outcome in (Outcome.GRANTED, Outcome.BADGE_SEEN):
            self._apply(Event.AUTH_OK, now_ms)
        elif outcome is Outcome.EXHAUSTED:
            # 2회 실패 — 30초 무응답과 같은 결론이다 (FR-10.3).
            self._apply(Event.AUTH_FAILED, now_ms)
        if self._auth.all_authenticated(result.tracks, now_ms):
            self._escalation.note_authenticated(now_ms)
        else:
            self._escalation.note_authentication_lost()

    def _request_auth(self, now_ms: int) -> None:
        """L2 에 올랐으면 인증을 요구한다 (FR-10 · 아키텍처 3.1).

        ⚠️ **이것이 없으면 `AUTH_WAIT` 에 들어가지 못해 30초 타이머가 돌지 않는다.**
        그러면 `AUTH_FAILED` 를 낼 경로가 사라져 L2 가 출구 없이 남는다 —
        `3.8.3` 을 올릴 때 비워 둔 자리가 여기다.

        전이 가능 여부를 **표에 묻는다** — 그래야 상태 이름이 여기 들어오지 않고,
        이미 `AUTH_WAIT` 면 자동으로 한 번만 발행된다.
        """
        if self._escalation.level is not Level.L2:
            return
        if not self._behavior.fsm.can(Event.AUTH_REQUIRED):
            return
        self._apply(Event.AUTH_REQUIRED, now_ms)

    def _record_person_event(self, result: Any) -> None:
        """확정 검출의 원본과 판단 근거를 한 번 저장하고 이벤트 채널에 넘긴다.

        게이트의 거짓→참 엣지에서만 호출되므로 10Hz 반복 저장은 일어나지 않는다.
        저장·대시보드 오류가 제어 루프를 죽이면 로깅이 안전보다 우선하는 꼴이 되므로
        두 실패는 각각 기록하고 제어는 계속한다.
        """
        if self._blackbox is None:
            return
        try:
            entry = self._blackbox.record(
                "person_found",
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

    def _observe_yaw_rate(self, yaw: float | None, now_ms: int) -> None:
        """IMU 방위를 **각속도**로 바꿔 1초 요약에 싣는다 (`3.5.4` 좌우 대칭 근거).

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
        emit = LOG.error if after in ERROR_ON_ENTER else LOG.info
        emit(
            "fsm_transition",
            **{
                "from": previous,
                "to": after,
                "trigger": trigger.name if trigger is not None else None,
            },
        )

    def _rearm_person_gate(self, previous: str, _target: str = "") -> None:
        """순찰을 **시작할 때** 사람 게이트를 재장전한다 (FR-3.2)."""
        if previous in STANDBY:
            self._edge.forget("person")

    def _watch_track_blocked(self, reading: Any, now_ms: int) -> None:
        """추종 중에 온보드가 전진을 거부한 구간을 기록한다 (설계 규칙 ④ · FR-2.2).

        ⚠️ **판단은 아무것도 바꾸지 않는다 — 기록만 붙인다.** `AVOID` 는 `PATROL`
        에서만 열리는 것이 의도이고(추종 중에 회피를 돌리면 카메라가 대상을 놓쳐
        회피가 임무를 취소한다), 그래서 추종 중 막힘에는 **대응할 상태가 없다.**
        문제는 대응이 없는 것이 아니라 **일어난 줄도 몰랐다**는 것이다.

        실제로 일어나는 일 — 온보드가 **전진만** 거부하고(`3.2.6`) 호 조향은 전진이
        있어야 돌므로 로봇은 **선 채로** 멈춘다. 호스트는 `TRACK` 을 유지하며 지시를
        계속 보내고, 대상이 계속 보이니 `TARGET_LOST` 도 돌지 않아 **그 자리에
        머문다.** 안전한 정지이지만 «왜 멈췄는지» 가 로그에 없었다.

        ⚠️ **2026-09-19 실기에서 운용자가 이것을 눈으로 봤다** — 순찰 중 사람을 발견한
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

    def tick(self, now_ms: int) -> list[str]:
        """한 주기. 보낼 전문 목록을 돌려준다 (보내지는 않는다)."""
        self._drain_confirmations(now_ms)
        # (잠정) 임무 밖(대기·수동)에서는 래치되지 않은 단계를 내린다 (`fsm.STANDBY`).
        if self._behavior.standby:
            self._escalation.stand_down(now_ms)
        self._poll_vision(now_ms)
        # 단계의 시간 조건 — L1 해제(5초)와 L2 승격(10초). **전이와 무관하게 돈다.**
        #
        # ⚠️ **판단보다 앞에 둔다.** 뒤에 두면 이번 틱의 전문이 이전 단계에서 만들어져,
        # 눈 LED·음향을 단계에서 내려보내기 시작하면(`4.7.3`) 한 주기씩 밀린다.
        self._escalation.tick(now_ms)
        self._request_auth(now_ms)
        before = self._behavior.state
        lines = self._behavior.tick(now_ms)
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
            self._intervals.note(now_ms)
        # 주기 요약 — fps·지연·카운터를 1초에 한 줄로 (1.3)
        digest = self._summary.drain(now_ms)
        if digest:
            LOG.info("telemetry_summary", **digest)
        if self._dashboard is not None:
            self._dashboard.publish(
                telemetry=self._last_telemetry if self._last_telemetry["available"] else None,
                state=self._behavior.state,
                escalation=self._escalation.level.value,
                mode=self._mission.mode,
                received_at=self._telemetry_received_at,
            )
        return lines

    def _apply(self, event: Event, now_ms: int) -> bool:
        """사건을 넣고 **전이했으면 반드시 남긴다.**

        ⚠️ 이 경로를 우회하면 로그의 `state` 가 실제와 어긋난다. 처음에는
        `start_patrol()` 만 직접 `behavior.event()` 를 불렀는데, 그 결과 15초 실행에서
        **앞 6초의 모든 레코드가 `IDLE` 로 찍혔다** — 로봇은 순찰 중이었다.
        로그가 거짓을 말하면 로그가 없는 것보다 나쁘다.
        """
        # ⚠️ **모드 게이트는 FSM 보다 앞이다** (FR-11.1 · `3.4.4`). 뒤에 두면 전이는
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
        self._log_transition(before)
        return True

    def start_patrol(self, now_ms: int) -> bool:
        return self._apply(Event.START_PATROL, now_ms)

    #: 명령이 무시되는 상태가 이만큼 이어지면 경고한다. 한 번 튄 것으로 떠들지 않는다.
    _link_ignored_warn_ms = 1000

    def request_reset(self) -> None:
        """사람이 원인 해소를 확인했다 — `RESET_SAFE` 를 예약한다 (대시보드 4.5.3 진입점).

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

    def apply_external(self, event: Event) -> bool:
        """대시보드 명령이 FSM 사건을 넣는 진입점. **다른 스레드에서 부른다.**

        ⚠️ **`behavior.event()` 를 직접 부르는 대신 이것을 쓴다.** 직접 부르면 전이는
        일어나지만 `_apply()` 가 묶어 둔 **대응 단계 갱신과 전이 로그가 함께 빠진다.**
        2026-09-14 실기에서 그렇게 드러났다 — E-Stop 이 로봇을 잠갔는데 단계가 `L0`
        (파랑) 에 머물러 **눈 LED 가 흰색으로 바뀌지 않았고**(FR-10.4), `MANUAL` 26.7초의
        전이도 로그에 한 줄도 남지 않았다. 같은 기록에서 온보드가 스스로 잠근 쪽은
        `F` 까지 정상으로 올라갔다 — **차이는 잠긴 이유가 아니라 어느 경로로 들어왔는가**였다.

        ⚠️ **예약이 아니라 즉시 적용이다.** `ask_reset()`·`ask_alarm_confirm()` 은 틱을
        기다리지만 이쪽은 그럴 수 없다 — `ESTOP` 은 틱 하나도 기다리면 안 되고
        (`estop()` 이 전문을 받은 자리에서 보내는 것과 같은 이유), `MANUAL_ON` 은
        결과를 그 자리에서 화면에 돌려줘야 한다.
        """
        return self._apply(event, self._clock())

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
        # ⚠️ **리셋을 기다린다.** 해제가 정착하기 전에 순찰을 시작하면 그 해제가
        # 순찰을 `IDLE` 로 되돌린다.
        if (
            self._patrol_asked
            and not self._reset_pending
            and self._behavior.fsm.can(Event.START_PATROL)
        ):
            self._patrol_asked = False
            self.start_patrol(now_ms)

    def _settle_reset(self, latched: bool | None, now_ms: int) -> None:
        """로봇이 래치를 풀었다고 보고하면 그때 `RESET_CONFIRMED` 를 넣는다."""
        if not self._reset_pending or latched is not False:
            return
        self._reset_pending = False
        self._apply(Event.RESET_CONFIRMED, now_ms)

    def emergency_stop(self) -> str:
        """종료 전문. **틱을 기다리지 않는다.**

        정상 종료에도 보내는 이유 — 호스트가 사라진 뒤 로봇이 마지막 이동 명령을
        계속 실행하면 안 된다. 온보드 300ms 타임아웃이 어차피 잡지만 그것은
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
        # 세션 생성·워밍업은 운용 루프 전에 끝낸다. 루프 안에서 처음 열면 DirectML
        # 초기화가 300ms 명령 타임아웃을 넘겨 로봇을 멈출 수 있다.
        if self._vision is not None:
            self._vision.start()
        self._sock = sock
        started = clock()
        end_ms = started + int(duration_s * 1000) if duration_s is not None else None
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
        try:
            while end_ms is None or clock() < end_ms:
                now = clock()
                due = self._commander.next_due_ms
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
                    out = self.ingest(data, clock())
                    if self._peer is None and out.reading is not None:
                        self._peer = (addr[0], self._cmd_port)
                        LOG.info("peer_learned", peer=_peer_text(self._peer))
                self._send(sock, self.tick(clock()))
        finally:
            self._shutdown(sock)
        return self._stats

    def send_immediate(self, line: str) -> None:
        """다음 틱을 기다리지 않고 전문 한 줄을 즉시 보낸다.

        대시보드 `CommandService` 가 쓴다 — ESTOP 은 100ms 틱 하나도 기다리면
        안 된다. 상대를 아직 모르면(텔레메트리 0건) 조용히 버린다 — 보낼 곳이
        없는데 세우는 것보다, FSM 은 어차피 `halt` 로 내려간다.
        """
        sock, peer = self._sock, self._peer
        if sock is None or peer is None:
            return
        with contextlib.suppress(OSError):
            sock.sendto(line.encode("utf-8"), peer)

    def _send(self, sock: socket.socket, lines: list[str]) -> None:
        if self._peer is None:
            return
        if self._session_open is not None:
            # 상대를 늦게 알았어도 **이 전문이 첫 datagram 이어야** 한다.
            lines = [self._session_open, *lines]
            self._session_open = None
        if not lines:
            return
        for line in lines:
            with contextlib.suppress(OSError):
                sock.sendto(line.encode("utf-8"), self._peer)

    def _shutdown(self, sock: socket.socket) -> None:
        # ⚠️ **`ESTOP` 을 먼저 보낸다.** 워커 정리를 기다리다 늦으면, 로봇을 멈추는
        # 신호가 스레드 조인 뒤로 밀린다. 순서가 안전을 결정한다.
        line = self.emergency_stop()
        if self._peer is not None:
            payload = line.encode("utf-8")
            # UDP 한 건의 유실로 300ms 온보드 타임아웃까지 마지막 동작이 남는 것을
            # 줄인다. 같은 seq의 중복은 멱등이고, 첫 건이 도착하면 나머지는 무시된다.
            for attempt in range(SHUTDOWN_ESTOP_REPEATS):
                with contextlib.suppress(OSError):
                    sock.sendto(payload, self._peer)
                if attempt + 1 < SHUTDOWN_ESTOP_REPEATS:
                    time.sleep(SHUTDOWN_ESTOP_INTERVAL_S)
        if self._vision is not None:
            self._vision.stop()
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


def _latest_jpeg(vision: VisionWorker) -> Callable[[], bytes | None]:
    """대시보드 카메라 경로가 매번 호출하는 최신 프레임 공급자."""

    def grab() -> bytes | None:
        result = vision.latest()
        return result.jpeg if result is not None else None

    return grab


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m host.runtime",
        description="MechDog 호스트 운용 런타임 (WBS 4.3.7)",
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
        help="운용 모드 — guard | factory | assist (config 의 mission.mode 를 덮어쓴다)",
    )
    parser.add_argument("--patrol", action="store_true", help="기동 직후 순찰을 시작한다")
    parser.add_argument(
        "--reset-on-start",
        action="store_true",
        help="기동 시 사람이 원인 해소를 확인한 것으로 보고 RESET_SAFE 를 보낸다",
    )
    parser.add_argument(
        "--log-level", default=None, help="config.logging.level 을 덮어쓴다 (DEBUG 실행용)"
    )
    return parser


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
            }
        )

    return publish


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dashboard_port is not None and not 1 <= args.dashboard_port <= 65535:
        raise SystemExit("dashboard-port must be between 1 and 65535")
    # ⚠️ 설정을 읽기 전에는 우리 로거가 없다. 여기서만 표준 출력을 쓴다.
    try:
        config = load_config(args.device)
        # ⚠️ **모드는 여기서 정해진다** (FR-11.2 · `3.4.4`). 로거를 세우기 전에
        # 만드는 이유는 **모르는 모드나 선행 기능 없는 모드로는 기동 자체를 거부**
        # 하기 때문이다 (FR-11.7) — `ModeError` 가 `ConfigError` 라서 이 절이 잡는다.
        mission = Mission(config, mode=args.mode)
    except (ConfigError, OSError) as exc:
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

    vision = None if args.no_vision else build_worker(config)
    blackbox = EventBlackbox(config)
    dashboard = (
        DashboardState(args.device, stale_after_ms=int(config["safety"]["link_loss_failsafe_ms"]))
        if args.dashboard_port is not None
        else None
    )
    runtime = Runtime(
        config,
        device_id=args.device,
        robot_ip=args.robot_ip,
        context=context,
        vision=vision,
        blackbox=blackbox,
        dashboard=dashboard,
        mission=mission,
        # 사건을 관제 화면으로 밀어 준다 (WBS 4.4.3). ⚠️ **저장만으로는 완료가
        # 아니다** — 블랙박스는 디스크에 남기고 사람은 화면을 본다. 이 연결이
        # 없으면 기록은 쌓이는데 아무도 모른다. 실제로 그 상태였다.
        event_publisher=(None if dashboard is None else _publish_event(dashboard)),
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
            if dashboard is not None:
                from host.dashboard.commands import CommandService
                from host.dashboard.server import running_server

                commands = CommandService(
                    runtime.behavior,
                    runtime.commander,
                    runtime.send_immediate,
                    request_reset=runtime.ask_reset,
                    apply_event=runtime.apply_external,
                    ask_patrol=runtime.ask_patrol,
                    set_mode=runtime.set_mode,
                )
                camera = _latest_jpeg(vision) if vision is not None else None
                stack.enter_context(
                    running_server(
                        dashboard,
                        args.dashboard_port,
                        commands=commands,
                        camera=camera,
                        # 박스와 그 박스를 계산한 JPEG 를 함께 보낸다 (WBS 4.5.2).
                        vision=vision.latest if vision is not None else None,
                        # 사건 전문에는 디렉터리 이름만 실으므로(`4.4.3`) 그림은 여기서
                        # 꺼낸다. 이름 검증은 저장 구조를 아는 블랙박스가 한다.
                        event_snapshot=(None if blackbox is None else blackbox.snapshot_bytes),
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
