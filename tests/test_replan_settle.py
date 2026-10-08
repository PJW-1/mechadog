"""AU: mask replans must not alternate settled STOP and route rotation forever."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from test_live_nav import revolution
from test_route_direct import direct, tick

from host.behavior.patrol import Phase
from host.common.protocol import CommandEncoder


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_direct_spin_does_not_replan_for_changing_static_mask(entry):
    c = direct()
    tick(c, 1000, pose=(2, 2, math.pi), entry=entry)
    assert c._spinning
    # Live clearing / map integration can repeatedly change this remembered cell.
    # The direct route uses the current scan, while an A* detour still uses the mask.
    cell = c.grid.to_cell(2.16, 2)
    for now in range(1100, 4200, 100):
        c.grid.cells[cell] = 5 if now % 200 else -3
        tick(c, now, pose=(2, 2, math.pi), entry=entry)
        assert not c._replan_stop_required
        assert c.phase is Phase.MOVING
        assert c.local_status["reason"] == "route_direct"
        assert c.commander.intent.fields["step"] == 0
    tick(c, 4300, entry=entry)
    assert c.commander.intent.fields["step"] > 0


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_refresh_replan_cannot_be_overwritten_by_direct_spin(monkeypatch, entry):
    c = direct()
    original = c._refresh_navigation
    c.note_sent([CommandEncoder().encode("MOVE", step=0, angle=-30)], 1099)

    def refresh(now):
        original(now)
        c._replan_stop_required = True
        c.commander.halt()

    monkeypatch.setattr(c, "_refresh_navigation", refresh)
    tick(c, 1100, pose=(2, 2, math.pi), entry=entry)
    assert c._replan_stop_required
    assert c.commander.intent.type_ == "STOP"
    assert c.phase is Phase.LOST


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_new_complete_settled_scan_releases_immediately_then_spins(entry):
    c = direct()
    c._require_replan_stop()
    c.note_sent([CommandEncoder().encode("MOVE", step=0, angle=-30)], 999)
    c.note_sent([CommandEncoder().encode("STOP")], 1000)
    tick(c, 1749, pose=(2, 2, math.pi), entry=entry)
    assert c.commander.intent.type_ == "STOP"
    tick(c, 1750, pose=(2, 2, math.pi), entry=entry)
    assert not c._replan_stop_required
    assert c.commander.intent.type_ == "MOVE"
    assert c.commander.intent.fields["step"] == 0
    tick(c, 1850, entry=entry)
    assert c.commander.intent.fields["step"] > 0


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_interleaved_rotations_cannot_extend_three_second_wait(entry):
    c = direct()
    c._require_replan_stop()
    encoder = CommandEncoder()
    for now in range(1100, 4001, 100):
        # Reproduce competing rotations resetting the sent STOP clock.
        c.note_sent([encoder.encode("MOVE", step=0, angle=-30)], now - 2)
        c.note_sent([encoder.encode("STOP")], now - 1)
        tick(c, now, pose=(2, 2, math.pi), entry=entry)
        assert c.commander.intent.type_ == "STOP"
        if now < 4000:
            assert c._replan_stop_required
    assert not c._replan_stop_required
    assert c.phase is Phase.PLANNING
    tick(c, 4100, pose=(2, 2, math.pi), entry=entry)
    assert c.commander.intent.type_ == "MOVE"
    assert c.commander.intent.fields["step"] == 0


@pytest.mark.parametrize("gate", ["sent_stop", "pose", "scan", "rejected", "incomplete"])
def test_timeout_keeps_localization_and_scan_safety_gates(gate):
    c = direct()
    c._require_replan_stop()
    c.note_sent([CommandEncoder().encode("STOP")], 3999)
    c.observe_map_pose((2, 2, math.pi), 4000)
    c.observe_obstacle_scan(revolution(4000), 4000)
    c.safety.last_seen_ms = 4000
    if gate == "sent_stop":
        c._last_sent_moving = True
        c._stopped_since_ms = None
    elif gate == "pose":
        c.localization.last_pose_ms = 1000
    elif gate == "scan":
        c._local_scan.received_ms = 1000
    elif gate == "rejected":
        c._local_scan.clear_allowed = False
    else:
        c._local_scan.points = ((0, 1),)
    c.step(4000)
    assert c._replan_stop_required
    assert c.commander.intent.type_ == "STOP"
    assert c.phase is Phase.LOST


@pytest.mark.parametrize("entry", ["step", "steer"])
def test_timeout_does_not_bypass_onboard_obstacle(entry):
    c = direct()
    c._require_replan_stop()
    c.note_sent([CommandEncoder().encode("MOVE", step=0, angle=-30)], 3998)
    c.note_sent([CommandEncoder().encode("STOP")], 3999)
    tick(c, 4000, entry=entry, onboard=True)
    tick(c, 4100, entry=entry, onboard=True)
    assert c.commander.intent.type_ == "STOP"


def test_mask_update_cannot_change_lost_to_planning_during_spin():
    c = direct()
    tick(c, 1000, pose=(2, 2, math.pi))
    c._route_direct_detour_start = (2, 2)
    c.phase = Phase.LOST
    c.grid.cells[c.grid.to_cell(2.16, 2)] = 5
    c._rebuild_masks()
    assert c.phase is Phase.LOST
    assert not c.plan.reachable


def test_detour_still_replans_when_static_mask_blocks_start():
    c = direct()
    tick(c, 1000, pose=(2, 2, math.pi))
    c._route_direct_detour_start = (2, 2)
    c.grid.cells[c.grid.to_cell(2.16, 2)] = 5
    c._rebuild_masks()
    assert c._replan_stop_required
    assert c.commander.intent.type_ == "STOP"


def test_scan_started_before_settling_cannot_release_wait_at_receipt():
    c = direct()
    c._require_replan_stop()
    encoder = CommandEncoder()
    c.note_sent([encoder.encode("MOVE", step=0, angle=-30)], 999)
    c.note_sent([encoder.encode("STOP")], 1000)
    c.observe_map_pose((2, 2, math.pi), 1830)
    c.safety.last_seen_ms = 1830
    c.observe_obstacle_scan(replace(revolution(2), started_ms=1749, received_ms=1830), 1830)
    c.step(1830)
    assert c._replan_stop_required and c.commander.intent.type_ == "STOP"
    c.observe_obstacle_scan(replace(revolution(3), started_ms=1750, received_ms=1831), 1831)
    c.step(1831)
    assert not c._replan_stop_required
    assert c.commander.intent.type_ == "MOVE"


def test_new_goal_discards_previous_settle_deadline():
    c = direct()
    c._require_replan_stop()
    assert c._replan_wait_started_ms == 1000
    assert c.goto(3, 3)[0]
    assert c._replan_wait_started_ms is None
