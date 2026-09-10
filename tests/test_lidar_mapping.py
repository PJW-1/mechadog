"""점유격자 · 스캔 정합 · 경로 계획 · 구역 저장 (FR-6 · FR-7).

하드웨어 없이 닫힌다 — 격자는 배열이고 정합은 순수 함수다.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import numpy as np
import pytest
import yaml

from host.behavior.planner import (
    PlanParams,
    astar,
    dilate,
    inflate,
    path_length_m,
    simplify,
    to_waypoints,
)
from host.behavior.zones import ZoneStore, nearest_free_cell, select_next
from host.common.units import cm_to_m, deg_to_rad, ms_to_s, s_to_ms, wrap_pi
from host.slam.occupancy import (
    LOGODDS_MAX,
    LOGODDS_MIN,
    MapMeta,
    OccupancyGrid,
    bresenham,
)
from host.slam.scan_match import (
    MatchParams,
    integrate_scan,
    match,
    merge_batch,
    predict,
    preprocess,
    to_world,
)
from host.slam.settings import REQUIRED_LIDAR_KEYS, read_lidar_section
from host.slam.simulation import DEFAULT_ROOM, SimParams, scan_world

PLAN = PlanParams(occ_thresh=1.0, free_thresh=-1.0, clearance_m=0.25, simplify_eps_m=0.08)


def blank(width: int = 60, height: int = 60) -> OccupancyGrid:
    return OccupancyGrid(
        MapMeta(resolution=0.05, origin_x=0.0, origin_y=0.0, width=width, height=height)
    )


# ══════════════════════════════════════════════════════════════
#  단위 경계 — units.py
# ══════════════════════════════════════════════════════════════


def test_wire_units_convert_both_ways() -> None:
    assert cm_to_m(25) == pytest.approx(0.25)
    assert deg_to_rad(180) == pytest.approx(math.pi)
    assert ms_to_s(300) == pytest.approx(0.3)


def test_seconds_become_integer_milliseconds() -> None:
    """규약의 `ts` 는 정수 밀리초다 — 초 단위 실수를 실으면 폐기된다."""
    value = s_to_ms(1756800000.123)
    assert isinstance(value, int)
    assert value == 1756800000123


def test_wrap_pi_folds_into_one_turn() -> None:
    """범위는 `[-pi, pi)` 다 — 정확히 반대 방향은 `-pi` 로 나온다."""
    assert wrap_pi(3 * math.pi) == pytest.approx(-math.pi)
    assert wrap_pi(-3 * math.pi) == pytest.approx(-math.pi)
    assert wrap_pi(0.5) == pytest.approx(0.5)
    assert abs(wrap_pi(math.radians(370)) - math.radians(10)) < 1e-9


# ══════════════════════════════════════════════════════════════
#  점유격자
# ══════════════════════════════════════════════════════════════


def test_cell_and_world_round_trip() -> None:
    grid = blank()
    for x, y in ((0.0, 0.0), (1.23, 2.34), (2.975, 0.025)):
        row, col = grid.to_cell(x, y)
        back_x, back_y = grid.to_world(row, col)
        assert abs(back_x - x) <= grid.meta.resolution
        assert abs(back_y - y) <= grid.meta.resolution


def test_world_to_cell_uses_row_for_y() -> None:
    """⚠️ 행이 y 이고 열이 x 다. 뒤집으면 지도가 전치되어 조용히 틀린다."""
    grid = blank()
    row, col = grid.to_cell(1.0, 2.0)
    assert col == 20
    assert row == 40


def test_logodds_are_bounded() -> None:
    """상·하한이 없으면 오래 본 셀이 새 관측으로 안 고쳐진다."""
    grid = blank()
    endpoints = np.array([[1.0, 1.0]])
    for _ in range(200):
        grid.integrate((0.5, 0.5), endpoints, hit=0.9, miss=-0.4)
    assert grid.cells.max() <= LOGODDS_MAX
    assert grid.cells.min() >= LOGODDS_MIN


def test_endpoint_is_occupied_and_the_ray_is_free() -> None:
    grid = blank()
    grid.integrate((0.5, 0.5), np.array([[1.5, 0.5]]), hit=0.9, miss=-0.4)
    end_row, end_col = grid.to_cell(1.5, 0.5)
    mid_row, mid_col = grid.to_cell(1.0, 0.5)
    assert grid.cells[end_row, end_col] > 0
    assert grid.cells[mid_row, mid_col] < 0


def test_grid_expands_when_scans_leave_the_map() -> None:
    """공간 크기를 미리 입력받지 않으므로 경계에 닿으면 늘려야 한다."""
    grid = blank(40, 40)
    before_shape = grid.cells.shape
    before_origin = grid.meta.origin_x
    grid.expand_for(np.array([-1.5, 0.0]), np.array([0.0, 0.0]), pad_cells=10)
    assert grid.cells.shape[1] > before_shape[1]
    assert grid.meta.origin_x < before_origin
    assert grid.meta.width == grid.cells.shape[1], "메타가 배열과 같이 움직여야 한다"


def test_expansion_keeps_existing_observations() -> None:
    """늘리면서 지도를 잃으면 매핑이 처음부터 다시 시작된다."""
    grid = blank(40, 40)
    grid.cells[10, 10] = 2.5
    world = grid.to_world(10, 10)
    grid.expand_for(np.array([-1.0]), np.array([-1.0]), pad_cells=5)
    row, col = grid.to_cell(*world)
    assert grid.cells[row, col] == pytest.approx(2.5)


def test_bresenham_ends_at_the_target() -> None:
    cells = bresenham(0, 0, 5, 12)
    assert cells[0] == (0, 0)
    assert cells[-1] == (5, 12)


def test_map_saves_and_loads_as_a_pair(tmp_path: Path) -> None:
    """`.npy` 와 `map_meta.json` 은 한 쌍이다 — 메타 없이는 숫자 배열이다."""
    grid = blank()
    grid.cells[5, 7] = 3.0
    grid.save(tmp_path)
    loaded = OccupancyGrid.load(tmp_path)
    assert loaded.cells[5, 7] == pytest.approx(3.0)
    assert loaded.meta.resolution == grid.meta.resolution
    assert loaded.meta.origin_x == grid.meta.origin_x


def test_loading_without_a_map_says_what_to_run(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="lidar_slam"):
        OccupancyGrid.load(tmp_path)


def test_save_also_writes_the_ros2_pair(tmp_path: Path) -> None:
    """`maps/README.md` 가 지정한 형식이다 — `*.pgm` + `*.yaml` (ADR-18)."""
    grid = blank(20, 10)
    written = grid.save(tmp_path)
    assert written["pgm"].is_file()
    assert written["yaml"].is_file()
    yaml_text = written["yaml"].read_text(encoding="utf-8")
    assert "image: slam_map.pgm" in yaml_text
    assert f"resolution: {grid.meta.resolution}" in yaml_text
    assert "negate: 0" in yaml_text


def test_pgm_is_free_white_and_occupied_black(tmp_path: Path) -> None:
    """map_server 규약 — 픽셀 255 가 자유, 0 이 점유다 (`negate: 0`)."""
    grid = blank(4, 3)
    grid.cells[0, 0] = 10.0  # 확실히 막힘
    grid.cells[0, 1] = -10.0  # 확실히 빔
    pgm_path, _ = grid.save_ros2(tmp_path)
    raw = pgm_path.read_bytes()
    header_end = 0
    for _ in range(4):  # P5 / 주석 / 크기 / 최대값
        header_end = raw.index(b"\n", header_end) + 1
    pixels = np.frombuffer(raw[header_end:], dtype=np.uint8).reshape(3, 4)
    bottom = pixels[-1]  # 위아래를 뒤집었으므로 격자의 행 0 은 이미지의 마지막 행
    assert bottom[0] < 10, "점유는 검정(0에 가까움)"
    assert bottom[1] > 245, "자유는 흰색(255에 가까움)"
    assert 120 < bottom[2] < 136, "미관측은 회색(중간값)"


def test_pgm_rows_are_flipped_so_the_map_is_not_upside_down(tmp_path: Path) -> None:
    """⚠️ ROS2 의 첫 행은 **y 최대**다. 뒤집지 않으면 지도가 상하 반전으로
    로드되어 벽은 그럴듯한데 경로가 전부 틀린다 — 조용한 종류의 오류다."""
    grid = blank(3, 4)
    grid.cells[3, 0] = 10.0  # y 최대 쪽에 벽
    pgm_path, _ = grid.save_ros2(tmp_path)
    raw = pgm_path.read_bytes()
    header_end = 0
    for _ in range(4):
        header_end = raw.index(b"\n", header_end) + 1
    pixels = np.frombuffer(raw[header_end:], dtype=np.uint8).reshape(4, 3)
    assert pixels[0, 0] < 10, "y 최대의 벽이 이미지 첫 행에 있어야 한다"


# ══════════════════════════════════════════════════════════════
#  스캔 정합
# ══════════════════════════════════════════════════════════════


def test_preprocess_drops_out_of_range_points() -> None:
    """범위 밖을 0 으로 바꾸면 로봇 발밑에 벽이 찍힌다."""
    points = ((0.0, 0.05), (0.0, 1.0), (0.0, 99.0), (0.0, float("inf")))
    kept = preprocess(points, 0.12, 8.0)
    assert kept.shape == (1, 2)
    assert kept[0][0] == pytest.approx(1.0)


def test_merge_uses_the_median_not_the_mean() -> None:
    """한 발의 이상치가 평균은 끌고 가지만 중앙값은 못 움직인다."""
    batch = [((0.1, 1.0),), ((0.1, 1.0),), ((0.1, 9.0),)]
    merged = merge_batch(batch, bins=36)
    assert len(merged) == 1
    assert merged[0][1] == pytest.approx(1.0)


def test_match_recovers_a_known_shift() -> None:
    """지도를 만든 자세에서 20cm 옮긴 스캔이 그 자리를 되찾는다."""
    grid = blank(200, 200)
    params = MatchParams(
        search_lin_m=0.25,
        search_lin_step_m=0.05,
        search_ang_rad=deg_to_rad(6),
        search_ang_step_rad=deg_to_rad(2),
        occ_thresh=1.0,
        min_known_cells=20,
    )
    sim = SimParams(200.0, 25.0, 0.0, 0.0, 180, 8.0)
    truth = (3.0, 2.5, 0.0)
    wire = scan_world(truth, DEFAULT_ROOM, sim, random.Random(1))
    points = preprocess(tuple((math.radians(a), d / 1000.0) for a, d in wire), 0.12, 8.0)
    # 지도를 만든다 (정답 자세로 3회 누적해 확신을 올린다)
    for _ in range(3):
        integrate_scan(grid, truth, points, hit=1.5, miss=-0.2, pad_cells=0)

    shifted = (truth[0] + 0.2, truth[1] - 0.15, truth[2])
    result = match(grid, points, shifted, params)
    assert not result.skipped
    assert result.score > 0
    assert result.pose[0] == pytest.approx(truth[0], abs=0.1)
    assert result.pose[1] == pytest.approx(truth[1], abs=0.1)


def test_match_takes_imu_yaw_as_a_delta_not_an_absolute() -> None:
    """⚠️ **IMU yaw 를 탐색 중심으로 쓰면 조용히 망가진다.**

    지도 좌표계는 매핑을 시작한 자리가 정하고 IMU 의 0 은 부팅한 자리가
    정하므로 **둘 사이에 임의의 옵셋이 있다.** 절대 yaw 를 중심으로 쓰면
    지도 기준 방위가 IMU 값에서 탐색 범위(±8°) 이상 벗어날 수 없고, 로봇이
    지도 기준 90° 를 보는데 IMU 가 0° 를 보고하면 **정합이 정답을 절대 찾지
    못한다.**

    그 증상은 자세 오차가 아니라 **장애물 오탐**으로 나타났다 — 회전된 자세로
    광선을 쏘니 엉뚱한 방향의 지도 벽과 비교되어 실측이 예상보다 가까워
    보였다. 한 번의 순찰에서 30건이 오탐되었다.

    그래서 `match` 는 **변화량**을 받는다. 이 시험은 그 계약을 못박는다 —
    지도 기준 방위 90° 인 자세를 중심으로 넘기고 변화량 0 을 주면, 결과는
    0° 근처가 아니라 **90° 근처**여야 한다.
    """
    grid = blank(200, 200)
    params = MatchParams(
        search_lin_m=0.1,
        search_lin_step_m=0.05,
        search_ang_rad=deg_to_rad(6),
        search_ang_step_rad=deg_to_rad(2),
        occ_thresh=1.0,
        min_known_cells=20,
    )
    sim = SimParams(200.0, 25.0, 0.0, 0.0, 180, 8.0)
    truth = (3.0, 2.5, math.pi / 2)  # 지도 기준 90° 를 보고 있다
    wire = scan_world(truth, DEFAULT_ROOM, sim, random.Random(1))
    points = preprocess(tuple((math.radians(a), d / 1000.0) for a, d in wire), 0.12, 8.0)
    for _ in range(3):
        integrate_scan(grid, truth, points, hit=1.5, miss=-0.2, pad_cells=0)

    # 변화량 0 — IMU 가 무엇을 보고하든 지도 기준 방위가 기준이어야 한다.
    result = match(grid, points, truth, params, yaw_delta=0.0)
    assert abs(wrap_pi(result.pose[2] - truth[2])) < deg_to_rad(7), (
        "지도 기준 방위를 유지해야 한다 — 절대 yaw 를 중심으로 쓰고 있다"
    )

    # 변화량을 주면 그만큼 옮겨서 탐색한다.
    turned = match(grid, points, truth, params, yaw_delta=deg_to_rad(4))
    assert abs(wrap_pi(turned.pose[2] - truth[2])) < deg_to_rad(11)


def test_match_skips_on_an_empty_map() -> None:
    """빈 지도에 정합하면 점수가 전부 0 이라 첫 후보가 우연히 채택된다."""
    grid = blank()
    params = MatchParams(0.1, 0.05, 0.1, 0.05, 1.0, min_known_cells=50)
    points = np.array([[1.0, 0.0], [0.0, 1.0]])
    result = match(grid, points, (0.0, 0.0, 0.0), params)
    assert result.skipped
    assert result.score == 0


def test_predict_extrapolates_the_last_step() -> None:
    assert predict((0.0, 0.0, 0.0), (0.1, 0.2, 0.3))[0] == pytest.approx(0.2)
    assert predict((0.0, 0.0, 0.0), (0.1, 0.2, 0.3))[1] == pytest.approx(0.4)


def test_to_world_rotates_then_translates() -> None:
    points = np.array([[1.0, 0.0]])
    moved = to_world(points, (2.0, 3.0, math.pi / 2))
    assert moved[0][0] == pytest.approx(2.0)
    assert moved[0][1] == pytest.approx(4.0)


# ══════════════════════════════════════════════════════════════
#  경로 계획
# ══════════════════════════════════════════════════════════════


def test_dilate_does_not_wrap_around_the_border() -> None:
    """⚠️ `np.roll` 을 쓰면 왼쪽 벽이 오른쪽 벽을 부풀린다."""
    mask = np.zeros((10, 10), dtype=bool)
    mask[5, 0] = True
    grown = dilate(mask, 2)
    assert grown[5, 1]
    assert not grown[5, 9], "반대편 경계로 감겨서는 안 된다"


def test_inflate_blocks_unknown_cells() -> None:
    """순찰 로봇에서 **모르는 곳은 빈 곳이 아니다.**"""
    grid = blank(20, 20)  # 전부 0 = 미관측
    blocked = inflate(grid, PLAN)
    assert blocked.all(), "미관측은 통과 불가여야 한다"

    grid.cells[:, :] = -3.0
    assert not inflate(grid, PLAN).any(), "확실히 빈 셀은 통과 가능하다"


def test_astar_goes_around_a_wall() -> None:
    """직선거리가 아니라 **실제 이동 가능한 경로**를 낸다."""
    blocked = np.zeros((40, 40), dtype=bool)
    blocked[10:30, 20] = True  # 가로지르는 벽 (양 끝은 열려 있다)
    path = astar((20, 5), (20, 35), blocked)
    assert path is not None
    assert path[0] == (20, 5)
    assert path[-1] == (20, 35)
    # 벽을 지나지 않고 우회했으므로 직선(30셀)보다 길다
    assert len(path) > 30


def test_astar_refuses_a_blocked_goal() -> None:
    blocked = np.zeros((20, 20), dtype=bool)
    blocked[10, 10] = True
    assert astar((1, 1), (10, 10), blocked) is None


def test_astar_starts_even_from_a_blocked_cell() -> None:
    """측위 오차로 로봇이 막힌 셀에 있으면, 거절하면 스스로 갇힌다."""
    blocked = np.zeros((20, 20), dtype=bool)
    blocked[1, 1] = True
    assert astar((1, 1), (10, 10), blocked) is not None


def test_diagonal_moves_cost_more_than_straight() -> None:
    """대각을 1.0 으로 두면 계단 경로가 직선과 같은 값이 된다."""
    straight = path_length_m([(0, 0), (0, 1)], 0.05)
    diagonal = path_length_m([(0, 0), (1, 1)], 0.05)
    assert diagonal > straight


def test_simplify_removes_collinear_waypoints() -> None:
    points = [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0), (1.5, 0.0)]
    assert simplify(points, 0.08) == [(0.0, 0.0), (1.5, 0.0)]


def test_simplify_keeps_real_corners() -> None:
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    assert len(simplify(points, 0.08)) == 3


def test_waypoints_come_back_in_world_metres() -> None:
    grid = blank()
    path = [(0, 0), (0, 10), (0, 20)]
    waypoints = to_waypoints(path, grid, 0.08)
    assert waypoints[0][0] == pytest.approx(0.025)
    assert waypoints[-1][0] == pytest.approx(1.025)


def test_next_zone_uses_path_length_not_straight_line() -> None:
    """**요점 시험** — 벽 건너 가까운 구역보다 열린 쪽 먼 구역을 고른다."""
    grid = OccupancyGrid(
        MapMeta(resolution=0.05, origin_x=0.0, origin_y=0.0, width=120, height=120)
    )
    grid.cells[:, :] = -3.0
    grid.cells[0:100, 40] = 3.0  # 벽 — 위쪽만 열려 있다
    blocked = inflate(grid, PLAN)

    start = (1.0, 1.0)
    candidates = {
        "A": (2.5, 1.0),  # 직선 1.5m — 그런데 벽 뒤라 돌아가야 한다
        "B": (1.0, 4.0),  # 직선 3.0m — 벽이 없다
    }
    plan = select_next(
        cycle=1,
        visited=frozenset({"C"}),
        start=start,
        candidates=candidates,
        order=("A", "B", "C"),
        grid=grid,
        blocked=blocked,
        params=PLAN,
        random_after_first_cycle=True,
    )
    assert plan.label == "B", "직선거리로 골랐다 — 벽을 무시한 것이다"


# ══════════════════════════════════════════════════════════════
#  구역 저장 — config.zones.ids 가 정본
# ══════════════════════════════════════════════════════════════


def test_zone_labels_come_from_the_config() -> None:
    store = ZoneStore(("A", "B", "C"))
    assert store.next_label == "A"
    store.place(1.0, 1.0)
    assert store.next_label == "B"


def test_zone_store_refuses_labels_beyond_the_config() -> None:
    """⚠️ 제멋대로 `D` 를 만들면 마커 매핑·대시보드와 어긋난다."""
    store = ZoneStore(("A", "B"))
    store.place(1.0, 1.0)
    store.place(2.0, 2.0)
    assert store.place(3.0, 3.0) is None
    assert len(store) == 2


def test_unknown_labels_in_the_file_are_ignored(tmp_path: Path) -> None:
    """합쳐 오기 전 `zones.json` 은 A~D 였다 — 설정은 A~C 다."""
    (tmp_path / "zones.json").write_text(
        json.dumps(
            {
                "A": {"x": 1.0, "y": 1.0},
                "B": {"x": 2.0, "y": 2.0},
                "C": {"x": 3.0, "y": 3.0},
                "D": {"x": 4.0, "y": 4.0},
            }
        ),
        encoding="utf-8",
    )
    store = ZoneStore.load(tmp_path, ("A", "B", "C"))
    assert store.labels == ("A", "B", "C")
    assert "D" not in store


def test_zones_survive_a_save_and_load(tmp_path: Path) -> None:
    store = ZoneStore(("A", "B", "C"))
    store.place(1.275, 1.275)
    store.place(4.025, 1.225)
    store.save(tmp_path)
    loaded = ZoneStore.load(tmp_path, ("A", "B", "C"))
    assert loaded.xy("A") == pytest.approx((1.275, 1.275))
    assert loaded.labels == ("A", "B")


def test_undo_removes_the_last_placed_zone() -> None:
    store = ZoneStore(("A", "B", "C"))
    store.place(1.0, 1.0)
    store.place(2.0, 2.0)
    removed = store.undo()
    assert removed is not None
    assert removed.label == "B"
    assert store.next_label == "B"


def test_clicking_a_wall_moves_to_a_definitely_free_cell() -> None:
    """미관측 셀로 옮기면 A* 가 거기까지 갈 경로를 못 찾는다."""
    grid = blank(40, 40)
    grid.cells[:, :] = 0.0  # 전부 미관측
    grid.cells[20, 20] = 3.0  # 벽
    grid.cells[20, 25] = -3.0  # 확실히 빈 곳
    cell = nearest_free_cell(grid, 20, 20, free_thresh=-1.0, max_radius_cells=10)
    assert cell == (20, 25)


def test_no_free_cell_nearby_returns_none() -> None:
    grid = blank(40, 40)
    grid.cells[20, 20] = 3.0
    assert nearest_free_cell(grid, 20, 20, free_thresh=-1.0, max_radius_cells=5) is None


# ══════════════════════════════════════════════════════════════
#  설정 — NFR-3①
# ══════════════════════════════════════════════════════════════


def test_lidar_config_has_every_required_key() -> None:
    section = read_lidar_section()
    missing = [key for key in REQUIRED_LIDAR_KEYS if key not in section]
    assert not missing, missing


def test_lidar_scan_port_differs_from_the_other_links() -> None:
    """⚠️ 같은 포트를 쓰면 두 스키마가 한 소켓에 섞여 서로를 폐기한다."""
    root = Path(__file__).resolve().parents[1]
    base = yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
    section = read_lidar_section()
    taken = {base["network"]["cmd_port"], base["network"]["telemetry_port"]}
    assert section["scan_port"] not in taken


def test_path_clearance_exceeds_the_host_estop_distance() -> None:
    """⚠️ **계획된 경로가 E-STOP 거리를 지나가면 안 된다.**

    팽창(로봇 반경 + 추종 여유)이 E-STOP 거리보다 크지 않으면, 경로를 완벽히
    따라 걷는 것만으로 LiDAR 가 E-STOP 거리를 읽어 **정상 순찰이 비상정지로
    끝난다.** 실제로 겪었다 — 반경 150mm 와 E-STOP 150mm 로 두었더니 한
    사이클에 E-STOP 이 6회 나고 구역 하나를 못 갔다.

    값 하나하나는 그럴듯해 보이므로 사람이 검토로 잡기 어렵다. 그래서 시험과
    `settings._validate` 양쪽에서 **관계**를 본다.
    """
    section = read_lidar_section()
    clearance = section["robot_radius_mm"] + section["tracking_margin_mm"]
    assert clearance > section["estop_distance_mm"]


def test_config_rejects_clearance_below_the_estop_distance() -> None:
    """설정이 그 관계를 깨면 **기동을 거부한다.**"""
    from host.common.config import ConfigError
    from host.slam.settings import validate_section

    section = dict(read_lidar_section())
    section["estop_distance_mm"] = 400  # 반경 150 + 여유 100 보다 크다
    with pytest.raises(ConfigError, match="estop_distance_mm"):
        validate_section(section)


def test_host_lidar_estop_is_tighter_than_the_onboard_threshold() -> None:
    """온보드(Tier 1)가 먼저 반응해야 한다 — 호스트 판정은 그것을 더한다."""
    root = Path(__file__).resolve().parents[1]
    base = yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
    section = read_lidar_section()
    onboard_mm = base["safety"]["obstacle_stop_cm"] * 10
    assert section["estop_distance_mm"] < onboard_mm


# ══════════════════════════════════════════════════════════════
#  ROS2 맵 판독 — `slam_toolbox` 교체 접점 (ADR-9)
# ══════════════════════════════════════════════════════════════


def write_ros2_map(directory: Path, pixels: np.ndarray, **spec: object) -> Path:
    """map_server 형식 맵을 만든다. 시험이 `slam_toolbox` 를 대신한다."""
    height, width = pixels.shape
    newline = chr(10)
    header = f"P5{newline}{width} {height}{newline}255{newline}".encode("ascii")
    (directory / "m.pgm").write_bytes(header + pixels.tobytes())
    fields = {
        "image": "m.pgm",
        "resolution": 0.05,
        "origin": [0.0, 0.0, 0.0],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
        **spec,
    }
    path = directory / "m.yaml"
    path.write_text(yaml.safe_dump(fields), encoding="utf-8")
    return path


def test_slam_toolbox_unknown_value_does_not_become_free(tmp_path: Path) -> None:
    """⚠️ **이것이 ROS2 판독의 핵심 함정이다.**

    `slam_toolbox`(map_server)의 기본 미지값은 **205** 이고, `negate: 0` 에서
    확률로는 `(255-205)/255 = 0.196` 이다. 그 값이 하필 `free_thresh` 기본값
    `0.196` 과 같아서, **임계로 분류하지 않고 확률을 그대로 로그오즈에 넣으면
    미지가 자유쪽으로 떨어진다.**

    그러면 `planner.inflate` 가 지키는 불변식(*"모르는 곳은 빈 곳이 아니다"*)이
    깨지고, A* 가 한 번도 관측하지 않은 공간을 최단 경로로 골라 로봇을
    내보낸다. 지도는 그럴듯해 보이므로 눈으로는 잡히지 않는다.
    """
    # 254 자유 · 0 점유 · 205 미지 — map_server 기본값 그대로
    pixels = np.array([[254, 0, 205, 254]], dtype=np.uint8)
    grid = OccupancyGrid.load_ros2(write_ros2_map(tmp_path, pixels))

    assert grid.cells[0, 0] < 0, "254 는 자유여야 한다"
    assert grid.cells[0, 1] > 0, "0 은 점유여야 한다"
    assert grid.cells[0, 2] == 0.0, "205 는 **미관측**이어야 한다 (자유가 아니다)"

    blocked = inflate(grid, PlanParams(1.0, -1.0, 0.0, 0.08))
    assert not blocked[0, 0], "자유는 통행 가능"
    assert blocked[0, 1], "점유는 통행 불가"
    assert blocked[0, 2], "미지도 통행 불가 — 모르는 곳은 빈 곳이 아니다"


def test_ros2_map_rows_are_unflipped_on_read(tmp_path: Path) -> None:
    """ROS2 의 첫 행은 y 최대다. 되뒤집지 않으면 지도가 상하 반전으로 들어온다."""
    pixels = np.array([[0, 254], [254, 254]], dtype=np.uint8)  # 첫 행 = y 최대에 벽
    grid = OccupancyGrid.load_ros2(write_ros2_map(tmp_path, pixels))
    assert grid.cells[1, 0] > 0, "y 최대(행 1)에 벽이 와야 한다"
    assert grid.cells[0, 0] < 0


def test_ros2_map_carries_origin_and_resolution(tmp_path: Path) -> None:
    pixels = np.full((3, 4), 254, dtype=np.uint8)
    path = write_ros2_map(tmp_path, pixels, resolution=0.1, origin=[-2.5, 1.25, 0.0])
    grid = OccupancyGrid.load_ros2(path)
    assert grid.meta.resolution == pytest.approx(0.1)
    assert grid.meta.origin_x == pytest.approx(-2.5)
    assert grid.meta.origin_y == pytest.approx(1.25)
    assert (grid.meta.height, grid.meta.width) == (3, 4)


def test_negate_flips_the_probability(tmp_path: Path) -> None:
    """`negate: 1` 이면 픽셀이 그대로 점유 확률이다 (map_server 규약)."""
    pixels = np.array([[255, 0]], dtype=np.uint8)
    grid = OccupancyGrid.load_ros2(write_ros2_map(tmp_path, pixels, negate=1))
    assert grid.cells[0, 0] > 0, "negate=1 에서 255 는 점유"
    assert grid.cells[0, 1] < 0, "negate=1 에서 0 은 자유"


def test_rotated_map_origin_is_refused(tmp_path: Path) -> None:
    """회전된 원점을 조용히 무시하면 **지도 전체가 어긋난 채로 순찰이 돈다.**"""
    pixels = np.full((2, 2), 254, dtype=np.uint8)
    path = write_ros2_map(tmp_path, pixels, origin=[0.0, 0.0, 1.57])
    with pytest.raises(ValueError, match="origin yaw"):
        OccupancyGrid.load_ros2(path)


def test_pgm_header_comments_are_skipped(tmp_path: Path) -> None:
    """주석은 헤더 어디에나 올 수 있다. 고정 오프셋으로 읽으면 어긋난다."""
    pixels = np.array([[0, 254, 254, 254]], dtype=np.uint8)
    header = (
        chr(10)
        .join(("P5", "# made by some other tool", "4 1", "# another comment", "255", ""))
        .encode("ascii")
    )
    (tmp_path / "m.pgm").write_bytes(header + pixels.tobytes())
    (tmp_path / "m.yaml").write_text(
        yaml.safe_dump({"image": "m.pgm", "resolution": 0.05, "origin": [0.0, 0.0, 0.0]}),
        encoding="utf-8",
    )
    grid = OccupancyGrid.load_ros2(tmp_path / "m.yaml")
    assert grid.cells.shape == (1, 4)
    assert grid.cells[0, 0] > 0


def test_load_falls_back_to_the_ros2_pair(tmp_path: Path) -> None:
    """⚠️ **`slam_toolbox` 만 돌린 경우 `.npy` 가 없다.**

    그때도 구역 지정(`3.9.1`)과 경로계획이 그대로 돌아야 ADR-9 의 *"블랙박스로
    교체"* 가 성립한다. 이 시험이 그 계약이다.
    """
    grid = blank(12, 8)
    grid.cells[:, :] = -3.0
    grid.cells[7, 0] = 3.0
    grid.save(tmp_path)

    # 우리 작업 형식을 지운다 = slam_toolbox 산출물만 남은 상태
    (tmp_path / "slam_map.npy").unlink()
    (tmp_path / "map_meta.json").unlink()

    loaded = OccupancyGrid.load(tmp_path)
    assert loaded.cells.shape == grid.cells.shape
    assert loaded.cells[7, 0] > 0, "벽이 자리를 지켜야 한다"
    assert loaded.cells[3, 5] < 0, "자유 공간이 자유로 남아야 한다"
    assert loaded.meta.origin_x == pytest.approx(grid.meta.origin_x)


def test_load_prefers_the_richer_working_format(tmp_path: Path) -> None:
    """`.npy` 는 로그오즈를 온전히 갖고 있어 이어서 매핑할 수 있다."""
    grid = blank(10, 10)
    grid.cells[4, 4] = 2.5  # 8비트로는 복원 불가한 중간 확신
    grid.save(tmp_path)
    assert OccupancyGrid.load(tmp_path).cells[4, 4] == pytest.approx(2.5)


def test_missing_map_says_both_sources(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="slam_toolbox"):
        OccupancyGrid.load(tmp_path)
