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

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from host.behavior.commander import Commander
from host.common.protocol import ONBOARD_STATES

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
    PERSON_FOUND = "PERSON_FOUND"  # 300ms 시간 창 안에 3회 검출 (FR-3.2)
    TARGET_OFF_CENTER = "TARGET_OFF_CENTER"  # x축 편차 > 데드존 (FR-3.5)
    TARGET_CENTERED = "TARGET_CENTERED"  # 중앙 정렬 유지
    TARGET_LOST = "TARGET_LOST"  # 미검출 5s 지속 (FR-3.7)
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

#: **로봇의 안전 래치가 걸려 있는 동안 막는 사건** (WBS 3.4.3).
#:
#: `EXCLUSIVE` 는 상태로 막고 이쪽은 로봇의 보고로 막는다. 둘이 필요한 이유가 있다 —
#: `EXCLUSIVE` 만으로는 *"호스트가 FAILSAFE 를 떠나도 되는가"* 를 판단할 수 없다.
#: 그 답은 표에 없고 **로봇의 래치가 풀렸는지**에 있다.
#:
#: ⚠️ 이것이 없으면 다음이 성립한다. 로봇이 전도로 멈춰 있는데 조작자가 리셋을
#: 누르면 호스트만 `IDLE` 로 나오고, 같은 상태의 반복 보고는 사건을 재발행하지
#: 않으므로 **어떤 텔레메트리도 그 어긋남을 고치지 못한다.** 로봇은 잠겨 있어
#: 움직이지 않지만 **관제 화면이 거짓을 말한다.**
#:
#: ⚠️ **`state == "FAILSAFE"` 로 막으면 안 된다.** `FAILSAFE` 는 `STATE` 명령으로도
#: 내려가므로 로봇이 되돌려준 값이 *우리가 알려준 것의 반향*인지 *로봇 자신의
#: 판정*인지 구분할 수 없다. 반향으로 막으면 호스트가 스스로를 영구히 잠근다.
#: 규약이 `safety_latched` 를 둔 이유가 정확히 이것이다 (PROTOCOL 2절).
#:
#: 래치를 보고하지 않는(구형) 펌웨어에서는 막지 않는다. 알 수 없는 것을 근거로
#: 잠그면 해제 수단이 없어지고, 그 조합은 규약이 *"실기 운용 전 송수신을 함께
#: 갱신한다"* 로 이미 금지한 상태다.
LATCH_GUARDED: frozenset[Event] = frozenset({Event.RESET_CONFIRMED})

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

#: **대응 단계(3.8.3)를 올리지 않는 상태** — 순찰 임무 밖이다.
#:
#: ⚠️ **잠정이다 (2026-09-12 · 확정 전).** 단계 축은 FSM 과 직교하도록 설계됐지만
#: (아키텍처 3.1), 대기 중에는 `AUTH_WAIT` 로 갈 수 없어서 로봇 앞에 머물다 떠난
#: 사람이 곧바로 L3 경보가 됐다 — 시연 준비 중 팀원 때문에 빨간 경보가 뜬다.
#: 래치된 단계(L3·F)는 여기서도 그대로다. 확정되면 아키텍처 3.1 에 올린다.
STANDBY: frozenset[str] = frozenset({"IDLE", "MANUAL"})


@dataclass(frozen=True, slots=True)
class StateTimer:
    """상태에 머문 시간이 임계를 넘으면 사건을 낸다.

    `path` 는 `config.yaml` 에서 값을 읽을 경로이고 `unit_ms` 는 그 값의 단위다
    (설정 키가 `_s` 로 끝나므로 1000). **코드에 숫자를 두지 않기 위한 표기다.**
    """

    state: str
    event: Event
    path: tuple[str, ...]
    unit_ms: int = 1000


