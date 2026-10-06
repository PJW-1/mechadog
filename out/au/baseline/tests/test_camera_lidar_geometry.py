"""Camera/LiDAR projection must keep centimetres, axes, and uncertainty honest."""

import math

import pytest

from host.slam.camera_lidar_geometry import CameraGeometry


def camera(*, pitch=15.0, yaw=0.0):
    return CameraGeometry(
        width_px=640,
        height_px=480,
        fx_px=424,
        fy_px=424,
        cx_px=320,
        cy_px=240,
        height_m=1.0,
        forward_m=0.0,
        left_m=0.0,
        pitch_down_deg=pitch,
        yaw_left_deg=yaw,
    )


def test_ground_intersection_uses_pitch_and_rejects_above_horizon():
    assert camera().floor_xy(320, 240) == pytest.approx((1 / math.tan(math.radians(15)), 0))
    assert camera(pitch=0).floor_xy(320, 240) is None
    assert camera(pitch=0).floor_xy(320, 340) == pytest.approx((4.24, 0))
    assert camera(yaw=90).floor_xy(320, 240) == pytest.approx(
        (0, 1 / math.tan(math.radians(15))), abs=1e-12
    )


def test_lidar_scan_plane_and_floor_pixel_can_be_cross_checked():
    geometry = camera()
    ground_pixel = geometry.project_xyz(5.0, 0.5, 0.0)
    lidar_pixel = geometry.lidar_hit_pixel(5.0, 0.5, lidar_height_m=0.2)
    assert ground_pixel is not None and lidar_pixel is not None
    assert geometry.floor_xy(*ground_pixel) == pytest.approx((5.0, 0.5))
    assert lidar_pixel[1] < ground_pixel[1]  # laser plane is above the floor
    assert geometry.project_xyz(-1.0, 0.0, 0.0) is None


def test_height_requires_visible_floor_contact_and_reports_pixel_mismatch():
    geometry = camera()
    foot = geometry.project_xyz(5.0, 0.5, 0.0)
    top = geometry.project_xyz(5.0, 0.5, 1.5)
    assert foot is not None and top is not None
    assert geometry.apparent_height(*foot, *top) == pytest.approx((1.5, 0.0), abs=1e-12)
    shifted = geometry.apparent_height(*foot, top[0] + 20, top[1])
    assert shifted is not None and shifted[1] > 0
    assert geometry.apparent_height(320, 0, *top) is None


def test_calibration_requires_measured_finite_geometry():
    with pytest.raises(ValueError, match="초점거리"):
        CameraGeometry(640, 480, 0, 424, 320, 240, 1, 0, 0, 15, 0)
    with pytest.raises(ValueError, match="유한"):
        CameraGeometry(640, 480, 424, 424, 320, 240, math.nan, 0, 0, 15, 0)
    with pytest.raises(ValueError, match="라이다"):
        camera().lidar_hit_pixel(1, 0, lidar_height_m=-0.1)


def test_upward_mounted_camera_only_intersects_floor_below_horizon():
    geometry = CameraGeometry(640, 480, 424, 424, 320, 240, 0.14, 0.10, 0, -15, 0)
    assert geometry.floor_xy(320, 240) is None
    assert geometry.floor_xy(320, 340) is None
    bottom = geometry.floor_xy(320, 479)
    assert bottom is not None
    assert bottom[0] > 0.10
    assert geometry.project_xyz(bottom[0], bottom[1], 0) == pytest.approx((320, 479))
