"""명령 API — 수동 오버라이드와 E-Stop (WBS 4.5.3 · FR-4.3/4.4).

지금까지 대시보드는 **읽기 전용**이었다(`server.py` 첫 줄). 여기서 처음으로
사람이 로봇을 건드린다. 그래서 두 가지를 분명히 한다 — **무엇이 즉시 나가고
무엇이 다음 틱을 기다리는가**, 그리고 **무엇이 거절될 수 있는가**.

⚠️ **E-Stop 은 모든 상태에서 통해야 한다** (4.5.3 완료 기준). 순찰 중이든
추종 중이든 이미 수동 조종 중이든 눌리면 즉시 멈춘다. 이 모듈에서 `estop()`
만 유일하게 **어떤 조건도 검사하지 않는다** — 조건을 하나라도 달면 그 조건이
틀렸을 때 비상정지가 안 듣는다.

⚠️ **E-Stop 은 틱을 기다리지 않는다.** 주기 송신은 100ms 마다인데, 비상정지를
큐에 넣으면 최악의 경우 100ms 늦게 나간다. `Commander.emergency_stop()` 이
완성된 전문을 돌려주는 이유가 그것이고, 이 모듈은 그 전문을 **받은 자리에서**
보낸다.

⚠️ **FSM 에 사건을 넣는 것은 전문을 보낸 뒤다.** 순서를 뒤집으면 상태만 바뀌고
로봇은 계속 걷는 창이 생긴다. 로봇이 먼저 멈추고 호스트가 따라간다.

수동 오버라이드는 **거절될 수 있다.** `FAILSAFE` 에서는 받지 않는다 (전이표) —
비상정지로 멈춘 로봇을 조이스틱으로 다시 움직이게 두면 멈춘 이유가 사라진다.
거절을 조용히 삼키지 않고 `accepted=False` 로 돌려주어 화면이 그대로 표시한다.

`RESET_SAFE` 는 `reset()` 이 **예약**한다(#100). 바로 보내지 않고 운용 루프의 다음
틱이 보낸다 — 해제는 급하지 않고 비상정지만 틱을 앞지른다. ⚠️ **사람 확인은 지금
버튼 한 번뿐이다.** DR-16 의 별도 확인 절차(원인 해소를 한 번 더 확인받기)를 붙일지는
정해지지 않았고, 4.5.3 의 완료 기준(오버라이드 · E-Stop)에는 들어가지 않는다.
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
    """명령 하나의 결과. **거절도 결과다** — 화면이 그대로 보여 준다."""

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

    전송 수단을 모른다 — `send` 로 받은 호출 가능 객체에 완성된 전문을 넘길
    뿐이다. 그래서 소켓 없이 전수 시험된다.
    """

    def __init__(
        self,
        behavior: Behavior,
        commander: Commander,
        send: Callable[[str], None],
        request_reset: Callable[[], None] | None = None,
        apply_event: Callable[[Event], bool] | None = None,
        ask_patrol: Callable[[], None] | None = None,
        set_mode: Callable[[str], str | None] | None = None,
        note_voice_auth: Callable[[bool], tuple[bool, str]] | None = None,
        note_voice_listening: Callable[[int | None], tuple[bool, str]] | None = None,
        confirm_alarm: Callable[[], None] | None = None,
        reset_zone_baseline: Callable[[str], tuple[bool, str]] | None = None,
        pose: tuple[float, int] | None = None,
    ) -> None:
        self._behavior = behavior
        self._commander = commander
        self._send = send
        self._request_reset = request_reset
        self._ask_patrol = ask_patrol
        # 운용 모드 전환 (`3.4.4`). ⚠️ **`Mission` 을 직접 쥐지 않는다** — 전환 가부는
        # FSM 상태에 달렸고 그 둘을 잇는 곳은 런타임 하나다 (`runtime.set_mode`).
        self._set_mode = set_mode
        # ⚠️ **사건은 `apply_event` 로 넣는다. `behavior.event()` 를 직접 부르지 않는다.**
        # 직접 부르면 전이는 일어나지만 **대응 단계 갱신과 전이 로그가 함께 빠진다** —
        # 런타임의 `_apply()` 가 그 둘을 묶어 두고 있기 때문이다. 2026-09-14 실기에서
        # E-Stop 이 로봇을 잠갔는데 단계가 `L0`(파랑) 에 머물러 **눈 LED 가 흰색으로
        # 바뀌지 않았다**(FR-10.4). 같은 실기에서 `MANUAL` 26.7초의 전이도 로그에 한
        # 줄도 남지 않았다. 런타임이 없는 시험에서는 전이만 필요하므로 기본값을 둔다.
        self._apply_event = apply_event if apply_event is not None else behavior.event
        # 음성 암구호는 **시도 횟수를 세야 해서** 사건 하나로 옮길 수 없다 (FR-10.3).
        # 세는 곳은 런타임이다 — `AUTH_WAIT` 에 언제 들어왔는지를 아는 쪽이 거기뿐이다.
        # 런타임이 없는 시험에서는 예전처럼 사건만 넣는다.
        self._note_voice_auth = note_voice_auth
        # «판정이 오는 중» 신호 (FR-10.3 · ADR-37). 판정이 아니라 **창의 수명**만
        # 건드리므로 `note_voice_auth` 와 다른 자리다 — 섞으면 «시도를 세는 것» 과
        # «기다려 주는 것» 이 한 함수에 들어간다.
        self._note_voice_listening = note_voice_listening
        # 경보(L3) 확인. ⚠️ **`request_reset` 과 같은 자리에 두지 않는다** — 확인해야
        # 하는 것이 물리 상태(F)와 상황 판단(L3)으로 다르다 (ADR-26). 하나로 묶으면
        # **비상정지를 눌렀다 푸는 것으로 경보가 지워진다.**
        self._confirm_alarm = confirm_alarm
        # 구역 기준 재등록 (3.6.5). 어느 구역이 있는지 아는 쪽이 런타임이라 판정도 거기서 한다.
        self._reset_zone_baseline = reset_zone_baseline
        # 수동 자세 (B6). `(posture.pitch_up_deg, posture.settle_ms)` — PPE 자세 상승과
        # **같은 각도**만 쓴다. ±15° 는 2026-09-15 실물로 부호·크기를 확인한 값이다.
        # 임의 각도를 받지 않는 이유: 검증하지 않은 자세로 보행하면 넘어진다.
        self._pose_pitch: dict[str, float] = (
            {} if pose is None else {"up": pose[0], "level": 0.0, "down": -pose[0]}
        )
        self._pose_dur_ms = 0 if pose is None else int(pose[1])
        self._pose_tilted = False

    @property
    def state(self) -> str:
        return self._behavior.state

    def estop(self) -> CommandResult:
        """**어떤 상태에서도 통한다.** 조건을 검사하지 않는다.

        전문을 먼저 보내고 FSM 을 따라가게 한다. 순서를 뒤집으면 상태만 바뀌고
        로봇이 계속 걷는 창이 생긴다.
        """
        telegram = self._commander.emergency_stop()
        self._send(telegram)
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
        # 진입 순간 로봇이 이전 지시를 이어서 수행하지 않게 한다. 조작자가
        # 조이스틱을 잡기 전까지는 멈춰 있어야 한다.
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
        # ⚠️ **기울인 채로 자율에 넘기지 않는다.** 순찰은 기준 자세를 전제로 걷는다.
        if self._pose_tilted:
            self._send_pose(0.0)
        return CommandResult(command="manual_off", accepted=True, state=self._behavior.state)

    def _send_pose(self, pitch: float) -> None:
        self._commander.once("POSE", pitch=pitch, roll=0.0, height=0.0, dur=self._pose_dur_ms)
        self._pose_tilted = pitch != 0.0

    def pose(self, preset: str) -> CommandResult:
        """본체 자세 — `up`·`level`·`down`. **`MANUAL` 에서만 받는다** (B6).

        자율 상태에서는 PPE 자세 상승(`runtime._ppe_*`)이 같은 `POSE` 를 쓴다. 섞으면
        조작자가 올린 자세를 시퀀스가 되돌리거나 그 반대가 된다. 다음 틱에 보낸다.
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
        """수동 조종 입력. **`MANUAL` 에서만 받는다.**

        자율 주행 중에 조이스틱 입력을 섞으면 FSM 이 내린 지시와 사람이 민 값이
        같은 주기를 다투게 된다. 조종하려면 먼저 오버라이드를 잡아야 한다.

        ⚠️ 즉시 보내지 않는다 — 다음 틱에 반영된다. 조종 입력은 계속 들어오므로
        한 번 늦는 것이 문제가 되지 않고, 틱마다 다시 보내는 것이 규약이다.
        """
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

        즉시 보내지 않고 예약한다 — 실제 `RESET_SAFE` 전문은 운용 루프의 다음
        틱이 만들어 보낸다 (해제는 급하지 않고, `ESTOP` 만이 틱을 앞지른다).
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
        """사람이 상황을 확인하고 누르는 **경보(L3) 해제** (FR-10.3.2).

        `reset()` 과 나란히 있지만 **다른 것을 확인한다** — 저쪽은 물리 상태
        (넘어졌나·배터리·링크)이고 이쪽은 상황 판단(침입자가 갔나·안전모·물건)이다.
        하나로 묶으면 **비상정지를 눌렀다 푸는 것으로 경보가 지워진다** ([ADR-26]).

        ⚠️ **이 경로가 없으면 헤드리스 런타임은 경보를 풀 방법이 없다.** 콘솔
        확인 키는 tty 를 요구하므로(`runtime.watch_console`) 백그라운드로 띄운
        런타임에서는 아무도 누를 수 없고, 실기에서 **런타임을 재시작해** L3 를
        지워야 했다 — 재시작은 확인이 아니라 증거 인멸에 가깝다.

        즉시 풀지 않고 **예약한다** — 운용 루프가 전문을 만드는 중간에 단계가
        바뀌면 그 틱의 명령이 어느 단계의 것인지 말할 수 없다 (`reset` 과 같은 이유).
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

    def zone_baseline(self, zone: str) -> CommandResult:
        """관리자가 «이 상태가 정상» 이라고 인정한 구역의 기준을 지운다 (FR-8 · WBS 3.6.5).

        물건을 영구히 옮기면 그 구역은 순찰마다 «반출» 을 낸다. 기준을 지우면 다음
        방문에서 새로 뜬다. `alarm_confirm` 처럼 **예약한다** — 지우는 것은 다음 틱이다.
        ⚠️ 경보(L3)를 풀지 않는다 — 경보 확인은 따로 누른다.
        """
        if self._reset_zone_baseline is None:
            return CommandResult(
                command="zone_baseline",
                accepted=False,
                state=self._behavior.state,
                detail="기준 재등록 경로가 연결되지 않았다",
            )
        accepted, detail = self._reset_zone_baseline(zone)
        return CommandResult(
            command="zone_baseline",
            accepted=accepted,
            state=self._behavior.state,
            detail=detail,
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

        `enter` 는 로봇을 주차시키고 루프 워치독을 건다 — OTA·진단 중 행거를
        잡기 위한 것이다. `exit` 는 워치독을 해제한다; safe 래치는 남으므로
        보행 복귀에는 별도 `RESET_SAFE` 가 필요하다. 급하지 않으므로 다음
        틱에 실어 보낸다 (`once` — `reset` 과 같은 이유).
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

        음성 프로세스가 자기 발화를 로봇 스피커로 트는 경로다 — 로봇 링크는 런타임
        혼자 쥐므로 음성 쪽은 여기를 거친다. 급하지 않으므로 다음 틱에 싣는다
        (`once` — `service` 와 같다). seq·세션 개시는 다른 전문과 같이 송신기와
        런타임이 맡는다.

        ⚠️ **상태를 보지 않는다.** 펌웨어는 FAILSAFE 래치 중에도 `SOUND` 를 받는다
        (스피커는 구동 장치가 아니라 출력이다). 그래서 여기서도 거절하지 않고,
        ⚠️ **래치를 건드리지 않는다** — `RESET_SAFE` 를 보내거나 의도를 바꾸지 않는다.

        ⚠️ **범위 밖은 보내지 않는다.** 펌웨어도 `applied=false` 로 거부하지만 잘라
        받으면 다른 문장이 나가므로 호스트가 먼저 막는다. `bool` 은 정수로 받지 않는다.

        ACK 를 기다리지 않는다(다른 일회성 명령과 같다). 로봇 ACK 의 `applied=true`
        는 «받았다» 이지 «소리가 났다» 가 아니다 — 모듈 무응답은 펌웨어 시리얼의
        `SOUND dropped` 로만 남는다(PROTOCOL `SOUND` 절).
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

        `runtime.ask_patrol()` 경로를 쓴다 — `START_PATROL` 을 바로 넣으면 기동
        리셋과 순서가 어긋나 순찰이 조용히 취소되는 함정이 실기에서 드러났다.
        예약이므로 반환값은 "의도를 받았다"이지 "순찰 중"이 아니다.
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

        ⚠️ **거절이 흔한 명령이다.** `IDLE`·`MANUAL` 밖에서는 받지 않고(대응 중에
        판정 규칙이 바뀌면 진행 중인 인증·자세 시퀀스가 의미를 잃는다), 선행 기능이
        없는 모드도 받지 않는다(FR-11.7). 두 경우 모두 **사유를 문장으로 돌려주어**
        화면이 그대로 보여 준다 — 조용히 무시하면 조작자는 버튼 고장으로 읽는다.

        ⚠️ **전환은 L3·F 를 풀지 않는다** (FR-11.4). 풀리면 *"경보가 뜨면 모드를
        바꾼다"* 가 확인 없는 해제 요령이 된다 — 비상정지로 경보를 지우지 못하게 한
        것(ADR-26)과 같은 이유다.
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

        이 엔드포인트는 *인증을 수행하지 않는다* — 암구호 문구 대조는 음성
        파이프라인(등록 목록과 일치 비교)이 끝내고, 여기서는 그 판정만
        `AUTH_OK` / `AUTH_FAILED` 로 옮긴다. 전이표가 `AUTH_WAIT` 에서만 두
        사건을 받으므로 다른 상태의 호출은 자동으로 거절된다 — `apply_event`
        가 False 를 돌려주는 게 그 거절이다.

        ⚠️ **불일치는 사건이 아니라 시도다** (FR-10.3). `max_attempts` 를 세는
        일은 런타임이 하므로(`note_voice_auth`) 여기서 `AUTH_FAILED` 를 바로
        만들지 않는다. 그렇게 만들었더니 오인식 한 번이 곧 L3 경보였다.
        """
        if result not in ("ok", "fail", "pending"):
            return CommandResult(
                command="auth",
                accepted=False,
                state=self._behavior.state,
                detail=f"모르는 인증 결과: {result!r} (ok|fail|pending)",
            )
        if result == "pending":
            # **판정이 아니다.** 발화를 받아 두었고 전사가 돌고 있다는 통지이며,
            # 하는 일은 `AUTH_WAIT` 마감을 한 번 미루는 것뿐이다 (ADR-37).
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
            # `captured_at_ms` 는 **사람이 말한 시각**이다. 창이 열리기 전의 발화를
            # 시도로 세지 않기 위해 런타임까지 그대로 내려보낸다 — 창이 언제
            # 열렸는지 아는 쪽이 거기뿐이다.
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
