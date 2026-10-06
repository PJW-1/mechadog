"""Synthetic gate tests; these do not demonstrate real robot calibration."""

from __future__ import annotations

import copy
import json
import math
from typing import Any

import numpy as np
import pytest

from host.common.lidar_link import encode_scan
from host.slam.continuous_map import LidarPose
from host.slam.map_refinement import MapRefinement, UpdateResult
from host.slam.occupancy import LOGODDS_MAX, MapMeta, OccupancyGrid

DEFAULT_POSE = LidarPose(1000, 0, 0, 0, True, True)


def config() -> dict[str, Any]:
    return {
        "lidar": {
            "resolution_mm": 50,
            "initial_span_cells": 40,
            "mount_yaw_deg": 0,
            "angle_direction": 1,
            "range_min_mm": 100,
            "range_max_mm": 8000,
            "hit_logodds": 0.85,
            "miss_logodds": -0.4,
            "expand_pad_cells": 4,
        }
    }


def seed() -> OccupancyGrid:
    return OccupancyGrid(MapMeta(0.05, -1.0, -1.0, 40, 40))


def scan(
    seq: int,
    *,
    angles: range = range(360),
    distance_mm: int = 1000,
    device: str = "lidar-test",
    boot: str = "boot-a",
) -> str:
    return encode_scan(
        seq=seq,
        ts_ms=1000 + seq,
        device_id=device,
        boot_id=boot,
        points_wire=[[angle + 0.5, distance_mm] for angle in angles],
    )


def submit(
    refinement: MapRefinement,
    raw: str,
    received_ms: int = 1000,
    pose: LidarPose | None = DEFAULT_POSE,
    **overrides: Any,
) -> UpdateResult:
    options = {
        "pose_frame_id": "map",
        "localization_valid": True,
        "laser_extrinsics_verified": True,
        "stationary_verified": True,
        "level_verified": True,
    }
    options.update(overrides)
    return refinement.submit(raw, received_ms, pose, **options)


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"pose_frame_id": "house"}, "frame_mismatch"),
        ({"localization_valid": False}, "localization_unverified"),
        ({"laser_extrinsics_verified": False}, "laser_extrinsics_unverified"),
        ({"stationary_verified": False}, "stationary_unverified"),
        ({"level_verified": False}, "level_unverified"),
        ({"stationary_verified": 1}, "stationary_unverified"),
    ],
)
def test_unverified_gate_cannot_commit(options: dict[str, Any], reason: str) -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    revision = refinement.revision
    result = submit(refinement, scan(1), **options)
    assert not result.accepted
    assert result.reason == reason
    assert result.revision == revision
    assert result.changed_cells == 0
    assert refinement.snapshot().known_cells() == 0


@pytest.mark.parametrize(
    ("pose", "reason"),
    [
        (None, "missing_pose"),
        (LidarPose(749, 0, 0, 0, True, True), "stale_pose"),
        (LidarPose(1001, 0, 0, 0, True, True), "future_pose"),
        (LidarPose(1000, math.nan, 0, 0, True, True), "invalid_pose"),
        (LidarPose(1000, 0, 0, 0, False, True), "moving_pose"),
        (LidarPose(1000, 0, 0, 0, True, False), "tilted_pose"),
    ],
)
def test_bad_pose_never_changes_map(pose: LidarPose | None, reason: str) -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    result = submit(refinement, scan(1), pose=pose)
    assert not result.accepted
    assert result.reason == reason
    assert refinement.snapshot().known_cells() == 0


def test_complete_verified_revolution_commits_and_does_not_mutate_seed() -> None:
    original = seed()
    mapping_config = config()
    unchanged_config = copy.deepcopy(mapping_config)
    refinement = MapRefinement(mapping_config, "lidar-test", original, "map")
    revision = refinement.revision
    result = submit(refinement, scan(1), pose=LidarPose(750, 0, 0, 0, True, True))
    assert result.accepted
    assert result.reason == "committed"
    assert result.changed_cells > 270
    assert result.revision != revision
    assert original.known_cells() == 0
    assert mapping_config == unchanged_config
    assert refinement.metadata()["counts"]["mapped_revolutions"] == 1
    snapshot = refinement.snapshot()
    snapshot.cells[:, :] = 123
    snapshot.meta.origin_x = 200
    assert not np.all(refinement.snapshot().cells == 123)
    assert refinement.snapshot().meta.origin_x != 200
    json.dumps(refinement.metadata())


