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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from host.behavior.commander import Commander
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
        host = robot_ip or config.get("mechdog_ip")
        self._peer: tuple[str, int] | None = (host, self._cmd_port) if host else None
        self._reset_pending = False
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

    @property
    def behavior(self) -> Behavior:
        return self._behavior

    @property
    def commander(self) -> Commander:
        return self._commander

    @property
    def stats(self) -> Stats:
        return self._stats

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
        before = self._behavior.state
        lines = self._behavior.tick(now_ms)
        if self._behavior.state != before:
            self._stats.transitions += 1
            self._log_transition(before)
        if lines:
            self._stats.ticks += 1
            self._stats.sent += len(lines)
            self._stats.note_state(self._behavior.state)
            self._summary.count("sent", len(lines))
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
        if not self._behavior.event(event, now_ms=now_ms):
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
        line = self.emergency_stop()
        if self._peer is not None:
            with contextlib.suppress(OSError):
                sock.sendto(line.encode("utf-8"), self._peer)
        LOG.info(
            "runtime_stopped",
            ticks=self._stats.ticks,
            sent=self._stats.sent,
            accepted=self._stats.accepted,
            discarded=self._stats.discarded,
            foreign=self._stats.foreign,
            transitions=self._stats.transitions,
            states=self._stats.states,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m host.runtime",
        description="MechDog 호스트 운용 런타임 (WBS 4.3.7)",
    )
    parser.add_argument("--device", required=True, help="개체 id — config/devices/<id>.yaml")
    parser.add_argument("--robot-ip", default=None, help="설정의 mechdog_ip 를 덮어쓴다")
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
    context = setup_logging(config, device_id=args.device)

    runtime = Runtime(config, device_id=args.device, robot_ip=args.robot_ip, context=context)
    sock = open_socket(runtime.telemetry_port)
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
