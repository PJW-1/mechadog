"""측위 점유와 항법 공간의 충돌 정책 — 자료 모순을 자유 공간으로 숨기지 않는다."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from host.behavior.planner import PlanParams, inflate, plan_to
from host.slam.occupancy import MapMeta, OccupancyGrid

PARAMS = PlanParams(1.0, -1.0, 0.25, 0.0, hard_thresh=4.0, soft_clearance_m=0.15)


def free_grid() -> OccupancyGrid:
    return OccupancyGrid(MapMeta(0.05, 0.0, 0.0, 60, 40), np.full((40, 60), -5.0))


def test_nav_free_excludes_loc_only_but_loc_free_never_opens_unknown() -> None:
    nav, loc = free_grid(), free_grid()
    nav.cells[30, 40] = 0.0
    nav.cells[10, 10] = 5.0
    loc.cells[20, 30] = 3.0
    before_nav, before_loc = nav.cells.copy(), loc.cells.copy()

    blocked = inflate(nav, PARAMS, obstacle_grid=loc)

    assert blocked[30, 40], "loc free만으로 nav 미관측을 열지 않는다"
    assert blocked[10, 10], "loc free가 원래 벽을 지우지 않는다"
    assert not blocked[20, 30], "선택한 정책은 nav free 위 loc-only만 계획에서 제외한다"
    np.testing.assert_array_equal(nav.cells, before_nav)
    np.testing.assert_array_equal(loc.cells, before_loc)


@pytest.mark.parametrize(("value", "radius"), [(0.9, 0), (1.0, 3), (3.0, 3), (4.0, 5)])
def test_loc_only_uses_the_configured_occupancy_and_clearances(value: float, radius: int) -> None:
    nav, loc = free_grid(), free_grid()
    nav.cells[20, 30] = 0.0  # 미관측은 사용자 free 예외에 포함하지 않는다.
    loc.cells[20, 30] = value
    blocked = inflate(nav, PARAMS, obstacle_grid=loc)

    if radius == 0:
        assert blocked.sum() == 1
    else:
        assert blocked[20, 30 + radius]
        assert not blocked[20, 31 + radius]


def test_expanded_loc_origin_and_outside_obstacles_use_world_coordinates() -> None:
    nav = free_grid()
    loc = OccupancyGrid(MapMeta(0.05, -0.5, -0.5, 80, 60))
    leg = (1.525, 1.025)
    nav.cells[nav.to_cell(*leg)] = 0.0
    loc.cells[loc.to_cell(*leg)] = 3.0
    loc.cells[loc.to_cell(-0.025, 0.525)] = 5.0

    blocked = inflate(nav, PARAMS, obstacle_grid=loc)

    assert blocked[nav.to_cell(*leg)]
    assert not blocked[30, 40], "원점 오프셋을 무시한 셀 번호 복사가 아니다"
    assert blocked[10, 3], "nav 바깥 벽도 몸체 여유가 안쪽에 닿으면 막는다"
    assert not blocked[10, -1], "바깥 점유가 배열 반대편으로 감기면 안 된다"


@pytest.mark.parametrize(
    "meta", [MapMeta(0.1, 0.0, 0.0, 60, 40), MapMeta(0.05, 0.025, 0.0, 60, 40)]
)
def test_unknown_grid_alignment_is_rejected(meta: MapMeta) -> None:
    with pytest.raises(ValueError, match="aligned cell lattice"):
        inflate(free_grid(), PARAMS, obstacle_grid=OccupancyGrid(meta))


def test_disjoint_loc_does_not_modify_the_navigation_mask() -> None:
    nav = free_grid()
    loc = OccupancyGrid(MapMeta(0.05, 50.0, 50.0, 10, 10), np.full((10, 10), 5.0))
    np.testing.assert_array_equal(inflate(nav, PARAMS, obstacle_grid=loc), inflate(nav, PARAMS))


def test_excluding_free_conflicts_opens_corridor_without_erasing_evidence() -> None:
    """실제 지도처럼 양쪽 앵커가 비어도 모순 점유의 팽창이 통로를 끊을 수 있다."""
    nav = free_grid()
    nav.cells[:, 30] = 5.0
    nav.cells[13:28, 30] = -5.0
    loc = OccupancyGrid(replace(nav.meta), nav.cells.copy())
    loc.cells[13:28, 30] = 3.0
    start, goal = nav.to_world(20, 10), nav.to_world(20, 50)
    baseline = inflate(nav, PARAMS)
    fused = inflate(nav, PARAMS, obstacle_grid=loc)

    assert plan_to("A", goal, start, nav, baseline, PARAMS).reachable
    assert not fused[nav.to_cell(*start)] and not fused[nav.to_cell(*goal)]
    plan = plan_to("A", goal, start, nav, fused, PARAMS)
    assert plan.reachable
    assert loc.cells[20, 30] == 3.0 and nav.cells[20, 30] == -5.0
