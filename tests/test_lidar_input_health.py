"""패킷 수신 정상과 완성 스캔 정상은 다르다 — 실제 각도 반복 장애의 회귀시험."""

import json
import logging

from host.common.lidar_link import ScanDecoder
from host.telemetry.lidar_feed import LidarFeed


def test_repeated_angles_warn_once_then_complete_scan_recovers(caplog):
    now = [0]
    feed = LidarFeed(
        lidar_device="lidar-test", decoder=ScanDecoder(), forward_fan_rad=0.3,
        estop_m=0.15, armed=lambda: False, on_danger=lambda: None, clock=lambda: now[0],
    )

    def send(seq, points, boot="boot-one"):
        feed.handle(json.dumps({"type": "SCAN", "device_id": "lidar-test", "boot_id": boot,
                                "seq": seq, "ts": now[0], "points": points}).encode())

    with caplog.at_level(logging.INFO):
        for seq in range(1, 101):
            now[0] = (seq - 1) * 10
            send(seq, [[347.38, 1000], [347.38, 0]])
        assert feed.take() is None
        warnings = [x for x in caplog.records if x.msg == "lidar_scan_input_incomplete"]
        assert len(warnings) == 1
        assert warnings[0].detail["coverage_bins"] == 1
        assert warnings[0].detail["invalid_points"] > 0
        now[0] = 1010
        send(101, [[i * 5, 1000] for i in range(72)])
        assert feed.take() is not None
        assert any(x.msg == "lidar_scan_input_recovered" for x in caplog.records)


def test_healthy_revolutions_and_new_boot_do_not_warn(caplog):
    now = [0]
    feed = LidarFeed(
        lidar_device="lidar-test", decoder=ScanDecoder(), forward_fan_rad=0.3,
        estop_m=0.15, armed=lambda: False, on_danger=lambda: None, clock=lambda: now[0],
    )
    with caplog.at_level(logging.INFO):
        for i in range(30):
            now[0] = i * 100
            boot = "boot-one" if i < 15 else "boot-two"
            feed.handle(json.dumps({"type": "SCAN", "device_id": "lidar-test", "boot_id": boot,
                                    "seq": i + 1 if i < 15 else i - 14, "ts": now[0],
                                    "points": [[a * 5, 1000] for a in range(72)]}).encode())
    assert feed.revolutions[0] == 30
    assert not any(x.msg == "lidar_scan_input_incomplete" for x in caplog.records)
