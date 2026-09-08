"""전이표 13상태 검증 (WBS 3.4.1 · 아키텍처 3절).

전이가 테이블이므로 **표를 전수로 돌려볼 수 있다.** 분기문으로 흩어놓으면
"어떤 조합이 남았는지" 를 세는 것 자체가 불가능해진다.
"""

import json
import re

import pytest
from conftest import ROOT, FakeClock

from host.behavior.commander import HALT, Commander
from host.behavior.fsm import (
    ANY,
    DIRECTIVES,
    EXCLUSIVE,
    TRANSITIONS,
    Behavior,
    Directive,
    Event,
    Fsm,
    Transition,
)
from host.common import protocol as p


# ── 표 자체의 구조 ────────────────────────────────────────────
def test_table_covers_exactly_the_protocol_state_set() -> None:
    """**상태 이름의 정본은 규약이다.**

    여기서 임의의 이름을 쓰면 `STATE` 명령이 수신측에서 폐기되고 텔레메트리의
    `state` 도 채울 수 없다 — 대시보드에 표시할 수 없는 상태가 된다.
    """
    assert set(DIRECTIVES) == set(p.FSM_STATES), (
        f"지시 누락 {set(p.FSM_STATES) - set(DIRECTIVES)} · "
        f"규약에 없는 상태 {set(DIRECTIVES) - set(p.FSM_STATES)}"
    )
    for t in TRANSITIONS:
        assert t.dst in DIRECTIVES, f"모르는 도착 상태: {t.dst}"
        assert t.src == ANY or t.src in DIRECTIVES, f"모르는 출발 상태: {t.src}"


def test_code_table_matches_the_architecture_document() -> None:
    """**문서의 전이표와 코드의 전이표가 정확히 같아야 한다.**

    이것이 `3.4.1` 의 요점이다. 표를 데이터로 두는 이유는 문서와 코드가 각각
    진화하는 것을 막기 위해서고, 그 대조를 사람이 아니라 CI 가 한다.

    문서가 `ALERT / TRACK` 처럼 두 출발지를 한 행에 적으므로 행 수는 다를 수
    있다. **`(출발, 도착)` 쌍의 집합**으로 비교하는 것이 옳다.

    호스트 전이가 아닌 행은 문서에 `전이표에 없다` 로 표시돼 있고 제외한다 —
    온보드 300ms 타임아웃과 `ALERT` 의 PPE 위반(에스컬레이션) 둘이다.
    """
    doc = ROOT / "docs" / "ARCHITECTURE.md"
    body = doc.read_text(encoding="utf-8")
    table = body[body.index("## 3. 행동 상태 전이표") : body.index("### 3.1 대응 에스컬레이션")]

    documented: set[tuple[str, str]] = set()
    for line in table.splitlines():
        if not line.startswith("|") or line.startswith("| :---") or "현재 상태" in line:
            continue
        if "전이표에 없다" in line:
            continue
        cells = line.strip().strip("|").split("|")
        if len(cells) < 3:
            continue
        sources = re.findall(r"`([A-Z][A-Z_]{2,})`", cells[0]) or (
            [ANY] if "ANY" in cells[0] else []
        )
        targets = re.findall(r"`([A-Z][A-Z_]{2,})`", cells[2])
        documented.update((s, d) for s in sources for d in targets)

    in_code = {(t.src, t.dst) for t in TRANSITIONS}
    assert documented == in_code, (
        f"코드에만 있음: {sorted(in_code - documented)} / "
        f"문서에만 있음: {sorted(documented - in_code)}"
    )


def test_no_state_is_a_dead_end() -> None:
    """**어느 상태든 정상 경로로 나갈 길이 있어야 한다.**

    안전 전이(`ANY` → `FAILSAFE`)는 어느 상태에나 있으므로 그것만으로는 판정할 수
    없다. **자기 상태에서 출발하는 명시적 전이**가 하나라도 있어야 한다.

    이 시험이 실제로 문서의 구멍 셋을 잡아냈다 — 아키텍처 3절 전이표에
    `MANUAL`·`LOST`·`HAZARD_SCAN` 에서 나가는 행이 없었다.
    """
    for state in DIRECTIVES:
        exits = [t for t in TRANSITIONS if t.src == state and t.dst != state]
        assert exits, f"{state} 에서 나가는 명시적 전이가 없다 (막힌 상태)"


