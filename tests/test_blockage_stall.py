"""AM: failed goto completion, bounded recovery and grid-boundary consistency."""

from __future__ import annotations

import json

import pytest
from test_blockage_policy import blocked_target, finish_scan, tick
from test_live_nav import ready, revolution
from test_runtime import config
from test_runtime_lidar import _patrolling

from host.behavior.blockage import Recovery
from host.behavior.fsm import Event
from host.behavior.patrol import GOAL_LABEL, Phase
from host.behavior.planner import Plan, segment_clear
from host.common.protocol import CommandEncoder

__all__ = ["config"]


#: 경로 계획을 부르는 모듈 — 순찰기와 협력 객체가 각자 `plan_to` 를 import 한다.
PLAN_TO = ("host.behavior.patrol.plan_to", "host.behavior.recovery.plan_to")


def test_fully_blocked_single_goto_finishes_idle_and_preserves_event(monkeypatch):
    """AO: only a scan with no body corridor ends the mission as spatially blocked."""
    c = ready(front=0.446)
    c._goal = (4, 2)
    c.plan = Plan(GOAL_LABEL)
    c.recovery.begin("global_path_blocked")
    for target in PLAN_TO:
        monkeypatch.setattr(
            target, lambda *_args, **_kwargs: Plan(None, fail_reason="start_clearance_blocked")
        )
    finish_scan(c, front=0.3, distance=0.3)
    assert c.recovery.active is None
    assert c.phase is Phase.IDLE
    assert c.goal is None and c.holding_goal and c.goal_hold_reason == "blocked"
    assert c.local_status["reason"] == "goal_unreachable"
    (event,) = c.take_navigation_events()
    assert event["event"] == "zone_skipped"
    assert event["judgement"]["zone"] == GOAL_LABEL
    assert event["judgement"]["reason"] == "start_clearance_blocked"
    for now in range(7000, 48000, 1000):
        tick(c, now, front=0.446)
        assert c.phase is Phase.IDLE and c.commander.intent.type_ == "STOP"
    assert not c.take_navigation_events()


@pytest.mark.parametrize(
    "gate", ["stop", "moving", "scan", "settled_scan", "stale_pose", "onboard", "unverified"]
)
@pytest.mark.parametrize("entry", ["step", "steer"])
def test_every_recovery_wait_expires_without_movement(gate, entry):
    c = ready(front=0.6)
    c._goal = (4, 2)
    c.plan = Plan(GOAL_LABEL)
    c.recovery.begin("path_obstacle")
    recovery = c.recovery.active
    assert recovery is not None
    c._stopped_since_ms = None if gate in ("stop", "moving") else 1000
    c._last_sent_moving = gate == "moving"
    c._local_scan.received_ms = None if gate == "scan" else 1000
    if gate == "settled_scan":
        recovery.scanning = True
        recovery.settling_ms = 1100
    if gate == "stale_pose":
        c.localization.last_pose_ms = None
    if gate == "onboard":
        c.safety.obstacle = True
    if gate == "unverified":
        c.localization.own_localization = True
        c.localization.verified = False
    now = 1000 + c.nav_params.recovery_scan_timeout_ms
    c.safety.last_seen_ms = now
    getattr(c, entry)(now)
    assert c.recovery.active is None
    assert c.goal is None and c.goal_hold_reason == "blocked"
    assert c.commander.intent.type_ == "STOP"
    assert len(c.take_navigation_events()) == 1


def test_missing_stop_has_short_deadline_and_zero_time_is_valid():
    c = ready(front=0.6)
    blocked_target(c)
    c._now_ms = 0
    c.recovery.begin("path_obstacle")
    c._stopped_since_ms = None
    budget = (
        max(c.drive.settle_delay_ms, c.nav_params.blockage_confirm_ms)
        + c.nav_params.recovery_motion_timeout_ms
    )
    assert not c.expire_recovery(budget - 1)
    assert c.expire_recovery(budget)
    assert c.recovery.active is None and c.skipped == {"A"}
    assert c.commander.intent.type_ == "STOP"
    assert not c.expire_recovery(budget + 1)


