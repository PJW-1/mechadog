"""순찰 항법 공유 상태(`PatrolNavState`) 단위 시험 — 목표·재계획 대기의 전이와 순찰기와의 공유."""

from __future__ import annotations

from test_live_nav import ready

from host.behavior.live_nav import LocalScan, NavParams
from host.behavior.nav_state import GoalState, PatrolNavState, ReplanGate


def test_goal_set_hold_and_clear_move_all_three_fields_together() -> None:
    goal = GoalState()
    goal.set((1.0, 2.0))
    assert (goal.xy, goal.hold, goal.hold_reason) == ((1.0, 2.0), False, None)
    goal.hold_at("blocked")
    assert (goal.xy, goal.hold, goal.hold_reason) == (None, True, "blocked")
    goal.set((3.0, 4.0))
    assert (goal.xy, goal.hold, goal.hold_reason) == ((3.0, 4.0), False, None)
    goal.clear()
    assert (goal.xy, goal.hold, goal.hold_reason) == (None, False, None)


def test_replan_require_keeps_the_first_wait_start() -> None:
    gate = ReplanGate()
    gate.require(1000)
    assert (gate.required, gate.wait_started_ms) == (True, 1000)
    gate.require(1500)
    assert gate.wait_started_ms == 1000, "이미 기다리는 중이면 시작 시각을 밀지 않는다"
    gate.wait_started_ms = None
    gate.require(2000)
    assert gate.wait_started_ms == 2000, "시작 시각을 잃었으면 지금부터 잰다"


def test_replan_clear_keeps_the_timeout_hold_tick() -> None:
    gate = ReplanGate(required=True, wait_started_ms=10, timeout_hold_ms=20)
    gate.clear()
    assert (gate.required, gate.wait_started_ms, gate.timeout_hold_ms) == (False, None, 20)


def test_each_state_has_its_own_edge_goal_and_gate() -> None:
    a = PatrolNavState(local_scan=LocalScan(NavParams()))
    b = PatrolNavState(local_scan=LocalScan(NavParams()))
    assert a.edge is not b.edge and a.goal is not b.goal and a.replan is not b.replan


def test_the_controller_shares_one_state_with_its_old_names() -> None:
    c = ready()
    state = c.nav_state
    assert c._local_scan is state.local_scan
    c._goal = (1.0, 1.0)
    c._now_ms = 1234
    c._replan_stop_required = True
    assert (state.goal.xy, state.now_ms, state.replan.required) == ((1.0, 1.0), 1234, True)
    c.require_replan_stop()
    assert c._replan_wait_started_ms == 1234
    assert c.commander.intent.type_ == "STOP"