#: 상태 타이머. **표이며 분기문이 아니다** — `if state == "PATROL" and elapsed > 10`
#: 을 엔진에 넣으면 상태 이름이 다시 코드로 들어온다.
#:
#: ⚠️ **전도 2초는 여기 없다.** 그것은 온보드 Tier 1 이며(`3.2.3`) 호스트가
#: 흉내내면 로봇이 이미 토크를 뗀 뒤에 호스트가 또 판정하는 이중 판정이 된다.
#: 마찬가지로 명령 타임아웃 300ms 도 호스트의 일이 아니다 (아키텍처 1.2).
#:
#: ⚠️ **대상 상실 5초도 여기 없다.** 그것은 *상태에 머문 시간*이 아니라
#: *마지막 검출 이후 시간*이다. 상태 타이머로 만들면 사람이 계속 서 있어도
#: 5초마다 `ALERT` 를 떠난다. 그래서 `note_target()` 감시로 따로 둔다.
TIMERS: tuple[StateTimer, ...] = (
    StateTimer("PATROL", Event.SCAN_DUE, ("fsm", "patrol_scan_interval_s")),
    StateTimer("SCAN", Event.SCAN_DONE, ("fsm", "scan_duration_s")),
    StateTimer("AUTH_WAIT", Event.AUTH_FAILED, ("auth", "timeout_s")),
)

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
        self._exit_hooks: dict[str, list[Callable[[str, str], None]]] = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def directive(self) -> Directive:
        return DIRECTIVES[self._state]

    def on_enter(self, state: str, hook: Callable[[str, str], None]) -> None:
        """상태 진입 훅. 인자는 `(이전 상태, 새 상태)` 다."""
        self._hooks.setdefault(state, []).append(hook)

    def on_exit(self, state: str, hook: Callable[[str, str], None]) -> None:
        """상태 이탈 훅. 인자는 `(떠나는 상태, 갈 상태)` 다.

        **이탈 훅이 진입 훅보다 먼저 불린다.** 뒷정리가 새 상태의 준비보다 앞서야
        한다 — `SCAN` 을 떠날 때 스캔 타이머를 멈추지 않고 `PATROL` 진입에서
        순찰 타이머를 켜면, 두 타이머가 겹쳐 도는 순간이 생긴다.
        """
        self._exit_hooks.setdefault(state, []).append(hook)

    def _target_for(self, event: Event) -> str | None:
        """이 사건이 데려갈 상태. 전이가 없으면 `None`. **표에만 묻는다.**

        `can()` 과 `handle()` 이 같은 답을 쓰도록 조회를 여기 한 곳에 모았다.
        따로 쓰면 한쪽만 고쳐지는 날이 온다.
        """
        allowed = EXCLUSIVE.get(self._state)
        if allowed is not None and event not in allowed:
            return None
        target = self._table.get((self._state, event)) or self._table.get((ANY, event))
        if target is None or target == self._state:
            return None
        return target

    def can(self, event: Event) -> bool:
        """지금 이 사건이 전이를 만드는가. **감시자가 헛발질을 줄이는 데 쓴다.**

        예를 들어 대상 상실 감시는 `ALERT`·`TRACK` 에서만 의미가 있는데, 그 판단을
        상태 이름으로 하면 감시자 안에 상태 이름이 생긴다. 표에 물으면 안 생긴다.
        """
        return self._target_for(event) is not None

    def handle(self, event: Event) -> bool:
        """전이했으면 `True`. **모르는 사건은 조용히 무시한다.**

        예외를 내지 않는 이유 — 사건은 바깥(텔레메트리·비전·키보드)에서 오고,
        지금 상태와 무관한 사건이 오는 것은 정상이다. `FAILSAFE` 중에
        `TARGET_LOST` 가 와도 그건 버그가 아니다.
        """
        target = self._target_for(event)
        if target is None:
            return False
        previous, self._state = self._state, target
        for hook in self._exit_hooks.get(previous, ()):
            hook(previous, target)
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

    def __init__(
        self,
        commander: Commander,
        fsm: Fsm | None = None,
        *,
        link_loss_ms: int = 3000,
        vision_stall_ms: int = 2000,
        timers: Mapping[str, tuple[Event, int]] | None = None,
        target_lost_ms: int | None = None,
    ) -> None:
        if link_loss_ms <= 0 or vision_stall_ms <= 0:
            raise ValueError("타임아웃은 1 이상이어야 함")
        if target_lost_ms is not None and target_lost_ms <= 0:
            raise ValueError("타임아웃은 1 이상이어야 함")
        self._commander = commander
        self._fsm = fsm if fsm is not None else Fsm()
        self._sequences: dict[str, Sequence] = {}
        self._link_loss_ms = link_loss_ms
        self._vision_stall_ms = vision_stall_ms
        self._last_telemetry_ms: int | None = None
        self._last_vision_ms: int | None = None
        self._degraded = False
        # ⚠️ 타이머 기본값이 "없음" 인 것은 의도다. 생성자 기본값은 시험 편의용이고
        # 실제 운용은 `behavior_from_config` 로 만든다 — 코드에 숫자를 박으면
        # `config.yaml` 을 고쳐도 동작이 안 바뀐다 (NFR-3①).
        self._timers: dict[str, tuple[Event, int]] = dict(timers or {})
        self._target_lost_ms = target_lost_ms
        self._last_target_ms: int | None = None
        self._onboard_state: str | None = None
        self._robot_latched: bool | None = None
        self._known_state = self._fsm.state
        self._last_trigger: Event | None = None
        self._state_since_ms: int | None = None
        self._fired: set[str] = set()

    # ── 두 링크는 다르게 대응한다 (NFR-2.6) ────────────────────
    #
    # 로봇 링크 두절은 **안전 문제**다 — 명령이 닿지 않으므로 페일세이프로 간다.
    # 비전 단절은 **기능 저하**다 — 순찰·회피는 계속하고 사람 인지만 끈다.
    #
    # 둘을 같은 사건으로 묶으면 **카메라가 딸꾹질할 때마다 로봇이 멈춘다.**
    # 반대로 로봇 링크를 저하로 취급하면 명령이 닿지 않는 채로 계속 걸어간다.
    # 그래서 타임아웃도 다른 설정 절에 둔다 — `safety` vs `vision`.

    def note_telemetry(self, now_ms: int) -> None:
        """로봇이 살아 있다는 증거. 텔레메트리를 받을 때마다 부른다."""
        self._last_telemetry_ms = now_ms

    def note_vision(self, now_ms: int) -> None:
        """비전 프레임이 왔다. 받을 때마다 부른다."""
        self._last_vision_ms = now_ms
        self._degraded = False

    def note_vision_stalled(self) -> None:
        """비전 워커가 단절·중단을 확정했다. 인지만 기능 저하로 표시한다."""
        self._degraded = True

    def note_target(self, now_ms: int) -> None:
        """대상이 보인다. **검출될 때마다** 부른다 (FR-3.7).

        대상 상실은 *상태에 머문 시간*이 아니라 *마지막 검출 이후 시간*이다.
        그래서 링크 감시와 같은 모양이고 상태 타이머와는 다르다.
        """
        self._last_target_ms = now_ms

    def note_onboard_state(self, state: str) -> None:
        """로봇이 보고한 **온보드 상태**를 기억한다. **표시용이며 가드의 근거가 아니다.**

        호스트 전용 상태(`ALERT`·`TRACK` 등)는 담지 않는다. 그것은 우리가 `STATE`
        로 내려보낸 값이 되돌아온 것이라 판단 근거가 아니다 — 수신기가 사건을
        만들지 않는 것과 같은 이유다.
        """
        if state in ONBOARD_STATES:
            self._onboard_state = state

    def note_robot_latch(self, latched: bool | None) -> None:
        """로봇의 안전 래치 보고. **`LATCH_GUARDED` 가 보는 유일한 값이다.**"""
        self._robot_latched = latched

    @property
    def onboard_state(self) -> str | None:
        """로봇이 마지막으로 보고한 온보드 상태. 대시보드가 어긋남을 보이는 데 쓴다."""
        return self._onboard_state

    @property
    def robot_latched(self) -> bool | None:
        """로봇의 안전 래치. `None` 이면 아직 보고를 못 받았거나 구형 펌웨어다."""
        return self._robot_latched

    @property
    def degraded(self) -> bool:
        """**비전만 죽은 상태.** 순찰은 계속되며 사람 인지가 비활성이다."""
        return self._degraded

    @property
    def standby(self) -> bool:
        """순찰 임무 밖인가 (`STANDBY` · 잠정). 대응 단계를 올리지 않는다."""
        return self._fsm.state in STANDBY

    def _watch_links(self, now_ms: int) -> None:
        # 아직 한 번도 못 받았으면 감시하지 않는다 — 기동 직후를 두절로 보면
        # 켜는 순간 페일세이프가 걸린다.
        if (
            self._last_telemetry_ms is not None
            and now_ms - self._last_telemetry_ms >= self._link_loss_ms
        ):
            self._handle(Event.LINK_LOST, now_ms)
        if (
            self._last_vision_ms is not None
            and now_ms - self._last_vision_ms >= self._vision_stall_ms
        ):
            self._degraded = True

    def _watch_target(self, now_ms: int) -> None:
        """마지막 검출 이후 임계가 지나면 `TARGET_LOST` (FR-3.7).

        **지금 상태에서 그 사건이 전이를 만들 때만 본다** — 표에 물어보므로
        여기에 `ALERT`·`TRACK` 같은 이름이 들어오지 않는다.
        """
        if self._target_lost_ms is None or self._last_target_ms is None:
            return
        if not self._fsm.can(Event.TARGET_LOST):
            return
        if now_ms - self._last_target_ms < self._target_lost_ms:
            return
        if self._handle(Event.TARGET_LOST, now_ms):
            # 이미 반영했으므로 다음 검출까지 다시 재지 않는다.
            self._last_target_ms = None

    def _watch_timers(self, now_ms: int) -> None:
        """상태에 머문 시간이 임계를 넘으면 사건을 낸다 (FR-2.4)."""
        entry = self._timers.get(self._fsm.state)
        if entry is None or self._state_since_ms is None:
            return
        if self._fsm.state in self._fired:
            return
        event, after_ms = entry
        if now_ms - self._state_since_ms < after_ms:
            return
        # ⚠️ **한 번만 발화한다.** 전이가 막혀 있으면(가드·봉인) 매 틱마다 같은
        # 사건을 다시 내게 되고, 그러면 로그가 초당 10건씩 쌓인다.
        self._fired.add(self._fsm.state)
        self._handle(event, now_ms)

    def _mark_state(self, now_ms: int | None) -> None:
        """상태가 바뀌었으면 진입 시각을 새로 적고 발화 기록을 비운다."""
        if self._fsm.state == self._known_state:
            return
        self._known_state = self._fsm.state
        self._state_since_ms = now_ms  # None 이면 다음 틱이 채운다
        self._fired.clear()

    def _handle(self, event: Event, now_ms: int | None) -> bool:
        changed = self._fsm.handle(event)
        if changed:
            self._last_trigger = event
            self._mark_state(now_ms)
        return changed

    @property
    def last_trigger(self) -> Event | None:
        """마지막 전이를 일으킨 사건. **로그의 `trigger` 가 이 값이다.**

        트리거 없는 전이 로그는 *"왜 그때 그 판단을 했는가"* 에 답하지 못한다 —
        그것이 로깅 설계의 목적이고 규칙 기반 FSM 을 고른 실질적 근거다 (DR-9).
        """
        return self._last_trigger

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

    def event(self, event: Event, now_ms: int | None = None) -> bool:
        """사건을 넣는다. **여기서 `ESTOP` 전문을 만들지 않는다** — 로봇이 이미
        스스로 멈춘 상태를 호스트가 따라가는 것일 뿐이다. 사람이 누른 비상정지는
        송신기의 `emergency_stop()` 이 전문을 돌려준다.

        `now_ms` 를 주면 상태 진입 시각이 정확해진다. 생략하면 다음 틱이 채우므로
        타이머가 최대 한 주기(100ms) 늦게 시작한다 — 10초 타이머에는 무해하지만
        **운용 루프는 반드시 넘긴다.**
        """
        if event in LATCH_GUARDED and self._robot_latched is True:
            return False
        return self._handle(event, now_ms)

    def tick(self, now_ms: int) -> list[str]:
        """링크를 살피고, 상태에 맞는 지시를 적용하고, 송신기의 틱 결과를 돌려준다.

        **링크 감시가 지시 적용보다 먼저다.** 두절을 이번 틱에 반영해야 그 틱의
        명령이 페일세이프 상태에 맞는다. 나중에 보면 한 주기 동안 낡은 상태의
        명령이 나간다.
        """
        if self._state_since_ms is None:
            self._state_since_ms = now_ms
        self._mark_state(now_ms)  # 시각 없이 들어온 사건을 여기서 보정한다
        self._watch_links(now_ms)  # 안전이 먼저
        self._watch_target(now_ms)
        self._watch_timers(now_ms)
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


