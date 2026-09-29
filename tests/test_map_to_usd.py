"""`tools/lidar/map_to_usd.py` — 점유격자에서 세운 USD 가 지도와 같은 자리인지 본다.

이 도구의 존재 이유가 **시뮬과 실기가 같은 좌표계를 쓰게 하는 것**이므로,
시험도 거기에 맞춘다: 프림이 몇 개 나왔는지보다 **어디에 섰는지**가 중요하다.
"""

from __future__ import annotations

import json
import re

import numpy as np
import pytest

from host.slam.occupancy import LOGODDS_MAX, LOGODDS_MIN, MapMeta, OccupancyGrid
from tools.lidar.map_to_usd import boxes_from_grid, build_usda, main, merge_rectangles


def _grid(cells: np.ndarray, *, resolution: float = 0.05) -> OccupancyGrid:
    meta = MapMeta.of(
        {
            "resolution": resolution,
            "origin_x": -1.0,
            "origin_y": -2.0,
            "width": cells.shape[1],
            "height": cells.shape[0],
        }
    )
    return OccupancyGrid(meta, cells)


def test_merge_rectangles_covers_exactly_once() -> None:
    """겹치지도 빠지지도 않아야 한다 — 충돌 형상의 최소 조건이다."""
    rng = np.random.default_rng(7)
    mask = rng.random((13, 17)) < 0.4
    painted = np.zeros_like(mask, dtype=int)
    for row0, row1, col0, col1 in merge_rectangles(mask):
        painted[row0:row1, col0:col1] += 1
    assert (painted == mask.astype(int)).all()


def test_merge_rectangles_joins_a_solid_block() -> None:
    """한 덩어리는 상자 하나로 합쳐진다 — 칸마다 프림을 두면 GPU 가 버틴 적이 없다."""
    mask = np.zeros((6, 6), dtype=bool)
    mask[1:5, 2:5] = True
    assert merge_rectangles(mask) == [(1, 5, 2, 5)]


def test_box_sits_on_the_cells_it_came_from() -> None:
    """상자의 중심과 크기가 **칸의 실좌표**와 맞아야 한다."""
    cells = np.zeros((4, 4))
    cells[1:3, 1:3] = LOGODDS_MAX
    grid = _grid(cells)
    (box,) = boxes_from_grid(grid, include_unknown=False)

    assert box.kind == "wall"
    assert box.size_x == pytest.approx(0.10)
    assert box.size_y == pytest.approx(0.10)
    # 칸 1·2 의 중심은 -1.0 + 1.5*0.05 와 -1.0 + 2.5*0.05 → 그 중간
    assert box.x == pytest.approx(-1.0 + 2.0 * 0.05)
    assert box.y == pytest.approx(-2.0 + 2.0 * 0.05)


def test_unknown_is_not_free() -> None:
    """미지는 기본적으로 형상이 없지만, 막아 달라면 막는다."""
    cells = np.full((3, 3), LOGODDS_MIN)
    cells[0, 0] = LOGODDS_MAX
    cells[2, 2] = 0.0
    grid = _grid(cells)

    kinds_open = [b.kind for b in boxes_from_grid(grid, include_unknown=False)]
    kinds_wall = [b.kind for b in boxes_from_grid(grid, include_unknown=True)]

    assert kinds_open == ["wall"]
    assert sorted(kinds_wall) == ["unknown", "wall"]


def test_usda_keeps_map_metres() -> None:
    """`metersPerUnit` 이 1 이 아니면 지도의 m 가 USD 에서 다른 길이가 된다."""
    cells = np.zeros((2, 2))
    cells[0, 0] = LOGODDS_MAX
    grid = _grid(cells)
    text = build_usda(
        grid, boxes_from_grid(grid, include_unknown=False), height=1.0, lidar_plane_m=0.225
    )

    assert "metersPerUnit = 1" in text
    assert 'upAxis = "Z"' in text
    assert "PhysicsCollisionAPI" in text
    assert "PhysicsScene" in text
    # 벽 높이가 측정값이 아니라는 경고가 문서 문자열에 남아야 한다.
    assert "측정값이 아니다" in text


def test_cube_scale_is_full_extent() -> None:
    """`size = 1` 과 `scale` 이 짝이 맞아야 벽 두께가 두 배로 서지 않는다."""
    cells = np.zeros((1, 3))
    cells[0, :] = LOGODDS_MAX
    grid = _grid(cells)
    text = build_usda(
        grid, boxes_from_grid(grid, include_unknown=False), height=2.0, lidar_plane_m=0.225
    )

    assert "double size = 1" in text
    scale = re.search(r"xformOp:scale = \(([\d.]+), ([\d.]+), ([\d.]+)\)", text)
    assert scale is not None
    assert float(scale.group(1)) == pytest.approx(0.15)  # 3 칸 × 0.05
    assert float(scale.group(3)) == pytest.approx(2.0)
    # 상자 바닥이 z=0 에 닿도록 중심이 높이의 절반에 있어야 한다.
    translate = re.search(r"xformOp:translate = \(\S+ \S+ (-?[\d.]+)\)", text)
    assert translate is not None
    assert float(translate.group(1)) == pytest.approx(1.0)


def test_main_writes_usd_and_stats(tmp_path) -> None:
    cells = np.full((5, 5), LOGODDS_MIN)
    cells[0, :] = LOGODDS_MAX
    cells[4, 4] = 0.0
    _grid(cells).save(tmp_path, stem="room")

    out = tmp_path / "out" / "room.usda"
    assert main([str(tmp_path), "--stem", "room", "--out", str(out)]) == 0

    assert out.is_file()
    stats = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert stats["occupied_cells"] == 5
    assert stats["unknown_cells"] == 1
    assert stats["wall_prims"] == 1  # 다섯 칸이 한 줄이라 상자 하나
    assert stats["unknown_prims"] == 0  # 기본값은 미지를 세우지 않는다
    assert stats["unknown_mode"] == "open"
    # ⚠️ 이 값이 참으로 바뀌면 누군가 벽 높이를 쟀다는 뜻이다. 지금은 아니다.
    assert stats["wall_height_measured"] is False
