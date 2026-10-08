"""AR: 직접 추종, 실제 STOP 지연 회피, 복귀 및 기존 안전 경계."""

from __future__ import annotations

import math

import pytest
from test_lidar_patrol import Reading, build
from test_live_nav import revolution
from test_route_runtime import route

from host.behavior.live_nav import NavParams
from host.behavior.patrol import GOAL_LABEL, Phase
from host.behavior.planner import Plan
from host.behavior.routes import RoutePoint
from host.common.config import ConfigError


def direct():
    c = build(nav_params=NavParams(relaxed_follow=False, local_slow_m=0.36))
    c.observe_telemetry(Reading(), 1000)
    c.observe_map_pose((2, 2, 0), 1000)
    assert c.start_route(route(RoutePoint(x=4, y=2)), 1000)[0]
    return c


def tick(c, now, *, front=3.0, pose=(2, 2, 0), entry="step", onboard=False):
    c.observe_telemetry(Reading(obstacle=onboard), now)
    c.observe_map_pose(pose, now)
    c.observe_obstacle_scan(revolution(now, front=front), now)
    getattr(c, entry)(now)
    c.note_sent(c.commander.tick(now), now)


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_remembered_wall_does_not_replan_validated_route(monkeypatch, entry):
    c = direct()
    c._blockages.remember(c.grid, [(2.37, 2.0)], 1000)
    c._refresh_navigation(1000)
    assert not c._local_path_clear((4, 2))

    def forbidden(*_args, **_kwargs):
        pytest.fail("직접 추종 구간에서 A*/막힘 복구를 호출했다")

    monkeypatch.setattr("host.behavior.patrol.plan_to", forbidden)
    monkeypatch.setattr(c, "_begin_recovery", forbidden)
    tick(c, 1000, front=0.37, entry=entry)
    assert c.commander.intent.fields == {"step": c.drive.step_mm, "angle": 0.0}
    assert c.local_status["reason"] == "route_direct"
    assert c._recovery is None and c._avoidance is None
    assert c.stats.replans == 0


@pytest.mark.parametrize("distance", [0.24, 0.25])
@pytest.mark.parametrize("entry", ["step", "steer"])
def test_stop_for_three_seconds_then_existing_avoidance(distance, entry):
    c = direct()
    tick(c, 1000, front=distance, entry=entry)
    tick(c, 3999, front=distance, entry=entry)
    assert c.commander.intent.type_ == "STOP"
    assert c._avoidance is None and c._recovery is None
    tick(c, 4000, front=distance, entry=entry)
    assert c.commander.intent.type_ == "STOP"
    assert c._avoidance is not None
    assert c._avoidance[-1] == "lidar_corridor"
    # 전방이 막혀 있을 때는 통로 방향 제자리 회전만 한다.
    tick(c, 4100, front=distance, entry=entry)
    assert c.commander.intent.type_ == "MOVE"
    assert c.commander.intent.fields["step"] == 0
    assert c.local_status["action"] == "avoid"


def test_avoidance_finishes_and_direct_following_resumes(monkeypatch):
    c = direct()
    tick(c, 1000, front=0.24)
    tick(c, 4000, front=0.24)
    tick(c, 4100, pose=(2.21, 2.1, 0))
    assert c._avoidance is None

    def forbidden(*_args, **_kwargs):
        pytest.fail("회피 후 A*를 다시 호출했다")

    monkeypatch.setattr("host.behavior.patrol.plan_to", forbidden)
    tick(c, 4200, pose=(2.21, 2.1, 0))
    assert c.commander.intent.fields["step"] > 0
    assert c.local_status["reason"] == "route_direct"


def test_reverse_heading_spins_until_aligned_then_walks():
    c = direct()
    tick(c, 1000, pose=(2, 2, math.pi))
    assert c.commander.intent.fields["step"] == 0
    assert abs(c.commander.intent.fields["angle"]) == c.drive.spin_turn_deg
    tick(c, 1100, pose=(2, 2, math.radians(30)))
    assert c.commander.intent.fields["step"] == 0
    tick(c, 1200)
    assert c.commander.intent.fields == {"step": c.drive.step_mm, "angle": 0.0}


def test_temporary_obstacle_and_pose_loss_reset_stop_timer():
    c = direct()
    tick(c, 1000, front=0.24)
    tick(c, 3000)
    tick(c, 4000, front=0.24)
    c.step(4700)  # 실제 측위 공백은 회피 근거가 될 수 없다.
    assert c.phase is Phase.LOST
    tick(c, 7000, front=0.24)
    assert c._avoidance is None and c._recovery is None
    tick(c, 9999, front=0.24)
    assert c._avoidance is None
    tick(c, 10000, front=0.24)
    assert c._avoidance is not None