def _lookup(config: Mapping[str, Any], path: tuple[str, ...]) -> int:
    """설정 경로를 따라가 정수를 꺼낸다. **없으면 조용히 넘기지 않는다.**

    빠진 키를 기본값으로 때우면 `config.yaml` 에서 항목을 지워도 동작이 그대로라
    설정이 정본이 아니게 된다. 그래서 `KeyError` 를 그대로 올린다.
    """
    node: Any = config
    for key in path:
        node = node[key]
    return int(node)


def behavior_from_config(
    commander: Commander,
    config: Mapping[str, Any],
    fsm: Fsm | None = None,
) -> Behavior:
    """설정에서 두 타임아웃을 읽어 `Behavior` 를 만든다.

    **생성자 기본값은 시험 편의용이고 실제 운용은 이 함수를 쓴다.** 코드에 박힌
    숫자를 정본으로 두면 `config.yaml` 을 고쳐도 동작이 안 바뀐다 (NFR-3①).

    두 값이 서로 다른 절에서 오는 것에 주의한다 — `safety` 는 안전 임계이고
    `vision` 은 기능 저하 임계다. 같은 절에 두면 언젠가 같은 대응으로 묶인다.
    """
    timers: dict[str, tuple[Event, int]] = {}
    for timer in TIMERS:
        timers[timer.state] = (timer.event, _lookup(config, timer.path) * timer.unit_ms)
    return Behavior(
        commander,
        fsm,
        link_loss_ms=int(config["safety"]["link_loss_failsafe_ms"]),
        vision_stall_ms=int(config["vision"]["stall_timeout_ms"]),
        timers=timers,
        target_lost_ms=_lookup(config, ("fsm", "target_lost_timeout_s")) * 1000,
    )
