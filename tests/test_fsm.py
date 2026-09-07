"""최소 FSM 검증 (WBS 3.4.1 · 아키텍처 3절).

전이가 테이블이므로 **표를 전수로 돌려볼 수 있다.** 분기문으로 흩어놓으면
"어떤 조합이 남았는지" 를 세는 것 자체가 불가능해진다.
"""

import json

import pytest
from conftest import FakeClock

from host.behavior.commander import HALT, Commander
from host.behavior.fsm import (
    ANY,
    DIRECTIVES,
    TRANSITIONS,
    Behavior,
    Directive,
    Event,
    Fsm,
    Transition,
)
from host.common import protocol as p


def test_states_exist_in_the_protocol_state_set() -> None:
    """**FSM 상태 이름의 정본은 규약이다.**

    여기서 임의의 이름을 쓰면 `STATE` 명령이 수신측에서 폐기되고, 텔레메트리의
    `state` 도 채울 수 없게 된다 — 대시보드에 표시할 수 없는 상태가 된다.
    """
    for state in DIRECTIVES:
        assert state in p.FSM_STATES, f"규약에 없는 상태: {state}"
    for t in TRANSITIONS:
        assert t.dst in DIRECTIVES, f"지시가 정의되지 않은 도착 상태: {t.dst}"
        assert t.src == ANY or t.src in DIRECTIVES, f"모르는 출발 상태: {t.src}"


def test_manual_round_trip() -> None:
    f = Fsm()
    assert f.state == "IDLE"
    assert f.handle(Event.MANUAL_ON) and f.state == "MANUAL"
    assert f.handle(Event.MANUAL_OFF) and f.state == "IDLE"


@pytest.mark.parametrize("start", sorted(DIRECTIVES))
@pytest.mark.parametrize("event", [Event.ONBOARD_FAILSAFE, Event.LINK_LOST])
def test_failsafe_is_reachable_from_every_state(start: str, event: Event) -> None:
    """**어느 상태에 있든 페일세이프로 갈 수 있어야 한다.**

    `ANY` 전이가 그것을 보장한다. 상태별로 일일이 적으면 새 상태를 추가할 때
    빠뜨리고, 그 상태에서만 안전 전이가 없는 구멍이 생긴다.
    """
    f = Fsm(initial=start)
    f.handle(event)
    assert f.state == "FAILSAFE"


def test_failsafe_does_not_clear_by_itself() -> None:
    """**원인이 사라져도 자동으로 나오지 않는다** (DR-16).

    자동 복귀를 만들면 무엇 때문에 멈췄는지 모르는 채로 다시 걷는다.
    """
    f = Fsm(initial="FAILSAFE")
    for event in (Event.MANUAL_ON, Event.MANUAL_OFF, Event.LINK_LOST):
        f.handle(event)
        assert f.state == "FAILSAFE", f"{event} 로 풀렸다"
    assert f.handle(Event.RESET_CONFIRMED) and f.state == "IDLE"


def test_unknown_event_for_the_current_state_is_ignored() -> None:
    """사건은 바깥에서 온다. **지금 상태와 무관한 사건이 오는 것은 정상이다.**"""
    f = Fsm()
    assert f.handle(Event.MANUAL_OFF) is False
    assert f.state == "IDLE"
    assert f.handle(Event.RESET_CONFIRMED) is False


def test_entry_hook_receives_previous_and_new_state() -> None:
    f = Fsm()
    seen: list[tuple[str, str]] = []
    f.on_enter("MANUAL", lambda old, new: seen.append((old, new)))
    f.handle(Event.MANUAL_ON)
    f.handle(Event.MANUAL_OFF)
    f.handle(Event.MANUAL_ON)
    assert seen == [("IDLE", "MANUAL"), ("IDLE", "MANUAL")]


def test_duplicate_transition_is_rejected_at_construction() -> None:
    """같은 (상태, 사건) 이 두 줄이면 어느 쪽이 이기는지 표만 보고 알 수 없다."""
    with pytest.raises(ValueError, match="중복"):
        Fsm(
            transitions=(
                Transition("IDLE", Event.MANUAL_ON, "MANUAL"),
                Transition("IDLE", Event.MANUAL_ON, "FAILSAFE"),
            )
        )


def test_every_state_has_a_directive() -> None:
    """상태만 추가하고 지시를 빠뜨리면 그 상태에서 아무 명령도 안 나간다."""
    assert set(DIRECTIVES) == {t.dst for t in TRANSITIONS} | {"IDLE"}


# ── FSM ↔ 송신기 연결 ─────────────────────────────────────────
def make(clock: FakeClock) -> Behavior:
    return Behavior(Commander(p.CommandEncoder(clock=clock), period_ms=100))


def test_halting_states_override_any_leftover_drive_intent(clock: FakeClock) -> None:
    """**`IDLE`·`FAILSAFE` 에서는 이전 보행 의도가 남아 있어도 정지가 나간다.**

    수동 조작 중 페일세이프가 걸렸는데 마지막 `MOVE` 가 계속 나가면 로봇은
    래치 때문에 무시하겠지만, 호스트가 무엇을 지시하는지와 실제가 어긋난다.
    """
    b = make(clock)
    b.event(Event.MANUAL_ON)
    b.commander.drive(step=80, angle=0)
    b.event(Event.ONBOARD_FAILSAFE)
    lines = b.tick(clock.ms)
    assert json.loads(lines[-1])["type"] == "STOP"
    assert b.commander.intent == HALT


def test_manual_state_yields_control_to_the_operator(clock: FakeClock) -> None:
    """`MANUAL` 에서는 FSM 이 의도를 건드리지 않는다 — 조작자가 정한다."""
    b = make(clock)
    b.event(Event.MANUAL_ON)
    b.commander.drive(step=60, angle=20)
    lines = b.tick(clock.ms)
    assert json.loads(lines[-1])["type"] == "MOVE"
    assert b.fsm.directive is Directive.YIELD


def test_state_change_is_announced_to_the_robot(clock: FakeClock) -> None:
    """로봇은 자기가 `MANUAL` 인지 알 수 없다 — 호스트가 알려줘야 한다."""
    b = make(clock)
    assert "STATE" in [json.loads(x)["type"] for x in b.tick(clock.ms)]
    clock.advance(100)
    b.event(Event.MANUAL_ON)
    announced = [json.loads(x) for x in b.tick(clock.ms) if json.loads(x)["type"] == "STATE"]
    assert announced and announced[0]["state"] == "MANUAL"


def test_behavior_keeps_sending_while_state_is_unchanged(clock: FakeClock) -> None:
    """상태가 그대로여도 **명령은 계속 나가야 한다.** 조용해지면 로봇이 멈춘다."""
    b = make(clock)
    count = 0
    for _ in range(20):
        count += len(b.tick(clock.ms))
        clock.advance(100)
    assert count == 21, "첫 틱의 STATE 1건 + 매 틱 STOP 20건"
