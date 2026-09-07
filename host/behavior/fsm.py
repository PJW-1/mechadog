"""행동 상태 머신 — 최소 3상태 (WBS 3.4.1 · 아키텍처 3절).

**전이는 테이블이고 분기문이 아니다.** 조건을 `if` 로 흩어놓으면 전이표(문서)와
코드가 각각 진화해서 반드시 어긋난다. 여기서는 표를 데이터로 두고 엔진이 그
표만 본다 — 상태나 전이를 추가할 때 엔진은 손대지 않는다.

지금 사는 상태는 셋뿐이다. 나머지 10개(`PATROL`·`ALERT`·`TRACK` 등)는 카메라와
순찰 로직이 붙은 뒤에 같은 표에 행을 더하면 된다.

    IDLE ──MANUAL_ON──▶ MANUAL
      ◀──MANUAL_OFF────┘
      ▲
      │RESET_CONFIRMED          어느 상태에서든
    FAILSAFE ◀── ONBOARD_FAILSAFE · LINK_LOST

**FAILSAFE 는 자동으로 풀리지 않는다.** 원인이 사라져도 사람이 확인해야 나온다
(DR-16). 자동 복귀를 만들면 무엇 때문에 멈췄는지 모르는 채 다시 걷는다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from host.behavior.commander import Commander

#: 모든 상태에서 받는 전이의 출발지 표기.
ANY = "*"


class Event(StrEnum):
    """전이를 일으키는 입력. **상태가 아니라 사건이다.**"""

    MANUAL_ON = "MANUAL_ON"  # 조작자가 수동 조종을 잡았다
    MANUAL_OFF = "MANUAL_OFF"  # 조작자가 놓았다
    ONBOARD_FAILSAFE = "ONBOARD_FAILSAFE"  # 로봇이 스스로 FAILSAFE 를 보고했다
    LINK_LOST = "LINK_LOST"  # 텔레메트리가 끊겼다
    RESET_CONFIRMED = "RESET_CONFIRMED"  # 사람이 원인 해소를 확인했다


class Directive(StrEnum):
    """상태가 송신기에 요구하는 것. **행동 자체가 아니라 누가 정하느냐다.**"""

    HALT = "HALT"  # 정지를 계속 보낸다
    YIELD = "YIELD"  # 바깥(조작자·상위 로직)이 의도를 정한다. FSM 은 손대지 않는다


@dataclass(frozen=True, slots=True)
class Transition:
    src: str
    event: Event
    dst: str


#: **전이표가 정본이다.** 아키텍처 3절의 표와 일치해야 하며
#: `tests/test_fsm.py` 가 상태 이름을 규약 상수와 대조한다.
TRANSITIONS: tuple[Transition, ...] = (
    Transition("IDLE", Event.MANUAL_ON, "MANUAL"),
    Transition("MANUAL", Event.MANUAL_OFF, "IDLE"),
    Transition(ANY, Event.ONBOARD_FAILSAFE, "FAILSAFE"),
    Transition(ANY, Event.LINK_LOST, "FAILSAFE"),
    Transition("FAILSAFE", Event.RESET_CONFIRMED, "IDLE"),
)

#: 상태별 지시. 표에 없는 상태가 생기면 엔진이 즉시 예외를 내므로,
#: 상태만 추가하고 지시를 빠뜨리는 실수가 조용히 넘어가지 않는다.
DIRECTIVES: dict[str, Directive] = {
    "IDLE": Directive.HALT,
    "MANUAL": Directive.YIELD,
    "FAILSAFE": Directive.HALT,
}

INITIAL = "IDLE"


class Fsm:
    """표만 보고 상태를 옮긴다. 부수효과도 로깅도 여기서 하지 않는다."""

    def __init__(
        self,
        transitions: Iterable[Transition] = TRANSITIONS,
        *,
        initial: str = INITIAL,
    ) -> None:
        self._table: dict[tuple[str, Event], str] = {}
        for t in transitions:
            key = (t.src, t.event)
            if key in self._table:
                raise ValueError(f"전이 중복: {t.src} + {t.event}")
            self._table[key] = t.dst
        if initial not in DIRECTIVES:
            raise ValueError(f"지시가 정의되지 않은 초기 상태: {initial!r}")
        self._state = initial
        self._hooks: dict[str, list[Callable[[str, str], None]]] = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def directive(self) -> Directive:
        return DIRECTIVES[self._state]

    def on_enter(self, state: str, hook: Callable[[str, str], None]) -> None:
        """상태 진입 훅. 인자는 `(이전 상태, 새 상태)` 다."""
        self._hooks.setdefault(state, []).append(hook)

    def handle(self, event: Event) -> bool:
        """전이했으면 `True`. **모르는 사건은 조용히 무시한다.**

        예외를 내지 않는 이유 — 사건은 바깥(텔레메트리·키보드)에서 오고, 지금
        상태와 무관한 사건이 오는 것은 정상이다. `FAILSAFE` 중에 `MANUAL_OFF` 가
        와도 그건 버그가 아니다.
        """
        target = self._table.get((self._state, event)) or self._table.get((ANY, event))
        if target is None or target == self._state:
            return False
        previous, self._state = self._state, target
        for hook in self._hooks.get(target, ()):
            hook(previous, target)
        return True


class Behavior:
    """FSM 상태를 송신기의 의도로 옮긴다 — **둘을 잇는 유일한 지점이다.**

    FSM 은 상태만 알고 송신기는 명령만 안다. 그 사이를 여기서 잇기 때문에
    양쪽 모두 서로를 몰라도 단독으로 시험할 수 있다.
    """

    def __init__(self, commander: Commander, fsm: Fsm | None = None) -> None:
        self._commander = commander
        self._fsm = fsm if fsm is not None else Fsm()

    @property
    def state(self) -> str:
        return self._fsm.state

    @property
    def fsm(self) -> Fsm:
        return self._fsm

    @property
    def commander(self) -> Commander:
        return self._commander

    def event(self, event: Event) -> bool:
        """사건을 넣는다. **비상정지가 필요하면 전문을 돌려받아야 하므로**
        `ONBOARD_FAILSAFE` 는 여기서 `ESTOP` 을 만들지 않는다 — 로봇이 이미
        스스로 멈춘 상태를 호스트가 따라가는 것일 뿐이다.
        """
        return self._fsm.handle(event)

    def tick(self, now_ms: int) -> list[str]:
        """상태에 맞는 지시를 적용하고 송신기의 틱 결과를 돌려준다."""
        if self._fsm.directive is Directive.HALT:
            self._commander.halt()
        self._commander.announce(self._fsm.state)
        return self._commander.tick(now_ms)
