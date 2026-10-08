"""AG: 빔 비움/복귀, 최신 거리, gap, 탈출, TTL과 실제 MOVE 경계."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from test_lidar_patrol import Reading, build, open_room

from host.behavior.live_nav import LiveClear, LocalScan, NavParams
from host.behavior.patrol import Phase
from host.behavior.planner import Plan, inflate, plan_to
from host.behavior.zones import ZoneStore
from host.common.config import ConfigError
from host.common.lidar_link import Scan


def revolution(seq: int, distance: float = 3.0, *, front: float | None = None) -> Scan:
    return Scan(
        "lidar-a",
        "boot-a",
        seq,
        seq * 100,
        tuple(
            (math.radians(a), front if front is not None and abs(a) <= 30 else distance)
            for a in range(-180, 180, 5)
        ),
    )


def ready(*, front: float | None = None):
    c = build(ready=False)
    c.zones = ZoneStore(("A",))
    c.zones.place(4.0, 2.0)
    c.observe_telemetry(Reading(), 1000)
    c.observe_map_pose((2.0, 2.0, 0.0), 1000)
    c.observe_obstacle_scan(revolution(1, front=front), 1000)
    c.start()
    return c


def test_verified_beams_open_old_occupied_wall_and_do_not_change_source():
    c = ready()
    c.grid.cells[:, 50] = 5.0
    original = c.grid.cells.copy()
    overlay = LiveClear(NavParams())
    # Wide fan passes the entire old separator, endpoints stay beyond it.
    scan = Scan("lidar-a", "boot-a", 1, 0, tuple((math.radians(a), 4.0) for a in range(-80, 81)))
    for seq in range(1, 4):
        overlay.observe(
            c.grid,
            (2.0, 2.0, 0.0),
            replace(scan, seq=seq),
            seq * 100,
            verified=True,
            range_m=c.range_m,
        )
        if seq < 3:
            assert not overlay.mask(c.grid, seq * 100).any()
    nav = overlay.grid(c.grid, 300, c.plan_params.free_thresh)
    result = plan_to("A", (4, 2), (2, 2), nav, inflate(nav, c.plan_params), c.plan_params)
    assert result.reachable
    assert np.array_equal(c.grid.cells, original)
    assert not plan_to(
        "A", (4, 2), (2, 2), c.grid, inflate(c.grid, c.plan_params), c.plan_params
    ).reachable


def test_overlay_expiry_and_hit_contradiction_restore_original_occupancy():
    grid = open_room()
    cell = grid.to_cell(2, 2)
    grid.cells[cell] = 5
    overlay = LiveClear(NavParams())
    for seq in range(1, 4):
        overlay.observe(
            grid,
            (1, 2, 0),
            Scan("a", "b", seq, 0, ((0, 3),)),
            seq * 100,
            verified=True,
            range_m=(0.1, 8),
        )
    assert overlay.mask(grid, 2299)[cell]
    assert not overlay.mask(grid, 2300)[cell]
    overlay.observe(
        grid, (1, 2, 0), Scan("a", "b", 4, 0, ((0, 1),)), 400, verified=True, range_m=(0.1, 8)
    )
    assert not overlay.mask(grid, 400)[cell]


def test_unverified_duplicate_and_nonconsecutive_scans_cannot_clear():
    grid = open_room()
    overlay = LiveClear(NavParams())
    scan = Scan("a", "b", 1, 0, ((0, 3),))
    for now in range(3):
        overlay.observe(grid, (1, 2, 0), scan, now, verified=True, range_m=(0.1, 8))
    assert not overlay.mask(grid, 3).any()
    overlay.observe(
        grid, (1, 2, 0), replace(scan, seq=2, points=()), 4, verified=True, range_m=(0.1, 8)
    )
    overlay.observe(grid, (1, 2, 0), replace(scan, seq=3), 5, verified=True, range_m=(0.1, 8))
    assert not overlay.mask(grid, 5).any()
    overlay.observe(grid, (1, 2, 0), replace(scan, seq=4), 6, verified=False, range_m=(0.1, 8))
    assert not overlay.mask(grid, 6).any()


@pytest.mark.parametrize(
    ("distance", "action"), [(0.24, "stop"), (0.25, "slow"), (0.32, "slow"), (0.40, "clear")]
)
def test_latest_distance_constrains_actual_forward_move(distance, action):
    c = ready()
    c.plan = Plan("A", ((2, 2), (4, 2)), 2, effective=(4, 2))
    c.phase = Phase.MOVING
    # Sensor decision is tested independently of global map replanning.
    c._local_scan.observe(revolution(2, front=distance), 1000, c.range_m)
    c._follow()
    if action == "stop":
        assert c.commander.intent.type_ == "STOP"
    else:
        assert c.local_status["action"] == action
        step = c.commander.intent.fields["step"]
        assert 0 <= step <= c.drive.step_mm
        if action == "slow":
            assert step < c.drive.step_mm


def test_gap_uses_widest_observed_run_but_prefers_small_turn():
    local = LocalScan(NavParams())
    local.observe(revolution(1, front=0.2), 1000, (0.1, 8))
    gap = local.gap(0.15)
    assert gap is not None
    assert 30 < abs(math.degrees(gap[0])) < 60
    assert gap[2] > math.pi
    # Missing directions are not inferred free.
    local.observe(Scan("lidar-a", "boot-a", 2, 0, ((0, 3),)), 1001, (0.1, 8))
    assert local.gap(0.15) is None


def test_start_inside_old_map_obstacle_escapes_without_settle_wait():
    c = ready()
    c.grid.cells[c.grid.to_cell(*c.pose[:2])] = 5
    c._rebuild_masks()
    c.step(1000)
    assert c.phase is not Phase.LOST
    assert c.local_status["action"] == "escape"
    c.observe_map_pose(c.pose, 1100)
    c.observe_obstacle_scan(revolution(2), 1100)
    c.step(1100)
    assert c.commander.intent.type_ == "MOVE"
    assert c.commander.intent.fields["step"] > 0
    assert c.commander.intent.fields["angle"] == 0


def test_dynamic_hit_expires_after_two_seconds_and_refreshes_when_seen():
    c = ready(front=1.0)
    assert c._dynamic_seen
    cell = c.grid.to_cell(3, 2)
    assert cell in c._dynamic_seen
    c.observe_map_pose(c.pose, 2900)
    c.observe_obstacle_scan(revolution(2, front=1.0), 2900)
    c._refresh_navigation(3000)
    assert cell in c._dynamic_seen
    c._refresh_navigation(4899)
    assert cell in c._dynamic_seen
    c._refresh_navigation(4900)
    assert cell not in c._dynamic_seen
    assert not c._dynamic.any()


def test_close_positive_range_below_localization_minimum_still_stops():
    c = ready()
    c._local_scan.observe(revolution(2, front=0.05), 1000, c.range_m)
    c.plan = Plan("A", ((2, 2), (4, 2)), 2)
    c.phase = Phase.MOVING
    c._follow()
    assert c.commander.intent.type_ == "STOP"


def test_scan_loss_stops_motion_and_duplicate_does_not_refresh_age():
    c = ready()
    c.step(1000)
    assert c.commander.intent.type_ == "MOVE"
    c.observe_map_pose(c.pose, 1501)
    c.observe_telemetry(Reading(), 1501)
    c.observe_obstacle_scan(revolution(1), 1501)
    c.step(1501)
    assert c.phase is Phase.LOST
    assert c.commander.intent.type_ == "STOP"
    assert c.local_status["reason"] == "scan_unavailable"


def test_estop_onboard_stop_and_pose_loss_override_escape():
    for mode in ("estop", "onboard", "pose"):
        c = ready()
        c._start_avoidance("start_escape")
        if mode == "estop":
            c.emergency_stop("test")
        elif mode == "onboard":
            c.safety.obstacle = True
        else:
            c.localization.last_pose_ms = None
        c.step(1000)
        assert c.commander.intent.type_ != "MOVE"


def test_gap_turn_forward_then_replan_preserves_target():
    c = ready(front=0.2)
    c._start_avoidance("path_obstacle")
    assert c._avoidance is not None
    heading = c._avoidance[2]
    c._avoid()
    assert c.commander.intent.fields["step"] == 0
    assert 0 < abs(c.commander.intent.fields["angle"]) <= 15
    c.observe_map_pose((2, 2, heading), 1100)
    c.observe_obstacle_scan(revolution(2), 1100)
    c._avoid()
    assert 0 < c.commander.intent.fields["step"] <= c.drive.step_mm * 0.5
    assert c.commander.intent.fields["angle"] == 0
    c.observe_map_pose((2 + 0.21 * math.cos(heading), 2 + 0.21 * math.sin(heading), heading), 1200)
    c._now_ms = 1200
    c._avoid()
    assert c._avoidance is None
    assert c.phase is Phase.PLANNING
    assert c.commander.intent.type_ == "STOP"


def test_new_hit_events_are_bounded_but_all_points_are_kept():
    c = ready()
    c.take_new_obstacles()
    c.observe_obstacle_scan(revolution(2, distance=1), 1100)
    assert len(c.take_new_obstacles()) == 1
    assert len(c.obstacles) > 20
    c.observe_obstacle_scan(revolution(3, distance=1.1), 1200)
    assert c.take_new_obstacles() == ()
    assert len(c.obstacles) > 40


def test_live_clear_evidence_does_not_accumulate_across_long_scan_gaps():
    grid = open_room()
    overlay = LiveClear(NavParams())
    for seq, now in enumerate((0, 100, 1000), start=1):
        overlay.observe(
            grid,
            (1, 2, 0),
            Scan("a", "b", seq, now, ((0, 3),)),
            now,
            verified=True,
            range_m=(0.1, 8),
        )
    assert not overlay.mask(grid, 1000).any()


def test_blocked_patrol_target_is_preserved_until_recovery_decision():
    c = ready()
    c.grid.cells[:, 60] = 5
    c._rebuild_masks()
    c.plan = Plan("A")
    c._replan()
    assert c.plan.label == "A"
    assert c.recovery.active is not None
    assert c.commander.intent.type_ == "STOP"
    assert c.stats.cycles == 0 and not c.visited


def test_unreachable_remaining_zone_does_not_complete_patrol_cycle():
    c = ready()
    c.zones = ZoneStore(("A", "B"))
    c.zones.place(4, 2)
    c.zones.place(4, 3)
    c.grid.cells[:, 60] = 5
    c._rebuild_masks()
    c.visited = frozenset({"A"})
    c._replan()
    assert c.plan.label == "B"
    assert c.visited == frozenset({"A"})
    assert c.stats.cycles == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"local_stop_m": 0.1},
        {"local_stop_m": 0.4},
        {"live_clear_scans": True},
        {"dynamic_ttl_ms": -1},
        {"local_slow_m": float("nan")},
    ],
)
def test_invalid_nav_configuration_rejected(patch):
    with pytest.raises(ConfigError):
        NavParams.of({"nav": patch})
