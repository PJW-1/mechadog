"""동선 순서·방위·머무름·반복과 취소 경합을 합성 지도/가짜 시계로 검증한다."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from conftest import FakeClock
from test_lidar_patrol import DRIVE, Reading, build
from test_runtime import FakeSocket, config
from test_runtime_lidar import _camera_aim_runtime, _camera_aim_tick, _patrolling

from host.behavior.fsm import Event
from host.behavior.live_nav import NavParams
from host.behavior.patrol import PatrolController, Phase
from host.behavior.planner import Plan
from host.behavior.routes import Route, RoutePoint, load_routes, route_digest, routes_content
from host.common.lidar_link import Scan
from host.runtime import Runtime

__all__ = ["config"]


def route(*points: RoutePoint, repeat: int = 1) -> Route:
    return Route(id="route-test", name="합성 동선", points=points, repeat=repeat)


def fresh(controller: PatrolController, now: int, pose: tuple[float, float, float]) -> None:
    controller.observe_telemetry(Reading(), now)
    controller.observe_map_pose(pose, now)
    controller.observe_obstacle_scan(Scan("test", "boot", now, now, ((0.0, 3.0),)), now)


def tick(controller: PatrolController, now: int, pose: tuple[float, float, float]) -> None:
    fresh(controller, now, pose)
    controller.step(now)


def test_route_keeps_point_order_then_aims_before_dwell_and_completes() -> None:
    controller = build()
    first = RoutePoint(x=1.0, y=1.0, aim_deg=90, dwell_s=1.0)
    second = RoutePoint(x=4.0, y=1.0)
    fresh(controller, 1000, (2.0, 2.0, 0.0))
    assert controller.start_route(route(first, second), 1000)[0]
    controller.step(1000)
    assert controller.goal == (first.x, first.y)
    assert controller.commander.intent.type_ == "MOVE"
    tick(controller, 1100, (1.0, 1.0, 0.0))
    assert controller.phase is Phase.AIMING
    tick(controller, 1200, (1.0, 1.0, 0.0))
    assert controller.commander.intent.fields == {"step": 0.0, "angle": DRIVE.spin_turn_deg}
    tick(controller, 1300, (1.0, 1.0, math.pi / 2))
    assert controller.route_status()["phase"] == "dwell"
    assert controller.commander.intent.type_ == "STOP"
    tick(controller, 2299, (1.0, 1.0, math.pi / 2))
    assert controller.goal == (first.x, first.y)
    tick(controller, 2300, (1.0, 1.0, math.pi / 2))
    assert controller.goal == (second.x, second.y)
    assert controller.route_status()["point_index"] == 1
    for now in (2400, 2500, 2600):
        tick(controller, now, (4.0, 1.0, math.pi / 2))
    assert controller.route_status()["status"] == "completed"
    assert controller.commander.intent.type_ == "STOP"
    tick(controller, 2700, (4.0, 1.0, math.pi / 2))
    assert controller.commander.intent.type_ == "STOP"
    assert controller.stats.zones_visited == 0


@pytest.mark.parametrize("repeat,cycles", [(1, 1), (3, 3), (0, 5)])
def test_route_repeat_count_and_infinite_stop(repeat: int, cycles: int) -> None:
    controller = build()
    points = (RoutePoint(x=1.0, y=1.0), RoutePoint(x=4.0, y=1.0))
    now = 1000
    fresh(controller, now, (1.0, 1.0, 0.0))
    assert controller.start_route(route(*points, repeat=repeat), now)[0]
    visited = []
    for cycle in range(cycles):
        for index, point in enumerate(points):
            status = controller.route_status()
            visited.append((status["cycle"], status["point_index"]))
            for _ in range(3):
                now += 100
                tick(controller, now, (point.x, point.y, 0.0))
            assert visited[-1] == (cycle + 1, index)
    assert controller.stats.cycles == cycles
    assert controller.route_active is (repeat == 0)
    controller.cancel_route()
    tick(controller, now + 100, (4.0, 1.0, 0.0))
    assert controller.commander.intent.type_ == "STOP"
    assert not controller.route_active


@pytest.mark.parametrize("untrusted", ["stale", "unverified"])
def test_route_refuses_untrusted_start(untrusted: str) -> None:
    controller = build()
    fresh(controller, 1000, (2.0, 2.0, 0.0))
    if untrusted == "unverified":
        controller.localization.own_localization = True
    accepted, detail = controller.start_route(
        route(RoutePoint(x=1.0, y=1.0)), 2000 if untrusted == "stale" else 1000
    )
    assert not accepted
    assert "위치" in detail
    assert controller.goal is None


def test_route_rejects_wall_crossing_even_when_goto_can_go_around() -> None:
    controller = build()
    controller.grid.cells[10:55, 50] = 3
    controller._rebuild_masks()
    fresh(controller, 1000, (1.0, 1.0, 0.0))
    assert controller.goto(4.0, 1.0)[0], "벽 끝으로 우회하는 goto는 가능하다"
    accepted, detail = controller.start_route(
        route(RoutePoint(x=1.0, y=1.0), RoutePoint(x=4.0, y=1.0)), 1000
    )
    assert not accepted
    assert "동선" in detail
    assert not controller.route_active


@pytest.mark.parametrize("guard", ["stale", "obstacle", "body", "trust"])
def test_route_aiming_obeys_existing_safety_guards(guard: str) -> None:
    controller = build()
    fresh(controller, 1000, (1.0, 1.0, 0.0))
    assert controller.start_route(route(RoutePoint(x=1.0, y=1.0, aim_deg=90)), 1000)[0]
    controller.step(1000)
    tick(controller, 1100, (1.0, 1.0, 0.0))
    assert controller.commander.intent.type_ == "MOVE"
    if guard == "obstacle":
        controller.safety.obstacle = True
    elif guard == "body":
        controller.grid.cells[controller.grid.to_cell(1.0, 1.0)] = 5.0
    elif guard == "trust":
        controller.localization.own_localization = True
    controller.step(1800 if guard == "stale" else 1200)
    assert controller.commander.intent.type_ == "STOP"
    assert controller.route_status()["point_index"] == 0


def test_route_blocked_while_moving_does_not_fall_back_to_zone_patrol() -> None:
    controller = build(nav_params=NavParams(relaxed_follow=False, route_direct=False))
    fresh(controller, 1000, (1.0, 1.0, 0.0))
    assert controller.start_route(route(RoutePoint(x=4.0, y=1.0)), 1000)[0]
    controller.grid.cells[:, 50:60] = 3
    controller._rebuild_masks()
    controller.plan = Plan("GOAL")
    controller.step(1000)
    assert controller.route_status()["status"] == "active"
    assert controller.commander.intent.type_ == "STOP"
    assert not controller.holding_goal
    assert controller.goal == (4.0, 1.0)


@pytest.mark.parametrize("after_start", [False, True])
def test_route_does_not_snap_a_dynamically_blocked_point_elsewhere(after_start):
    controller = build(nav_params=NavParams(relaxed_follow=False, route_direct=False))
    fresh(controller, 1000, (1.0, 1.0, 0.0))
    saved = route(RoutePoint(x=4.0, y=1.0))
    if after_start:
        assert controller.start_route(saved, 1000)[0]
    controller._dynamic_seen[controller.grid.to_cell(4.0, 1.0)] = (4.0, 1.0, 1000)
    controller._refresh_navigation(1000)
    if after_start:
        controller.step(1000)
        assert controller.route_status()["status"] == "active"
    else:
        accepted, _detail = controller.start_route(saved, 1000)
        assert not accepted
    assert controller.commander.intent.type_ == "STOP"


def test_route_waits_for_zone_inspection_after_requested_aim() -> None:
    controller = build()
    controller.zones.set_aim_deg("A", -90.0)
    waiting = True
    controller.wait_for_inspection = lambda _label: waiting
    fresh(controller, 1000, (1.0, 1.0, 0.0))
    assert controller.start_route(route(RoutePoint(x=1.0, y=1.0, label="A", aim_deg=90)), 1000)[0]
    controller.step(1000)
    tick(controller, 1100, (1.0, 1.0, math.pi / 2))
    assert controller.inspection_ready("A", 1100)
    assert not controller.inspection_ready("B", 1100)
    tick(controller, 1200, (1.0, 1.0, math.pi / 2))
    assert controller.route_active
    assert controller.route_status()["phase"] == "inspection"
    waiting = False
    tick(controller, 1300, (1.0, 1.0, math.pi / 2))
    assert controller.route_status()["status"] == "completed"
    assert controller.stats.zones_visited == 1


def saved_runtime(
    config: dict, clock: FakeClock, directory: Path
) -> tuple[Runtime, PatrolController]:
    runtime, navigator = _patrolling(config, clock)
    navigator.maps_dir = directory
    saved = route(RoutePoint(x=1.0, y=1.0), RoutePoint(x=4.0, y=1.0))
    (directory / "routes.json").write_bytes(routes_content({saved.id: saved}))
    return runtime, navigator


def test_runtime_starts_new_saved_route_without_restart_and_exposes_progress(
    config, clock, tmp_path
):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    assert runtime.ask_route("route-test")[0]
    assert not navigator.route_active
    runtime.tick(clock.ms)
    status = runtime.nav_status()
    assert status["route_feedback"]["accepted"]
    assert status["route"]["point_index"] == 0
    assert status["route"]["status"] == "active"
    assert status["pose"] == [2.0, 2.0, 0.0]


def test_runtime_rechecks_localization_after_start_queue(config, clock, tmp_path):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    assert runtime.ask_route("route-test")[0]
    clock.advance(2001 if navigator.nav_params.relaxed_follow else DRIVE.pose_timeout_ms + 1)
    runtime.tick(clock.ms)
    assert not navigator.route_active
    assert not runtime.nav_status()["route_feedback"]["accepted"]
    assert runtime.commander.intent.type_ == "STOP"


@pytest.mark.parametrize("pending", [False, True])
def test_runtime_route_stop_is_immediate_and_cancels_queued_start(config, clock, tmp_path, pending):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    socket = FakeSocket(clock)
    runtime.begin(socket)
    assert runtime.ask_route("route-test")[0]
    if not pending:
        runtime.tick(clock.ms)
        assert navigator.route_active
    assert runtime.ask_route_stop()[0]
    assert runtime.commander.intent.type_ == "STOP"
    assert socket.types()[-1] == "STOP", "다음 tick 전 실제 STOP 전문이 나가야 한다"
    assert runtime.behavior.state == "IDLE"
    runtime.tick(clock.ms)
    assert not navigator.route_active
    assert not runtime._patrol_asked
    assert runtime.nav_requests.route_asked is None
    assert runtime.commander.intent.type_ == "STOP"


def test_existing_patrol_stop_also_cancels_pending_route(config, clock, tmp_path):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    assert runtime.ask_route("route-test")[0]
    runtime.apply_external(Event.MANUAL_ON)
    runtime.apply_external(Event.MANUAL_OFF)
    runtime.tick(clock.ms)
    assert not navigator.route_active
    assert runtime.behavior.state == "IDLE"
    assert runtime.commander.intent.type_ == "STOP"


def test_start_after_stop_before_next_tick_is_explicitly_refused(config, clock, tmp_path):
    runtime, _navigator = saved_runtime(config, clock, tmp_path)
    runtime.ask_route_stop()
    accepted, detail = runtime.ask_route("route-test")
    assert not accepted
    assert "정지" in detail
    assert runtime.nav_requests.route_asked is None


def test_stop_during_route_file_read_cannot_rearm_start(config, clock, tmp_path, monkeypatch):
    runtime, _navigator = saved_runtime(config, clock, tmp_path)

    def read_then_stop(directory):
        result = load_routes(directory)
        runtime.ask_route_stop()
        return result

    monkeypatch.setattr("host.behavior.nav_requests.load_routes", read_then_stop)
    assert not runtime.ask_route("route-test")[0]
    runtime.tick(clock.ms)
    assert runtime.behavior.state == "IDLE"
    assert runtime.nav_requests.route_asked is None
    assert runtime.commander.intent.type_ == "STOP"


def test_route_rejects_changed_confirmation_content_and_keeps_accepted_snapshot(
    config, clock, tmp_path
):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    initial = load_routes(tmp_path)["route-test"]
    digest = route_digest(initial)
    changed = initial.model_copy(update={"name": "변경된 동선"})
    (tmp_path / "routes.json").write_bytes(routes_content({changed.id: changed}))
    assert not runtime.ask_route(initial.id, digest)[0]
    assert runtime.nav_requests.route_asked is None
    assert runtime.ask_route(initial.id, route_digest(changed))[0]
    (tmp_path / "routes.json").write_bytes(routes_content({initial.id: initial}))
    runtime.tick(clock.ms)
    assert navigator.route_status()["name"] == changed.name


@pytest.mark.parametrize("interruption", ["stale", "obstacle", "trust", "alarm"])
def test_interrupted_dwell_requires_full_dwell_after_recovery(interruption):
    controller = build()
    fresh(controller, 1000, (1.0, 1.0, 0.0))
    assert controller.start_route(route(RoutePoint(x=1.0, y=1.0, dwell_s=1)), 1000)[0]
    controller.step(1000)
    tick(controller, 1100, (1.0, 1.0, 0.0))
    assert controller.route_status()["phase"] == "dwell"
    if interruption == "obstacle":
        controller.safety.obstacle = True
        controller.step(1200)
        controller.safety.obstacle = False
    elif interruption == "trust":
        controller.localization.own_localization = True
        controller.step(1200)
        controller.pose_seeded = True
    elif interruption == "alarm":
        controller.resume()
    else:
        controller.step(2000)
    tick(controller, 2200, (1.0, 1.0, 0.0))
    assert controller.route_status()["dwell_remaining_s"] == 1.0
    tick(controller, 3199, (1.0, 1.0, 0.0))
    assert controller.route_active
    tick(controller, 3200, (1.0, 1.0, 0.0))
    assert controller.route_status()["status"] == "completed"


@pytest.mark.usefixtures("unlock_modes")
def test_single_zone_repeated_route_reopens_actual_inspection(config, clock):
    runtime, navigator, vision = _camera_aim_runtime(config, clock)
    assert navigator.start_route(
        route(RoutePoint(x=1.0, y=1.0, aim_deg=90, label="A"), repeat=2), clock.ms
    )[0]
    for _ in range(3):
        _camera_aim_tick(runtime, navigator, vision, clock, yaw=math.pi / 2)
    assert runtime.behavior.state == "ZONE_INSPECT"
    first_visit = navigator.route_visit
    # 실제 카메라 점검이 완료했을 때와 같은 FSM 경계를 통과한다.
    runtime._zone_inspector._concluded = True
    runtime.apply_external(Event.ZONE_CLEAR)
    for _ in range(5):
        _camera_aim_tick(runtime, navigator, vision, clock, yaw=math.pi / 2)
        if runtime.behavior.state == "ZONE_INSPECT":
            break
    assert navigator.route_visit != first_visit
    assert navigator.route_status()["cycle"] == 2
    assert runtime.behavior.state == "ZONE_INSPECT"
    assert runtime._zone_inspector._zone == "A"


@pytest.mark.usefixtures("unlock_modes")
def test_mismatched_zone_label_does_not_wait_for_impossible_camera_visit(config, clock):
    runtime, navigator, vision = _camera_aim_runtime(config, clock)
    assert navigator.start_route(route(RoutePoint(x=1.0, y=1.0, label="B", aim_deg=0)), clock.ms)[0]
    for _ in range(4):
        _camera_aim_tick(runtime, navigator, vision, clock)
    assert navigator.route_status()["status"] == "completed"
    assert runtime.behavior.state == "PATROL"
    assert runtime.commander.intent.type_ == "STOP"
    assert runtime._zone_inspector._visit_seen == []


@pytest.mark.usefixtures("unlock_modes")
@pytest.mark.parametrize("label", [None, "A"])
def test_route_camera_does_not_preempt_arrival_and_uses_route_heading(config, clock, label):
    runtime, navigator, vision = _camera_aim_runtime(config, clock)
    # 구역 aim 유무와 무관하게 지점 도착·동선 방위가 먼저다.
    navigator.zones.set_aim_deg("A", None)
    runtime._zone_inspector._anchors = navigator.zones.as_tuple()
    assert navigator.start_route(
        route(RoutePoint(x=1.0, y=1.0, aim_deg=-90, label=label, dwell_s=1)), clock.ms
    )[0]
    _camera_aim_tick(runtime, navigator, vision, clock)
    _camera_aim_tick(runtime, navigator, vision, clock)
    assert runtime.behavior.state == "PATROL"
    assert runtime.commander.intent.fields == {"step": 0.0, "angle": -DRIVE.spin_turn_deg}
    _camera_aim_tick(runtime, navigator, vision, clock, yaw=-math.pi / 2)
    _camera_aim_tick(runtime, navigator, vision, clock, yaw=-math.pi / 2)
    assert runtime.behavior.state == ("ZONE_INSPECT" if label else "PATROL")
