"""The sim exchange must preserve measured geometry and its uncertainty."""

import json

import numpy as np
import pytest

from host.slam import settings
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.scene_bundle import build_scene_bundle, export_scene_bundle


def test_scene_bundle_preserves_unknown_and_does_not_invent_3d():
    grid = OccupancyGrid(
        MapMeta(0.1, -1.0, 2.0, 4, 2),
        np.array([[0.0, 1.2, 1.4, -1.5], [0.1, -1.2, 0.0, 1.1]], dtype=np.float32),
    )
    bundle = build_scene_bundle(
        grid,
        occupied_logodds=1.0,
        free_logodds=-1.0,
        source="simulation",
        photos=[
            {
                "x_m": 0.0,
                "y_m": 1.0,
                "yaw_rad": 0.0,
                "frame_received_ms": 123,
                "photo": "photos/step-0001.jpg",
            }
        ],
        track=[[0.0, 1.0, 0.0]],
    )
    geometry = bundle["geometry"]
    assert geometry["occupied_runs_row_start_end"] == [[0, 1, 3], [1, 3, 4]]
    assert geometry["free_runs_row_start_end"] == [[0, 3, 4], [1, 1, 2]]
    assert geometry["origin_xy_m"] == [-1.0, 2.0]
    assert geometry["height_m"] is None
    assert bundle["navigation_ready"] is False
    assert bundle["camera_observations"][0]["projection"].startswith("unavailable")


def test_scene_export_uses_saved_map_and_photo_pose(tmp_path):
    grid = OccupancyGrid.blank(resolution=0.1, span_cells=4)
    grid.cells[1, 2] = 2.0
    grid.save(tmp_path)
    (tmp_path / "photo_poses.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "x_m": 0,
                        "y_m": 0,
                        "yaw_rad": 0,
                        "frame_received_ms": 1000,
                        "photo": "photos/step-0001.jpg",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "scene.json"
    bundle = export_scene_bundle(tmp_path, output, settings.load(None))
    assert json.loads(output.read_text(encoding="utf-8")) == bundle
    assert len(bundle["camera_observations"]) == 1
    assert bundle["geometry"]["occupied_runs_row_start_end"] == [[1, 2, 3]]


def test_stationary_snapshot_keeps_single_pose_source(tmp_path):
    grid = OccupancyGrid.blank(resolution=0.1, span_cells=4)
    grid.cells[1, 1] = 2.0
    grid.save(tmp_path, stem="static_map")
    (tmp_path / "summary.json").write_text(
        json.dumps({"kind": "stationary_single_pose_snapshot_not_slam"}), encoding="utf-8"
    )
    bundle = export_scene_bundle(
        tmp_path, tmp_path / "scene.json", settings.load(None), stem="static_map"
    )
    assert bundle["source"] == "stationary_single_pose_snapshot_not_slam"
    assert bundle["navigation_ready"] is False


def test_scene_export_rejects_nonfinite_and_unmeasured_photo_pose():
    grid = OccupancyGrid.blank(resolution=0.1, span_cells=2)
    grid.cells[0, 0] = float("nan")
    with pytest.raises(ValueError, match="유한하지"):
        build_scene_bundle(grid, occupied_logodds=1, free_logodds=-1, source="test")
    grid.cells[0, 0] = 0
    with pytest.raises(ValueError, match="카메라 위치"):
        build_scene_bundle(
            grid,
            occupied_logodds=1,
            free_logodds=-1,
            source="test",
            photos=[
                {
                    "x_m": float("nan"),
                    "y_m": 0,
                    "yaw_rad": 0,
                    "frame_received_ms": 1000,
                    "photo": "photo.jpg",
                }
            ],
        )
