"""AH 막힘 정책: 실제 정지/스캔 관문, 기억, 순회, 비용과 사건 검증."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from test_live_nav import ready, revolution

from host.behavior.blockage import BlockageMemory
from host.behavior.live_nav import LiveClear, NavParams
from host.behavior.patrol import HOME_LABEL, Phase
from host.behavior.planner import (
    Plan,
    astar,
    body_collision_mask,
    distance_costs,
    inflate,
    plan_to,
    segment_clear,
)
from host.behavior.routes import Route, RoutePoint
from host.behavior.zones import ZoneStore
from host.common.lidar_link import Scan
from host.report.situation import describe


def tick(c, now, yaw=0.0, front=None, distance=3.0):
    c.observe_map_pose((*c.pose[:2], yaw), now)
    scan = revolution(now, distance=distance, front=front)
    scan = replace(
        scan,
        points=tuple(((a - yaw + math.pi) % (2 * math.pi) - math.pi, d) for a, d in scan.points),
    )
    c.observe_obstacle_scan(scan, now)
    c.safety.last_seen_ms = now
    c.step(now)
    c.note_sent(c.commander.tick(now), now)


def finish_scan(c, start=1100, front=0.6, distance=3.0):
    """STOP 송신 후 측정 방위 한 바퀴, 다시 STOP 후 새 스캔을 입력한다."""
    tick(c, start, front=front, distance=distance)
    tick(c, start + 2100, front=front, distance=distance)
    for index in range(1, 14):
        tick(
            c,
            start + 2100 + index * 100,
            yaw=(index * math.pi / 6) % (2 * math.pi),
            front=front,
            distance=distance,
        )
    tick(c, start + 5000, yaw=math.pi / 6, front=front, distance=distance)


def blocked_target(c):
    c.plan = Plan("A", ((2, 2), (4, 2)), 2, effective=(4, 2))
    c.waypoint_index = 1
    c.phase = Phase.MOVING


def test_persistent_obstacle_detours_after_one_measured_sweep_and_deduplicates():
    c = ready(front=0.6)
    blocked_target(c)
    c._follow()
    assert c.recovery.active is not None
    assert c.commander.intent.type_ == "STOP"
    assert not c.take_navigation_events()
    finish_scan(c)
    assert c.recovery.active is None
    assert c.plan.reachable and c.plan.label == "A"
    events = c.take_navigation_events()
    assert [e["event"] for e in events] == ["obstacle_detour"]
    assert events[0]["judgement"]["severity"] == "low"
    assert events[0]["judgement"]["x"] > c.pose[0]
    assert c.recovery.memory.items[0].confirmed
    assert c.recovery.memory.items[0].expires_ms > 100000
    recovery_id = c.recovery.memory.items[0].id
    from host.behavior.blockage import Recovery

    c.recovery.report("obstacle_detour", Recovery("A", 0, 0, recovery_id))
    assert not c.take_navigation_events()
    assert not any(
        segment_clear(c.grid, c.recovery.memory.mask(c.grid, 0.15), a, b) is False
        for a, b in zip(c.plan.waypoints, c.plan.waypoints[1:], strict=False)
    )


def test_temporary_person_is_cleared_and_resumes_without_cleanup_event():
    c = ready(front=0.6)
    blocked_target(c)
    c._follow()
    finish_scan(c, front=None)
    assert c.plan.reachable
    assert not c.recovery.memory.items
    assert not c.take_navigation_events()
    assert not c.skipped


def test_known_wall_is_not_reported_or_grown_into_new_blockage():
    c = ready()
    memory = BlockageMemory(NavParams())
    c.grid.cells[:, 56] = 5.0
    assert memory.remember(c.grid, [(2.8, 2.0)], 1000) is None
    item = memory.remember(c.grid, [(2.6, 2.0)], 1000)
    assert item is not None
    scan = Scan("a", "b", 1, 0, ((0.0, 0.6), (0.05, 0.8)))
    memory.observe(c.grid, (2.0, 2.0, 0.0), scan, 3100)
    assert list(item.points.values()) == [(2.6, 2.0)]


def test_no_detour_skips_zone_and_does_not_count_as_visited():
    c = ready(front=0.6)
    c.grid.cells[:, 60] = 5
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    finish_scan(c, front=0.3, distance=0.3)
    assert c.skipped == {"A"}
    assert not c.visited and c.stats.zones_visited == 0
    (event,) = c.take_navigation_events()
    assert event["event"] == "zone_skipped"
    assert event["judgement"]["zone"] == "A"
    assert event["judgement"]["severity"] == "medium"
    c._complete_cycle()
    assert not c.skipped


def test_all_zones_blocked_attempts_home_and_waits_once_if_home_unreachable():
    c = ready()
    c._home = (4, 2)
    c.grid.cells[:, 60] = 5
    c.skipped = frozenset({"A"})
    c._rebuild_masks()
    c._replan()
    assert c.recovery.returning_home and c.plan.label == HOME_LABEL
    c._replan()
    assert c.recovery.waiting
    assert c.commander.intent.type_ == "STOP"
    assert [e["event"] for e in c.take_navigation_events()] == ["patrol_unavailable"]
    c.recovery.wait_patrol()
    assert not c.take_navigation_events()
    tick(c, 1100)
    assert c.recovery.waiting


def test_cleared_blockage_releases_wait_and_retries_skipped_zone():
    c = ready(front=0.6)
    c._home = c.pose[:2]
    item = c.recovery.memory.remember(c.grid, [(2.6, 2)], 1000)
    assert item is not None
    c.skipped = frozenset({"A"})
    c.recovery.wait_patrol()
    tick(c, 1100)
    assert not c.recovery.waiting and not c.skipped
    assert c.plan.reachable


def test_no_rotation_without_complete_safe_observation():
    c = ready(front=0.2)
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    tick(c, 1100, front=0.2)
    tick(c, 3200, front=0.2)
    assert c.commander.intent.type_ == "STOP"
    assert c.recovery.active is not None and c.recovery.active.settling_ms == 3200


@pytest.mark.parametrize("mode", ["stale", "onboard", "estop"])
def test_safety_overrides_recovery_scan(mode):
    c = ready(front=0.6)
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    tick(c, 1100, front=0.6)
    tick(c, 3200, front=0.6)
    assert c.recovery.active is not None
    if mode == "stale":
        c.localization.last_pose_ms = None
    elif mode == "onboard":
        c.safety.obstacle = True
    else:
        c.emergency_stop("test")
    c.step(3201)
    assert c.commander.intent.type_ == "STOP"


def test_stop_must_be_sent_before_scan_and_same_pose_does_not_complete_sweep():
    c = ready(front=0.6)
    blocked_target(c)
    c._last_sent_moving = True
    c.recovery.begin("path_obstacle")
    c._now_ms = 3200
    c.recovery.step()
    assert not c.recovery.active.scanning
    c._last_sent_moving = False
    c._stopped_since_ms = 1100
    for now in range(3200, 4500, 100):
        tick(c, now, front=0.6)
    assert c.recovery.active is not None
    assert c.recovery.active.swept_rad == 0


def test_blockage_memory_clears_only_observed_cells_and_expires_short_and_long():
    c = ready()
    memory = BlockageMemory(NavParams())
    item = memory.remember(c.grid, [(2.6, 2), (2.6, 2.4)], 1000)
    assert item is not None
    memory.observe(c.grid, c.pose, Scan("a", "b", 1, 0, ((0, 0.6),)), 3100)
    assert item.confirmed and item.expires_ms == 183100
    memory.observe(c.grid, c.pose, Scan("a", "b", 2, 0, ((0, 2),)), 3200)
    assert c.grid.to_cell(2.6, 2) not in item.points
    assert c.grid.to_cell(2.6, 2.4) in item.points
    memory.expire(183100)
    assert not memory.items
    memory.remember(c.grid, [(2.6, 2)], 200000)
    memory.expire(204000)
    assert not memory.items


def test_clearing_rays_stop_at_three_metres_and_marking_at_two_point_five():
    c = ready()
    memory = BlockageMemory(NavParams())
    item = memory.remember(c.grid, [(4, 2), (5.4, 2)], 1000)
    assert item is not None
    free = memory.observe(c.grid, c.pose, Scan("a", "b", 1, 0, ((0, 7),)), 1100)
    assert c.grid.to_cell(4, 2) in free
    assert c.grid.to_cell(5.4, 2) not in free
    assert c.grid.to_cell(5.4, 2) in item.points
    c.observe_obstacle_scan(Scan("lidar-a", "boot-a", 2, 0, ((0, 2.6),)), 1100)
    assert c.grid.to_cell(4.6, 2) not in c._dynamic_seen


def test_static_clear_overlay_also_has_raytrace_range_limit():
    c = ready()
    overlay = LiveClear(NavParams())
    for seq in range(1, 4):
        overlay.observe(
            c.grid,
            c.pose,
            Scan("a", "b", seq, 0, ((0, 7),)),
            seq * 100,
            verified=True,
            range_m=(0.1, 8),
        )
    assert overlay.mask(c.grid, 300)[c.grid.to_cell(4, 2)]
    assert not overlay.mask(c.grid, 300)[c.grid.to_cell(5.4, 2)]


def test_cost_decay_and_astar_choose_space_away_from_wall():
    blocked = np.zeros((35, 50), dtype=bool)
    blocked[:2] = True
    costs = distance_costs(blocked, 0.05, 0.55, 3)
    assert costs[2, 10] > costs[5, 10] > costs[10, 10] > costs[20, 10]
    direct = astar((3, 3), (3, 45), blocked)
    weighted = astar((3, 3), (3, 45), blocked, costs * 4)
    assert direct and weighted
    assert max(r for r, _ in weighted) > max(r for r, _ in direct) + 3
    assert all(not blocked[cell] for cell in weighted)


def test_weighted_plan_preserves_safe_start_connector_and_wall_spacing():
    c = ready()
    c.grid.cells[:25] = 5
    params = replace(c.plan_params, cost_weight=3)
    start, goal = (1.5, 1.5), (4.5, 1.5)
    blocked = inflate(c.grid, params)
    plan = plan_to("A", goal, start, c.grid, blocked, params)
    assert plan.reachable
    assert max(y for _, y in plan.waypoints) > 1.6
    body = body_collision_mask(c.grid, params)
    assert all(
        segment_clear(c.grid, body if i < plan.escape_end_index else blocked, a, b)
        for i, (a, b) in enumerate(zip(plan.waypoints, plan.waypoints[1:], strict=False))
    )


def test_local_costmap_tracks_robot_and_is_three_metres_wide():
    c = ready()
    local = c.local_costmap
    assert local["width"] * local["resolution_m"] == pytest.approx(3.05)
    origin = local["origin"]
    c.pose = (3, 2, 0)
    assert c.local_costmap["origin"][0] == pytest.approx(origin[0] + 1)


@pytest.mark.parametrize(
    "kind,word",
    [
        ("obstacle_detour", "치워 주세요"),
        ("zone_skipped", "구역 X 건너뜀"),
        ("patrol_unavailable", "순찰 불가"),
    ],
)
def test_event_sentences(kind, word):
    assert word in describe(kind, {"zone": "X"})


def test_next_zone_is_selected_after_confirmed_skip():
    c = ready(front=0.6)
    c.zones = ZoneStore(("A", "B"))
    c.zones.place(4, 2)
    c.zones.place(2, 4)
    c.grid.cells[:, 60] = 5
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    finish_scan(c, front=0.3, distance=0.3)
    assert c.skipped == {"A"}
    c.observe_obstacle_scan(revolution(10000), 6100)
    c._replan()
    assert c.plan.label == "B" and c.plan.reachable


def test_explicit_route_skips_blocked_zone_and_retries_on_next_lap():
    c = ready()
    route = Route(
        id="ah",
        name="AH",
        repeat=2,
        points=[RoutePoint(x=4, y=2, label="A"), RoutePoint(x=2, y=4, label="B")],
    )
    assert c.start_route(route, 1000)[0]
    c.skipped = frozenset({"A"})
    c.route.skip_point()
    assert c.route_status()["point_index"] == 1
    assert c.goal == (2, 4) and c.route_active
    c.route.next_point()
    assert c.route_status()["cycle"] == 2
    assert c.route_status()["point_index"] == 0
    assert not c.skipped and c.goal == (4, 2)


def test_route_next_dynamic_blockage_uses_recovery_instead_of_cancelling_route():
    c = ready()
    route = Route(
        id="ah",
        name="AH",
        repeat=2,
        points=[RoutePoint(x=2, y=2, label="B"), RoutePoint(x=4, y=2, label="A")],
    )
    assert c.start_route(route, 1000)[0]
    c.grid.cells[:, 60] = 5
    c.route.next_point()
    c._rebuild_masks()
    c._replan()
    assert c.route_active and c.recovery.active is not None
    assert c.recovery.active.target == "GOAL"


def test_grid_origin_change_preserves_world_blockage_until_real_beam_clears():
    c = ready()
    memory = BlockageMemory(NavParams())
    item = memory.remember(c.grid, [(2.6, 2)], 1000)
    assert item is not None
    from host.slam.occupancy import OccupancyGrid

    grid = OccupancyGrid(replace(c.grid.meta, origin_x=-0.5), c.grid.cells.copy())
    memory.observe(grid, c.pose, Scan("a", "b", 1, 0, ((math.pi / 2, 1),)), 1100)
    assert grid.to_cell(2.6, 2) in item.points
    memory.observe(grid, c.pose, Scan("a", "b", 2, 0, ((0, 2),)), 1200)
    assert not memory.items


def test_recovery_rotation_stalls_at_five_seconds_then_stops():
    c = ready(front=0.6)
    blocked_target(c)
    c.recovery.begin("path_obstacle")
    tick(c, 1100, front=0.6)
    tick(c, 3200, front=0.6)
    tick(c, 8200, front=0.6)
    assert c.recovery.active is not None and c.recovery.active.settling_ms == 8200
    assert c.commander.intent.type_ == "STOP"
