"""행동 상태 머신 — 전이표 13상태 (아키텍처 3절).

전이는 데이터(표)이고 엔진은 그 표만 본다 — 엔진 본문에는 상태 이름이 없다. 상태나
전이를 추가할 때 엔진은 손대지 않는다 (ADR-14).

    IDLE ──START_PATROL──▶ PATROL ⇄ SCAN · AVOID · ZONE_INSPECT
                              │
                              ├── PERSON_FOUND ──▶ ALERT ⇄ TRACK
                              │                     └── AUTH_REQUIRED ──▶ AUTH_WAIT
                              └── HAZARD_ALARM ──▶ HAZARD_DISPATCH ──▶ HAZARD_SCAN

    어느 상태에서든 ── ONBOARD_FAILSAFE · LINK_LOST · ESTOP ──▶ FAILSAFE
                   ── MANUAL_ON ──▶ MANUAL
                   ── POSE_STALE `[P2]` ──▶ LOST

⚠️ FAILSAFE 는 `RESET_CONFIRMED` 외의 사건을 받지 않고, 로봇이 래치를 보고하는 동안에는 그것도
막는다(`LATCH_GUARDED`) — 자동 복귀는 없다 (ADR-21).

전이표에 없는 것 — 명령 타임아웃 600ms(온보드 Tier 1)와 `ALERT` 에서의 PPE 위반(단계
축 `behavior/escalation.py` 소관)이다 (아키텍처 3절).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from host.behavior.commander import Commander

# `Event` 는 텔레메트리 수신(`host.telemetry`)도 쓰므로 아래 계층에 둔다. 옛 경로로도 읽히게 다시 내보낸다.
from host.common.events import Event as Event
from host.common.protocol import ONBOARD_STATES

#: 모든 상태에서 받는 전이의 출발지 표기.
ANY = "*"


class Directive(StrEnum):
    """상태가 송신기에 요구하는 것 — 행동이 아니라 누가 정하느냐다."""

    HALT = "HALT"  # 정지를 계속 보낸다
    YIELD = "YIELD"  # 조작자가 정한다. FSM 은 손대지 않는다
    SEQUENCE = "SEQUENCE"  # 그 상태 전용 모션 시퀀스가 정한다 (3.5 소관)


@dataclass(frozen=True, slots=True)
class Transition:
    src: str
    event: Event
    dst: str


#: 전이표. 아키텍처 3절의 표와 일치해야 하며 `tests/test_fsm.py` 가 상태 이름을
#: `protocol.FSM_STATES` 와 대조한다.
TRANSITIONS: tuple[Transition, ...] = (
    # ── 순찰 루프 ──
    Transition("IDLE", Event.START_PATROL, "PATROL"),
    Transition("PATROL", Event.SCAN_DUE, "SCAN"),
    Transition("SCAN", Event.SCAN_DONE, "PATROL"),
    Transition("PATROL", Event.GOAL_UNREACHABLE, "IDLE"),
    Transition("SCAN", Event.GOAL_UNREACHABLE, "IDLE"),
    Transition("AVOID", Event.GOAL_UNREACHABLE, "IDLE"),
    Transition("LOST", Event.GOAL_UNREACHABLE, "IDLE"),
    # ── 온보드 반사를 호스트가 따라간다 (FAILSAFE 와 같은 방식) ──
    Transition("PATROL", Event.ONBOARD_AVOID, "AVOID"),
    Transition("AVOID", Event.AVOID_CLEARED, "PATROL"),
    # ── 사람 대응 ──
    Transition("PATROL", Event.PERSON_FOUND, "ALERT"),
    Transition("ALERT", Event.TARGET_OFF_CENTER, "TRACK"),
    Transition("TRACK", Event.TARGET_CENTERED, "ALERT"),
    Transition("ALERT", Event.TARGET_LOST, "PATROL"),
    Transition("TRACK", Event.TARGET_LOST, "PATROL"),
    # `ALERT` 에서의 PPE 위반은 상태가 그대로이므로 표에 없다 — 에스컬레이션(`behavior/escalation.py`) 소관
    Transition("TRACK", Event.PPE_VIOLATION, "ALERT"),
    # 공장 모드의 순찰 복귀 (FR-11.6 · ADR-33 규칙 4). 모드 게이트는 `mission.py` 가 건다.
    Transition("ALERT", Event.PPE_SETTLED, "PATROL"),
    # 공장 쓰러짐 의심·해제. 모드 게이트는 `mission.py` 의 `fallen` 이다.
    Transition("PATROL", Event.FALL_SUSPECTED, "ALERT"),
    Transition("ZONE_INSPECT", Event.FALL_SUSPECTED, "ALERT"),
    Transition("ALERT", Event.FALL_RESOLVED, "PATROL"),
    Transition("TRACK", Event.FALL_RESOLVED, "PATROL"),
    Transition("ALERT", Event.ALARM_CONFIRMED, "PATROL"),
    Transition("TRACK", Event.ALARM_CONFIRMED, "PATROL"),
    Transition("AUTH_WAIT", Event.ALARM_CONFIRMED, "PATROL"),
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
    # 구역 변화의 `ALERT` 는 사람이 없으면 대상 상실이 걸리지 않으므로, 경보 확인으로
    # 순찰에 돌아간다 (FR-8.4).
    Transition("ALERT", Event.ZONE_ALARM_CONFIRMED, "PATROL"),
    # 구역 앞의 사람은 물체 변화가 아니라 사람 대응이다 (FR-8.3 · ADR-41 결정 5).
    Transition("ZONE_INSPECT", Event.PERSON_FOUND, "ALERT"),
)

#: 이 상태에서는 나열된 사건만 받는다 — `FAILSAFE` 를 데이터로 봉인한다.
EXCLUSIVE: dict[str, frozenset[Event]] = {
    "FAILSAFE": frozenset({Event.RESET_CONFIRMED}),
}

#: 로봇의 안전 래치(`safety_latched`)가 보고되는 동안 막는 사건 (ADR-21).
#:
#: ⚠️ 텔레메트리 `state == "FAILSAFE"` 로 막지 않는다 — 우리가 `STATE` 로 보낸 값의 반향일
#: 수 있어 호스트가 스스로를 영구히 잠근다. 래치를 보고하지 않는 펌웨어에서는 막지 않는다.
LATCH_GUARDED: frozenset[Event] = frozenset({Event.RESET_CONFIRMED})

#: 상태별 지시. 모든 상태가 여기 있어야 한다.
DIRECTIVES: dict[str, Directive] = {
    "IDLE": Directive.HALT,
    "MANUAL": Directive.YIELD,
    "FAILSAFE": Directive.HALT,
    "LOST": Directive.HALT,  # 즉시 정지 후 재측위 대기
    # 정지한 채 경계 자세를 잡는다 — 사원증을 읽어야 하는 구간이다.
    "AUTH_WAIT": Directive.SEQUENCE,  # 정지한 채 인증을 기다린다 + 경계 자세
    "PATROL": Directive.SEQUENCE,  # 순찰 행동
    "AVOID": Directive.SEQUENCE,  # 후진하며 선회 (ADR-29)
    "SCAN": Directive.SEQUENCE,  # 상체 스캔
    "ALERT": Directive.SEQUENCE,  # Pitch Up 경계 자세
    "TRACK": Directive.SEQUENCE,  # 선회 보행 추종
    "HAZARD_DISPATCH": Directive.SEQUENCE,  # 웨이포인트 추종
    "HAZARD_SCAN": Directive.SEQUENCE,
    "ZONE_INSPECT": Directive.SEQUENCE,
}

INITIAL = "IDLE"

#: 대응 단계를 올리지 않는 순찰 임무 밖 상태 (잠정). 대기 중에는 `AUTH_WAIT` 로 갈 수
#: 없어 앞을 지난 사람이 L3 가 되기 때문이다. 래치된 L3·F 는 그대로다.
STANDBY: frozenset[str] = frozenset({"IDLE", "MANUAL"})


@dataclass(frozen=True, slots=True)
class StateTimer:
    """상태에 머문 시간이 임계를 넘으면 사건을 낸다.

    `path` 는 `config.yaml` 의 값 경로이고 `unit_ms` 는 그 값의 단위다 (`_s` 키는 1000).
    """

    state: str
    event: Event
    path: tuple[str, ...]
    unit_ms: int = 1000


#: 상태 체류 타이머 표. 온보드 Tier 1 의 시간(명령 타임아웃 등)은 두지 않는다 (아키텍처 1.2).
#: 대상 상실 5초는 체류 시간이 아니라 마지막 검출 이후 시간이므로 `note_target()` 이 잰다.
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
        self._exit_hooks: dict[str, list[Callable[[str, str], object]]] = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def directive(self) -> Directive:
        return DIRECTIVES[self._state]

    def on_enter(self, state: str, hook: Callable[[str, str], None]) -> None:
        """상태 진입 훅. 인자는 `(이전 상태, 새 상태)` 다."""
        self._hooks.setdefault(state, []).append(hook)

    def on_exit(self, state: str, hook: Callable[[str, str], object]) -> None:
        """상태 이탈 훅. 인자는 `(떠나는 상태, 갈 상태)` 다. 진입 훅보다 먼저 불린다."""
        self._exit_hooks.setdefault(state, []).append(hook)

    def _target_for(self, event: Event) -> str | None:
        """이 사건이 데려갈 상태. 전이가 없으면 `None`. `can()`·`handle()` 의 공통 조회다."""
        allowed = EXCLUSIVE.get(self._state)
        if allowed is not None and event not in allowed:
            return None
        target = self._table.get((self._state, event)) or self._table.get((ANY, event))
        if target is None or target == self._state:
            return None
        return target

    def can(self, event: Event) -> bool:
        """지금 이 사건이 전이를 만드는가 — 감시자가 상태 이름 없이 표에 묻는 데 쓴다."""
        return self._target_for(event) is not None

    def handle(self, event: Event) -> bool:
        """전이했으면 `True`. 지금 상태와 무관한 사건은 정상이므로 조용히 무시한다."""
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
    """FSM 상태를 송신기의 의도로 옮기는 유일한 지점. 링크·대상·상태 타이머를 감시한다.

    시퀀스가 등록되지 않은 `SEQUENCE` 상태는 정지로 처리한다. 내부 잠금이 없다 — 운용 루프와
    대시보드 스레드(`Runtime.apply_external`) 양쪽에서 사건이 들어온다.
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
        hold_on_latched_alarm: bool = False,
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
        # 타이머 기본값은 «없음» 이다 — 운용은 `behavior_from_config` 로 만든다 (NFR-3①).
        self._timers: dict[str, tuple[Event, int]] = dict(timers or {})
        self._target_lost_ms = target_lost_ms
        self._last_target_ms: int | None = None
        self._onboard_state: str | None = None
        self._robot_latched: bool | None = None
        self._known_state = self._fsm.state
        self._last_trigger: Event | None = None
        self._state_since_ms: int | None = None
        self._fired: set[str] = set()
        # 이번 체류에서 타이머 마감을 미룬 총합. 상태가 바뀌면 0 이 된다 (`_mark_state`).
        self._timer_deferred_ms = 0
        self._hold_on_latched_alarm = hold_on_latched_alarm
        self._alarm_latched: Callable[[], bool] = lambda: False

    def set_alarm_guard(self, latched: Callable[[], bool]) -> None:
        self._alarm_latched = latched

    @property
    def alarm_holding(self) -> bool:
        return self._hold_on_latched_alarm and self._alarm_latched()

    # ── 두 링크는 다르게 대응한다 (NFR-2.6) ────────────────────
    #
    # 로봇 링크 두절은 안전 문제(→ FAILSAFE), 비전 단절은 기능 저하(순찰·회피는 계속,
    # 사람 인지만 끈다)다. 타임아웃도 다른 설정 절(`safety` · `vision`)에 둔다.

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
        """대상이 보인다. 검출될 때마다 부른다 — 상실은 마지막 검출 이후 시간이다 (FR-3.7)."""
        self._last_target_ms = now_ms

    def note_onboard_state(self, state: str) -> None:
        """로봇이 보고한 온보드 상태(`ONBOARD_STATES` 만)를 기억한다. 표시용이며 가드 근거가 아니다."""
        if state in ONBOARD_STATES:
            self._onboard_state = state

    def note_robot_latch(self, latched: bool | None) -> None:
        """로봇의 안전 래치 보고 — `LATCH_GUARDED` 가 보는 유일한 값이다."""
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
        """비전만 죽은 상태. 순찰은 계속되며 사람 인지가 비활성이다."""
        return self._degraded

    @property
    def standby(self) -> bool:
        """순찰 임무 밖인가 (`STANDBY` · 잠정). 대응 단계를 올리지 않는다."""
        return self._fsm.state in STANDBY

    @property
    def tracking(self) -> bool:
        """추종 지시를 만들 자리인가 (`ALERT`·`TRACK` — `ALERT` 가 `TARGET_OFF_CENTER` 를 낸다)."""
        return self._fsm.state in {"ALERT", "TRACK"}

    def _watch_links(self, now_ms: int) -> None:
        # 한 번도 못 받았으면 감시하지 않는다 — 기동 직후를 두절로 보지 않는다.
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
        """마지막 검출 이후 임계가 지나면 `TARGET_LOST` (FR-3.7). 전이가 될 때만 낸다."""
        if self._target_lost_ms is None or self._last_target_ms is None:
            return
        if not self._fsm.can(Event.TARGET_LOST):
            return
        if now_ms - self._last_target_ms < self._target_lost_ms:
            return
        if self._handle(Event.TARGET_LOST, now_ms):
            # 이미 반영했으므로 다음 검출까지 다시 재지 않는다.
            self._last_target_ms = None

    def defer_timer(self, *, by_ms: int, cap_ms: int) -> int:
        """이번 체류의 상태 타이머 마감을 체류 누계 `cap_ms` 안에서 미룬다. 실제로 미룬 ms.

        상한이 있어야 신호만 반복해 경보를 영영 막는 길이 없다. 어느 상태인지는 묻지 않는다
        — 정책은 부르는 쪽(`VoiceAuthWindow`)에 있다 (ADR-37 결정 2·4).
        """
        if by_ms <= 0 or cap_ms <= 0:
            return 0
        granted = min(by_ms, cap_ms - self._timer_deferred_ms)
        if granted <= 0:
            return 0
        self._timer_deferred_ms += granted
        return granted

    @property
    def timer_deferred_ms(self) -> int:
        """이번 체류에서 미룬 총합. 시험과 로그가 본다."""
        return self._timer_deferred_ms

    def _watch_timers(self, now_ms: int) -> None:
        """상태에 머문 시간이 임계를 넘으면 사건을 낸다 (FR-2.4)."""
        entry = self._timers.get(self._fsm.state)
        if entry is None or self._state_since_ms is None:
            return
        if self._fsm.state in self._fired:
            return
        event, after_ms = entry
        if now_ms - self._state_since_ms < after_ms + self._timer_deferred_ms:
            return
        # 체류당 한 번만 발화한다 — 전이가 막혀 있어도 매 틱 다시 내지 않는다.
        self._fired.add(self._fsm.state)
        self._handle(event, now_ms)

    def _mark_state(self, now_ms: int | None) -> None:
        """상태가 바뀌었으면 진입 시각을 새로 적고 발화 기록을 비운다."""
        if self._fsm.state == self._known_state:
            return
        self._known_state = self._fsm.state
        self._state_since_ms = now_ms  # None 이면 다음 틱이 채운다
        self._fired.clear()
        self._timer_deferred_ms = 0

    def _handle(self, event: Event, now_ms: int | None) -> bool:
        if self.alarm_holding and event not in {
            Event.ONBOARD_FAILSAFE,
            Event.LINK_LOST,
            Event.ESTOP,
            Event.RESET_CONFIRMED,
            Event.MANUAL_ON,
            Event.MANUAL_OFF,
        }:
            return False
        changed = self._fsm.handle(event)
        if changed:
            self._last_trigger = event
            self._mark_state(now_ms)
        return changed

    @property
    def last_trigger(self) -> Event | None:
        """마지막 전이를 일으킨 사건 — 전이 로그의 `trigger` 다 (ADR-14)."""
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

    def sequence_for(self, state: str) -> Sequence | None:
        """등록된 시퀀스를 돌려준다(추종처럼 바깥에서 지시를 넣는 용도). 없으면 `None`."""
        return self._sequences.get(state)

    def event(self, event: Event, now_ms: int | None = None) -> bool:
        """사건을 넣고 전이했으면 `True`. `LATCH_GUARDED` 사건은 로봇 래치 중 거부한다.

        전문을 만들지 않는다(비상정지 전문은 `Commander.emergency_stop()`). `now_ms` 를
        생략하면 다음 틱이 진입 시각을 채운다 — 운용 루프는 넘긴다.
        """
        if event in LATCH_GUARDED and self._robot_latched is True:
            return False
        return self._handle(event, now_ms)

    def tick(self, now_ms: int) -> list[str]:
        """링크를 살피고, 상태에 맞는 지시를 적용하고, 송신기의 틱 결과를 돌려준다.

        링크 감시가 지시 적용보다 먼저다 — 두절이 이번 틱의 명령에 반영된다.
        """
        if self._state_since_ms is None:
            self._state_since_ms = now_ms
        self._mark_state(now_ms)  # 시각 없이 들어온 사건을 여기서 보정한다
        self._watch_links(now_ms)  # 안전이 먼저
        self._watch_target(now_ms)
        self._watch_timers(now_ms)
        state = self._fsm.state
        directive = self._fsm.directive
        if self.alarm_holding or directive is Directive.HALT:
            self._commander.halt()
        elif directive is Directive.SEQUENCE:
            sequence = self._sequences.get(state)
            if sequence is None:
                self._commander.halt()  # 3.5 미구현 — 안전측 기본값
            else:
                sequence(self._commander, now_ms)
        # YIELD 은 건드리지 않는다 — 조작자가 세운 의도를 살려둔다.
        # A sequence may end a navigation mission during this tick.
        self._commander.announce(self._fsm.state)
        return self._commander.tick(now_ms)


def _lookup(config: Mapping[str, Any], path: tuple[str, ...]) -> int:
    """설정 경로를 따라가 정수를 꺼낸다. 빠진 키는 `KeyError` 로 올린다(기본값 없음)."""
    node: Any = config
    for key in path:
        node = node[key]
    return int(node)


def behavior_from_config(
    commander: Commander,
    config: Mapping[str, Any],
    fsm: Fsm | None = None,
) -> Behavior:
    """설정에서 타임아웃과 상태 타이머를 읽어 운용용 `Behavior` 를 만든다 (NFR-3①).

    링크 두절은 `safety`, 비전 단절은 `vision` 절에서 읽는다 — 안전 임계와 기능 저하 임계다.
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
        hold_on_latched_alarm=bool(config["fsm"].get("hold_on_latched_alarm", False)),
    )
