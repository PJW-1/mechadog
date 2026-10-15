"""ROS2 map->base_link 반환 링크 (WBS 5.4.4)."""

from __future__ import annotations

import json
import math
import runpy
from pathlib import Path

import pytest

from host.common.map_pose_link import (
    MapPoseDecoder,
    MapPoseEncoder,
    encode_map_pose,
    map_pose_of,
)
from host.common.protocol import Verdict

BRIDGE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/ros2/pose_bridge.py"))
yaw_of_quaternion = BRIDGE["yaw_of_quaternion"]
transform_age_s = BRIDGE["transform_age_s"]


def line(**overrides: object) -> str:
    fields = {
        "seq": 1,
        "ts_ms": 1000,
        "device_id": "mechdog-02",
        "boot_id": "a" * 16,
        "x_m": 1.25,
        "y_m": -0.5,
        "yaw_rad": 0.75,
        "valid": True,
    }
    fields.update(overrides)
    return encode_map_pose(**fields)  # type: ignore[arg-type]


def test_map_pose_round_trip_and_frames() -> None:
    encoder = MapPoseEncoder("mechdog-02", "a" * 16)
    raw = encoder.encode(ts_ms=5, x_m=1.0, y_m=2.0, yaw_rad=-0.5, valid=True)
    pose = map_pose_of(MapPoseDecoder().decode(raw))
    message = json.loads(raw)
    assert pose is not None
    assert (pose.seq, pose.ts_ms, pose.x_m, pose.y_m, pose.yaw_rad, pose.valid) == (
        1,
        5,
        1.0,
        2.0,
        -0.5,
        True,
    )
    assert (message["frame_id"], message["child_frame_id"]) == ("map", "base_link")


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "[]",
        line(seq=0),
        line(ts_ms=-1),
        line(device_id=""),
        line().replace('"valid":true', '"valid":1'),
        line().replace('"x_m":1.25', '"x_m":"1.25"'),
        line().replace('"yaw_rad":0.75', '"yaw_rad":NaN'),
        line().replace('"frame_id":"map"', '"frame_id":"odom"'),
        line().replace('"child_frame_id":"base_link"', '"child_frame_id":"laser"'),
    ],
)
def test_malformed_or_wrong_frame_pose_is_discarded(raw: str) -> None:
    result = MapPoseDecoder().decode(raw)
    assert result.verdict in (Verdict.DISCARD, Verdict.DISCARD_WARN)
    assert map_pose_of(result) is None


def test_seq_reversal_is_discarded_and_new_bridge_session_is_accepted() -> None:
    decoder = MapPoseDecoder()
    assert decoder.decode(line(seq=5)).accepted
    assert not decoder.decode(line(seq=4)).accepted
    assert decoder.decode(line(seq=1, boot_id="b" * 16)).accepted


def test_pose_bridge_quaternion_and_staleness_helpers() -> None:
    half = math.pi / 4
    assert yaw_of_quaternion(0.0, 0.0, math.sin(half), math.cos(half)) == pytest.approx(math.pi / 2)
    assert transform_age_s(2_500_000_000, 2, 0) == pytest.approx(0.5)
    assert transform_age_s(1_000_000_000, 2, 0) == 0.0
