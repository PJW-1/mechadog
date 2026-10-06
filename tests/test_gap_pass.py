"""AO: raw-endpoint swept body, goalward gaps and unchanged safety gates."""

from __future__ import annotations

import math

import numpy as np
import pytest
from test_live_nav import ready, revolution

from host.behavior.blockage import BlockageMemory, Recovery
from host.behavior.live_nav import LocalScan, NavParams
from host.behavior.patrol import GOAL_LABEL, Phase
from host.behavior.planner import Plan, mark_obstacle
from host.common.lidar_link import Scan


def corridor_scan(width: float, seq: int = 50) -> Scan:
    """Raycast a 6m long passage; widths are physical, independent of the grid."""
    points = []
    for angle in np.arange(-180.0, 180.0, 1.0):
        a = math.radians(float(angle))
        distance = min(3 / max(abs(math.cos(a)), 1e-10), width / 2 / max(abs(math.sin(a)), 1e-10))
        points.append((a, distance))
    return Scan("lidar-a", "boot-a", seq, 1000, tuple(points))


@pytest.mark.parametrize(
    "width,passes",
    [
        (0.29, False),
        (0.30, False),
        (0.33, False),
        (0.34, True),
        (0.35, True),
        (0.40, True),
        (0.45, True),
    ],
)
def test_physical_passage_width(width, passes):
    local = LocalScan(NavParams())
    local.observe(corridor_scan(width), 1000, (0.12, 8))
    gap = local.corridor(0.15, 0, 0.2)
    assert (gap is not None) == passes
    if gap:
        assert gap[0] == pytest.approx(0)
        assert gap[2] >= 0.34 - 1e-9


def blocked_retry(monkeypatch, scan):
    c = ready()
    c._goal = (4, 2)
    c.plan = Plan(GOAL_LABEL)
    c._recovery = Recovery(GOAL_LABEL, 0, 0, None, scanning=True, settling_ms=0)
    c.observe_obstacle_scan(scan, 1000)
    monkeypatch.setattr(
        "host.behavior.patrol.plan_to",
        lambda *_a, **_kw: Plan(None, fail_reason="start_clearance_blocked"),
    )
    c._recover()
    return c


@pytest.mark.parametrize("reason", ["start_clearance_blocked", "no_path", "goal_unreachable"])
def test_goto_accepts_goalward_observed_gap_despite_grid_failure(monkeypatch, reason):
    c = ready()
    c.observe_obstacle_scan(corridor_scan(0.4), 1000)
    monkeypatch.setattr(
        "host.behavior.patrol.plan_to", lambda *_a, **_kw: Plan(None, fail_reason=reason)
    )
    assert c.goto(4, 2)[0]
    assert not c.goto(50, 50)[0]


def test_grid_blocked_but_raw_corridor_moves_and_replans(monkeypatch):
    c = blocked_retry(monkeypatch, corridor_scan(0.4))
    assert c.goal == (4, 2) and not c.holding_goal and not c.skipped
    assert c._avoidance is not None and c._recovery is None
    c.step(1000)
    assert c.commander.intent.type_ == "MOVE"
    assert 0 < c.commander.intent.fields["step"] <= c.drive.step_mm * 0.5
    assert c.commander.intent.fields["angle"] == 0
    assert [e["event"] for e in c.take_navigation_events()] == ["obstacle_detour"]
    c.note_sent(c.commander.tick(1000), 1000)
    c.observe_map_pose((2.21, 2, 0), 1100)
    c.observe_obstacle_scan(corridor_scan(0.4, 51), 1100)
    c.step(1100)
    assert c._avoidance is None and c.phase is Phase.PLANNING
    assert c.commander.intent.type_ == "STOP" and c.goal == (4, 2)


def test_fully_enclosed_scan_skips_but_missing_coverage_waits(monkeypatch):
    c = blocked_retry(monkeypatch, revolution(50, distance=0.3))
    assert c.phase is Phase.IDLE and c.goal_hold_reason == "blocked"
    assert [e["event"] for e in c.take_navigation_events()] == ["zone_skipped"]
    c = blocked_retry(monkeypatch, Scan("lidar-a", "boot-a", 50, 1000, ((0, 3),)))
    assert c._recovery is not None and c.goal == (4, 2)
    assert not c.take_navigation_events()
    assert c.local_status["reason"] == "corridor_scan_incomplete"


