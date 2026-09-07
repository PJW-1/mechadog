"""행동 상태 머신 — 전이표 13상태 (WBS 3.4.1 · 아키텍처 3절).

**전이는 테이블이고 분기문이 아니다.** 조건을 `if` 로 흩어놓으면 전이표(문서)와
코드가 각각 진화해서 반드시 어긋난다. 여기서는 표를 데이터로 두고 엔진이 그 표만
본다 — **엔진 본문에는 상태 이름이 하나도 없다.** 상태나 전이를 추가할 때 엔진은
손대지 않는다.

    IDLE ──START_PATROL──▶ PATROL ⇄ SCAN · AVOID · ZONE_INSPECT
                              │
                              ├── PERSON_FOUND ──▶ ALERT ⇄ TRACK
                              │                     └── AUTH_REQUIRED ──▶ AUTH_WAIT
                              └── HAZARD_ALARM ──▶ HAZARD_DISPATCH ──▶ HAZARD_SCAN

    어느 상태에서든 ── ONBOARD_FAILSAFE · LINK_LOST · ESTOP ──▶ FAILSAFE
                   ── MANUAL_ON ──▶ MANUAL
                   ── POSE_STALE `[P2]` ──▶ LOST

**FAILSAFE 는 `RESET_CONFIRMED` 외의 어떤 사건도 받지 않는다** (DR-16). 원인이
사라져도 사람이 확인해야 나온다. 자동 복귀를 만들면 무엇 때문에 멈췄는지 모르는
채로 다시 걷는다.

전이표에 없는 것 두 가지 — 둘 다 아키텍처 3절이 스스로 제외한 것이다.

1. **명령 타임아웃 300ms** — 온보드 Tier 1 반사이며 표에도 *"FSM 무관"* 으로 적혀
   있다. 호스트가 흉내내면 안 되는 유일한 부류다.
2. **`ALERT` 에서의 PPE 위반** — 상태가 그대로이고 바뀌는 것은 에스컬레이션 단계다.
   그쪽은 별도 상태기(`3.8.3`)가 맡는다. 다만 `TRACK` 에서의 PPE 위반은 실제 전이라
   표에 있다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from host.behavior.commander import Commander

#: 모든 상태에서 받는 전이의 출발지 표기.
ANY = "*"


class Event(StrEnum):
    """전이를 일으키는 입력. **상태가 아니라 사건이다.**

    이름이 *"무엇이 일어났다"* 이고 *"무엇을 해라"* 가 아닌 것에 주의한다.
    특히 `ONBOARD_FAILSAFE` 는 **로봇이 이미 멈췄다는 보고**이며 호스트의 지시가
    아니다 — Tier 1 은 호스트와 무관하게 항상 우선한다 (아키텍처 1.2).
    """

    # ── 순찰 ──
    START_PATROL = "START_PATROL"  # 대시보드에서 순찰 시작을 눌렀다
    SCAN_DUE = "SCAN_DUE"  # 순찰 타이머 10s 만료 (FR-2.4)
    SCAN_DONE = "SCAN_DONE"  # 상체 스캔 3초 완료
    # ── 온보드 반사를 호스트가 따라간다 ──
    ONBOARD_AVOID = "ONBOARD_AVOID"  # 초음파 25cm 반사 정지를 로봇이 보고했다
    AVOID_CLEARED = "AVOID_CLEARED"  # 회피 시퀀스 완료 & 전방 clear
    # ── 사람 대응 ──
    PERSON_FOUND = "PERSON_FOUND"  # 3프레임 연속 검출 (FR-3.1)
    TARGET_OFF_CENTER = "TARGET_OFF_CENTER"  # x축 편차 > 데드존 (FR-3.5)
    TARGET_CENTERED = "TARGET_CENTERED"  # 중앙 정렬 유지
    TARGET_LOST = "TARGET_LOST"  # 미검출 5s 지속 (FR-3.6)
    PPE_VIOLATION = "PPE_VIOLATION"  # 보호구 미착용 확정 (FR-9.3)
    # ── 인증 ──
    AUTH_REQUIRED = "AUTH_REQUIRED"  # 미인증 상태 지속 → L2
    AUTH_OK = "AUTH_OK"  # 사원증 또는 암구호 인증 성공
    AUTH_FAILED = "AUTH_FAILED"  # 2회 실패 또는 30초 초과 → L3
    # ── 수동 ──
    MANUAL_ON = "MANUAL_ON"  # 조작자가 수동 조종을 잡았다
    MANUAL_OFF = "MANUAL_OFF"  # 조작자가 놓았다
    # ── 안전 ──
    ONBOARD_FAILSAFE = "ONBOARD_FAILSAFE"  # 링크 두절·저전압·전도를 로봇이 보고
    LINK_LOST = "LINK_LOST"  # 텔레메트리가 끊겼다
    ESTOP = "ESTOP"  # 사람이 비상정지를 눌렀다
    RESET_CONFIRMED = "RESET_CONFIRMED"  # 원인 해소를 사람이 확인했다
    # ── Phase 2 ──
    HAZARD_ALARM = "HAZARD_ALARM"  # 위험구역 알람 수신
    HAZARD_ARRIVED = "HAZARD_ARRIVED"  # 웨이포인트 목표 도달
    HAZARD_SCAN_DONE = "HAZARD_SCAN_DONE"  # 판독 완료
    POSE_STALE = "POSE_STALE"  # SLAM pose 500ms 미갱신
    POSE_REACQUIRED = "POSE_REACQUIRED"  # 재측위 성공
    ZONE_ARRIVED = "ZONE_ARRIVED"  # 구역 도착
    ZONE_CLEAR = "ZONE_CLEAR"  # 검사 완료 & 변화 없음
    ZONE_CHANGED = "ZONE_CHANGED"  # 물체 변화 확정 (FR-8.4)


class Directive(StrEnum):
    """상태가 송신기에 요구하는 것. **행동 자체가 아니라 누가 정하느냐다.**"""

    HALT = "HALT"  # 정지를 계속 보낸다
    YIELD = "YIELD"  # 조작자가 정한다. FSM 은 손대지 않는다
    SEQUENCE = "SEQUENCE"  # 그 상태 전용 모션 시퀀스가 정한다 (3.5 소관)


@dataclass(frozen=True, slots=True)
class Transition:
    src: str
    event: Event
    dst: str


#: **전이표가 정본이다.** 아키텍처 3절의 표와 일치해야 하며
#: `tests/test_fsm.py` 가 상태 이름을 규약 상수(`protocol.FSM_STATES`)와 대조한다.
TRANSITIONS: tuple[Transition, ...] = (
    # ── 순찰 루프 ──
    Transition("IDLE", Event.START_PATROL, "PATROL"),
    Transition("PATROL", Event.SCAN_DUE, "SCAN"),
    Transition("SCAN", Event.SCAN_DONE, "PATROL"),
    # ── 온보드 반사를 호스트가 따라간다 (FAILSAFE 와 같은 방식) ──
    Transition("PATROL", Event.ONBOARD_AVOID, "AVOID"),
    Transition("AVOID", Event.AVOID_CLEARED, "PATROL"),
    # ── 사람 대응 ──
    Transition("PATROL", Event.PERSON_FOUND, "ALERT"),
    Transition("ALERT", Event.TARGET_OFF_CENTER, "TRACK"),
    Transition("TRACK", Event.TARGET_CENTERED, "ALERT"),
    Transition("ALERT", Event.TARGET_LOST, "PATROL"),
    Transition("TRACK", Event.TARGET_LOST, "PATROL"),
    # `ALERT` 에서의 PPE 위반은 상태가 그대로이므로 표에 없다 — 에스컬레이션(3.8.3) 소관
    Transition("TRACK", Event.PPE_VIOLATION, "ALERT"),
    # ── 인증 ──
    Transition("ALERT", Event.AUTH_REQUIRED, "AUTH_WAIT"),
    Transition("AUTH_WAIT", Event.AUTH_OK, "PATROL"),
    Transition("AUTH_WAIT", Event.AUTH_FAILED, "ALERT"),
    # ── 수동 ──
    Transition(ANY, Event.MANUAL_ON, "MANUAL"),
    Transition("MANUAL", Event.MANUAL_OFF, "IDLE"),
    # ── 안전 (Tier 1 을 호스트가 따라간다) ──
    Transition(ANY, Event.ONBOARD_FAILSAFE, "FAILSAFE"),
    Transition(ANY, Event.LINK_LOST, "FAILSAFE"),
    Transition(ANY, Event.ESTOP, "FAILSAFE"),
    Transition("FAILSAFE", Event.RESET_CONFIRMED, "IDLE"),
    # ── Phase 2 — 측위가 확보된 뒤에 쓰인다 ──
    Transition(ANY, Event.HAZARD_ALARM, "HAZARD_DISPATCH"),
    Transition("HAZARD_DISPATCH", Event.HAZARD_ARRIVED, "HAZARD_SCAN"),
    Transition("HAZARD_SCAN", Event.HAZARD_SCAN_DONE, "PATROL"),
    Transition(ANY, Event.POSE_STALE, "LOST"),
    Transition("LOST", Event.POSE_REACQUIRED, "PATROL"),
    Transition("PATROL", Event.ZONE_ARRIVED, "ZONE_INSPECT"),
    Transition("ZONE_INSPECT", Event.ZONE_CLEAR, "PATROL"),
    Transition("ZONE_INSPECT", Event.ZONE_CHANGED, "ALERT"),
)

#: **이 상태에서는 나열된 사건만 받는다.** 다른 사건은 무시된다.
#:
#: `FAILSAFE` 를 데이터로 봉인하는 방법이다. `if state == "FAILSAFE"` 를 엔진에
#: 넣으면 하드코딩 분기가 되고, 자기 자신으로 가는 전이를 여러 줄 적으면
#: *"자기로 가는 전이는 차단을 뜻한다"* 는 암묵 규칙이 생긴다. 둘 다 피한다.
EXCLUSIVE: dict[str, frozenset[Event]] = {
    "FAILSAFE": frozenset({Event.RESET_CONFIRMED}),
}

#: 상태별 지시. **모든 상태가 여기 있어야 한다** — 상태만 추가하고 지시를
#: 빠뜨리면 그 상태에서 아무 명령도 안 나가므로 생성 시 예외로 막는다.
DIRECTIVES: dict[str, Directive] = {
    "IDLE": Directive.HALT,
    "MANUAL": Directive.YIELD,
    "FAILSAFE": Directive.HALT,
    "LOST": Directive.HALT,  # 즉시 정지 후 재측위 대기
    "AUTH_WAIT": Directive.HALT,  # 정지한 채 인증을 기다린다
    "PATROL": Directive.SEQUENCE,  # 3.5.1 순찰 행동
    "AVOID": Directive.SEQUENCE,  # 후진 200mm + 선회 (DR-11 로 제자리 회전 불가)
    "SCAN": Directive.SEQUENCE,  # 3.5.2 상체 스캔
    "ALERT": Directive.SEQUENCE,  # 3.5.3 Pitch Up 경계 자세
    "TRACK": Directive.SEQUENCE,  # 3.5.4 선회 보행 추종
    "HAZARD_DISPATCH": Directive.SEQUENCE,  # 3.9 웨이포인트 추종
    "HAZARD_SCAN": Directive.SEQUENCE,
    "ZONE_INSPECT": Directive.SEQUENCE,
}

INITIAL = "IDLE"

#: 상태 전용 모션 시퀀스의 서명. 송신기에 의도를 세우는 것이 전부이며
#: 소켓을 만지지 않는다.
Sequence = Callable[[Commander, int], None]


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

        예외를 내지 않는 이유 — 사건은 바깥(텔레메트리·비전·키보드)에서 오고,
        지금 상태와 무관한 사건이 오는 것은 정상이다. `FAILSAFE` 중에
        `TARGET_LOST` 가 와도 그건 버그가 아니다.
        """
        allowed = EXCLUSIVE.get(self._state)
        if allowed is not None and event not in allowed:
            return False
        target = self._table.get((self._state, event)) or self._table.get((ANY, event))
        if target is None or target == self._state:
            return False
        previous, self._state = self._state, target
        for hook in self._hooks.get(target, ()):
            hook(previous, target)
        return True