def test_scanning_has_an_absolute_deadline_even_with_continuing_rotation():
    c = ready(front=0.6)
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    recovery = c.recovery.active
    assert recovery is not None
    recovery.scanning = True
    recovery.last_motion_ms = 1000 + c.nav_params.recovery_scan_timeout_ms - 1
    c._last_sent_moving = True
    assert c.expire_recovery(1000 + c.nav_params.recovery_scan_timeout_ms)
    assert c.recovery.active is None and c.commander.intent.type_ == "STOP"


def test_expiry_does_not_release_existing_safety_halt():
    c = ready(front=0.6)
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    c.phase = Phase.HALTED
    assert c.expire_recovery(1000 + c.nav_params.recovery_scan_timeout_ms)
    assert c.phase is Phase.HALTED and c.commander.intent.type_ == "STOP"


def test_new_goal_and_cancellation_discard_old_recovery():
    c = ready()
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    assert c.goto(3, 3)[0]
    assert c.recovery.active is None and not c._replan_stop_required
    c.recovery.begin("path_obstacle")
    c.cancel_goal("test")
    assert c.recovery.active is None and not c._replan_stop_required


def test_escape_gate_uses_same_boundary_cells_as_planner(monkeypatch):
    c = ready()
    c.pose = (2.0, 2.0, 0.0)
    row, col = c.grid.to_cell(*c.pose[:2])
    c._dynamic[row, col - 1] = True
    assert not c.body_blocked[row, col]
    assert not segment_clear(c.grid, c.body_blocked, c.pose[:2], c.pose[:2])
    monkeypatch.setattr(c, "_refresh_navigation", lambda _now: None)
    c._guard_localization(1000)
    assert c.avoidance.needs_escape
    assert c.plan_params.body_radius_m == 0.15
    assert c.nav_params.local_stop_m == 0.25


@pytest.mark.parametrize("state", ["PATROL", "SCAN"])
def test_runtime_failure_finishes_idle_preserves_result_and_can_restart(config, clock, state):
    runtime, c = _patrolling(config, clock)
    c._goal = (4, 2)
    c.recovery.begin("path_obstacle")
    if state == "SCAN":
        assert runtime._apply(Event.SCAN_DUE, clock.ms)
    recovery = c.recovery.active
    assert recovery is not None
    deadline = clock.ms + c.nav_params.recovery_scan_timeout_ms
    clock.ms = deadline
    runtime.behavior.note_telemetry(deadline)
    c.safety.last_seen_ms = deadline
    events = []
    runtime._record_navigation_event = lambda event, _now: events.append(event)
    lines = runtime.tick(deadline)
    assert c.recovery.active is None
    assert runtime.behavior.state == "IDLE"
    assert c.goal is None and c.goal_hold_reason == "blocked"
    assert not any(json.loads(line)["type"] == "MOVE" for line in lines)
    assert any(json.loads(line).get("state") == "IDLE" for line in lines)
    assert len(events) == 1 and events[0]["event"] == "zone_skipped"
    runtime.tick(deadline + 100)
    assert c.goal_hold_reason == "blocked" and len(events) == 1
    runtime.ask_patrol()
    clock.advance(100)
    runtime.behavior.note_telemetry(clock.ms)
    c.safety.last_seen_ms = clock.ms
    c.observe_map_pose((2, 2, 0), clock.ms)
    c.observe_obstacle_scan(revolution(clock.ms), clock.ms)
    runtime.tick(clock.ms)
    assert runtime.behavior.state == "PATROL"
    assert not c.holding_goal


