"""Saved scan projection must use the measured mount correction once."""

import pytest

from host.common.lidar_link import encode_scan
from tools.lidar_camera_overlay import project_saved_scans


def test_project_saved_scan_uses_mount_and_scan_height():
    calibration = {
        "camera": {
            "width_px": 640,
            "height_px": 480,
            "fx_px": 424,
            "fy_px": 424,
            "cx_px": 320,
            "cy_px": 240,
            "height_m": 0.14,
            "forward_m": 0.10,
            "left_m": 0.0,
            "pitch_down_deg": -15,
            "yaw_left_deg": 0,
        },
        "lidar_height_m": 0.225,
        "mount_yaw_deg": 270,
        "angle_direction": -1,
    }
    # Raw 270° is robot forward after mount correction.
    raw = encode_scan(seq=1, ts_ms=1, device_id="test", boot_id="boot", points_wire=[[270, 1000]])
    result = project_saved_scans([raw], calibration)
    assert result["merged_angle_bins"] == 1
    assert result["visible_scan_points"] == 1
    point = result["points"][0]
    assert point["forward_m"] == pytest.approx(1)
    # Existing 1-degree merge bins report their centre, up to 0.5 degree away.
    assert abs(point["left_m"]) < 0.01
    assert abs(point["u_px"] - 320) < 5
