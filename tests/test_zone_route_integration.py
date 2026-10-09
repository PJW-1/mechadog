"""구역 영역 기준 정책과 따라가기 도착·측위 유예를 함께 보존하는 회귀 시험."""

import math

import pytest
from test_zone_inspector import T0, _build, _frame

from host.behavior.fsm import Event
from host.behavior.zones import Zone

pytestmark = pytest.mark.usefixtures("unlock_modes")


def test_route_arrival_outside_anchor_opens_only_once_per_visit(cfg):
    parts = _build(cfg, aim_deg=90)
    inspector = parts.inspector
    visit = [1]
    inspector._route_visit = lambda: (1, 0, visit[0])
    inspector._route_inspection = lambda _zone, _now: True
    inspector.note_pose((0.7, 0, math.pi / 2), T0)
    assert inspector.awaits_inspection("A", T0)
    inspector.inspect(_frame(), T0)
    assert parts.behavior.state == "ZONE_INSPECT"
    assert inspector._aligned
    inspector._concluded = True
    parts.behavior.event(Event.ZONE_CLEAR, now_ms=T0)
    inspector.inspect(_frame(), T0)
    assert parts.behavior.state == "PATROL"
    assert not inspector.awaits_inspection("A", T0)
    visit[0] += 1
    assert inspector.awaits_inspection("A", T0)
    inspector.inspect(_frame(), T0)
    assert parts.behavior.state == "ZONE_INSPECT"


@pytest.mark.parametrize("route_ready", [False, None])
def test_outside_anchor_requires_confirmed_route_arrival(cfg, route_ready):
    parts = _build(cfg)
    inspector = parts.inspector
    inspector._route_inspection = lambda _zone, _now: route_ready
    inspector.note_pose((0.7, 0, 0), T0)
    assert not inspector.awaits_inspection("A", T0)
    inspector.inspect(_frame(), T0)
    assert parts.behavior.state == "PATROL"


@pytest.mark.parametrize("age", [600, 2000, 2001])
@pytest.mark.parametrize("route_ready", [True, False, None])
def test_camera_pose_grace_requires_route_confirmation_and_expires(cfg, age, route_ready):
    parts = _build(cfg)
    inspector = parts.inspector
    inspector._route_inspection = lambda _zone, _now: route_ready
    inspector.note_pose((0, 0, 0), T0)
    ready = route_ready is True and age <= 2000
    assert inspector.awaits_inspection("A", T0 + age) is ready
    inspector.inspect(_frame(), T0 + age)
    assert parts.behavior.state == ("ZONE_INSPECT" if ready else "PATROL")


@pytest.mark.parametrize(
    "pose,at",
    [((0, 0, 0), T0 - 1), ((math.nan, 0, 0), T0), ((0, math.inf, 0), T0), ((0, 0, math.nan), T0)],
)
def test_route_grace_never_accepts_future_or_nonfinite_pose(cfg, pose, at):
    parts = _build(cfg)
    inspector = parts.inspector
    inspector._route_inspection = lambda _zone, _now: True
    inspector.note_pose(pose, T0)
    assert inspector._fresh_pose(at) is None
    assert not inspector.awaits_inspection("A", at)
    inspector.inspect(_frame(), at)
    assert parts.behavior.state == "PATROL"


@pytest.mark.parametrize("anchors", [(), (Zone("B", 4, 4, aim_deg=90),)])
def test_route_checks_actual_region_without_anchor_or_point_label(cfg, anchors):
    parts = _build(cfg)
    inspector = parts.inspector
    inspector._anchors = anchors
    inspector._zone_at = lambda _now: "B"
    inspector._route_inspection = lambda zone, _now: zone.label == "B"
    inspector.note_pose((1, 1, 0), T0)
    assert not inspector.awaits_inspection("A", T0)
    assert inspector.awaits_inspection("B", T0)
    inspector.inspect(_frame(), T0)
    assert parts.behavior.state == "ZONE_INSPECT"
    assert inspector._zone == "B"
    assert inspector._aligned


def test_route_grace_does_not_extend_region_or_hazard_freshness(cfg):
    parts = _build(cfg, zone="C")
    inspector = parts.inspector
    inspector._route_inspection = lambda _zone, _now: True
    inspector._zone_at = lambda now: "C" if now - T0 <= parts.pose_timeout_ms else None
    inspector.note_pose((0, 0, 0), T0)
    inspector.inspect(_frame(), T0)
    assert inspector.hazards_allowed(T0)
    at = T0 + parts.pose_timeout_ms + 1
    assert inspector._fresh_pose(at) is not None
    assert not inspector.hazards_allowed(at)
    inspector.inspect(_frame(), at)
    assert parts.behavior.state == "PATROL"
    assert inspector._visit_outcome == "zone_unverified"
    inspector.inspect(_frame(), at)
    assert parts.behavior.state == "PATROL"