class Behavior:
    """FSM 상태를 송신기의 의도로 옮긴다 — **둘을 잇는 유일한 지점이다.**

    FSM 은 상태만 알고 송신기는 명령만 안다. 그 사이를 여기서 잇기 때문에 양쪽
    모두 서로를 몰라도 단독으로 시험할 수 있다.

    `SEQUENCE` 상태에 시퀀스가 아직 등록되지 않았으면 **정지로 처리한다.**
    `3.5` 모션 시퀀스가 붙기 전까지는 그것이 유일하게 안전한 기본값이다 —
    등록되지 않은 상태에서 이전 의도를 그대로 두면 로봇이 계속 걸어간다.
    """

    def __init__(self, commander: Commander, fsm: Fsm | None = None) -> None:
        self._commander = commander
        self._fsm = fsm if fsm is not None else Fsm()
        self._sequences: dict[str, Sequence] = {}

    @property
    def state(self) -> str:
        return self._fsm.state

    @property
    def fsm(self) -> Fsm:
        return self._fsm

    @property
    def commander(self) -> Commander:
        return self._commander

    def register_sequence(self, state: str, sequence: Sequence) -> None:
        """`SEQUENCE` 상태의 동작을 등록한다 — `3.5` 가 붙는 지점이다."""
        if DIRECTIVES.get(state) is not Directive.SEQUENCE:
            raise ValueError(f"{state} 는 시퀀스를 쓰는 상태가 아니다")
        self._sequences[state] = sequence

    def event(self, event: Event) -> bool:
        """사건을 넣는다. **여기서 `ESTOP` 전문을 만들지 않는다** — 로봇이 이미
        스스로 멈춘 상태를 호스트가 따라가는 것일 뿐이다. 사람이 누른 비상정지는
        송신기의 `emergency_stop()` 이 전문을 돌려준다.
        """
        return self._fsm.handle(event)

    def tick(self, now_ms: int) -> list[str]:
        """상태에 맞는 지시를 적용하고 송신기의 틱 결과를 돌려준다."""
        state = self._fsm.state
        directive = self._fsm.directive
        if directive is Directive.HALT:
            self._commander.halt()
        elif directive is Directive.SEQUENCE:
            sequence = self._sequences.get(state)
            if sequence is None:
                self._commander.halt()  # 3.5 미구현 — 안전측 기본값
            else:
                sequence(self._commander, now_ms)
        # YIELD 은 건드리지 않는다 — 조작자가 세운 의도를 살려둔다.
        self._commander.announce(state)
        return self._commander.tick(now_ms)
