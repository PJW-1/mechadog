"""Safety decisions use offline fixtures; no sockets, real robot or assumed movement."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from host.behavior.refinement_mission import (
    MissionParams,
    MissionStop,
    ObservationFrame,
    RefinementMission,
    build_blocked,
    mission_params_from_config,
    safe_plan,
)
from host.common.lidar_link import Scan
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.settings import load, plan_params_from_config


def room() -> OccupancyGrid:
    return OccupancyGrid(MapMeta(0.1, 0.0, 0.0, 60, 50), np.full((50, 60), -3.0, dtype=np.float32))


def frame(
    now: int,
    *,
    pose: tuple[float, float, float] = (1.05, 1.05, 0.0),
    distance: float = 3.0,
    seq: int = 1,
    stationary: bool | None = False,
    level: bool | None = True,
) -> ObservationFrame:
    return ObservationFrame(
        pose,
        "localized",
        now,
        Scan("fixture", "boot1", seq, now, ((0.0, distance),)),
        now,
        now,
        stationary,
        level,
        sensor_extrinsics_verified=True,
        scan_pose=pose,
        scan_pose_received_ms=now,
    )


def mission(grid: OccupancyGrid | None = None) -> RefinementMission:
    return RefinementMission(grid or room(), "r1", (MissionStop("target", 4.05, 1.05),))


def test_unknown_no_go_and_boundary_all_receive_clearance() -> None:
    grid = room()
    grid.cells[20, 20] = 0.0
    no_go = np.zeros(grid.cells.shape, dtype=bool)
    no_go[30, 30] = True
    blocked = build_blocked(grid, MissionParams(), no_go)
    assert blocked[20, 24] and blocked[30, 34]
    assert blocked[0, 20] and blocked[4, 20]
    assert not blocked[5, 20]


def test_blocked_start_rejected_instead_of_legacy_escape() -> None:
    grid = room()
    blocked = np.zeros(grid.cells.shape, dtype=bool)
    blocked[10, 10] = True
    assert safe_plan(grid, blocked, (1.05, 1.05), (4.05, 1.05)) is None
    grid.cells[10, 10] = 3.0
    assert mission(grid).step(100, frame(100)).reason == "blocked_or_outside_start"


def test_raw_path_does_not_cut_an_obstacle_corner() -> None:
    grid = room()
    blocked = np.zeros(grid.cells.shape, dtype=bool)
    blocked[10:30, 25] = True
    blocked[29, 25:36] = True
    path = safe_plan(grid, blocked, (1.05, 1.05), (4.05, 3.05))
    assert path is not None and len(path) > 10
    cells = [grid.to_cell(*point) for point in path]
    for a, b in zip(cells, cells[1:], strict=False):
        assert max(abs(a[0] - b[0]), abs(a[1] - b[1])) == 1
        assert not blocked[b]
        if a[0] != b[0] and a[1] != b[1]:
            assert not blocked[a[0], b[1]] and not blocked[b[0], a[1]]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"pose_status": "candidate"}, "pose_candidate_unverified"),
        ({"pose_status": "lost"}, "pose_lost"),
        ({"pose_received_ms": 0}, "pose_stale"),
        ({"scan_received_ms": 0}, "scan_stale"),
        ({"telemetry_received_ms": 0}, "telemetry_stale"),
        ({"pose_frame_id": "other_map"}, "pose_frame_mismatch"),
        ({"sensor_extrinsics_verified": False}, "sensor_extrinsics_unverified"),
        ({"scan_pose": None}, "scan_pose_missing"),
        ({"scan_pose": (math.nan, 1.0, 0.0)}, "scan_pose_invalid"),
        ({"scan_pose_received_ms": 1749}, "scan_pose_stale"),
    ],
)
def test_unverified_or_stale_inputs_never_move(changes: dict[str, object], reason: str) -> None:
    # dataclasses.replace is deliberately exercised with malformed boundary inputs.
    observed = replace(frame(2000), **changes)  # type: ignore[arg-type]
    result = mission().step(2000, observed)
    assert result.action == "stop" and result.reason == reason


def test_forward_obstacle_stops_before_two_distinct_confirmations() -> None:
    controller = mission()
    first = frame(100, distance=0.28)
    assert controller.step(100, first).reason == "forward_obstacle_immediate_stop"
    assert controller.dynamic_obstacles == []
    assert controller.step(101, replace(first, pose_received_ms=101)).action == "stop"
    assert controller.dynamic_obstacles == []
    result = controller.step(102, frame(102, distance=0.28, seq=2))
    assert result.reason == "dynamic_obstacle_confirmed_replan"
    assert len(controller.dynamic_obstacles) == 1


def test_scan_translation_uses_laser_origin_while_route_uses_body_origin() -> None:
    controller = mission()
    body = (1.05, 1.05, 0.0)
    laser = (1.25, 1.15, 0.0)
    controller.step(100, replace(frame(100, pose=body, distance=0.65), scan_pose=laser))
    result = controller.step(
        110, replace(frame(110, pose=body, distance=0.65, seq=2), scan_pose=laser)
    )
    assert result.reason == "dynamic_obstacle_confirmed_replan"
    assert controller.dynamic_obstacles[0] == pytest.approx((1.9, 1.15))
    planned = controller.step(120, replace(frame(120, pose=body, seq=3), scan_pose=laser))
    assert planned.reason == "route_planned"
    assert planned.path[0] == pytest.approx(body[:2])


def test_25cm_clearance_includes_occupied_cell_area() -> None:
    grid = OccupancyGrid(MapMeta(0.05, 0, 0, 100, 100), np.full((100, 100), -3.0))
    grid.cells[50, 50] = 3.0
    blocked = build_blocked(grid, MissionParams())
    # Centres at sqrt(5^2+2^2)*5cm look farther than 25cm, but the
    # nearest corner of the occupied cell is only sqrt(4.5^2+1.5^2)*5cm.
    assert math.hypot(5, 2) * 0.05 > 0.25
    assert math.hypot(4.5, 1.5) * 0.05 < 0.25
    assert blocked[55, 52]


def test_confirmed_obstacle_detour_stays_in_observed_free_cells() -> None:
    controller = mission()
    controller.step(100, frame(100, distance=0.65))
    controller.step(110, frame(110, distance=0.65, seq=2))
    result = controller.step(120, frame(120, distance=3.0, seq=3))
    assert result.reason == "route_planned"
    assert result.path and any(abs(y - 1.05) >= 0.3 for _, y in result.path)
    assert all(not controller.blocked[controller.grid.to_cell(*p)] for p in result.path)
    assert controller.step(121, frame(121, seq=4)).action == "move"


def test_no_detour_through_unknown_when_corridor_is_blocked() -> None:
    grid = room()
    grid.cells[:, :] = 0.0
    grid.cells[5:16, 4:47] = -3.0
    controller = mission(grid)
    controller.step(100, frame(100, distance=0.65))
    controller.step(110, frame(110, distance=0.65, seq=2))
    result = controller.step(120, frame(120, seq=3))
    assert result.action == "stop" and result.reason == "route_blocked_no_unknown_sweep"


def test_map_revision_invalidates_route_and_expansion_reprojects_world_hit() -> None:
    controller = mission()
    controller.step(100, frame(100, distance=0.65))
    controller.step(110, frame(110, distance=0.65, seq=2))
    hit = controller.dynamic_obstacles[0]
    bigger = OccupancyGrid(
        MapMeta(0.1, -1.0, -1.0, 80, 70), np.full((70, 80), -3.0, dtype=np.float32)
    )
    controller.update_map(bigger, "r2")
    assert controller.blocked[bigger.to_cell(*hit)]
    assert (
        controller.step(120, frame(120, seq=3)).reason == "map_revision_changed_route_invalidated"
    )
    result = controller.step(130, frame(130, seq=4))
    assert result.map_revision == "r2" and result.reason == "route_planned"


def test_expanded_map_cannot_silently_drop_no_go_mask() -> None:
    controller = mission()
    mask = np.zeros(controller.grid.cells.shape, dtype=bool)
    mask[20, 20] = True
    controller.update_map(controller.grid, "r2", mask)
    controller.update_map(controller.grid, "r3")
    assert controller.blocked[20, 20]
    bigger = OccupancyGrid(MapMeta(0.1, -1, -1, 80, 70), np.full((70, 80), -3.0))
    with pytest.raises(ValueError, match="no-go"):
        controller.update_map(bigger, "r4")


def test_in_place_map_expansion_requires_explicit_snapshot_refresh() -> None:
    controller = mission()
    controller.grid.expand_for(np.array([-2.0]), np.array([-2.0]), 3)
    assert controller.step(100, frame(100)).reason == "map_geometry_changed_update_required"


def test_bad_map_update_is_atomic() -> None:
    controller = mission()
    old_grid = controller.grid
    bigger = OccupancyGrid(MapMeta(0.1, -1, -1, 80, 70), np.full((70, 80), -3.0))
    with pytest.raises(ValueError, match="no_go"):
        controller.update_map(bigger, "r2", np.zeros((1, 1), dtype=bool))
    assert controller.grid is old_grid and controller.map_revision == "r1"


def test_blocked_goal_is_rejected_before_planning() -> None:
    controller = mission()
    mask = np.zeros(controller.grid.cells.shape, dtype=bool)
    mask[10, 40] = True
    controller.update_map(controller.grid, "r2", mask)
    controller.step(0, frame(0))
    result = controller.step(10, frame(10, pose=(1.05, 1.05, 0.0), stationary=True))
    assert result.action == "stop"
    assert result.reason == "blocked_or_outside_goal"


def test_caller_mutation_cannot_change_owned_snapshot_or_no_go() -> None:
    supplied = room()
    mask = np.zeros(supplied.cells.shape, dtype=bool)
    mask[20, 20] = True
    controller = RefinementMission(supplied, "r1", (), no_go=mask)
    supplied.cells[:, :] = 3.0
    supplied.meta.origin_x = -20
    mask[:, :] = False
    assert controller.grid.cells[10, 10] == -3.0
    assert controller.grid.meta.origin_x == 0
    assert controller.blocked[20, 20]
    next_grid = room()
    next_mask = np.zeros(next_grid.cells.shape, dtype=bool)
    next_mask[30, 30] = True
    controller.update_map(next_grid, "r2", next_mask)
    next_grid.cells[:, :] = 3.0
    next_mask[:, :] = False
    assert controller.grid.cells[10, 10] == -3.0
    assert controller.blocked[30, 30]


def test_config_uses_canonical_safety_and_does_not_double_inflate_dynamic_hit() -> None:
    config = load(None)
    params = mission_params_from_config(config)
    canonical = plan_params_from_config(config)
    assert params.clearance_m == canonical.clearance_m
    assert params.settle_ms == config["localization"]["settle_delay_ms"]
    assert params.scan_timeout_ms == config["lidar"]["scan_stall_timeout_ms"]
    assert params.immediate_stop_m == config["lidar"]["estop_distance_mm"] / 1000.0
    controller = RefinementMission(room(), "r1", (), params=params)
    controller.dynamic_obstacles.append((2.05, 2.05))
    controller.update_map(controller.grid, "r2")
    centre = controller.grid.to_cell(2.05, 2.05)
    assert controller.blocked[centre]
    assert not controller.blocked[centre[0], centre[1] + 5]


def test_arrival_requires_actual_stationary_level_and_post_stop_settle() -> None:
    controller = RefinementMission(room(), "r1", (MissionStop("here", 1.05, 1.05),))
    assert controller.step(0, frame(0)).reason == "arrived_stop_before_settle"
    assert (
        controller.step(1000, frame(1000, stationary=None)).reason
        == "stationary_level_confirmation_required"
    )
    assert controller.step(1100, frame(1100, stationary=True, level=None)).action == "stop"
    assert controller.step(1200, frame(1200, stationary=True)).reason == "settling"
    assert controller.step(1949, frame(1949, stationary=True)).action == "stop"
    assert controller.step(1950, frame(1950, stationary=True)).action == "observe"
    with pytest.raises(ValueError, match="fresh accepted observation ID"):
        controller.accept_observation("here", controller.grid, "r1")
    controller.accept_observation("here", controller.grid, "r2")
    assert controller.step(1960, frame(1960, stationary=True)).action == "stop"
    assert controller.step(1970, frame(1970, stationary=True)).action == "finished"


def test_saturated_map_advances_using_unique_observation_proof_not_fake_revision() -> None:
    controller = RefinementMission(
        room(), "r1", (MissionStop("first", 1.05, 1.05), MissionStop("second", 1.05, 1.05))
    )
    controller.step(0, frame(0))
    controller.step(10, frame(10, stationary=True))
    assert controller.step(760, frame(760, stationary=True)).action == "observe"
    controller.accept_observation(
        "first", controller.grid, "r1", observation_id="device:boot:accepted-scan1"
    )
    assert controller.stop_index == 1 and controller.map_revision == "r1"
    assert controller.step(770, frame(770, stationary=True)).action == "stop"
    assert controller.step(780, frame(780, stationary=True)).reason == "arrived_stop_before_settle"
    controller.step(790, frame(790, stationary=True))
    assert controller.step(1540, frame(1540, stationary=True)).action == "observe"
    with pytest.raises(ValueError, match="already been consumed"):
        controller.accept_observation(
            "second", controller.grid, "r1", observation_id="device:boot:accepted-scan1"
        )
    assert controller.stop_index == 1
    controller.accept_observation(
        "second", controller.grid, "r1", observation_id="device:boot:accepted-scan2"
    )
    assert controller.stop_index == 2 and controller.map_revision == "r1"
    assert controller.step(1550, frame(1550, stationary=True)).action == "stop"
    assert controller.step(1560, frame(1560, stationary=True)).action == "finished"


def test_stationarity_interruption_restarts_settle_and_lost_revokes_observation() -> None:
    controller = RefinementMission(room(), "r1", (MissionStop("here", 1.05, 1.05),))
    controller.step(0, frame(0))
    controller.step(10, frame(10, stationary=True))
    controller.step(500, frame(500, stationary=False))
    assert controller.step(760, frame(760, stationary=True)).reason == "settling"
    assert controller.step(1510, frame(1510, stationary=True)).action == "observe"
    controller.step(1520, replace(frame(1520, stationary=True), pose_status="lost"))
    with pytest.raises(ValueError, match="No matching"):
        controller.accept_observation("here", controller.grid, "r2")


def test_obstacle_revokes_previously_settled_observation() -> None:
    controller = RefinementMission(room(), "r1", (MissionStop("here", 1.05, 1.05),))
    controller.step(0, frame(0))
    controller.step(10, frame(10, stationary=True))
    assert controller.step(760, frame(760, stationary=True)).action == "observe"
    assert controller.step(770, frame(770, stationary=True, distance=0.65, seq=2)).action == "stop"
    with pytest.raises(ValueError, match="No matching"):
        controller.accept_observation("here", controller.grid, "r2")


def test_pre_stop_telemetry_cannot_begin_settle() -> None:
    controller = RefinementMission(room(), "r1", (MissionStop("here", 1.05, 1.05),))
    controller.step(100, frame(100))
    observed = replace(frame(110, stationary=True), telemetry_received_ms=100)
    assert controller.step(110, observed).reason == "post_stop_telemetry_required"


def test_reusing_early_stationary_telemetry_cannot_complete_settle() -> None:
    controller = RefinementMission(room(), "r1", (MissionStop("here", 1.05, 1.05),))
    controller.step(0, frame(0))
    controller.step(10, frame(10, stationary=True))
    reused = replace(frame(760, stationary=True), telemetry_received_ms=10)
    assert controller.step(760, reused).reason == "settle_confirmation_telemetry_required"
    assert controller.step(770, frame(770, stationary=True)).action == "observe"


def test_large_heading_error_stops_without_new_reverse_or_turn_model() -> None:
    controller = mission()
    controller.step(100, frame(100, pose=(1.05, 1.05, math.pi)))
    result = controller.step(110, frame(110, pose=(1.05, 1.05, math.pi)))
    assert result.action == "stop" and result.reason == "pending_inplace_turn_driver"


def test_clock_reverse_and_onboard_latch_stop() -> None:
    controller = mission()
    controller.step(100, frame(100))
    assert controller.step(90, frame(90)).reason == "clock_reversed"
    assert controller.step(110, replace(frame(110), safety_latched=True)).action == "halted"
    assert controller.step(120, frame(120)).action == "halted"


@pytest.mark.parametrize("interruption", ["map_revision", "grid_geometry", "clock_reverse"])
def test_safety_latch_precedes_map_or_clock_and_stays_sticky(interruption: str) -> None:
    controller = mission()
    controller.step(100, frame(100))
    now = 110
    if interruption == "map_revision":
        controller.update_map(controller.grid, "r2")
    elif interruption == "grid_geometry":
        controller.grid.expand_for(np.array([-1.0]), np.array([-1.0]), 2)
    else:
        now = 90
    latched = controller.step(now, replace(frame(now), safety_latched=True))
    assert latched.action == "halted" and latched.reason == "onboard_safety_latched"
    resumed = controller.step(120, frame(120))
    assert resumed.action == "halted" and resumed.reason == "onboard_safety_latched"
