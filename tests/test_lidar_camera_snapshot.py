"""A stationary observation must map the measured rays, without inventing travel."""

import math
from dataclasses import dataclass

import pytest

from host.slam import settings
from tools.lidar_camera_snapshot import build_map


@dataclass
class Scan:
    points: tuple[tuple[float, float], ...]


def test_stationary_map_has_measured_wall_and_unknown_outside():
    config = settings.load(None)
    rays = tuple((math.radians(angle + 0.5), 1.0) for angle in range(360))
    grid, bins = build_map([Scan(rays) for _ in range(50)], config)
    assert bins == 360
    wall = grid.to_cell(math.cos(math.radians(0.5)), math.sin(math.radians(0.5)))
    nearer = grid.to_cell(0.5, 0.0)
    outside = grid.to_cell(2.0, 0.0)
    assert grid.cells[wall] > 0
    assert grid.cells[nearer] < 0
    assert grid.cells[outside] == 0


def test_sparse_scan_is_rejected():
    config = settings.load(None)
    rays = tuple((math.radians(angle), 1.0) for angle in range(0, 360, 2))
    with pytest.raises(ValueError, match="유효 각도"):
        build_map([Scan(rays) for _ in range(50)], config)
