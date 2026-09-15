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

__all__ = ["CommandResult", "CommandService"]


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
    ) -> None:
        self._behavior = behavior
        self._commander = commander
        self._send = send
        self._request_reset = request_reset
        self._ask_patrol = ask_patrol
        # ⚠️ **사건은 `apply_event` 로 넣는다. `behavior.event()` 를 직접 부르지 않는다.**
        # 직접 부르면 전이는 일어나지만 **대응 단계 갱신과 전이 로그가 함께 빠진다** —
        # 런타임의 `_apply()` 가 그 둘을 묶어 두고 있기 때문이다. 2026-09-14 실기에서
        # E-Stop 이 로봇을 잠갔는데 단계가 `L0`(파랑) 에 머물러 **눈 LED 가 흰색으로
        # 바뀌지 않았다**(FR-10.4). 같은 실기에서 `MANUAL` 26.7초의 전이도 로그에 한
        # 줄도 남지 않았다. 런타임이 없는 시험에서는 전이만 필요하므로 기본값을 둔다.
        self._apply_event = apply_event if apply_event is not None else behavior.event

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
        return CommandResult(command="manual_off", accepted=True, state=self._behavior.state)

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
