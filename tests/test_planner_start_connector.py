"""출발 스냅 회귀 — 실제 로봇이 통과하지 않은 구간을 경로에서 생략하지 않는다."""

from __future__ import annotations

import math

import numpy as np
import pytest

from host.behavior.planner import (
    PlanParams,
    body_collision_mask,
    inflate,
    plan_to,
    segment_clear,
    snap_to_free,
)
from host.slam.occupancy import MapMeta, OccupancyGrid

PARAMS = PlanParams(1.0, -1.0, 0.2, 0.0, soft_clearance_m=0.2)


def free_grid() -> OccupancyGrid:
    return OccupancyGrid(
        MapMeta(0.1, 0.0, 0.0, 30, 30),
        np.full((30, 30), -5.0, dtype=np.float32),
    )


@pytest.mark.parametrize("barrier_value", [5.0, 0.0], ids=["wall", "unobserved"])
def test_start_cannot_snap_across_a_wall_or_unobserved_band(barrier_value: float) -> None:
    """가까운 자유 셀이 벽 반대편이어도 실제 출발점에서 건너갈 수는 없다."""
    grid = free_grid()
    grid.cells[:, 6] = barrier_value
    start = grid.to_world(10, 5)
    goal = grid.to_world(10, 20)
    blocked = np.ones(grid.cells.shape, dtype=bool)
    blocked[:, 8:] = False
    snapped, distance = snap_to_free(grid, blocked, start, 0.6)
    assert math.isfinite(distance)
    assert grid.to_cell(*snapped)[1] > 6  # 기존 경로는 이 미검사 구간을 생략했다.

    plan = plan_to("A", goal, start, grid, blocked, PARAMS)

    assert not plan.reachable
    assert not plan.waypoints
    assert plan.fail_reason == "start_clearance_blocked"
    assert plan.start_moved_m == 0.0


def test_start_cannot_snap_diagonally_through_a_blocked_corner() -> None:
    grid = free_grid()
    start = grid.to_world(10, 10)
    goal = grid.to_world(12, 12)
    blocked = np.ones(grid.cells.shape, dtype=bool)
    blocked[11:, 11:] = False
    snapped, distance = snap_to_free(grid, blocked, start, 0.6)
    assert math.isfinite(distance)
    assert grid.to_cell(*snapped) == (11, 11)
    assert blocked[10, 11] and blocked[11, 10]

    plan = plan_to("A", goal, start, grid, blocked, PARAMS)

    assert not plan.reachable
    assert plan.fail_reason == "start_clearance_blocked"


def test_start_inside_inflation_is_not_assumed_to_have_escaped() -> None:
    """원본 지도 바닥은 비어도 몸체 여유가 없으면 출발 자동 복구를 하지 않는다."""
    grid = free_grid()
    grid.cells[:, 5] = 5.0
    start = grid.to_world(10, 7)
    blocked = inflate(grid, PARAMS)
    assert grid.cells[grid.to_cell(*start)] <= PARAMS.free_thresh
    assert blocked[grid.to_cell(*start)]
    assert math.isfinite(snap_to_free(grid, blocked, start, 0.6)[1])

    plan = plan_to("A", grid.to_world(10, 20), start, grid, blocked, PARAMS)

    assert not plan.reachable
    assert plan.fail_reason == "start_clearance_blocked"


@pytest.mark.parametrize(
    ("value", "reason"),
    [(5.0, "start_occupied"), (0.0, "start_unobserved")],
    ids=["occupied", "unobserved"],
)
def test_nonfree_start_is_rejected_even_if_mask_omits_it(value: float, reason: str) -> None:
    grid = free_grid()
    grid.cells[10, 10] = value
    plan = plan_to(
        "A",
        grid.to_world(20, 20),
        grid.to_world(10, 10),
        grid,
        np.zeros(grid.cells.shape, dtype=bool),
        PARAMS,
    )

    assert not plan.reachable
    assert plan.fail_reason == reason


@pytest.mark.parametrize("start", [(-0.01, 1.0), (3.01, 1.0), (1.0, -0.01), (1.0, 3.01)])
def test_start_outside_map_is_not_snapped_through_unobserved_space(
    start: tuple[float, float],
) -> None:
    grid = free_grid()
    blocked = inflate(grid, PARAMS)
    assert math.isfinite(snap_to_free(grid, blocked, start, 0.6)[1])

    plan = plan_to("A", (1.5, 1.5), start, grid, blocked, PARAMS)

    assert not plan.reachable
    assert plan.fail_reason == "start_outside_map"