def test_onboard_states_are_reachable_the_same_way_failsafe_is() -> None:
    """온보드가 스스로 판정하는 상태는 **호스트가 보고를 받아 따라간다.**

    `AVOID` 도 `FAILSAFE` 와 같은 방식이다 — 호스트가 명령해서 들어가는 것이
    아니라 로봇이 이미 멈춘 것을 따라간다.
    """
    onboard = {"PATROL", "AVOID", "FAILSAFE"}
    assert onboard <= set(p.ONBOARD_STATES) | {"AVOID"}
    events = {t.event for t in TRANSITIONS if t.dst == "AVOID"}
    assert events == {Event.ONBOARD_AVOID}, "AVOID 진입은 온보드 보고로만 일어나야 한다"


def test_duplicate_transition_is_rejected_at_construction() -> None:
    """같은 (상태, 사건) 이 두 줄이면 어느 쪽이 이기는지 표만 보고 알 수 없다."""
    with pytest.raises(ValueError, match="중복"):
        Fsm(
            transitions=(
                Transition("IDLE", Event.START_PATROL, "PATROL"),
                Transition("IDLE", Event.START_PATROL, "FAILSAFE"),
            )
        )


def test_initial_state_must_have_a_directive() -> None:
    with pytest.raises(ValueError, match="지시가 정의되지 않은"):
        Fsm(initial="NOT_A_STATE")


# ── 전이 동작 ─────────────────────────────────────────────────
def test_patrol_loop_round_trips() -> None:
    f = Fsm()
    assert f.handle(Event.START_PATROL) and f.state == "PATROL"
    assert f.handle(Event.SCAN_DUE) and f.state == "SCAN"
    assert f.handle(Event.SCAN_DONE) and f.state == "PATROL"
    assert f.handle(Event.ONBOARD_AVOID) and f.state == "AVOID"
    assert f.handle(Event.AVOID_CLEARED) and f.state == "PATROL"


def test_person_response_loop() -> None:
    f = Fsm(initial="PATROL")
    assert f.handle(Event.PERSON_FOUND) and f.state == "ALERT"
    assert f.handle(Event.TARGET_OFF_CENTER) and f.state == "TRACK"
    assert f.handle(Event.TARGET_CENTERED) and f.state == "ALERT"
    assert f.handle(Event.AUTH_REQUIRED) and f.state == "AUTH_WAIT"
    assert f.handle(Event.AUTH_FAILED) and f.state == "ALERT"
    assert f.handle(Event.TARGET_LOST) and f.state == "PATROL"


def test_ppe_violation_only_transitions_from_track() -> None:
    """`ALERT` 에서의 PPE 위반은 **상태가 바뀌지 않는다** — 에스컬레이션 소관.

    표에 넣지 않은 것이 의도적이라는 것을 여기서 고정한다. `TRACK` 에서는
    경계 자세로 돌아가야 하므로 실제 전이다.
    """
    track = Fsm(initial="TRACK")
    assert track.handle(Event.PPE_VIOLATION) and track.state == "ALERT"
    alert = Fsm(initial="ALERT")
    assert alert.handle(Event.PPE_VIOLATION) is False
    assert alert.state == "ALERT"


@pytest.mark.parametrize("start", sorted(DIRECTIVES))
@pytest.mark.parametrize("event", [Event.ONBOARD_FAILSAFE, Event.LINK_LOST, Event.ESTOP])
def test_failsafe_is_reachable_from_every_state(start: str, event: Event) -> None:
    """**어느 상태에 있든 페일세이프로 갈 수 있어야 한다.**

    `ANY` 전이가 그것을 보장한다. 상태별로 일일이 적으면 새 상태를 추가할 때
    빠뜨리고, 그 상태에서만 안전 전이가 없는 구멍이 생긴다.
    """
    f = Fsm(initial=start)
    f.handle(event)
    assert f.state == "FAILSAFE"


