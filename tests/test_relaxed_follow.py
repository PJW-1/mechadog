"""AV: 실제 전문 의도, 겹친 관문 우회, 빈 방향과 검사 연동."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from test_lidar_patrol import Reading, build
from test_live_nav import revolution
from test_route_runtime import route
from test_runtime import config
from test_runtime_lidar import _camera_aim_runtime, _camera_aim_tick, vision_result

from host.behavior.live_nav import LocalScan, NavParams
from host.behavior.patrol import Phase
from host.behavior.routes import RoutePoint
from host.common.config import ConfigError
from host.report.situation import describe

__all__ = ["config"]


#: 경로 계획을 부르는 모듈 — 순찰기와 협력 객체가 각자 `plan_to` 를 import 한다.
PLAN_TO = ("host.behavior.patrol.plan_to", "host.behavior.recovery.plan_to")


def relaxed(*points, pose=(2, 2, 0), **kwargs):
    c = build(nav_params=NavParams(), **kwargs)
    c.observe_telemetry(Reading(), 1000)
    c.observe_map_pose(pose, 1000)
    assert c.start_route(route(*(points or (RoutePoint(x=4, y=2),))), 1000)[0]
    return c


def tick(c, now, *, pose=(2, 2, 0), front=None, distance=3, entry="step", **reading):
    c.observe_telemetry(Reading(**reading), now)
    c.observe_map_pose(pose, now)
    c.observe_obstacle_scan(revolution(now, distance, front=front), now)
    getattr(c, entry)(now)
    c.note_sent(c.commander.tick(now), now)


@pytest.mark.parametrize("entry", ["step", "steer"])
@pytest.mark.parametrize(
    "heading,spin", [(0, False), (2, False), (10, False), (34, True), (150, True)]
)
def test_heading_and_never_reverse(entry, heading, spin):
    c = relaxed()
    tick(c, 1000, pose=(2, 2, math.radians(heading)), entry=entry)
    assert c.commander.intent.type_ == "MOVE"
    assert (c.commander.intent.fields["step"] == 0) is spin
    assert c.commander.intent.fields["step"] >= 0
    if heading > 3:
        # 30° 안은 걸으면서 조향, 넘으면 제자리 회전 — 어느 쪽이든 오차 반대로 돈다
        assert c.commander.intent.fields["angle"] < 0
    else:
        assert c.commander.intent.fields == {"step": c.drive.step_mm, "angle": 0.0}


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_front_stop_then_continuous_escape_and_forward(entry):
    c = relaxed()
    tick(c, 1000, front=0.24, entry=entry)
    tick(c, 3999, front=0.24, entry=entry)
    assert c.commander.intent.type_ == "STOP"
    for now in (4000, 4100, 4200):
        tick(c, now, front=0.24, entry=entry)
        assert c.commander.intent.type_ == "MOVE"
        assert c.commander.intent.fields["step"] == 0
        assert abs(c.commander.intent.fields["angle"]) == c.drive.spin_turn_deg
    tick(c, 4300, entry=entry)
    assert c.commander.intent.fields["step"] > 0
    assert c.recovery.active is None and c.avoidance.active is None


def test_escape_requires_transmitted_stop():
    c = relaxed()
    c._last_sent_moving = True
    c._stopped_since_ms = None
    for now in (1000, 4000):
        c.observe_telemetry(Reading(), now)
        c.observe_map_pose((2, 2, 0), now)
        c.observe_obstacle_scan(revolution(now, front=0.24), now)
        c.step(now)
        assert c.commander.intent.type_ == "STOP"


def test_fully_blocked_event_once_then_resumes():
    c = relaxed()
    events = []
    for now in (1000, 4000, 10000, 200000):
        tick(c, now, distance=0.24)
        assert c.commander.intent.type_ == "STOP"
        events.extend(c.take_navigation_events())
    assert [event["event"] for event in events] == ["path_blocked"]
    assert events[0]["judgement"]["message"] == "길 막힘"
    assert describe("path_blocked", events[0]["judgement"]) == (
        "길이 막혔습니다. 장애물을 치워 주세요."
    )
    tick(c, 200100)
    assert c.commander.intent.fields["step"] > 0


@pytest.mark.parametrize("gate", ["replan", "escape", "memory", "recovery"])
def test_old_gates_do_not_run(monkeypatch, gate):
    c = relaxed()
    if gate == "replan":
        c._replan_stop_required = True
        c._stopped_since_ms = None
    elif gate == "escape":
        c.avoidance.needs_escape = True
        c.grid.cells[c.grid.to_cell(2, 2)] = 5
    elif gate == "memory":
        c.recovery.memory.remember(c.grid, [(2.37, 2)], 1000)
    else:
        c.recovery.begin("test")

    def forbidden(*_args, **_kwargs):
        pytest.fail("AV에서 기존 회복/계획 관문 실행")

    for method in ("_replan", "expire_recovery"):
        monkeypatch.setattr(c, method, forbidden)
    for method in ("step", "begin", "expire"):
        monkeypatch.setattr(c.recovery, method, forbidden)
    for method in ("start", "step"):
        monkeypatch.setattr(c.avoidance, method, forbidden)
    tick(c, 1000)
    assert c.commander.intent.fields["step"] > 0
    assert c.recovery.active is None and c.avoidance.active is None


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_ultrasonic_always_stops(entry):
    c = relaxed()
    for now in (1000, 5000, 10000):
        tick(c, now, obstacle=True, entry=entry)
        assert c.commander.intent.type_ == "STOP"


@pytest.mark.parametrize("gate", ["estop", "latch", "link"])
def test_safety_retained(gate):
    c = relaxed()
    tick(c, 1000)
    if gate == "estop":
        c.emergency_stop("test")
    elif gate == "latch":
        c.safety.latched = True
    c.step(5000 if gate == "link" else 1100)
    assert c.commander.intent.type_ == "STOP"
    assert c.phase is Phase.HALTED


def test_verified_grace_then_stops_without_exploration():
    c = relaxed()
    c.localization.own_localization = True
    c.localization.verified = False
    for now in (1500, 3000, 3001, 6000):
        c.observe_telemetry(Reading(), now)
        c.observe_map_pose((2, 2, 0), now)  # 미검증 국소 정합으로 유예를 연장하지 않는다.
        c.observe_obstacle_scan(revolution(now), now)
        c.step(now)
        assert c.commander.intent.type_ == ("MOVE" if now <= 3000 else "STOP")
    c.localization.mark_verified()
    c.step(6000)
    assert c.commander.intent.type_ == "MOVE"


def test_old_verified_pose_stops():
    c = relaxed()
    c.observe_telemetry(Reading(), 3001)
    c.observe_obstacle_scan(revolution(3001), 3001)
    c.step(3001)
    assert c.commander.intent.type_ == "STOP"
    assert c.phase is Phase.LOST


def test_tilt_keeps_last_normal_scan_and_does_not_restart_dwell():
    c = relaxed(RoutePoint(x=2, y=2, aim_deg=90, dwell_s=1, label="A"))
    tick(c, 1000)
    tick(c, 1100, pose=(2, 2, math.pi / 2))
    assert c.route.stage == "dwell"
    normal = c._relaxed_scan.last_id
    c.observe_telemetry(Reading(pitch=15), 1500)
    c.observe_obstacle_scan(revolution(1500), 1500)
    assert not c._local_scan.clear_allowed
    c.step(1500)
    assert c._relaxed_scan.last_id == normal
    assert c.route_status()["dwell_remaining_s"] == pytest.approx(0.6)
    c.step(2100)
    assert c.route.status == "completed"


def test_passed_point_finishes_aim_dwell_inspection_without_returning():
    waiting = True
    c = relaxed(RoutePoint(x=3, y=2, aim_deg=90, dwell_s=1, label="A"))
    c.wait_for_inspection = lambda _label: waiting
    tick(c, 1000, pose=(2.6, 2, 0))
    tick(c, 1100, pose=(3.6, 2, 0))
    assert c.route.stage == "aiming"
    tick(c, 1200, pose=(3.6, 2, 0))
    assert c.commander.intent.fields["step"] == 0
    tick(c, 1300, pose=(3.6, 2, math.pi / 2))
    tick(c, 2300, pose=(3.6, 2, math.pi / 2))
    assert c.route.stage == "inspection"
    assert c.inspection_ready("A", 2300)
    waiting = False
    tick(c, 2400, pose=(3.6, 2, math.pi / 2))
    assert c.route.status == "completed"
    assert c.stats.zones_visited == 1


def test_projection_does_not_skip_from_far_side():
    c = relaxed(RoutePoint(x=3, y=2))
    tick(c, 1000, pose=(2.6, 2, 0))
    tick(c, 1100, pose=(3.6, 3, 0))
    assert c.route.stage == "moving"


def test_first_approach_ignores_map_clearance(monkeypatch):
    c = relaxed()
    c.cancel_route()
    c.grid.cells[20:70, 60] = 5
    for target in PLAN_TO:
        monkeypatch.setattr(target, lambda *_a, **_k: pytest.fail("A*"))
    assert c.start_route(route(RoutePoint(x=4, y=2)), 1000)[0]
    tick(c, 1000)
    assert c.commander.intent.fields["step"] > 0


def test_missing_sector_does_not_block_observed_forward():
    scan = LocalScan(NavParams())
    full = revolution(1)
    partial = replace(full, points=tuple((a, d) for a, d in full.points if abs(a) < 2.8))
    scan.observe(partial, 1000, (0.12, 8))
    assert scan.open_heading(0) == 0
    assert scan.open_heading(math.pi) != pytest.approx(math.pi)


def test_close_side_return_allows_motion_away_but_blocks_approach():
    scan = LocalScan(NavParams())
    full = revolution(1)
    points = full.points + ((math.radians(101.2), 0.127),)
    scan.observe(replace(full, points=points), 1000, (0.12, 8))
    assert scan.open_heading(0) == 0
    assert abs(scan.open_heading(math.radians(101.2)) - math.radians(101.2)) > math.pi / 2


def test_sparse_scan_stops_without_claiming_all_paths_blocked():
    c = relaxed()
    full = revolution(1000)
    c.observe_obstacle_scan(replace(full, points=full.points[:3]), 1000)
    c.step(1000)
    assert c.commander.intent.type_ == "STOP"
    assert not c.take_navigation_events()


@pytest.mark.usefixtures("unlock_modes")
def test_passed_stop_still_opens_runtime_camera_inspection(config, clock):
    runtime, navigator, vision = _camera_aim_runtime(config, clock)
    navigator.observe_map_pose((2, 1, 0), clock.ms)
    assert navigator.start_route(
        route(RoutePoint(x=1, y=1, aim_deg=90, dwell_s=1, label="A")), clock.ms
    )[0]
    for pose in ((1.4, 1, 0), (0.3, 1, 0), (0.3, 1, math.pi / 2), (0.3, 1, math.pi / 2)):
        clock.advance(100)
        navigator.observe_map_pose(pose, clock.ms)
        navigator.observe_obstacle_scan(revolution(clock.ms), clock.ms)
        runtime.note_pose(pose, clock.ms)
        vision.result = vision_result(clock.ms, clock.ms, present=False, hits=0, last_seen_ms=None)
        runtime.tick(clock.ms)
    assert runtime.behavior.state == "ZONE_INSPECT"
    assert runtime._zone_inspector._zone == "A"


@pytest.mark.usefixtures("unlock_modes")
def test_camera_consumes_arrival_during_verified_pose_grace(config, clock):
    runtime, navigator, vision = _camera_aim_runtime(config, clock)
    assert navigator.start_route(
        route(RoutePoint(x=1, y=1, aim_deg=90, dwell_s=0.1, label="A")), clock.ms
    )[0]
    _camera_aim_tick(runtime, navigator, vision, clock, yaw=math.pi / 2)
    _camera_aim_tick(runtime, navigator, vision, clock, yaw=math.pi / 2)
    assert navigator.phase is Phase.INSPECT
    clock.advance(600)
    navigator.observe_obstacle_scan(revolution(clock.ms), clock.ms)
    vision.result = vision_result(clock.ms, clock.ms, present=False, hits=0, last_seen_ms=None)
    runtime.tick(clock.ms)
    assert runtime.behavior.state == "ZONE_INSPECT"
    assert runtime._zone_inspector._zone == "A"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_setting_is_boolean(value):
    with pytest.raises(ConfigError):
        NavParams.of({"nav": {"relaxed_follow": value}})


def test_default_on_and_switch_off():
    assert NavParams.of({}).relaxed_follow
    assert not NavParams.of({"nav": {"relaxed_follow": False}}).relaxed_follow