def test_observed_free_start_still_plans_without_moving_it() -> None:
    grid = free_grid()
    start, goal = grid.to_world(5, 5), grid.to_world(20, 20)
    plan = plan_to("A", goal, start, grid, inflate(grid, PARAMS), PARAMS)

    assert plan.reachable
    assert plan.waypoints[0] == start
    assert plan.effective == goal
    assert plan.start_moved_m == 0.0
    assert plan.goal_moved_m == 0.0
    assert plan.fail_reason == ""


def test_observed_free_start_preserves_reachable_goal_snapping() -> None:
    grid = free_grid()
    start, goal = grid.to_world(5, 5), grid.to_world(20, 20)
    grid.cells[20, 20] = 5.0
    blocked = inflate(grid, PARAMS)
    plan = plan_to("A", goal, start, grid, blocked, PARAMS)

    assert plan.reachable
    assert plan.requested == goal
    assert plan.effective is not None
    assert not blocked[grid.to_cell(*plan.effective)]
    assert plan.waypoints[0] == start
    assert plan.start_moved_m == 0.0
    assert plan.goal_moved_m > 0.0


def escape_room():
    grid = OccupancyGrid(MapMeta(0.05, 0, 0, 60, 60), np.full((60, 60), -5.0))
    grid.cells[:, 10] = 5.0
    params = PlanParams(1, -1, 0.25, 0.08, body_radius_m=0.15)
    start = grid.to_world(30, 14)  # 벽 셀 가장자리에서17.5cm, 추종 여유25cm 안.
    return grid, params, start


def test_escape_keeps_actual_start_and_checks_every_connector_segment():
    grid, params, start = escape_room()
    blocked = inflate(grid, params)
    body = body_collision_mask(grid, params)
    assert blocked[grid.to_cell(*start)] and not body[grid.to_cell(*start)]
    plan = plan_to("A", grid.to_world(30, 45), start, grid, blocked, params)
    assert plan.reachable and plan.start_moved_m > 0
    assert plan.waypoints[0] == start and plan.escape_end_index > 0
    connector = plan.waypoints[: plan.escape_end_index + 1]
    assert not blocked[grid.to_cell(*connector[-1])]
    assert all(
        segment_clear(grid, body, a, b) for a, b in zip(connector, connector[1:], strict=False)
    )
    assert plan.length_m == pytest.approx(
        sum(math.dist(a, b) for a, b in zip(plan.waypoints, plan.waypoints[1:], strict=False))
    )


def test_escape_refuses_body_overlap_and_dynamic_obstacles():
    grid, params, start = escape_room()
    blocked = inflate(grid, params)
    overlapping = grid.to_world(30, 13)  # 셀 사각형 기준 몸체15cm 여유 없음.
    assert not plan_to("A", (2, 2), overlapping, grid, blocked, params).reachable
    dynamic = np.zeros_like(blocked)
    dynamic[grid.to_cell(*start)] = True
    body = body_collision_mask(grid, params) | dynamic
    assert not plan_to(
        "A", (2, 2), start, grid, blocked | dynamic, params, body_blocked=body
    ).reachable


def test_escape_refuses_unobserved_connector_and_distance_limit():
    from dataclasses import replace

    grid, params, start = escape_room()
    short = replace(params, start_escape_max_m=0.01)
    assert not plan_to("A", (2, 2), start, grid, inflate(grid, params), short).reachable
    grid.cells[:, 15] = 0
    assert not plan_to("A", (2, 2), start, grid, inflate(grid, params), params).reachable


def test_segment_checks_corner_sides_and_body_cells():
    grid = free_grid()
    blocked = np.zeros_like(grid.cells, dtype=bool)
    blocked[10, 11] = True
    assert not segment_clear(grid, blocked, grid.to_world(10, 10), grid.to_world(11, 11))
    assert segment_clear(grid, blocked, grid.to_world(10, 10), grid.to_world(11, 10))


def test_escape_does_not_cross_wall_to_reachable_goal():
    grid, params, start = escape_room()
    plan = plan_to("A", grid.to_world(30, 3), start, grid, inflate(grid, params), params)
    assert plan.fail_reason == "goal_unreachable" and not plan.reachable