@pytest.mark.parametrize("event", sorted(set(Event) - {Event.RESET_CONFIRMED}))
def test_failsafe_accepts_nothing_but_a_confirmed_reset(event: Event) -> None:
    """**원인이 사라져도 사람이 확인해야 나온다** (DR-16).

    전 사건을 하나씩 넣어 본다. `EXCLUSIVE` 표가 데이터로 봉인하므로 사건을
    새로 추가해도 이 시험이 자동으로 덮는다.
    """
    f = Fsm(initial="FAILSAFE")
    assert f.handle(event) is False, f"{event} 로 페일세이프가 풀렸다"
    assert f.state == "FAILSAFE"


def test_confirmed_reset_returns_to_idle() -> None:
    f = Fsm(initial="FAILSAFE")
    assert f.handle(Event.RESET_CONFIRMED) and f.state == "IDLE"


def test_exclusive_table_only_seals_failsafe() -> None:
    """봉인은 페일세이프 하나뿐이다 — 다른 상태를 막으면 순찰이 멈춘다."""
    assert set(EXCLUSIVE) == {"FAILSAFE"}


def test_manual_override_works_from_any_state_except_failsafe() -> None:
    for start in DIRECTIVES:
        f = Fsm(initial=start)
        f.handle(Event.MANUAL_ON)
        expected = "FAILSAFE" if start == "FAILSAFE" else "MANUAL"
        assert f.state == expected, f"{start} 에서 수동 전환이 {f.state} 로 갔다"


def test_unknown_event_for_the_current_state_is_ignored() -> None:
    """사건은 바깥에서 온다. **지금 상태와 무관한 사건이 오는 것은 정상이다.**"""
    f = Fsm()
    assert f.handle(Event.SCAN_DONE) is False
    assert f.state == "IDLE"


def test_entry_hook_receives_previous_and_new_state() -> None:
    f = Fsm()
    seen: list[tuple[str, str]] = []
    f.on_enter("PATROL", lambda old, new: seen.append((old, new)))
    f.handle(Event.START_PATROL)
    f.handle(Event.SCAN_DUE)
    f.handle(Event.SCAN_DONE)
    assert seen == [("IDLE", "PATROL"), ("SCAN", "PATROL")]


# ── FSM ↔ 송신기 연결 ─────────────────────────────────────────
def make(clock: FakeClock) -> Behavior:
    return Behavior(Commander(p.CommandEncoder(clock=clock), period_ms=100))


def types_of(lines: list[str]) -> list[str]:
    return [json.loads(x)["type"] for x in lines]


def test_halting_states_override_any_leftover_drive_intent(clock: FakeClock) -> None:
    """**정지 계열 상태에서는 이전 보행 의도가 남아 있어도 정지가 나간다.**"""
    b = make(clock)
    b.event(Event.MANUAL_ON)
    b.commander.drive(step=80, angle=0)
    b.event(Event.ONBOARD_FAILSAFE)
    assert types_of(b.tick(clock.ms))[-1] == "STOP"
    assert b.commander.intent == HALT


def test_manual_state_yields_control_to_the_operator(clock: FakeClock) -> None:
    """`MANUAL` 에서는 FSM 이 의도를 건드리지 않는다 — 조작자가 정한다."""
    b = make(clock)
    b.event(Event.MANUAL_ON)
    b.commander.drive(step=60, angle=20)
    assert types_of(b.tick(clock.ms))[-1] == "MOVE"
    assert b.fsm.directive is Directive.YIELD


