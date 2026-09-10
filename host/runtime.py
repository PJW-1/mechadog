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
import logging
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from host.behavior.actions import register_actions
from host.behavior.commander import Commander
from host.behavior.escalation import Escalation
from host.behavior.fsm import Behavior, Event, behavior_from_config
from host.common.config import ConfigError, load_config
from host.common.logging_setup import (
    ERROR_ON_ENTER,
    EdgeTrigger,
    LogContext,
    PeriodicSummary,
    event_logger,
    setup_logging,
)
from host.common.protocol import CommandEncoder, system_clock_ms
from host.telemetry.receiver import Ingested, TelemetryReceiver
from host.vision.worker import TickIntervals, build_worker

LOG = event_logger("mechadog.runtime")

#: 한 번에 받아들이는 최대 바이트. 텔레메트리 한 줄은 300 바이트를 넘지 않는다.
RECV_BYTES = 2048


def _peer_text(peer: tuple[str, int] | None) -> str:
    """로그에 실을 상대 표기. 튜플을 그대로 문자열로 만들면 읽기 어렵다."""
    return f"{peer[0]}:{peer[1]}" if peer else "학습 대기"


def open_socket(bind_port: int) -> socket.socket:
    """텔레메트리 수신 포트에 묶은 UDP 소켓. 송신도 이 소켓으로 한다.

    ⚠️ **Windows 전용 처리가 하나 있다.** 아직 아무도 듣지 않는 포트로 보내면 ICMP
    Port Unreachable 이 돌아오고 Windows 는 그것을 *다음 `recvfrom` 의*
    `ConnectionResetError` 로 돌려준다. UDP 에 연결이 없으므로 의미 없는 오류이며,
    로봇이 아직 안 켜진 것은 정상이다. 그래서 그 통보를 끈다.

    `hasattr` 로 감싸는 이유 — 이 상수는 파이썬 빌드에 따라 없다. 없는 채로 부르면
    도구가 시작하자마자 죽는다 (`tools/teleop.py` 에서 실제로 겪었다).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", bind_port))
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        with contextlib.suppress(OSError):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
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
    ) -> None:
        network = config["network"]
        rate_hz = int(network["cmd_rate_hz"])
        if rate_hz <= 0:
            raise ConfigError("network.cmd_rate_hz 는 1 이상이어야 함")
        self._device_id = device_id
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
        self._reset_pending = False
        # ⚠️ **`_reset_pending` 과 다른 것이다.** 저것은 *로봇이 래치를 풀었다고
        # 보고할 때까지 기다리는 중*이고, 이 둘은 *사람이 눌렀고 아직 틱이 처리하지
        # 않았다*는 뜻이다. 콘솔·대시보드는 운용 루프와 다른 스레드에 있으므로
        # 거기서 단계나 송신기를 직접 만지면 루프가 전문을 만드는 중간에 바뀐다.
        # 급한 것은 `ESTOP` 하나뿐이다 (DR-16).
        self._alarm_confirm_asked = False
        self._reset_asked = False
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
        # ⚠️ **정상값을 미리 심는다.** `EdgeTrigger` 는 첫 관측을 변화로 보므로, 심지
        # 않으면 기동할 때마다 `vision_worker_unhealthy` 와 `vision_recovered` 가
        # 거짓으로 찍힌다 — 실제로 그렇게 찍혔다. **거짓 경보는 진짜 경보를 묻는다.**
        self._edge.changed("vision_healthy", True)
        self._edge.changed("vision_stalled", False)
        self._edge.changed("person", False)  # 첫 관측이 변화로 잡히지 않게
        # 틱 **간격**을 기록한다 — 개수만 세면 최악을 놓친다 (3.3.2 DoD).
        # 상한을 `cmd_timeout_ms` 로 잡는 이유: 그것을 넘으면 로봇이 스스로 멈춘다.
        self._intervals = TickIntervals(limit_ms=self._cmd_timeout_ms)
        # 대응 강도 축 (3.8.3). FSM 상태와 **직교한다** — 같은 `ALERT` 에서도 단계가
        # 다르면 눈 색깔과 음향이 다르다.
        self._escalation = Escalation(config)
        # ⚠️ **값을 넣지 않고 물어볼 대상을 넘긴다.** 단계는 사건·시간·확인 어느
        # 쪽으로도 바뀌므로 갱신 지점이 하나가 아니고, 복사해 두면 반드시 어긋난다.
        self._log.bind_escalation(lambda: self._escalation.level.value)

    @property
    def behavior(self) -> Behavior:
        return self._behavior

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
            self._stats.discarded += 1
            self._summary.count("discarded")
            # ⚠️ 폐기는 **요약으로만** 남긴다. 30% 손상 구간에서는 초당 3줄이 되고,
            # 그때 알고 싶은 것은 개별 사유가 아니라 *"얼마나 깨지고 있나"* 다.
            if out.warns and self._edge.changed("discard_reason", out.discarded):
                LOG.warning("telemetry_discarded", reason=out.discarded)
            return out

        if out.reading.device_id != self._device_id:
            # ⚠️ 링크 시각을 갱신하지 않고 사건도 적용하지 않는다. 남의 패킷으로
            # "살아 있음" 을 세면 우리 로봇의 침묵이 가려진다.
            self._stats.foreign += 1
            if self._edge.changed("foreign", out.reading.device_id):
                LOG.warning(
                    "foreign_device",
                    expected=self._device_id,
                    received=out.reading.device_id,
                )
            return Ingested(discarded=f"다른 개체: {out.reading.device_id}")

        self._stats.accepted += 1
        self._log.observe(seq=out.reading.seq)
        self._summary.count("accepted")
        if out.reading.batt_v is not None:
            self._summary.observe("batt_v", out.reading.batt_v)
        if out.reading.last_cmd_age_ms is not None:
            self._summary.observe("last_cmd_age_ms", float(out.reading.last_cmd_age_ms))
        self._watch_command_uptake(out.reading.last_cmd_age_ms, now_ms)
        self._behavior.note_telemetry(now_ms)
        self._behavior.note_onboard_state(out.reading.state)
        self._behavior.note_robot_latch(out.reading.safety_latched)
        self._settle_reset(out.reading.safety_latched, now_ms)
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
            self._escalation.note_person(
                present=result.sighting.present, last_seen_ms=seen_ms, now_ms=now_ms
            )
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

    def tick(self, now_ms: int) -> list[str]:
        """한 주기. 보낼 전문 목록을 돌려준다 (보내지는 않는다)."""
        self._drain_confirmations(now_ms)
        self._poll_vision(now_ms)
        # 단계의 시간 조건 — L1 해제(5초)와 L2 승격(10초). **전이와 무관하게 돈다.**
        #
        # ⚠️ **판단보다 앞에 둔다.** 뒤에 두면 이번 틱의 전문이 이전 단계에서 만들어져,
        # 눈 LED·음향을 단계에서 내려보내기 시작하면(`4.7.3`) 한 주기씩 밀린다.
        self._escalation.tick(now_ms)
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
        return lines

    def _apply(self, event: Event, now_ms: int) -> bool:
        """사건을 넣고 **전이했으면 반드시 남긴다.**

        ⚠️ 이 경로를 우회하면 로그의 `state` 가 실제와 어긋난다. 처음에는
        `start_patrol()` 만 직접 `behavior.event()` 를 불렀는데, 그 결과 15초 실행에서
        **앞 6초의 모든 레코드가 `IDLE` 로 찍혔다** — 로봇은 순찰 중이었다.
        로그가 거짓을 말하면 로그가 없는 것보다 나쁘다.
        """
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
                else:
                    out = self.ingest(data, clock())
                    if self._peer is None and out.reading is not None:
                        self._peer = (addr[0], self._cmd_port)
                        LOG.info("peer_learned", peer=_peer_text(self._peer))
                self._send(sock, self.tick(clock()))
        finally:
            self._shutdown(sock)
        return self._stats

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
            with contextlib.suppress(OSError):
                sock.sendto(line.encode("utf-8"), self._peer)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # ⚠️ 설정을 읽기 전에는 우리 로거가 없다. 여기서만 표준 출력을 쓴다.
    try:
        config = load_config(args.device)
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
    runtime = Runtime(
        config,
        device_id=args.device,
        robot_ip=args.robot_ip,
        context=context,
        vision=vision,
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
            runtime.start_patrol(system_clock_ms())
        runtime.serve(sock, duration_s=args.duration)
    except KeyboardInterrupt:
        LOG.info("interrupted", action="ESTOP 송신 후 종료")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
