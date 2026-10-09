"""국소 회피(`LocalAvoidance`) 단위 시험 — 판단 기록·회피 시작·끝을 순찰기 하나로 본다."""

from __future__ import annotations

import pytest
from test_live_nav import ready, revolution

from host.behavior.patrol import Phase
from host.behavior.planner import Plan


def test_decide_records_status_and_controller_merges_scan_gate() -> None:
    c = ready()
    c.avoidance.decide("slow", "path_obstacle", 0.3456, 0.5)
    status = c.avoidance.status
    assert status["action"] == "slow" and status["reason"] == "path_obstacle"
    assert status["distance_m"] == 0.346 and status["gap_deg"] == 28.6
    assert status["scan_age_ms"] == c._now_ms - c._local_scan.received_ms
    assert status["message"] == "앞 0.35m — 감속"
    assert c.local_status == {**status, **c.scan_gate.status}
    c.avoidance.decide("odd", "custom_reason")
    assert c.avoidance.status["message"] == "custom_reason", "모르는 판단은 이유를 그대로 쓴다"
    assert c.avoidance.status["distance_m"] is None


def test_start_refuses_without_a_fresh_scan() -> None:
    c = ready()
    c._local_scan.received_ms = None
    assert c.avoidance.start("path_obstacle") is False
    assert c.avoidance.active is None
    assert c.commander.intent.type_ == "STOP"


def test_start_without_an_observed_gap_stops_and_says_so() -> None:
    c = ready()
    c.observe_obstacle_scan(revolution(2, distance=0.05), 1000)
    assert c.avoidance.start("path_obstacle") is False
    assert c.avoidance.active is None
    assert c.avoidance.status["reason"] == "no_observed_gap"
    assert c.commander.intent.type_ == "STOP"


def test_start_toward_open_target_keeps_heading_and_records_reason() -> None:
    c = ready()
    corridor = c.avoidance.corridor_to((4.0, 2.0))
    assert corridor is not None and corridor[0] == pytest.approx(0.0, abs=0.2)
    assert c.avoidance.start("lidar_corridor") is True
    assert c.avoidance.active is not None
    x, y, _heading, started, reason = c.avoidance.active
    assert (x, y, started, reason) == (2.0, 2.0, c._now_ms, "lidar_corridor")
    assert c.avoidance.status["action"] == "avoid"


def test_step_ends_after_the_time_limit_and_returns_to_planning() -> None:
    c = ready(front=0.2)
    c.plan = Plan("A", ((2.0, 2.0), (4.0, 2.0)), 2.0)
    assert c.avoidance.start("path_obstacle")
    replans = c.stats.replans
    c._now_ms += c.nav_params.avoidance_timeout_ms
    c.avoidance.step()
    assert c.avoidance.active is None
    assert c.phase is Phase.PLANNING and c.plan.waypoints == () and c.plan.label == "A"
    assert c.stats.replans == replans + 1
    assert c.commander.intent.type_ == "STOP"