def test_sequence_state_without_a_handler_halts(clock: FakeClock) -> None:
    """**`3.5` 모션 시퀀스가 붙기 전까지 `SEQUENCE` 상태는 정지가 기본값이다.**

    등록되지 않은 상태에서 이전 의도를 그대로 두면 로봇이 계속 걸어간다.
    """
    b = make(clock)
    b.commander.drive(step=90, angle=0)
    b.event(Event.START_PATROL)
    assert b.fsm.directive is Directive.SEQUENCE
    assert types_of(b.tick(clock.ms))[-1] == "STOP"


def test_registered_sequence_decides_the_intent(clock: FakeClock) -> None:
    b = make(clock)
    b.register_sequence("PATROL", lambda cmd, _now: cmd.drive(step=60, angle=0))
    b.event(Event.START_PATROL)
    msg = json.loads(b.tick(clock.ms)[-1])
    assert (msg["type"], msg["step"]) == ("MOVE", 60)


def test_sequence_cannot_be_registered_for_a_halting_state(clock: FakeClock) -> None:
    """정지 상태에 시퀀스를 달면 그 상태가 정지가 아니게 된다."""
    b = make(clock)
    with pytest.raises(ValueError, match="시퀀스를 쓰는 상태가 아니다"):
        b.register_sequence("FAILSAFE", lambda cmd, _now: cmd.drive(10, 0))


def test_state_change_is_announced_to_the_robot(clock: FakeClock) -> None:
    """로봇은 자기가 `PATROL` 인지 알 수 없다 — 호스트가 알려줘야 한다."""
    b = make(clock)
    assert "STATE" in types_of(b.tick(clock.ms))
    clock.advance(100)
    b.event(Event.START_PATROL)
    announced = [json.loads(x) for x in b.tick(clock.ms) if json.loads(x)["type"] == "STATE"]
    assert announced and announced[0]["state"] == "PATROL"


def test_behavior_keeps_sending_while_state_is_unchanged(clock: FakeClock) -> None:
    """상태가 그대로여도 **명령은 계속 나가야 한다.** 조용해지면 로봇이 멈춘다."""
    b = make(clock)
    count = 0
    for _ in range(20):
        count += len(b.tick(clock.ms))
        clock.advance(100)
    assert count == 21, "첫 틱의 STATE 1건 + 매 틱 STOP 20건"


# ── 이탈 훅 (WBS 3.4.2) ───────────────────────────────────────
def test_exit_hook_fires_before_the_entry_hook() -> None:
    """**뒷정리가 새 상태의 준비보다 먼저다.**

    `SCAN` 을 떠날 때 스캔 타이머를 멈추지 않고 `PATROL` 진입에서 순찰 타이머를
    켜면 두 타이머가 겹쳐 도는 순간이 생긴다.
    """
    f = Fsm(initial="SCAN")
    order: list[str] = []
    f.on_exit("SCAN", lambda old, new: order.append(f"exit {old}->{new}"))
    f.on_enter("PATROL", lambda old, new: order.append(f"enter {old}->{new}"))
    assert f.handle(Event.SCAN_DONE)
    assert order == ["exit SCAN->PATROL", "enter SCAN->PATROL"]


def test_exit_hook_does_not_fire_when_no_transition_happens() -> None:
    f = Fsm(initial="ALERT")
    seen: list[str] = []
    f.on_exit("ALERT", lambda _old, new: seen.append(new))
    assert f.handle(Event.PPE_VIOLATION) is False  # 같은 상태 — 전이 아님
    assert seen == []


# ── 두 링크를 다르게 대응한다 (NFR-2.6 · WBS 3.4.2) ───────────
def test_robot_link_loss_goes_to_failsafe(clock: FakeClock) -> None:
    """로봇 링크 두절은 **안전 문제**다 — 명령이 닿지 않으므로 멈춰야 한다."""
    b = Behavior(Commander(p.CommandEncoder(clock=clock), period_ms=100), link_loss_ms=3000)
    b.event(Event.START_PATROL)
    b.note_telemetry(clock.ms)
    clock.advance(2900)
    b.tick(clock.ms)
    assert b.state == "PATROL", "3초 전에는 두절로 보지 않는다"
    clock.advance(100)
    b.tick(clock.ms)
    assert b.state == "FAILSAFE"


