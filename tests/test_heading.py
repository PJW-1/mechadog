"""IMU 방위 앵커(`HeadingTracker`) 단위 시험 — 순찰기 없이 안전 관측·자세만 흉내 낸다."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from host.behavior.heading import HeadingTracker
from host.behavior.patrol import SafetyView


def tracker(yaw_deg: float | None = None, seen_ms: int | None = None, pose_yaw: float = 0.0) -> Any:
    owner = SimpleNamespace(
        safety=SafetyView(yaw_deg=yaw_deg, last_seen_ms=seen_ms),
        pose=(0.0, 0.0, pose_yaw),
        imu_fresh_ms=300,
    )
    return owner, HeadingTracker(owner)


def test_imu_freshness_uses_the_owner_limit() -> None:
    owner, heading = tracker(yaw_deg=10.0, seen_ms=1000)
    assert heading.imu_is_fresh(1300) is True
    assert heading.imu_is_fresh(1301) is False
    assert heading.imu_is_fresh(999) is False, "미래 표본은 신선하지 않다"
    owner.safety = SafetyView(last_seen_ms=1000)
    assert heading.imu_is_fresh(1000) is False, "yaw 가 없으면 신선하지 않다"


def test_anchor_binds_only_a_fresh_imu() -> None:
    _, heading = tracker(yaw_deg=20.0, seen_ms=1000)
    assert heading.anchor_pose(1100) == pytest.approx(math.radians(20))
    assert heading.anchor == pytest.approx(math.radians(20))
    assert heading.anchor_pose(2000) is None
    assert heading.anchor is None


def test_steering_yaw_follows_the_imu_between_scans() -> None:
    owner, heading = tracker(yaw_deg=30.0, seen_ms=1000, pose_yaw=math.radians(10))
    assert heading.steering_yaw() == pytest.approx(math.radians(10)), "옵셋 전에는 정합 방위"
    heading.align_offset()
    assert heading.offset == pytest.approx(math.radians(20))
    owner.safety = SafetyView(yaw_deg=45.0, last_seen_ms=1100)
    assert heading.steering_yaw() == pytest.approx(math.radians(25))


def test_adopt_aligns_anchor_and_offset_to_the_request_imu() -> None:
    _, heading = tracker(yaw_deg=30.0, seen_ms=1000, pose_yaw=math.radians(10))
    heading.adopt(math.radians(10))
    assert heading.anchor == pytest.approx(math.radians(10))
    assert heading.offset == pytest.approx(0.0)
    assert heading.steering_yaw() == pytest.approx(math.radians(30))
    heading.adopt(None)
    assert heading.anchor is None
    assert heading.offset == pytest.approx(0.0), "요청 IMU 가 없으면 옵셋은 그대로"


def test_yaw_delta_is_measured_from_the_anchor_and_flags_freshness() -> None:
    owner, heading = tracker(yaw_deg=0.0, seen_ms=1000)
    assert heading.consume_yaw_delta(1000) == 0.0
    assert heading.anchor == pytest.approx(0.0), "앵커가 없으면 지금 값으로 묶는다"
    assert heading.delta_fresh is False
    owner.safety = SafetyView(yaw_deg=15.0, last_seen_ms=1100)
    assert heading.consume_yaw_delta(1150) == pytest.approx(math.radians(15))
    assert heading.delta_fresh is True
    assert heading.consume_yaw_delta(1150) == pytest.approx(math.radians(15)), "앵커는 그대로"
    assert heading.consume_yaw_delta(2000) == pytest.approx(math.radians(15))
    assert heading.delta_fresh is False, "텔레메트리가 묵었다"
    assert heading.consume_yaw_delta() == pytest.approx(math.radians(15))
    assert heading.delta_fresh is False, "시각이 없으면 신선하다고 보지 않는다"
