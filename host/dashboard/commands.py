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

`RESET_SAFE` 는 여기 없다. 그것은 **원인을 해소하고 사람이 확인한 뒤** 누르는
것이라 별도 확인 절차가 붙어야 하며(DR-16), 4.5.3 의 완료 기준이 아니다.
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
    ) -> None:
        self._behavior = behavior
        self._commander = commander
        self._send = send

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
        self._behavior.event(Event.ESTOP)
        return CommandResult(
            command="estop",
            accepted=True,
            state=self._behavior.state,
            detail="비상정지 전문을 즉시 보냈다",
        )

    def manual_on(self) -> CommandResult:
        """수동 오버라이드 진입. `FAILSAFE` 에서는 거절된다."""
        before = self._behavior.state
        accepted = self._behavior.event(Event.MANUAL_ON)
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
        accepted = self._behavior.event(Event.MANUAL_OFF)
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