def test_goal_opposite_space_is_not_a_detour():
    local = LocalScan(NavParams())
    scan = revolution(50)
    # A wall at the required 17cm clearance, with only the rear half-plane open.
    points = tuple(
        (a, min(d, 0.17 / math.cos(a)) if math.cos(a) > 0 else d) for a, d in scan.points
    )
    local.observe(Scan("a", "b", 1, 0, points), 1000, (0.12, 8))
    assert local.corridor(0.15, 0, 0.2) is None


def test_passage_between_angle_bins_turns_even_inside_normal_heading_tolerance(monkeypatch):
    scan = corridor_scan(0.35)
    tilt = math.radians(7.5)
    scan = Scan(
        scan.device_id,
        scan.boot_id,
        scan.seq,
        scan.ts_ms,
        tuple((a + tilt, d) for a, d in scan.points),
    )
    c = blocked_retry(monkeypatch, scan)
    assert c._avoidance is not None
    c.step(1000)
    assert c.commander.intent.type_ == "MOVE"
    assert c.commander.intent.fields["step"] == 0
    assert 0 < c.commander.intent.fields["angle"] < 8


@pytest.mark.parametrize("angle,moves", [(60, True), (20, False)])
def test_close_side_outside_stop_fan_moves_inside_fan_stops(monkeypatch, angle, moves):
    c = blocked_retry(monkeypatch, corridor_scan(0.45))
    assert c._avoidance is not None
    scan = corridor_scan(0.45, 51)
    points = scan.points + ((math.radians(angle), 0.24),)
    c.observe_obstacle_scan(Scan(scan.device_id, scan.boot_id, scan.seq, 1000, points), 1000)
    c.step(1000)
    assert (c.commander.intent.type_ == "MOVE" and c.commander.intent.fields["step"] > 0) == moves
    assert c.nav_params.local_stop_m == 0.25 and c.nav_params.local_fan_deg == 30
    assert c.plan_params.body_radius_m == 0.15


@pytest.mark.parametrize("gate", ["pose", "scan", "onboard", "estop", "unverified"])
def test_safety_overrides_raw_corridor(monkeypatch, gate):
    c = blocked_retry(monkeypatch, corridor_scan(0.4))
    if gate == "pose":
        c._last_pose_ms = None
    elif gate == "scan":
        c._local_scan.received_ms = None
    elif gate == "onboard":
        c.safety.obstacle = True
    elif gate == "estop":
        c.emergency_stop("test")
    else:
        c._own_localization = True
        c._pose_verified = False
    c.step(1000)
    assert c.commander.intent.type_ != "MOVE"


def test_endpoints_keep_exact_radius_without_grid_roundup():
    c = ready()
    mask = np.zeros_like(c.grid.cells, dtype=bool)
    hit = (2.001, 2.001)
    mark_obstacle(mask, c.grid, hit, 0.151)
    assert mask[c.grid.to_cell(2.15, 2.0)]
    assert not mask[c.grid.to_cell(2.20, 2.0)]


def test_motion_since_scan_is_included_in_swept_clearance():
    local = LocalScan(NavParams())
    local.observe(corridor_scan(0.4), 1000, (0.12, 8))
    assert local.corridor_clear(0.15, 0, 0.2)
    assert not local.corridor_clear(0.15, 0, 0.2, origin=(0, 0.06))
    assert not local.corridor_clear(0.15, 0, 0.2, origin=(4, 0))


def test_cleared_memory_cell_is_not_kept_by_a_neighbouring_new_hit():
    c = ready()
    memory = BlockageMemory(NavParams())
    old = (2.62, 2.02)
    new = (2.62, 2.08)
    item = memory.remember(c.grid, [old], 1000)
    assert item is not None
    scan = Scan(
        "a",
        "b",
        1,
        0,
        (
            (math.atan2(0.02, 0.62), 2.0),
            (math.atan2(0.08, 0.62), math.hypot(0.62, 0.08)),
        ),
    )
    free = memory.observe(c.grid, c.pose, scan, 3100)
    assert c.grid.to_cell(*old) in free
    assert c.grid.to_cell(*old) not in item.points
    assert item.points[c.grid.to_cell(*new)] == pytest.approx(new)