def test_duplicate_and_reversed_sequences_do_not_map_again() -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert submit(refinement, scan(2)).accepted
    revision = refinement.revision
    for raw in (scan(2), scan(1)):
        result = submit(refinement, raw)
        assert not result.accepted
        assert result.reason == "invalid_scan"
        assert result.revision == revision
    assert refinement.metadata()["counts"]["mapped_revolutions"] == 1


def test_rejected_scan_cannot_be_resubmitted_with_a_fresh_pose() -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert submit(refinement, scan(1), localization_valid=False).reason == "localization_unverified"
    result = submit(refinement, scan(1))
    assert not result.accepted
    assert result.reason == "invalid_scan"
    assert refinement.snapshot().known_cells() == 0


@pytest.mark.parametrize("bad_raw", ["broken", scan(2, device="another-device"), scan(1)])
def test_invalid_packet_discards_partial_revolution(bad_raw: str) -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert submit(refinement, scan(1, angles=range(180))).reason == "awaiting_revolution"
    assert not submit(refinement, bad_raw).accepted
    result = submit(refinement, scan(3, angles=range(180, 360)))
    assert not result.accepted
    assert refinement.snapshot().known_cells() == 0


def test_bad_observation_gate_discards_partial_revolution() -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert not submit(refinement, scan(1, angles=range(180))).accepted
    assert not submit(refinement, scan(2), stationary_verified=False).accepted
    assert not submit(refinement, scan(3, angles=range(180, 360))).accepted
    assert refinement.snapshot().known_cells() == 0


def test_device_restart_cannot_merge_old_and_new_boot_batches() -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert not submit(refinement, scan(1, angles=range(180))).accepted
    assert not submit(refinement, scan(1, angles=range(180, 360), boot="boot-b")).accepted
    assert submit(refinement, scan(2, angles=range(180), boot="boot-b")).accepted


def test_sparse_batch_expiration_and_changed_pose_discard_old_points() -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert not submit(refinement, scan(1, angles=range(180))).accepted
    result = submit(
        refinement,
        scan(2, angles=range(180, 360)),
        1501,
        LidarPose(1501, 0, 0, 0, True, True),
    )
    assert not result.accepted
    assert result.reason == "sparse_batch"
    assert not submit(
        refinement, scan(3, angles=range(180)), 1600, LidarPose(1600, 0.1, 0, 0, True, True)
    ).accepted
    assert refinement.snapshot().known_cells() == 0


def test_partial_valid_packets_merge_only_within_time_and_pose_bounds() -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert not submit(refinement, scan(1, angles=range(180))).accepted
    result = submit(
        refinement,
        scan(2, angles=range(180, 360)),
        1200,
        LidarPose(1200, 0.005, 0, math.radians(1), True, True),
    )
    assert result.accepted
    assert refinement.metadata()["counts"]["mapped_revolutions"] == 1


def test_no_go_survives_free_rays_and_grid_expansion() -> None:
    original = seed()
    refinement = MapRefinement(
        config(),
        "lidar-test",
        original,
        "map",
        no_go_rectangles=((0.1, -0.1, 0.3, 0.1), (1.5, -0.1, 1.8, 0.1)),
    )
    before = refinement.snapshot()
    assert before.cells[before.to_cell(0.2, 0)] == LOGODDS_MAX
    assert original.cells[original.to_cell(0.2, 0)] == 0
    for seq in range(1, 8):
        result = submit(refinement, scan(seq, distance_mm=3000))
        assert result.accepted
        after = refinement.snapshot()
        assert after.cells[after.to_cell(0.2, 0)] == LOGODDS_MAX
        assert after.cells[after.to_cell(1.7, 0)] == LOGODDS_MAX
    assert after.meta.origin_x < before.meta.origin_x
    assert after.meta.width > before.meta.width