def test_stop_timer_requires_actual_sent_stop():
    c = direct()
    tick(c, 1000)
    for now in (1100, 4100):
        c.observe_telemetry(Reading(), now)
        c.observe_map_pose((2, 2, 0), now)
        c.observe_obstacle_scan(revolution(now, front=0.24), now)
        c.step(now)
    assert c.commander.intent.type_ == "STOP"
    assert c._avoidance is None and c._recovery is None
    c.note_sent(c.commander.tick(4100), 4100)
    tick(c, 7099, front=0.24)
    assert c._avoidance is None
    tick(c, 7100, front=0.24)
    assert c._avoidance is not None


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_ultrasonic_stop_never_moves_even_after_fallback(entry):
    c = direct()
    tick(c, 1000, onboard=True, entry=entry)
    tick(c, 4000, onboard=True, entry=entry)
    tick(c, 4100, onboard=True, entry=entry)
    assert c.commander.intent.type_ == "STOP"


@pytest.mark.parametrize("gate", ["pose", "scan", "rejected", "trust", "estop", "link"])
def test_existing_safety_gates_override_direct_following(gate):
    c = direct()
    tick(c, 1000)
    if gate == "pose":
        c.localization.last_pose_ms = None
    elif gate == "scan":
        c._local_scan.received_ms = None
    elif gate == "rejected":
        c._local_scan.clear_allowed = False
    elif gate == "trust":
        c.localization.own_localization = True
        c.localization.verified = False
    elif gate == "estop":
        c.emergency_stop("test")
    else:
        c.safety.last_seen_ms = -3001
    c.step(1100)
    assert c.commander.intent.type_ == "STOP"
    assert c._route_direct_stopped_ms is None


def test_no_observed_corridor_uses_existing_recovery_after_delay():
    c = direct()
    for now in (1000, 3999, 4000):
        c.observe_telemetry(Reading(), now)
        c.observe_map_pose((2, 2, 0), now)
        c.observe_obstacle_scan(revolution(now, distance=0.24), now)
        c.step(now)
        c.note_sent(c.commander.tick(now), now)
        assert c.commander.intent.type_ == "STOP"
        if now < 4000:
            assert c._recovery is None
    assert c._recovery is not None
    assert c._recovery.target == GOAL_LABEL


@pytest.mark.parametrize("distance", [0.26, 0.32, 0.36, 0.37])
def test_direct_slow_and_clear_distances(distance):
    c = direct()
    tick(c, 1000, front=distance)
    assert c.commander.intent.fields["step"] == pytest.approx(
        c.drive.step_mm * min(1, (distance - 0.25) / 0.11)
    )
    assert c._recovery is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("route_direct", "true"),
        ("route_direct", 1),
        ("route_direct_stop_ms", True),
        ("route_direct_stop_ms", 0),
        ("route_direct_stop_ms", 3.5),
    ],
)
def test_invalid_config_is_rejected(key, value):
    with pytest.raises(ConfigError):
        NavParams.of({"nav": {key: value}})


def test_config_can_disable_direct_following():
    assert NavParams.of({}).route_direct
    assert not NavParams.of({"nav": {"route_direct": False}}).route_direct


def test_direct_start_ignores_memory_and_never_calls_astar(monkeypatch):
    c = direct()
    c.cancel_route()
    c._blockages.remember(c.grid, [(2.37, 2.0)], 1000)
    c._refresh_navigation(1000)

    def forbidden(*_args, **_kwargs):
        pytest.fail("동선 직접 추종 시작에서 A*를 호출했다")

    monkeypatch.setattr("host.behavior.patrol.plan_to", forbidden)
    assert c.start_route(route(RoutePoint(x=4, y=2)), 1000)[0]
    tick(c, 1000)
    assert c.commander.intent.fields["step"] == c.drive.step_mm


def test_current_pose_to_first_point_must_be_a_valid_line():
    c = direct()
    c.cancel_route()
    c.grid.cells[20:70, 60] = 5
    c._rebuild_masks()
    assert not c.start_route(route(RoutePoint(x=4, y=2)), 1000)[0]
    assert c.commander.intent.type_ == "STOP"


def test_astar_detour_returns_to_direct_when_goalward_sweep_is_clear():
    c = direct()
    c._route_direct_detour_start = (2, 2)
    c.plan = Plan(GOAL_LABEL, ((2, 2), (3, 3), (4, 2)), 3, effective=(4, 2))
    c.phase = Phase.MOVING
    tick(c, 1100, pose=(2.21, 2, 0))
    assert c._route_direct_detour_start is None
    assert c.local_status["reason"] == "route_direct"
    assert c.commander.intent.fields == {"step": c.drive.step_mm, "angle": 0.0}


def test_route_unavailable_return_home_does_not_restart_direct_point():
    c = direct()
    c._home = (1, 2)
    c._return_home()
    assert not c._route_direct_moving
    tick(c, 1100)
    assert c.plan.label == "HOME"


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_unverified_pose_cannot_arm_ultrasonic_avoidance(entry):
    c = direct()
    c.localization.own_localization = True
    c.localization.verified = False
    tick(c, 1000, onboard=True, entry=entry)
    tick(c, 5000, onboard=True, entry=entry)
    assert c.commander.intent.type_ == "STOP"
    assert c._route_direct_stopped_ms is None
    assert c._avoidance is None and c._recovery is None
