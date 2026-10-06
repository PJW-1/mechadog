"""File preparation preserves input maps and rejects unsupported observations."""

import json

import numpy as np
import pytest

from host.slam import settings
from host.slam.map_refinement import MapRefinement
from host.slam.occupancy import MapMeta, OccupancyGrid
from tools.ops.refinement_prepare import load_grid, prepare, scope_mask
from tools.ops.refinement_replay import audit_saved_observations, replay


def test_prepare_never_fills_unknown_gap_or_modifies_inputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    grid = OccupancyGrid(MapMeta(0.05, 0, 0, 40, 30), np.full((30, 40), -5, dtype=np.float32))
    grid.cells[:, 20] = 0
    grid.save(source)
    original = (source / "slam_map.npy").read_bytes()
    structure = tmp_path / "structure.json"
    structure.write_text(
        json.dumps(
            {
                "floors": [
                    {
                        "patrol_scope": "requested",
                        "model_include": True,
                        "bounds_xy_m": [0, 0, 2, 1.5],
                    }
                ]
            }
        )
    )
    route = tmp_path / "route.json"
    route.write_text(
        json.dumps({"anchors_m": {"a": [0.5, 0.75], "b": [1.5, 0.75]}, "sequence": ["a", "b", "a"]})
    )
    result = prepare(source, "slam_map", structure, route, tmp_path / "candidate")
    assert result["accepted_legs"] == 0
    assert all(leg["reason"] == "no_clear_observed_connection" for leg in result["legs"])
    assert result["physical_commands_sent"] == 0
    assert result["commands_enabled"] is False
    primary = json.loads((tmp_path / "candidate/mission_primary.json").read_text())
    assert primary["candidate_loop_available"] is False
    assert primary["stops"] == []
    scope = json.loads((tmp_path / "candidate/scope.json").read_text())
    assert scope["allowed_rectangles_m"] == [[0, 0, 2, 1.5]]
    assert (source / "slam_map.npy").read_bytes() == original
    with pytest.raises(ValueError, match="separate"):
        prepare(source, "slam_map", structure, route, source)


def test_scope_blocks_entire_entry_and_partial_boundary_cells():
    grid = OccupancyGrid(MapMeta(0.05, 0, 0, 8, 8))
    mask = scope_mask(
        grid,
        {
            "floors": [
                {
                    "patrol_scope": "requested",
                    "model_include": True,
                    "bounds_xy_m": [0.025, 0, 0.2, 0.4],
                },
                {
                    "patrol_scope": "excluded_by_user",
                    "model_include": False,
                    "bounds_xy_m": [0.2, 0, 0.4, 0.4],
                },
            ]
        },
    )
    assert mask[:, 0].all()
    assert mask[:, 4:].all()
    assert not mask[:, 1:4].any()


def test_loading_invalid_map_metadata_or_nonfinite_cells_fails(tmp_path):
    meta = {"resolution": 0.05, "origin_x": 0, "origin_y": 0, "width": 3, "height": 3}
    (tmp_path / "map_meta.json").write_text(json.dumps(meta))
    np.save(tmp_path / "slam_map.npy", np.full((3, 3), np.nan))
    with pytest.raises(ValueError, match="Invalid map"):
        load_grid(tmp_path, "slam_map")
    with pytest.raises(ValueError, match="filename"):
        load_grid(tmp_path, "../private")


def test_historical_snapshots_are_not_repeated_as_live_scans(tmp_path):
    source = tmp_path / "observations.json"
    source.write_text(
        json.dumps({"records": [{"id": "old-stop", "points": [[0.2, 0.3], [0.4, 0.5]]}]})
    )
    report = audit_saved_observations(source)
    assert report["actual_saved_points"] == 2
    assert report["committed_revolutions"] == 0
    assert report["rejections"][0]["eligible_for_online_map_update"] is False


def test_replay_rejects_base_link_pose_and_reversed_time():
    config = settings.load(None)
    seed = OccupancyGrid.blank(resolution=0.05, span_cells=30)
    mapper = MapRefinement(config, "lidar-test", seed, "map")
    report = replay(
        [
            {
                "kind": "pose",
                "received_ms": 100,
                "pose_origin": "base_link",
                "frame_id": "map",
                "localization_valid": True,
                "laser_extrinsics_verified": True,
                "stationary": True,
                "level": True,
            },
            {"kind": "scan", "received_ms": 100, "payload": {}},
        ],
        mapper,
    )
    assert report["committed_revolutions"] == 0
    assert report["updates"][0]["reason"] == "missing_pose"
    with pytest.raises(ValueError, match="monotonic"):
        replay(
            [
                {"kind": "localization_lost", "received_ms": 20},
                {"kind": "localization_lost", "received_ms": 10},
            ],
            mapper,
        )
