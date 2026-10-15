"""느슨한 동선 추종(`RelaxedFollower`) 단위 시험 — 켜짐 조건·측위 신선도·길 막힘·조향을 본다."""

from __future__ import annotations

import math

import pytest
from test_relaxed_follow import relaxed


def test_active_only_on_a_relaxed_route_that_is_not_returning_home() -> None:
    c = relaxed()
    assert c.relaxed.active
    c.recovery.returning_home = True
    assert not c.relaxed.active
    c.recovery.returning_home = False
    c.cancel_route()
    assert not c.relaxed.active


def test_pose_available_needs_a_verified_pose_within_two_seconds() -> None:
    c = relaxed()
    c.localization.last_verified_pose_ms = 1000
    assert c.relaxed.pose_available(3000)
    assert not c.relaxed.pose_available(3001)
    assert not c.relaxed.pose_available(999), "미래 시각의 검증은 믿지 않는다"
    c.localization.last_verified_pose_ms = None
    assert not c.relaxed.pose_available(1000)


def test_stuck_check_reports_once_then_releases_when_the_front_opens() -> None:
    c = relaxed()
    follower = c.relaxed
    target = (4.0, 2.0)
    c._now_ms = 1000
    assert not follower.stuck_check(target, 2.0, 0.3)
    c._now_ms = 1000 + c.nav_params.relaxed_stuck_ms
    assert follower.stuck_check(target, 2.0, 0.3)
    assert follower.stuck and follower.blocked_reported
    assert c.local_status["reason"] == "relaxed_stuck_wait"
    assert [e["event"] for e in c.take_navigation_events()] == ["path_blocked"]
    c._now_ms += 1000
    assert follower.stuck_check(target, 2.0, 0.3), "막힌 동안은 계속 선다"
    assert c.take_navigation_events() == ()
    assert not follower.stuck_check(target, 2.0, 0.5)
    assert not follower.stuck and not follower.blocked_reported
    assert follower.progress == (target, 2.0, c._now_ms)


@pytest.mark.parametrize(
    "error_deg,fields",
    [
        (0.0, {"step": None, "angle": 0.0}),
        (2.0, {"step": None, "angle": 0.0}),
        (10.0, {"step": None, "angle": 12.0}),
        (40.0, {"step": 0.0, "angle": None}),
    ],
)
def test_walk_steers_while_walking_or_spins_when_far_off(error_deg, fields) -> None:
    c = relaxed()
    c._spinning = False
    c.relaxed.walk(math.radians(error_deg), 0.0, 0.0, None)
    sent = c.commander.intent.fields
    assert sent["step"] == (c.drive.step_mm if fields["step"] is None else fields["step"])
    expected = c.drive.spin_turn_deg if fields["angle"] is None else fields["angle"]
    assert sent["angle"] == pytest.approx(expected)
    assert c._spinning is (error_deg > 30)
    assert c.local_status["reason"] == "relaxed_follow"


def test_follow_arrives_when_the_point_is_within_the_arrival_radius() -> None:
    c = relaxed(pose=(4.0, 2.0, 0.0))
    c.relaxed.follow()
    assert c.route.stage != "moving"
    assert c.relaxed.progress is None and not c.relaxed.stuck
