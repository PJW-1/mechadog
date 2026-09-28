"""Continuous mapping accepts only located, level, stationary observations."""

import json
import math

import pytest

from host.common.lidar_link import encode_scan
from host.slam import settings
from host.slam.continuous_map import ContinuousMap, LidarPose
from host.vision.stream_client import Frame
from tools.lidar_live_map import pose_from_datagram
from tools.lidar_map_replay import replay, simulation_events


def _config():
    config = settings.load(None)
    config["lidar"] = dict(config["lidar"])
    config["lidar"]["mount_yaw_deg"] = 0
    config["lidar"]["angle_direction"] = 1
    return config


def _scan(seq):
    return encode_scan(
        seq=seq,
        ts_ms=1000 + seq,
        device_id="lidar-test",
        boot_id="boot",
        points_wire=[[angle + 0.5, 1000] for angle in range(360)],
    )


def test_missing_stale_and_moving_pose_never_pollute_map(tmp_path):
    mapper = ContinuousMap(_config(), "lidar-test")
    assert not mapper.add(_scan(1), 1000, None)
    assert not mapper.add(_scan(2), 2000, LidarPose(1600, 0, 0, 0, True, True))
    assert not mapper.add(_scan(3), 3000, LidarPose(3000, 0, 0, 0, False, True))
    assert not mapper.add(_scan(4), 4000, LidarPose(4000, 0, 0, 0, True, False))
    assert mapper.grid.known_cells() == 0
    assert mapper.counts["mapped_revolutions"] == 0
    assert mapper.counts["missing_or_stale_pose"] == 2
    assert mapper.counts["moving_or_tilted"] == 2
    assert len(mapper.pose_track) == 2
    mapper.save(tmp_path)
    assert "<circle" in (tmp_path / "map_view.html").read_text(encoding="utf-8")


def test_no_pose_does_not_draw_robot_at_origin(tmp_path):
    mapper = ContinuousMap(_config(), "lidar-test")
    assert not mapper.add(_scan(1), 1000, None)
    mapper.save(tmp_path)
    assert "<circle" not in (tmp_path / "map_view.html").read_text(encoding="utf-8")


def test_two_measured_positions_accumulate_in_one_map(tmp_path):
    mapper = ContinuousMap(_config(), "lidar-test")
    first = LidarPose(1000, 0, 0, 0, True, True)
    second = LidarPose(2000, 0.5, 0, math.pi / 2, True, True)
    assert mapper.add(_scan(1), 1000, first)
    assert mapper.add(_scan(2), 2000, second)
    mapper.save(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["mapped_revolutions"] == 2
    assert json.loads((tmp_path / "track.json").read_text(encoding="utf-8")) == [
        [0, 0, 0],
        [0.5, 0, math.pi / 2],
    ]
    page = (tmp_path / "map_view.html").read_text(encoding="utf-8")
    assert "slam_map.png" in page
    assert "초록 선" in page
    assert (tmp_path / "slam_map.pgm").exists()
    assert len(json.loads((tmp_path / "pose_track.json").read_text(encoding="utf-8"))) == 2


def test_simulation_events_replay_to_map_without_robot_commands(tmp_path):
    config = _config()
    mapper = ContinuousMap(config, "lidar-sim", source="simulation")
    counts = replay(simulation_events(config, "lidar-sim"), mapper, tmp_path, tmp_path, 5)
    assert counts["mapped_revolutions"] == 18
    assert len(mapper.track) == 18
    assert len({(x, y) for x, y, _ in mapper.track}) == 6
    assert (tmp_path / "map_view.html").exists()


def test_invalid_or_wrong_device_scan_does_not_change_map():
    mapper = ContinuousMap(_config(), "lidar-test")
    pose = LidarPose(1000, 0, 0, 0, True, True)
    assert not mapper.add(b"broken", 1000, pose)
    raw = json.loads(_scan(1))
    raw["device_id"] = "another-device"
    assert not mapper.add(json.dumps(raw), 1001, pose)
    assert mapper.counts["invalid_scan"] == 1
    assert mapper.counts["wrong_device"] == 1
    assert mapper.grid.known_cells() == 0


def test_photo_is_pinned_only_to_fresh_mapped_pose(tmp_path):
    captured = []

    class Photos:
        def capture(self, _directory, _step, pose, scan_ms, frame):
            captured.append((pose, scan_ms, frame.received_ms))

    mapper = ContinuousMap(_config(), "lidar-test", Photos())
    pose = LidarPose(1000, 0, 0, 0, True, True)
    stale = Frame(b"\xff\xd8photo\xff\xd9", 400, 1)
    fresh = Frame(b"\xff\xd8photo\xff\xd9", 1000, 2)
    assert mapper.add(_scan(1), 1000, pose, stale, photo_dir=tmp_path)
    assert captured == []
    assert mapper.add(
        _scan(2), 1100, LidarPose(1100, 0, 0, 0, True, True), fresh, photo_dir=tmp_path
    )
    assert captured == [((0, 0, 0), 1100, 1000)]
    assert mapper.add(
        _scan(3), 1200, LidarPose(1200, 0, 0, 0, True, True), fresh, photo_dir=tmp_path
    )
    assert len(captured) == 1


def test_live_pose_is_host_timestamped_and_rejects_guess_flags():
    pose = pose_from_datagram(
        b'{"x_m":0.5,"y_m":-0.2,"yaw_rad":0,"stationary":true,"level":true}',
        1234,
    )
    assert pose == LidarPose(1234, 0.5, -0.2, 0.0, True, True)
    with pytest.raises(ValueError, match="boolean"):
        pose_from_datagram(
            b'{"x_m":0,"y_m":0,"yaw_rad":0,"stationary":1,"level":true}',
            1234,
        )
    with pytest.raises(ValueError, match="유한한"):
        pose_from_datagram(
            b'{"x_m":NaN,"y_m":0,"yaw_rad":0,"stationary":true,"level":true}',
            1234,
        )