def test_runtime_immediate_failed_retry_announces_idle_in_same_tick(config, clock, monkeypatch):
    runtime, c = _patrolling(config, clock)
    clock.advance(100)
    runtime._navigator_resume = False
    c._goal = (4, 2)
    c.plan = Plan(GOAL_LABEL)
    c.recovery.active = Recovery(
        GOAL_LABEL, clock.ms, 0, None, scanning=True, settling_ms=clock.ms - 2000
    )
    c.note_sent([CommandEncoder().encode("STOP")], clock.ms - 2000)
    c._local_scan.received_ms = clock.ms
    c.observe_obstacle_scan(revolution(99999, distance=0.3), clock.ms)
    for target in PLAN_TO:
        monkeypatch.setattr(
            target, lambda *_args, **_kwargs: Plan(None, fail_reason="start_clearance_blocked")
        )
    transitions = runtime._stats.transitions
    lines = runtime.tick(clock.ms)
    assert runtime._stats.transitions == transitions + 1
    assert runtime.behavior.state == "IDLE" and c.phase is Phase.IDLE
    assert any(json.loads(line).get("state") == "IDLE" for line in lines)
    assert c.goal_hold_reason == "blocked"


def test_timeout_cannot_release_robot_failsafe(config, clock):
    runtime, c = _patrolling(config, clock)
    c._goal = (4, 2)
    c.recovery.begin("path_obstacle")
    runtime.behavior.note_robot_latch(True)
    runtime._apply(Event.ONBOARD_FAILSAFE, clock.ms)
    clock.advance(c.nav_params.recovery_scan_timeout_ms)
    lines = runtime.tick(clock.ms)
    assert runtime.behavior.state == "FAILSAFE"
    assert c.recovery.active is None
    assert not any(json.loads(line)["type"] == "MOVE" for line in lines)


def test_skipped_patrol_target_cannot_move_until_stop_and_settled_scan():
    c = ready(front=0.6)
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    c._last_sent_moving = True
    c._stopped_since_ms = None
    deadline = 1000 + c.nav_params.recovery_scan_timeout_ms
    assert c.expire_recovery(deadline)
    c.safety.last_seen_ms = deadline
    c.observe_map_pose(c.pose, deadline)
    c._local_scan.received_ms = deadline
    c.step(deadline)
    assert c.commander.intent.type_ == "STOP"
    assert c.recovery.active is None and c.local_status["reason"] == "replan_waiting_for_sent_stop"
    c.note_sent([CommandEncoder().encode("STOP")], deadline)
    c.observe_map_pose(c.pose, deadline + 100)
    c._local_scan.received_ms = deadline + 100
    c.step(deadline + 100)
    assert c.commander.intent.type_ == "STOP"
    assert c.local_status["reason"] == "replan_waiting_for_settled_scan"


def test_recorded_nearby_memory_blocks_start_without_changing_body_radius():
    # 2026-10-05 stop_2 23:39:48.3: static body clear, remembered hit 18.21 cm away.
    import math

    import numpy as np

    from host.behavior.blockage import BlockageMemory
    from host.behavior.live_nav import NavParams
    from host.behavior.planner import PlanParams, body_collision_mask, inflate, plan_to
    from host.slam.occupancy import MapMeta, OccupancyGrid

    grid = OccupancyGrid(
        MapMeta(0.05, -6.875000000000001, -4.375, 246, 162),
        np.full((162, 246), -5, dtype=np.float32),
    )
    pose = (0.985, -0.955)
    hit = (1.1230013130097842, -0.8362080281888469)
    params = PlanParams(1, -1, 0.2, 0.08, soft_clearance_m=0.2)
    memory = BlockageMemory(NavParams())
    memory.remember(grid, [hit], 1000)
    dynamic = memory.mask(grid, params.body_radius_m)
    body = body_collision_mask(grid, params)
    assert math.dist(pose, hit) == pytest.approx(0.1820876024313743)
    assert segment_clear(grid, body, pose, pose)
    assert not segment_clear(grid, dynamic, pose, pose)
    plan = plan_to(
        "GOAL",
        (2.95, -0.7),
        pose,
        grid,
        inflate(grid, params) | dynamic,
        params,
        body_blocked=body | dynamic,
    )
    assert plan.fail_reason == "start_clearance_blocked"
    assert params.body_radius_m == 0.15
