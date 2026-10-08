"""동선 추종(`RouteFollower`) 단위 시험 — 시작·취소·지점 넘기기·건너뛰기를 순찰기 하나로 본다."""

from __future__ import annotations

import math

import pytest
from test_lidar_patrol import build
from test_route_runtime import fresh, route

from host.behavior.patrol import GOAL_LABEL, Phase
from host.behavior.routes import RoutePoint

A = RoutePoint(x=1.0, y=1.0, label="A")
B = RoutePoint(x=4.0, y=1.0)
C = RoutePoint(x=4.0, y=3.0, label="A")


def started(*points: RoutePoint, repeat: int = 1):
    c = build()
    fresh(c, 1000, (2.0, 2.0, 0.0))
    ok, detail = c.start_route(route(*points, repeat=repeat), 1000)
    assert ok, detail
    return c


def test_start_resets_progress_and_controller_delegates() -> None:
    c = build()
    assert c.route.summary() is None and c.route_status() is None
    assert not c.route.active and c.route.visit is None
    fresh(c, 1000, (2.0, 2.0, 0.0))
    assert c.start_route(route(A, B), 1000)[0]
    follower = c.route
    assert follower.active and c.route_active
    assert (follower.run, follower.cycle, follower.index) == (1, 1, 0)
    assert follower.stage == "moving" and follower.status == "active"
    assert follower.visit == c.route_visit == (1, 1, 0)
    summary = follower.summary()
    assert summary == c.route_status()
    assert summary is not None
    assert (summary["point_index"], summary["point_count"], summary["phase"]) == (0, 2, "moving")
    assert c.goal == (A.x, A.y)


def test_cancel_holds_in_place_with_a_route_reason() -> None:
    c = started(A, B)
    c.cancel_route("stopped")
    assert not c.route.active
    assert c.route.status == c.route.stage == "stopped"
    assert c.goal is None and c.goal_hold_reason == "route_stopped"
    assert c.route.controls_inspection and c.route_controls_inspection
    c.route.cancel("replaced")
    assert c.route.status == "stopped", "끝난 동선은 다시 취소해도 이유가 바뀌지 않는다"


def test_next_point_advances_then_completes_the_last_cycle() -> None:
    c = started(A, B, repeat=1)
    c.arrival.zone = "A"
    c.route.next_point()
    assert c.route.index == 1 and c.goal == (B.x, B.y)
    assert c.stats.zones_visited == 1
    assert c.plan.label == GOAL_LABEL and c.phase is Phase.PLANNING
    c.route.next_point()
    assert c.route.status == "completed" and c.stats.cycles == 1


def test_skip_point_skips_every_point_with_the_same_label() -> None:
    c = started(A, B, C, repeat=2)
    c.route.skip_point()
    assert c.route.skipped_indices == {0, 2}
    assert c.route.index == 1 and c.goal == (B.x, B.y)
    c.route.skip_point()
    assert c.recovery.returning_home, "모든 지점이 막히면 집으로 돌아간다"


def test_aim_error_uses_the_point_aim_and_steering_yaw() -> None:
    c = started(RoutePoint(x=1.0, y=1.0, aim_deg=90))
    assert c.route.point().aim_deg == 90
    assert c.route.aim_error() == pytest.approx(math.pi / 2, abs=1e-6)
    c.route.current = route(B)
    assert c.route.aim_error() == 0.0, "겨눌 방향이 없으면 오차는 0"
