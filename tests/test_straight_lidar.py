"""직진 왕복 시험 도구 — 벽 맞춤·조향 부호·기울기 맞춤 (로봇 없이)."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location(
    "straight_lidar", Path(__file__).resolve().parents[1] / "tools" / "probe" / "straight_lidar.py"
)
straight = importlib.util.module_from_spec(spec)
sys.modules["straight_lidar"] = straight
spec.loader.exec_module(straight)

LEFT_Y, RIGHT_Y, FRONT_X, BACK_X = 0.8, -1.2, 6.0, -3.0


def _scan(pose, beams=360, noise=0.0, rng=None):
    """통로(왼벽 y=0.8, 오른벽 y=-1.2, 앞벽 x=6, 뒷벽 x=-3) 안에서 로봇 기준 점."""
    x, y, yaw = pose
    pts = []
    for i in range(beams):
        a = 2 * math.pi * i / beams
        dx, dy = math.cos(yaw + a), math.sin(yaw + a)
        hits = []
        if dy > 1e-9:
            hits.append((LEFT_Y - y) / dy)
        if dy < -1e-9:
            hits.append((RIGHT_Y - y) / dy)
        if dx > 1e-9:
            hits.append((FRONT_X - x) / dx)
        if dx < -1e-9:
            hits.append((BACK_X - x) / dx)
        r = min(hits) + (rng.normal(0, noise) if rng is not None else 0.0)
        pts.append((r * math.cos(a), r * math.sin(a)))
    return np.array(pts)


def test_wall_fit_reads_heading_and_distance():
    fit = straight.fit_side_wall(_scan((0.0, 0.0, math.radians(5))), "left")
    assert fit is not None
    assert fit.heading_deg == pytest.approx(5.0, abs=0.3), "왼쪽으로 5° 돌면 +5"
    assert fit.distance_m == pytest.approx(0.8, abs=0.01)
    right = straight.fit_side_wall(_scan((0.0, 0.0, math.radians(-4))), "right")
    assert right.heading_deg == pytest.approx(-4.0, abs=0.3)
    assert right.distance_m == pytest.approx(1.2, abs=0.01)


def test_wall_fit_ignores_a_furniture_leg():
    pts = _scan((0.0, 0.0, 0.0))
    legs = np.array([[0.3, 0.45], [0.31, 0.46], [0.32, 0.45], [-0.5, 0.5], [-0.51, 0.5]])
    fit = straight.fit_side_wall(np.vstack([pts, legs]), "left")
    assert fit.distance_m == pytest.approx(0.8, abs=0.02)
    assert fit.heading_deg == pytest.approx(0.0, abs=0.5)


def test_range_along_front_and_back():
    pts = _scan((1.0, 0.0, 0.0))
    assert straight.range_along(pts, True) == pytest.approx(5.0, abs=0.02)
    assert straight.range_along(pts, False) == pytest.approx(4.0, abs=0.02)


def _walk(mode, forward, *, seconds=30.0, drift_deg_s=-2.8, seed=0):
    """오른쪽으로 −2.8°/s 휘는 보행(09-22 실측)을 흉내 낸다. angle 은 선회율로 반영."""
    rng = np.random.default_rng(seed)
    pose = (0.0, 0.0, 0.0)
    ref = straight.fit_side_wall(_scan(pose), "left")
    speed = 0.041 * (1 if forward else -1)  # 60mm 걸음 ≈ 41mm/s
    dt = 0.1
    for _ in range(int(seconds / dt)):
        angle = 0.0
        if mode == "hold":
            now = straight.fit_side_wall(_scan(pose, noise=0.005, rng=rng), "left")
            angle = straight.hold_angle(ref, now, reverse=not forward)
        yaw_rate = math.radians(drift_deg_s + 1.0 * angle)  # angle 1° ≈ 1°/s (근사)
        x, y, yaw = pose
        yaw += yaw_rate * dt
        pose = (x + speed * math.cos(yaw) * dt, y + speed * math.sin(yaw) * dt, yaw)
    return pose


@pytest.mark.parametrize("forward", [True, False])
def test_hold_keeps_the_line_where_open_loop_curves_right(forward):
    open_pose = _walk("open", forward)
    hold_pose = _walk("hold", forward)
    assert abs(math.degrees(open_pose[2])) > 60, "그냥 걸으면 크게 돈다"
    assert abs(hold_pose[1]) < 0.05, f"벽을 보며 걸으면 옆으로 5cm 안 ({hold_pose[1]:.3f})"
    assert abs(math.degrees(hold_pose[2])) < 6, "방위도 거의 그대로"
    assert abs(hold_pose[0]) > 1.0, "실제로 1m 넘게 갔다"


def test_hold_angle_signs():
    ref = straight.WallFit("left", 0.0, 0.8, 50)
    turned_right = straight.WallFit("left", -5.0, 0.8, 50)
    assert straight.hold_angle(ref, turned_right, reverse=False) > 0, "오른쪽으로 돌았으면 왼쪽으로"
    pushed_right = straight.WallFit("left", 0.0, 0.85, 50)
    assert straight.hold_angle(ref, pushed_right, reverse=False) > 0, (
        "전진 중 오른쪽 밀림 → 코를 왼쪽"
    )
    assert straight.hold_angle(ref, pushed_right, reverse=True) < 0, (
        "후진 중 오른쪽 밀림 → 코를 오른쪽"
    )
    far = straight.WallFit("left", -40.0, 0.8, 50)
    assert straight.hold_angle(ref, far, reverse=False) == pytest.approx(12.0), "상한"


def test_roll_fit_recommends_level_command():
    # 10-03 실측: POSE −4 → IMU +5.9, −10 → +12.2, +2 → +2.0
    fit = straight.fit_roll_offset([(-4.0, 5.9), (-10.0, 12.2), (2.0, 2.0)])
    assert fit["slope"] < 0, "명령 부호와 IMU 부호가 반대"
    assert 3.0 < fit["cmd_for_level"] < 6.0
    assert straight.fit_roll_offset([(0.0, 3.7)]) is None


def test_trace_reports_right_shift_in_cm_from_both_walls():
    refs = {
        "left": straight.fit_side_wall(_scan((0.0, 0.0, 0.0)), "left"),
        "right": straight.fit_side_wall(_scan((0.0, 0.0, 0.0)), "right"),
    }
    row = straight.trace_point(_scan((0.5, -0.03, 0.0)), refs, 0.5)  # 3cm 오른쪽으로
    assert row["left_cm"] == pytest.approx(83.0, abs=0.3)
    assert row["right_cm"] == pytest.approx(117.0, abs=0.3)
    assert row["left_delta_cm"] == pytest.approx(3.0, abs=0.3)
    assert row["right_delta_cm"] == pytest.approx(-3.0, abs=0.3)
    assert row["right_shift_cm"] == pytest.approx(3.0, abs=0.3)
    turned = straight.trace_point(_scan((0.5, 0.0, math.radians(3))), refs, 0.5)
    assert turned["heading_change_deg"] == pytest.approx(3.0, abs=0.3)


def test_trace_summary_slope_and_extremes():
    rows = [
        {
            "travelled_m": d,
            "right_shift_cm": 2.0 * d,
            "heading_change_deg": -d,
            "left_cm": 80 + 2 * d,
        }
        for d in np.linspace(0, 2, 21)
    ]
    out = straight.summarize_trace(rows)
    assert out["shift_cm_per_m"] == pytest.approx(2.0, abs=0.01)
    assert out["end_right_shift_cm"] == pytest.approx(4.0)
    assert out["max_abs_shift_cm"] == pytest.approx(4.0)
    assert out["left_wall_cm"]["start"] == pytest.approx(80.0)
    assert straight.summarize_trace([]) is None


def test_trace_plot_writes_png(tmp_path):
    legs = [
        {
            "mode": "open",
            "direction": "forward",
            "_trace": [{"travelled_m": d, "right_shift_cm": 3 * d} for d in (0.0, 0.5, 1.0)],
        }
    ]
    straight.plot_traces(tmp_path / "t.png", legs)
    assert (tmp_path / "t.png").stat().st_size > 1000


def test_device_status_ready_and_warnings():
    kw = {"battery_warn_v": 7.0}
    ok_robot, ok_lidar, warn = straight.device_status(
        telemetry_age_ms=200, batt_v=8.1, latched=False, packets_per_s=70, revs_per_s=10, **kw
    )
    assert ok_robot and ok_lidar and warn == []
    _, ok_lidar, warn = straight.device_status(
        telemetry_age_ms=None, batt_v=None, latched=None, packets_per_s=8, revs_per_s=0, **kw
    )
    assert not ok_lidar and any("347" in w for w in warn), "10-04 이상 모양(8패킷·0바퀴)"
    ok_robot, _, warn = straight.device_status(
        telemetry_age_ms=300, batt_v=6.8, latched=True, packets_per_s=70, revs_per_s=10, **kw
    )
    assert ok_robot and any("배터리" in w for w in warn) and any("래치" in w for w in warn)
    ok_robot, _, _ = straight.device_status(
        telemetry_age_ms=3000, batt_v=8.0, latched=False, packets_per_s=0, revs_per_s=0, **kw
    )
    assert not ok_robot, "3초 묵은 텔레메트리는 연결이 아니다"


def test_tip_over_stops_the_run():
    class R:
        roll, pitch, safety_latched, obstacle = -62.0, 3.0, False, False

    link = straight.Link.__new__(straight.Link)
    link.reading, link.reading_ms, link.points_ms = (
        R(),
        straight.system_clock_ms(),
        straight.system_clock_ms(),
    )
    assert "넘어짐" in link.fresh()
    R.roll = 8.0
    assert link.fresh() is None
