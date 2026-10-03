"""벡터화한 `match`·`global_match` 가 예전 루프 구현과 같은 답을 내는가 (2026-10-03).

루프 구현은 이 파일에 기준으로 남긴다 — 후보 순서(방위 → x → y)·첫 최고점 채택·
대칭 격자(0 포함)까지 같아야 한다.
"""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from host.common.units import wrap_pi
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.scan_match import (
    MatchParams,
    _as_count,
    _scorer,
    global_match,
    match,
    rotate,
    symmetric_offsets,
)


def _room(seed: int = 3) -> OccupancyGrid:
    """5x4m 방 + 기둥 둘 + 빈 바닥(관측됨). 해상도 5cm."""
    res = 0.05
    width, height = 120, 100
    cells = np.full((height, width), -5.0, dtype=np.float32)
    cells[:10, :] = 0.0  # 미관측 띠 — 후보에서 빠져야 한다
    cells[10, 10:110] = 5.0
    cells[90, 10:110] = 5.0
    cells[10:91, 10] = 5.0
    cells[10:91, 110] = 5.0
    cells[40:46, 40:46] = 5.0
    cells[60:63, 80:95] = 5.0
    rng = random.Random(seed)
    for _ in range(30):  # 약간의 흩어진 물체 — 지형이 평평하지 않게
        cells[rng.randint(15, 85), rng.randint(15, 105)] = 5.0
    return OccupancyGrid(
        MapMeta(resolution=res, origin_x=0.0, origin_y=0.0, width=width, height=height), cells
    )


def _scan(grid: OccupancyGrid, pose: tuple[float, float, float], beams: int = 360) -> np.ndarray:
    """격자 레이캐스트 — 로봇 좌표계 점."""
    out = []
    occupied = grid.cells > 1.0
    for i in range(beams):
        a = i / beams * 2 * math.pi
        for d in np.arange(0.12, 6.0, 0.01):
            x = pose[0] + d * math.cos(a + pose[2])
            y = pose[1] + d * math.sin(a + pose[2])
            r, c = grid.to_cell(x, y)
            if not grid.inside(r, c):
                break
            if occupied[r, c]:
                out.append((d * math.cos(a), d * math.sin(a)))
                break
    return np.asarray(out)


def _match_loop(grid, points, center, params, yaw_delta=0.0):
    """루프 기준 — 최고점 중 **예측에 가장 가까운** 후보(위치 거리 + 0.1·|방위 차|)."""
    scorer = _scorer(grid, params.occ_thresh, params.sigma_m)
    base_yaw = wrap_pi(center[2] + yaw_delta)
    found = []
    for d_yaw in symmetric_offsets(params.search_ang_rad, params.search_ang_step_rad):
        yaw = base_yaw + float(d_yaw)
        rotated = rotate(points, yaw)
        for d_x in symmetric_offsets(params.search_lin_m, params.search_lin_step_m):
            for d_y in symmetric_offsets(params.search_lin_m, params.search_lin_step_m):
                x, y = center[0] + float(d_x), center[1] + float(d_y)
                score = scorer(rotated + np.array([x, y]))
                found.append(
                    (score, math.hypot(d_x, d_y) + abs(float(d_yaw)) * 0.1, (x, y, wrap_pi(yaw)))
                )
    best = max(f[0] for f in found)
    if best <= 0:
        return center, 0
    tied = [f for f in found if f[0] >= best - 1e-9]
    return min(tied, key=lambda f: f[1])[2], _as_count(best)


def test_flat_landscape_prefers_the_prediction() -> None:
    """점수가 모두 같으면(빈 지도 근처) 예측 자리 그대로 — 예전엔 대칭 격자의 −끝으로 끌려갔다."""
    grid = _room()
    params = MatchParams(0.15, 0.04, math.radians(8), math.radians(2), 1.0, 10, sigma_m=0.0)
    far_points = np.array([[50.0, 50.0]] * 10)  # 지도 밖 — 모든 후보 점수 0
    center = (2.6, 2.4, 0.4)
    assert match(grid, far_points, center, params).pose == center


@pytest.mark.parametrize("sigma", [0.0, 0.05, 0.1])
def test_vectorized_match_equals_loop(sigma: float) -> None:
    grid = _room()
    truth = (2.6, 2.4, 0.4)
    points = _scan(grid, truth)
    params = MatchParams(
        search_lin_m=0.15,
        search_lin_step_m=0.04,
        search_ang_rad=math.radians(8),
        search_ang_step_rad=math.radians(2),
        occ_thresh=1.0,
        min_known_cells=10,
        sigma_m=sigma,
    )
    for center in ((2.55, 2.45, 0.35), (2.7, 2.3, 0.5), truth):
        got = match(grid, points, center, params, yaw_delta=0.02)
        want_pose, want_score = _match_loop(grid, points, center, params, yaw_delta=0.02)
        assert got.score == want_score
        assert got.pose == pytest.approx(want_pose, abs=1e-9)


def test_symmetric_offsets_always_contain_zero() -> None:
    """`np.arange(-0.15, 0.15, 0.04)` 는 0 을 빼먹었다 — 서 있는 로봇이 매번 1cm 옮겨졌다."""
    assert 0.0 in symmetric_offsets(0.15, 0.04)
    assert symmetric_offsets(0.15, 0.04).tolist() == pytest.approx(
        [-0.16, -0.12, -0.08, -0.04, 0, 0.04, 0.08, 0.12, 0.16]
    )
    assert 0.0 in symmetric_offsets(math.radians(8), math.radians(2))


def test_match_on_true_pose_stays_put() -> None:
    """정확한 자세에서 시작하면 그 자리를 유지한다 (0 이 격자에 있으니)."""
    grid = _room()
    truth = (2.6, 2.4, 0.4)
    params = MatchParams(0.15, 0.04, math.radians(8), math.radians(2), 1.0, 10, sigma_m=0.05)
    got = match(grid, _scan(grid, truth), truth, params)
    assert got.pose == pytest.approx(truth, abs=1e-9)


@pytest.mark.parametrize("sigma", [0.0, 0.05])
def test_global_match_finds_the_pose_anywhere(sigma: float) -> None:
    grid = _room()
    truth = (3.3, 1.7, -2.0)
    result = global_match(
        grid,
        _scan(grid, truth),
        lin_step_m=0.1,
        ang_step_rad=math.radians(15),
        occ_thresh=1.0,
        min_known_cells=10,
        free_thresh=-0.5,
        sigma_m=sigma,
    )
    assert result is not None
    assert math.hypot(result.pose[0] - truth[0], result.pose[1] - truth[1]) < 0.06
    assert abs(wrap_pi(result.pose[2] - truth[2])) < math.radians(3)


def test_global_match_never_places_the_robot_on_unobserved_cells() -> None:
    grid = _room()
    result = global_match(
        grid,
        _scan(grid, (3.3, 1.7, -2.0)),
        lin_step_m=0.1,
        ang_step_rad=math.radians(15),
        occ_thresh=1.0,
        min_known_cells=10,
        free_thresh=-0.5,
    )
    assert result is not None
    row, _ = grid.to_cell(result.pose[0], result.pose[1])
    assert row >= 10, "미관측 띠(행 0~9)에 로봇을 세우면 안 된다"
