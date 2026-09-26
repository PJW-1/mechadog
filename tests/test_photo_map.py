"""Camera photos are pinned to measured poses only when their arrival is fresh."""

import json

import pytest

from host.common.lidar_link import Scan, points_from_wire
from host.slam import settings
from host.slam.photo_map import PhotoRecorder
from host.vision.stream_client import Frame
from tools import lidar_slam


def _recorder() -> PhotoRecorder:
    return PhotoRecorder(
        {"vision": {"reconnect_backoff_s": [1.0], "stall_timeout_ms": 1000}},
        "http://192.168.0.19:81/stream",
    )


def test_photo_pose_uses_fresh_arrival_and_never_claims_object_location(tmp_path):
    recorder = _recorder()
    recorder._queue.put(Frame(b"\xff\xd8image\xff\xd9", 1000, 1))
    frame = recorder.sample(1200)
    assert frame is not None
    recorder.capture(tmp_path, 1, (0.25, -0.5, 0.1), 1200, frame)
    recorder.save(tmp_path, [-1.0, 1.0, -1.0, 1.0], has_png=True)

    payload = json.loads((tmp_path / "photo_poses.json").read_text(encoding="utf-8"))
    assert payload["kind"] == "camera_pose_photos_not_object_locations"
    assert payload["records"][0]["skew_ms"] == 200
    assert (tmp_path / "photos/step-0001.jpg").read_bytes() == frame.payload
    page = (tmp_path / "photo_map.html").read_text(encoding="utf-8")
    assert 'href="photos/step-0001.jpg"' in page
    assert "사진 속 물체의 지도 좌표를 뜻하지 않습니다" in page


def test_stale_or_non_jpeg_frame_is_not_mapped(tmp_path):
    recorder = _recorder()
    recorder._queue.put(Frame(b"\xff\xd8valid\xff\xd9", 1000, 1))
    assert recorder.sample(1501) is None
    recorder._queue.put(Frame(b"broken", 2000, 2))
    assert recorder.sample(2000) is None
    recorder.save(tmp_path, [-1.0, 1.0, -1.0, 1.0], has_png=False)
    assert json.loads((tmp_path / "photo_poses.json").read_text(encoding="utf-8"))["records"] == []
    assert not (tmp_path / "photo_map.html").exists()


def test_camera_url_requires_camera_stream_and_real_lidar():
    with pytest.raises(ValueError):
        PhotoRecorder(
            {"vision": {"reconnect_backoff_s": [1.0], "stall_timeout_ms": 1000}},
            "http://192.168.0.19:81/other",
        )
    assert lidar_slam.main(["--simulate", "--camera-url", "http://192.168.0.19:81/stream"]) == 2


def test_real_mapping_path_attaches_a_photo_without_robot_commands(tmp_path, monkeypatch):
    """Exercise the actual mapper's non-simulation wiring with fake inbound sensors."""
    config = settings.load(None)
    config["localization"]["track"] = "lidar"
    points = points_from_wire([[angle, 1000] for angle in range(0, 360, 2)])
    scan = Scan("lidar-01", "boot", 1, 1000, points)
    calls = []

    class Socket:
        def close(self):
            calls.append("socket_closed")

    class Recorder:
        def __init__(self, _config, _url):
            calls.append("created")

        def start(self):
            calls.append("started")

        def sample(self, _received_ms):
            calls.append("sampled")
            return Frame(b"\xff\xd8photo\xff\xd9", 1000, 1)

        def capture(self, _path, _step, _pose, _received_ms, _frame):
            calls.append("captured")

        def close(self):
            calls.append("closed")

        def save(self, _path, _extent, *, has_png):
            calls.append(("saved", has_png))

    monkeypatch.setattr(lidar_slam, "open_scan_socket", lambda *_args: Socket())
    monkeypatch.setattr(lidar_slam, "prepare_real_capture", lambda *_args: None)
    monkeypatch.setattr(lidar_slam, "collect_real", lambda *_args: [scan])
    monkeypatch.setattr(lidar_slam, "PhotoRecorder", Recorder)
    output = tmp_path / "run"
    args = lidar_slam.build_parser().parse_args(
        [
            "--device",
            "mechdog-02",
            "--lidar-device",
            "lidar-01",
            "--steps",
            "1",
            "--out",
            str(output),
            "--camera-url",
            "http://192.168.0.19:81/stream",
        ]
    )
    assert lidar_slam.run(args, config) == 0
    assert calls.index("sampled") < calls.index("captured") < calls.index("closed")
    assert any(isinstance(call, tuple) and call[0] == "saved" for call in calls)
    assert (output / "slam_map.npy").exists()
