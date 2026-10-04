"""I 점 힌트와 J 구역 조준/L 저장 동선이 공유하는 실제 명령·안전 경계."""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient
from test_lidar_patrol import POINTS, SCAN, build
from test_route_runtime import fresh, route, saved_runtime, tick
from test_runtime import config, vision_result
from test_runtime_lidar import _camera_aim_runtime, _camera_aim_tick

from host.behavior.patrol import Phase
from host.behavior.routes import RoutePoint
from host.dashboard.server import create_app
from host.dashboard.state import DashboardState
from host.runtime import dashboard_wiring
from host.slam.scan_match import MatchResult

__all__ = ["config"]


def test_dashboard_wiring_keeps_route_and_point_hint_commands(config, clock, tmp_path):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    wiring = dashboard_wiring(runtime, config, vision=None, blackbox=None)
    app = create_app(DashboardState("integration-test", stale_after_ms=3000), **wiring)
    with TestClient(app) as client:
        response = client.post(
            "/api/command/route", json={"action": "start", "route_id": "route-test"}
        )
        assert response.json()["accepted"]
        runtime.tick(clock.ms)
        assert navigator.route_active

        response = client.post("/api/command/locate", json={"x": 2.5, "y": 2.0})
        assert response.json()["accepted"]
        runtime.tick(clock.ms)
        status = client.get("/api/nav").json()
        assert status["point_hint"] == {"x": 2.5, "y": 2.0, "radius": 0.6}
        assert status["stale"] and not status["verified"]
        assert status["route"]["id"] == "route-test"
        assert runtime.commander.intent.type_ == "STOP"

        assert client.post("/api/command/route", json={"action": "stop"}).json()["accepted"]
        runtime.tick(clock.ms)
        assert not navigator.route_active
        assert runtime.behavior.state == "IDLE"
        assert runtime.commander.intent.type_ == "STOP"


@pytest.mark.parametrize("hint_first", [False, True])
def test_point_hint_in_same_tick_prevents_queued_route_start(config, clock, tmp_path, hint_first):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    if hint_first:
        assert runtime.ask_locate_point(2.0, 2.0)[0]
    assert runtime.ask_route("route-test")[0]
    if not hint_first:
        assert runtime.ask_locate_point(2.0, 2.0)[0]
    runtime.tick(clock.ms)
    assert navigator._point_hint is not None
    assert not runtime.nav_status()["route_feedback"]["accepted"]
    assert not navigator.route_active
    assert runtime.commander.intent.type_ == "STOP"


def test_route_hint_waits_for_votes_then_repeats_full_aim_and_dwell():
    navigator = build(reloc_votes=3)
    navigator._own_localization = True
    fresh(navigator, 1000, (1.0, 1.0, math.pi / 2))
    navigator._mark_verified()
    assert navigator.start_route(route(RoutePoint(x=1.0, y=1.0, aim_deg=90, dwell_s=1)), 1000)[0]
    navigator.step(1000)
    tick(navigator, 1100, (1.0, 1.0, math.pi / 2))
    assert navigator.route_status()["phase"] == "dwell"
    assert navigator.hint_point(1.0, 1.0, 1200)
    navigator.step(1200)
    assert navigator.phase is Phase.LOST
    assert navigator.commander.intent.type_ == "STOP"
    assert navigator._route_dwell_until_ms is None

    result = MatchResult((1.0, 1.0, 0.0), 100)
    for now in (1300, 1400):
        navigator._apply_reloc_result(result, POINTS, SCAN, now)
        navigator.step(now)
        assert not navigator.pose_verified
        assert navigator.commander.intent.type_ == "STOP"
    navigator._apply_reloc_result(result, POINTS, SCAN, 1500)
    navigator.step(1500)
    assert navigator.pose_verified and navigator._point_hint is None
    assert navigator.phase is Phase.AIMING
    assert navigator.commander.intent.fields["step"] == 0

    tick(navigator, 1600, (1.0, 1.0, math.pi / 2))
    assert navigator.route_status()["dwell_remaining_s"] == 1.0
    tick(navigator, 2599, (1.0, 1.0, math.pi / 2))
    assert navigator.route_active
    tick(navigator, 2600, (1.0, 1.0, math.pi / 2))
    assert navigator.route_status()["status"] == "completed"


@pytest.mark.usefixtures("unlock_modes")
def test_point_hint_suspends_zone_camera_arrival_until_pose_is_verified(config, clock, tmp_path):
    runtime, navigator, vision = _camera_aim_runtime(config, clock, tmp_path)
    _camera_aim_tick(runtime, navigator, vision, clock)
    _camera_aim_tick(runtime, navigator, vision, clock, yaw=math.pi / 2)
    assert navigator.phase is Phase.INSPECT
    assert runtime.ask_locate_point(1.0, 1.0)[0]
    clock.advance(100)
    vision.result = vision_result(clock.ms, clock.ms, present=False, hits=0, last_seen_ms=None)
    runtime.tick(clock.ms)
    assert runtime.behavior.state == "PATROL"
    assert navigator.phase is Phase.LOST
    assert runtime._zone_inspector._visit_seen == []
    assert runtime.commander.intent.type_ == "STOP"
    assert navigator.zones.get("A").aim_deg == 90.0

    navigator.reloc_votes = 2
    result = MatchResult((1.0, 1.0, math.pi / 2), 100)
    for vote in range(2):
        clock.advance(100)
        navigator._apply_reloc_result(result, POINTS, SCAN, clock.ms)
        runtime.tick(clock.ms)
        assert navigator.pose_verified is (vote == 1)
        assert runtime._zone_inspector._visit_seen == []
    _camera_aim_tick(runtime, navigator, vision, clock, yaw=math.pi / 2)
    assert runtime.behavior.state == "ZONE_INSPECT"
    assert runtime._zone_inspector._aligned


def test_point_hint_cannot_release_estop_or_start_queued_route(config, clock, tmp_path):
    runtime, navigator = saved_runtime(config, clock, tmp_path)
    commands = dashboard_wiring(runtime, config, vision=None, blackbox=None)["commands"]
    assert commands.route_start("route-test").accepted
    assert commands.estop().accepted
    assert commands.locate_point(2.0, 2.0).accepted
    runtime.tick(clock.ms)
    assert runtime.behavior.state == "FAILSAFE"
    assert not navigator.route_active
    assert navigator._point_hint is not None
    assert not navigator.pose_verified and not navigator.pose_seeded
    assert runtime.commander.intent.type_ == "STOP"
