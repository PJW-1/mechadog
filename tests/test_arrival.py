"""구역 도착(`ZoneArrival`) 단위 시험 — 도착·조준·점검 준비·사이클 마감을 순찰기 하나로 본다."""

from __future__ import annotations

from test_live_nav import ready

from host.behavior.patrol_phase import GOAL_LABEL, Phase
from host.behavior.planner import Plan


def test_arrive_without_aim_finishes_and_closes_the_only_zone_cycle() -> None:
    c = ready()
    c.arrival.arrive("A")
    assert c.phase is Phase.INSPECT
    assert c.arrival.zone == "A"
    assert c.stats.zones_visited == 1
    assert c.cycle == 1 and c.stats.cycles == 1
    assert c.visited == frozenset() and c.skipped == frozenset()
    assert c.plan.label is None


def test_arrive_with_aim_deg_starts_aiming_and_keeps_the_visit_open() -> None:
    c = ready()
    c.zones.set_aim_deg("A", 90)
    c.arrival.arrive("A")
    assert c.phase is Phase.AIMING
    assert c.arrival.zone is None
    assert c.stats.zones_visited == 0


def test_arrive_at_a_picked_goal_holds_there() -> None:
    c = ready()
    c._goal = (2.0, 2.0)
    c.arrival.arrive(GOAL_LABEL)
    assert c._goal is None and c.holding_goal and c.goal_hold_reason == "reached"
    assert c.phase is Phase.PLANNING and c.plan.label is None


def test_aim_outside_the_arrival_radius_goes_back_to_planning() -> None:
    c = ready()
    c.plan = Plan("A")
    c._spinning = True
    c.arrival.aim()
    assert c.phase is Phase.PLANNING
    assert c.plan.label == "A" and not c._spinning


def test_steer_aim_spins_until_the_zone_bearing_is_within_tolerance() -> None:
    c = ready()
    c.zones.set_aim_deg("A", 90)
    assert c.arrival.aim_error("A") > c.drive.heading_tolerance_rad
    assert not c.arrival.steer_aim("A")
    assert c._spinning
    assert c.commander.intent.fields["step"] == 0.0
    c.zones.set_aim_deg("A", 0)
    assert c.arrival.aim_error("A") == 0.0
    assert c.arrival.steer_aim("A")
    assert not c._spinning


def test_ready_needs_the_inspect_phase_for_the_same_zone() -> None:
    c = ready()
    assert not c.inspection_ready("A", 1000)
    c.phase = Phase.INSPECT
    c.arrival.zone = "A"
    assert c.arrival.ready("A", 1000) and c.inspection_ready("A", 1000)
    assert not c.arrival.ready("B", 1000)
