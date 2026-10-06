"""명령 API — 수동 오버라이드와 E-Stop (WBS 4.5.3 · FR-4.3/4.4).

관제 서버 스레드에서 불린다. 무엇이 즉시 나가고 무엇이 운용 루프의 다음 틱을 기다리는지,
무엇이 거절될 수 있는지가 이 모듈의 계약이다.

- ⚠️ E-Stop 은 어떤 상태에서도 조건 없이 통하고, 틱을 기다리지 않고 받은 자리에서 전문을
  보낸 뒤에 FSM 사건을 넣는다 — 로봇이 먼저 멈추고 호스트가 따라간다.
- 수동 오버라이드는 `FAILSAFE` 에서 거절된다(전이표). 거절은 `accepted=False` 로 돌려준다.
- `RESET_SAFE`·경보 확인·모드 전환 등은 예약하고 운용 루프의 다음 틱이 처리한다.
  사람 확인은 버튼 한 번이다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from host.behavior.commander import Commander
from host.behavior.fsm import Behavior, Event

__all__ = ["SOUND_TRACK_MAX", "CommandResult", "CommandService"]

#: `SOUND.track` 의 재생 범위 상한 (PROTOCOL `SOUND` 절 · 펌웨어와 같은 값). 0 은 정지다.
SOUND_TRACK_MAX = 3000


@dataclass(frozen=True, slots=True)
class CommandResult:
    """명령 하나의 결과. 거절도 결과이며 화면이 그대로 보여 준다."""

    command: str
    accepted: bool
    state: str
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "accepted": self.accepted,
            "state": self.state,
            "detail": self.detail,
        }


class CommandService:
    """화면의 조작을 FSM 사건과 전문으로 옮긴다.

    전송 수단을 모른다 — `send` 로 받은 호출 가능 객체에 완성된 전문을 넘길 뿐이다.
    """

    def __init__(
        self,
        behavior: Behavior,
        commander: Commander,
        send: Callable[[str], None],
        emergency_stop: Callable[[], str] | None = None,
        request_reset: Callable[[], None] | None = None,
        apply_event: Callable[[Event], bool] | None = None,
        ask_patrol: Callable[[], None] | None = None,
        set_mode: Callable[[str], str | None] | None = None,
        note_voice_auth: Callable[[bool, int | None], tuple[bool, str]] | None = None,
        note_voice_listening: Callable[[int | None], tuple[bool, str]] | None = None,
        confirm_alarm: Callable[[], None] | None = None,
        locate_zone: Callable[[str], tuple[bool, str]] | None = None,
        goto_point: Callable[[float, float], tuple[bool, str]] | None = None,
        pose: tuple[float, int] | tuple[float, int, float] | None = None,
        start_route: Callable[..., tuple[bool, str]] | None = None,
        stop_route: Callable[[], tuple[bool, str]] | None = None,
        locate_point: Callable[[float, float], tuple[bool, str]] | None = None,
    ) -> None:
        self._behavior = behavior
        self._commander = commander
        self._send = send
        # 런타임이 주면 ESTOP 의 인코딩과 송신을 틱과 같은 락 안에서 한 번에 한다
        # (`Runtime.send_emergency_stop`). 따로 하면 틱과 송신 순서가 뒤바뀐다.
        self._emergency_stop = emergency_stop
        self._request_reset = request_reset
        self._ask_patrol = ask_patrol
        # 운용 모드 전환 (`3.4.4`) — 가부는 FSM 상태에 달려 런타임(`set_mode`)이 판정한다.
        self._set_mode = set_mode
        # ⚠️ 사건은 `apply_event`(런타임 `_apply`)로 넣는다 — `behavior.event()` 를 직접 부르면
        # 대응 단계 갱신(F 의 흰 눈)과 전이 로그가 빠진다. 기본값은 런타임 없는 시험용이다.
        self._apply_event = apply_event if apply_event is not None else behavior.event
        # 음성 암구호 판정 — 시도 횟수는 `AUTH_WAIT` 진입 시각을 아는 런타임이 센다 (FR-10.3).
        # 런타임이 없는 시험에서는 사건만 넣는다.
        self._note_voice_auth = note_voice_auth
        # «판정이 오는 중» 신호 (ADR-37) — 시도를 세지 않고 창의 수명만 건드린다.
        self._note_voice_listening = note_voice_listening
        # 경보(L3) 확인 — `request_reset`(F 해제)과 다른 경로다 (ADR-26 ①).
        self._confirm_alarm = confirm_alarm
        self._locate_zone = locate_zone
        self._locate_point = locate_point
        self._goto_point = goto_point
        self._start_route = start_route
        self._stop_route = stop_route
        # 수동 자세 (B6). `(posture.pitch_up_deg, posture.settle_ms)` — PPE 자세 상승과 같은 검증된
        # 각도만 쓰고 임의 각도는 받지 않는다(검증하지 않은 자세로 보행하면 넘어진다).
        self._pose_pitch: dict[str, float] = (
            {} if pose is None else {"up": pose[0], "level": 0.0, "down": -pose[0]}
        )
        self._pose_dur_ms = 0 if pose is None else int(pose[1])
        self._pose_roll = 0.0 if pose is None or len(pose) < 3 else float(pose[2])
        self._pose_tilted = False

    @property
    def state(self) -> str:
        return self._behavior.state

    def estop(self) -> CommandResult:
        """⚠️ 어떤 상태에서도 조건 없이 통한다. 전문을 먼저 보내고 FSM 이 따라간다."""
        if self._emergency_stop is not None:
            self._emergency_stop()
        else:
            self._send(self._commander.emergency_stop())
        self._apply_event(Event.ESTOP)
        return CommandResult(
            command="estop",
            accepted=True,
            state=self._behavior.state,
            detail="비상정지 전문을 즉시 보냈다",
        )

    def manual_on(self) -> CommandResult:
        """수동 오버라이드 진입. `FAILSAFE` 에서는 거절된다."""
        before = self._behavior.state
        accepted = self._apply_event(Event.MANUAL_ON)
        if not accepted:
            return CommandResult(
                command="manual_on",
                accepted=False,
                state=self._behavior.state,
                detail=f"{before} 에서는 수동 오버라이드를 받지 않는다",
            )
        # 조작자가 조이스틱을 잡기 전까지는 멈춰 있게 한다.
        self._commander.halt()
        return CommandResult(command="manual_on", accepted=True, state=self._behavior.state)

    def manual_off(self) -> CommandResult:
        """수동 오버라이드 해제. `MANUAL` 이 아니면 거절된다."""
        before = self._behavior.state
        accepted = self._apply_event(Event.MANUAL_OFF)
        if not accepted:
            return CommandResult(
                command="manual_off",
                accepted=False,
                state=self._behavior.state,
                detail=f"{before} 는 수동 조종 중이 아니다",
            )
        self._commander.halt()
        # 기울인 채로 자율에 넘기지 않는다 — 순찰은 기준 자세를 전제로 걷는다.
        if self._pose_tilted:
            self._send_pose(0.0)
        return CommandResult(command="manual_off", accepted=True, state=self._behavior.state)

    def _send_pose(self, pitch: float) -> None:
        self._commander.once(
            "POSE", pitch=pitch, roll=self._pose_roll, height=0.0, dur=self._pose_dur_ms
        )
        self._pose_tilted = pitch != 0.0

    def pose(self, preset: str) -> CommandResult:
        """본체 자세 — `up`·`level`·`down`. `MANUAL` 에서만 받고 다음 틱에 보낸다 (B6).

        자율 상태에서는 PPE 자세 상승이 같은 `POSE` 를 쓰므로 섞지 않는다.
        """
        if not self._pose_pitch:
            return CommandResult(
                command="pose",
                accepted=False,
                state=self._behavior.state,
                detail="자세 경로가 연결되지 않았다",
            )
        if preset not in self._pose_pitch:
            return CommandResult(
                command="pose",
                accepted=False,
                state=self._behavior.state,
                detail=f"모르는 자세: {preset!r} (up|level|down)",
            )
        if self._behavior.state != "MANUAL":
            return CommandResult(
                command="pose",
                accepted=False,
                state=self._behavior.state,
                detail="수동 오버라이드를 먼저 잡아야 한다",
            )
        pitch = self._pose_pitch[preset]
        self._send_pose(pitch)
        return CommandResult(
            command="pose",
            accepted=True,
            state=self._behavior.state,
            detail=f"POSE pitch={pitch:+.0f}° 를 다음 틱에 보낸다 — 반영은 IMU pitch 로 확인",
        )

    def drive(self, step: float, angle: float) -> CommandResult:
        """수동 조종 입력. `MANUAL` 에서만 받고 다음 틱에 반영된다(틱마다 다시 보내는 것이 규약이다)."""
        if self._behavior.state != "MANUAL":
            return CommandResult(
                command="drive",
                accepted=False,
                state=self._behavior.state,
                detail="수동 오버라이드를 먼저 잡아야 한다",
            )
        self._commander.drive(step, angle)
        return CommandResult(command="drive", accepted=True, state=self._behavior.state)

    def reset(self) -> CommandResult:
        """사람이 원인 해소를 확인하고 누르는 `FAILSAFE` 해제 요청.

        예약만 한다 — `RESET_SAFE` 전문은 운용 루프의 다음 틱이 보내고, 해제 확정은 로봇의 래치
        보고를 기다린다 (ADR-21).
        """
        if self._request_reset is None:
            return CommandResult(
                command="reset",
                accepted=False,
                state=self._behavior.state,
                detail="해제 경로가 연결되지 않았다",
            )
        self._request_reset()
        return CommandResult(
            command="reset",
            accepted=True,
            state=self._behavior.state,
            detail="안전 해제를 요청했다 — 로봇이 래치 해제를 보고할 때까지 기다린다",
        )

    def alarm_confirm(self) -> CommandResult:
        """사람이 상황을 확인하고 누르는 경보(L3) 해제 (FR-10.3.2).

        `reset()`(F 해제)과 다른 것을 확인한다 (ADR-26 ①). 헤드리스 런타임의 L3 해제 경로다.
        예약만 하고 운용 루프의 다음 틱이 푼다 — 틱 도중에 단계가 바뀌지 않게.
        """
        if self._confirm_alarm is None:
            return CommandResult(
                command="alarm",
                accepted=False,
                state=self._behavior.state,
                detail="경보 확인 경로가 연결되지 않았다",
            )
        self._confirm_alarm()
        return CommandResult(
            command="alarm",
            accepted=True,
            state=self._behavior.state,
            detail="경보 확인을 요청했다 — 다음 틱에 단계가 내려간다 (L3 가 아니면 무시된다)",
        )

    def goto(self, x: float, y: float) -> CommandResult:
        """지도에서 찍은 곳(순찰 좌표 m)으로 보낸다. 예약만 하고 다음 틱이 경로를 푼다.

        경로가 있는지·자기 위치를 아는지는 루프가 판정한다 — 결과는 `/api/nav` 의
        `goal_feedback` 으로 본다. 순찰 중이 아니면 순찰 시작을 함께 예약한다.
        """
        if self._goto_point is None:
            return CommandResult(
                command="goto",
                accepted=False,
                state=self._behavior.state,
                detail="지도 이동 경로가 연결되지 않았다 (LiDAR 측위 순찰이 아니다)",
            )
        accepted, detail = self._goto_point(x, y)
        return CommandResult(
            command="goto", accepted=accepted, state=self._behavior.state, detail=detail
        )

    def route_start(self, route_id: str, expected_digest: str | None = None) -> CommandResult:
        """확인한 저장 동선을 요청한다. 운용 안전 관문은 런타임이 기존 규칙대로 검사한다."""
        if self._start_route is None:
            accepted, detail = False, "동선 주행 경로가 연결되지 않았다"
        elif expected_digest is None:
            accepted, detail = self._start_route(route_id)
        else:
            accepted, detail = self._start_route(route_id, expected_digest)
        return CommandResult("route_start", accepted, self._behavior.state, detail)

    def route_stop(self) -> CommandResult:
        """예약 중인 동선까지 취소하는 런타임 정지 경로."""
        accepted, detail = (
            self._stop_route()
            if self._stop_route is not None
            else (False, "동선 주행 경로가 연결되지 않았다")
        )
        return CommandResult("route_stop", accepted, self._behavior.state, detail)

    def locate(self, zone: str) -> CommandResult:
        """사람이 «로봇은 지금 이 구역 안에 있다» 고 알려준다 — 그 구역 안에서만 위치를 다시 찾는다.

        집 지도는 스캔 하나로 구별이 안 되는 자리가 많아 들어 옮긴 뒤 전역 탐색이 확정을
        못 한다. 구역으로 범위를 좁히면 구역 안에서 유일한 답만 받는다. 지금 믿던 자세는
        버리므로 로봇은 다시 찾을 때까지 선다. 예약만 하고 다음 틱이 적용한다.
        """
        if self._locate_zone is None:
            return CommandResult(
                command="locate",
                accepted=False,
                state=self._behavior.state,
                detail="위치 알려주기 경로가 연결되지 않았다 (LiDAR 측위 순찰이 아니다)",
            )
        accepted, detail = self._locate_zone(zone)
        return CommandResult(
            command="locate",
            accepted=accepted,
            state=self._behavior.state,
            detail=detail,
        )

    def locate_point(self, x: float, y: float) -> CommandResult:
        """사람이 찍은 현재 위치 주변에서 다시 찾도록 다음 루프 틱에 예약한다.

        클릭 자체로 위치를 확정하거나 이동을 시작하지 않는다. 좌표의 지도 범위와
        장애물 검증은 지도를 소유한 런타임이 수행한다.
        """
        if self._locate_point is None:
            return CommandResult(
                command="locate",
                accepted=False,
                state=self._behavior.state,
                detail="지도 위치 알려주기 경로가 연결되지 않았다 (LiDAR 측위 순찰이 아니다)",
            )
        accepted, detail = self._locate_point(x, y)
        return CommandResult(
            command="locate", accepted=accepted, state=self._behavior.state, detail=detail
        )

    #: 자율 동작 상태 — "순찰 정지" 가 받을 수 있는 상태들이다.
    _AUTONOMOUS = frozenset(
        {
            "PATROL",
            "SCAN",
            "AVOID",
            "ALERT",
            "TRACK",
            "AUTH_WAIT",
            "HAZARD_DISPATCH",
            "HAZARD_SCAN",
        }
    )

    def service(self, mode: str) -> CommandResult:
        """온보드 서비스 모드 전환 (`SERVICE` 전문, WBS 3.2.4 확장).

        `enter` 는 로봇을 주차시키고 루프 워치독을 건다(OTA·진단용). `exit` 는 워치독을 해제하며
        safe 래치는 남아 보행 복귀에는 `RESET_SAFE` 가 필요하다. 다음 틱에 싣는다.
        """
        if mode not in ("enter", "exit"):
            return CommandResult(
                command="service",
                accepted=False,
                state=self._behavior.state,
                detail=f"모르는 서비스 모드: {mode!r} (enter|exit)",
            )
        self._commander.once("SERVICE", mode=mode)
        return CommandResult(
            command="service",
            accepted=True,
            state=self._behavior.state,
            detail=f"서비스 모드 {mode} 를 다음 틱에 보낸다 — ACK 의 service_mode·wdt_armed 로 확인",
        )

    def sound(self, track: int) -> CommandResult:
        """로봇 MP3 모듈의 TF 카드 트랙 재생 (`SOUND` 전문 · WBS 4.7.21 ⑤). 0 은 정지.

        음성 프로세스가 로봇 스피커로 트는 경로다(로봇 링크는 런타임 혼자 쥔다). 다음 틱에 싣고
        ACK 를 기다리지 않는다. 상태를 보지 않고 래치도 건드리지 않는다 — 펌웨어도 FAILSAFE 중에
        `SOUND` 를 받는다. 범위 밖이나 `bool` 은 보내지 않는다 (PROTOCOL `SOUND` 절).
        """
        if isinstance(track, bool) or not isinstance(track, int):
            return CommandResult(
                command="sound",
                accepted=False,
                state=self._behavior.state,
                detail=f"트랙 번호는 정수여야 한다: {track!r}",
            )
        if not 0 <= track <= SOUND_TRACK_MAX:
            return CommandResult(
                command="sound",
                accepted=False,
                state=self._behavior.state,
                detail=f"트랙 번호 범위 밖: {track} (0~{SOUND_TRACK_MAX})",
            )
        self._commander.once("SOUND", track=track)
        return CommandResult(
            command="sound",
            accepted=True,
            state=self._behavior.state,
            detail=(
                "재생 정지를 다음 틱에 보낸다"
                if track == 0
                else f"트랙 {track} 재생을 다음 틱에 보낸다 — 소리가 났는지는 확인하지 않는다"
            ),
        )

    def patrol(self) -> CommandResult:
        """순찰 시작을 **예약**한다 — 리셋 정착 후 `IDLE` 에서 발행된다 (WBS 4.7.11).

        `runtime.ask_patrol()` 로 예약한다 — `START_PATROL` 을 바로 넣으면 기동 리셋과 순서가
        어긋난다. 반환값은 «의도를 받았다» 이지 «순찰 중» 이 아니다.
        """
        if self._ask_patrol is None:
            return CommandResult(
                command="patrol",
                accepted=False,
                state=self._behavior.state,
                detail="순찰 경로가 연결되지 않았다",
            )
        self._ask_patrol()
        return CommandResult(
            command="patrol",
            accepted=True,
            state=self._behavior.state,
            detail="순찰을 예약했다 — FSM 이 허락하는 시점에 시작된다",
        )

    def patrol_stop(self) -> CommandResult:
        """자율 동작을 멈추고 `IDLE` 로 내린다.

        전이표에 `PATROL → IDLE` 직행 사건이 없으므로 **수동을 한 번 거친다** —
        `MANUAL_ON` 으로 자율을 끊고(로봇 halt) 곧바로 `MANUAL_OFF` 로 내려
        `IDLE` 에 정착한다. `ESTOP` 과 달리 래치를 걸지 않아 해제 절차가
        필요 없는, "정상 정지"다.
        """
        if self._stop_route is not None and (
            self._behavior.state in self._AUTONOMOUS or self._behavior.state == "IDLE"
        ):
            # 기존 정지 버튼도 새 동선·아직 IDLE인 시작 예약을 즉시 취소한다.
            # 런타임이 송신 락 안에서 상태 전환과 STOP 전문까지 처리한다.
            accepted, detail = self._stop_route()
            return CommandResult("patrol_stop", accepted, self._behavior.state, detail)
        if self._behavior.state not in self._AUTONOMOUS:
            return CommandResult(
                command="patrol_stop",
                accepted=False,
                state=self._behavior.state,
                detail="자율 동작 중이 아니다",
            )
        entered = self.manual_on()
        if not entered.accepted:
            return CommandResult(
                command="patrol_stop",
                accepted=False,
                state=self._behavior.state,
                detail=entered.detail,
            )
        released = self.manual_off()
        return CommandResult(
            command="patrol_stop",
            accepted=released.accepted,
            state=self._behavior.state,
            detail="수동을 거쳐 대기로 내렸다",
        )

    def mission_mode(self, target: str) -> CommandResult:
        """운용 모드 전환 (FR-4.7 · FR-11.3 · WBS 3.4.4).

        `IDLE`·`MANUAL` 밖이거나 선행 기능이 없으면 거절하고 사유를 문장으로 돌려준다
        (ADR-33 규칙 2·7). 전환은 L3·F 를 풀지 않는다 (ADR-33 규칙 3).
        """
        if not isinstance(target, str) or not target.strip():
            return CommandResult(
                command="mode",
                accepted=False,
                state=self._behavior.state,
                detail="운용 모드 이름이 없다",
            )
        if self._set_mode is None:
            return CommandResult(
                command="mode",
                accepted=False,
                state=self._behavior.state,
                detail="모드 전환 경로가 연결되지 않았다",
            )
        refused = self._set_mode(target)
        return CommandResult(
            command="mode",
            accepted=refused is None,
            state=self._behavior.state,
            detail=refused if refused is not None else f"운용 모드를 {target} 로 바꿨다",
        )

    def auth(self, result: str, captured_at_ms: int | None = None) -> CommandResult:
        """음성 암구호 인증의 **판정 결과**를 사건으로 넣는다 (WBS 3.8.2 · FR-10.2).

        대조는 음성 파이프라인이 끝냈고, 여기서는 판정을 옮긴다. `AUTH_WAIT` 가 아니면 거절된다.
        불일치는 사건이 아니라 시도다 — `max_attempts` 는 런타임(`note_voice_auth`)이 센다 (FR-10.3).
        """
        if result not in ("ok", "fail", "pending"):
            return CommandResult(
                command="auth",
                accepted=False,
                state=self._behavior.state,
                detail=f"모르는 인증 결과: {result!r} (ok|fail|pending)",
            )
        if result == "pending":
            # 판정이 아니다 — `AUTH_WAIT` 마감을 한 번 미루는 통지다 (ADR-37).
            if self._note_voice_listening is None:
                return CommandResult(
                    command="auth",
                    accepted=False,
                    state=self._behavior.state,
                    detail="이 호스트는 인증 유예를 다루지 않는다",
                )
            accepted, detail = self._note_voice_listening(captured_at_ms)
            return CommandResult(
                command="auth",
                accepted=accepted,
                state=self._behavior.state,
                detail=detail,
            )
        if self._note_voice_auth is not None:
            # `captured_at_ms`(사람이 말한 시각)는 창 열린 시각을 아는 런타임까지 그대로 보낸다.
            accepted, detail = self._note_voice_auth(result == "ok", captured_at_ms)
            return CommandResult(
                command="auth",
                accepted=accepted,
                state=self._behavior.state,
                detail=detail,
            )
        event = Event.AUTH_OK if result == "ok" else Event.AUTH_FAILED
        accepted = self._apply_event(event)
        return CommandResult(
            command="auth",
            accepted=accepted,
            state=self._behavior.state,
            detail=(
                "인증 결과를 반영했다"
                if accepted
                else f"{self._behavior.state} 에서는 인증 결과를 받지 않는다 (AUTH_WAIT 만)"
            ),
        )