def test_vision_stall_keeps_patrolling(clock: FakeClock) -> None:
    """**비전 단절은 기능 저하다** (NFR-2.6) — 순찰·회피는 계속하고 인지만 끈다.

    이 둘을 같은 사건으로 묶으면 **카메라가 딸꾹질할 때마다 로봇이 멈춘다.**
    """
    b = Behavior(
        Commander(p.CommandEncoder(clock=clock), period_ms=100),
        link_loss_ms=3000,
        vision_stall_ms=2000,
    )
    b.event(Event.START_PATROL)
    b.note_telemetry(clock.ms)
    b.note_vision(clock.ms)
    clock.advance(2500)
    b.note_telemetry(clock.ms)  # 로봇은 살아 있다
    b.tick(clock.ms)
    assert b.state == "PATROL", "비전이 죽어도 순찰은 계속된다"
    assert b.degraded is True, "저하 상태가 관측 가능해야 한다"


def test_vision_recovery_clears_the_degraded_flag(clock: FakeClock) -> None:
    b = Behavior(Commander(p.CommandEncoder(clock=clock)), vision_stall_ms=2000)
    b.note_vision(clock.ms)
    clock.advance(2000)
    b.tick(clock.ms)
    assert b.degraded is True
    b.note_vision(clock.ms)
    assert b.degraded is False


def test_links_are_not_watched_before_the_first_message(clock: FakeClock) -> None:
    """**기동 직후를 두절로 보면 켜는 순간 페일세이프가 걸린다.**"""
    b = Behavior(Commander(p.CommandEncoder(clock=clock)), link_loss_ms=100)
    b.event(Event.START_PATROL)
    clock.advance(10_000)
    b.tick(clock.ms)
    assert b.state == "PATROL"
    assert b.degraded is False


def test_link_watch_runs_before_the_directive_is_applied(clock: FakeClock) -> None:
    """두절을 **이번 틱에** 반영해야 그 틱의 명령이 페일세이프에 맞는다.

    나중에 보면 한 주기 동안 낡은 상태의 명령이 나간다.
    """
    b = Behavior(Commander(p.CommandEncoder(clock=clock), period_ms=100), link_loss_ms=1000)
    b.event(Event.MANUAL_ON)
    b.commander.drive(step=80, angle=0)
    b.note_telemetry(clock.ms)
    clock.advance(1000)
    lines = b.tick(clock.ms)
    assert b.state == "FAILSAFE"
    assert json.loads(lines[-1])["type"] == "STOP", "같은 틱에서 이미 정지가 나가야 한다"


def test_timeouts_must_be_positive(clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        Behavior(Commander(p.CommandEncoder(clock=clock)), link_loss_ms=0)


def test_behavior_reads_both_timeouts_from_the_real_config(clock: FakeClock, cfg: dict) -> None:
    """**코드에 박힌 숫자가 아니라 `config.yaml` 이 정본이어야 한다** (NFR-3①).

    두 값이 서로 다른 절에서 오는 것도 함께 고정한다 — `safety` 는 안전 임계,
    `vision` 은 기능 저하 임계다. 한 절로 합치면 언젠가 같은 대응으로 묶인다.
    """
    from host.behavior.fsm import behavior_from_config

    b = behavior_from_config(Commander(p.CommandEncoder(clock=clock)), cfg)
    b.event(Event.START_PATROL)
    b.note_telemetry(clock.ms)
    b.note_vision(clock.ms)

    clock.advance(int(cfg["vision"]["stall_timeout_ms"]))
    b.note_telemetry(clock.ms)
    b.tick(clock.ms)
    assert b.degraded is True and b.state == "PATROL"

    clock.advance(int(cfg["safety"]["link_loss_failsafe_ms"]))
    b.tick(clock.ms)
    assert b.state == "FAILSAFE"