def test_allowed_indoor_union_blocks_outside_even_after_expansion() -> None:
    refinement = MapRefinement(
        config(),
        "lidar-test",
        seed(),
        "map",
        allowed_rectangles=((-0.5, -0.5, 0.5, 0.5), (0.5, -0.25, 0.8, 0.25)),
    )
    before = refinement.snapshot()
    assert before.cells[before.to_cell(0.2, 0)] == 0
    assert before.cells[before.to_cell(0.7, 0)] == 0
    assert before.cells[before.to_cell(0.7, 0.4)] == LOGODDS_MAX
    result = submit(refinement, scan(1, distance_mm=3000))
    assert result.accepted
    after = refinement.snapshot()
    assert after.cells[after.to_cell(0.2, 0)] < 0
    assert after.cells[after.to_cell(0.7, 0)] < 0
    for point in ((0.7, 0.4), (2, 0), (-2, 0), (0, 2), (0, -2)):
        assert after.cells[after.to_cell(*point)] == LOGODDS_MAX
    assert refinement.metadata()["allowed_rectangles_m"] == [
        [-0.5, -0.5, 0.5, 0.5],
        [0.5, -0.25, 0.8, 0.25],
    ]


def test_allowed_scope_is_conservative_and_included_in_revision() -> None:
    unrestricted = MapRefinement(config(), "lidar-test", seed(), "map")
    restricted = MapRefinement(
        config(), "lidar-test", seed(), "map", allowed_rectangles=((0.01, 0.01, 0.99, 0.99),)
    )
    snapshot = restricted.snapshot()
    assert snapshot.cells[snapshot.to_cell(0.02, 0.02)] == LOGODDS_MAX
    assert snapshot.cells[snapshot.to_cell(0.98, 0.98)] == LOGODDS_MAX
    assert snapshot.cells[snapshot.to_cell(0.2, 0.2)] == 0
    assert restricted.revision != unrestricted.revision
    empty_scope = MapRefinement(config(), "lidar-test", seed(), "map", allowed_rectangles=())
    assert np.all(empty_scope.snapshot().cells == LOGODDS_MAX)


def test_revisions_are_deterministic_and_metadata_is_detached() -> None:
    first = MapRefinement(config(), "lidar-test", seed(), "map")
    second = MapRefinement(config(), "lidar-test", seed(), "map")
    assert first.revision == second.revision
    assert submit(first, scan(1)) == submit(second, scan(1))
    metadata = first.metadata()
    metadata["counts"]["rejected"]["invented"] = 3
    metadata["grid"]["origin_x"] = 999
    assert "invented" not in first.metadata()["counts"]["rejected"]
    assert first.metadata()["grid"]["origin_x"] != 999


def test_saturated_commit_still_has_a_new_observation_identity() -> None:
    saturation_config = config()
    saturation_config["lidar"].update(hit_logodds=5.0, miss_logodds=-5.0)
    refinement = MapRefinement(saturation_config, "lidar-test", seed(), "map")
    previous = submit(refinement, scan(1))
    for seq in range(2, 10):
        result = submit(refinement, scan(seq))
        assert result.accepted and result.observation_id is not None
        assert result.observation_id != previous.observation_id
        if result.changed_cells == 0:
            assert result.revision == previous.revision
            duplicate = submit(refinement, scan(seq))
            assert not duplicate.accepted and duplicate.observation_id is None
            break
        previous = result
    else:
        pytest.fail("Repeated stationary fixture did not reach saturated map content")


def test_receive_clock_reversal_is_rejected() -> None:
    refinement = MapRefinement(config(), "lidar-test", seed(), "map")
    assert submit(refinement, scan(1)).accepted
    result = submit(refinement, scan(2), 999, LidarPose(999, 0, 0, 0, True, True))
    assert not result.accepted
    assert result.reason == "receive_time_reversed"


def test_invalid_seed_and_exclusion_geometry_fail_before_mapping() -> None:
    original = seed()
    with pytest.raises(ValueError, match="positive width"):
        MapRefinement(config(), "lidar-test", original, "map", no_go_rectangles=((0, 0, 0, 1),))
    different_resolution = seed()
    different_resolution.meta.resolution = 0.1
    with pytest.raises(ValueError, match="resolution"):
        MapRefinement(config(), "lidar-test", different_resolution, "map")
    original.cells[0, 0] = math.nan
    with pytest.raises(ValueError, match="finite"):
        MapRefinement(config(), "lidar-test", original, "map")
