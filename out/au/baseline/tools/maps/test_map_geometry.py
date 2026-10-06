"""Coordinate and source-protection checks for offline map tools."""

import argparse

import numpy as np
import pytest

from host.slam.occupancy import MapMeta, OccupancyGrid
from tools.maps.build_house_map import build, polygons_mask, reproject


def test_reproject_uses_cell_centres_and_general_rotation():
    source = OccupancyGrid(MapMeta(1, 0, 0, 4, 4), np.arange(16, dtype=np.float32).reshape(4, 4))
    target = OccupancyGrid(MapMeta(1, 0, 0, 4, 4))
    cells, _, _ = reproject(source, target, {"rotate_deg": 90, "translate_x": 4, "translate_y": 0})
    np.testing.assert_array_equal(cells, np.rot90(source.cells, 1))


def test_polygons_use_inverse_pose_frame():
    grid = OccupancyGrid(MapMeta(1, 0, 0, 6, 6))
    frame = {"rotate_deg": 90, "translate_x": 6, "translate_y": 0}
    mask = polygons_mask([[[2, 1], [4, 1], [4, 3], [2, 3]]], grid, frame)
    assert mask[3, 2]
    assert not mask[0, 0]


def test_existing_destination_is_protected_before_sources_are_opened(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}")
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "preserved.txt"
    sentinel.write_text("original")
    with pytest.raises(FileExistsError):
        build(argparse.Namespace(evidence=evidence, output=destination, diagnostics=None))
    assert sentinel.read_text() == "original"
