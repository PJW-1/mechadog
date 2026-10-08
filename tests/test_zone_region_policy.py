"""영역 정책과 동선의 도착·정렬·검사 연동을 로봇 없이 검증한다."""

import math
from copy import deepcopy

import numpy as np
import pytest
from test_ppe_detector import FakeModel
from test_route_runtime import route
from test_runtime import config
from test_runtime_lidar import _camera_aim_runtime, _camera_aim_tick
from test_zone_inspector import SlotVlm

from host.behavior.fsm import Event
from host.behavior.routes import RoutePoint
from host.behavior.zone_inspector import HAZARD_ITEM, ZONE_KEYS
from host.behavior.zone_map import ZoneMap
from host.behavior.zone_policy import DEFAULT_PPE, ZonePpePolicy
from host.behavior.zones import Zone
from host.common.blackbox import EventBlackbox
from host.vision.detector import Detection
from host.vision.ppe_detector import OK, VIOLATION, PpeDetector
from host.vision.tracker import Track

__all__ = ["config"]


def region_config(cfg):
    cfg = deepcopy(cfg)
    cfg["zones"]["hazard_ids"] = ["A"]
    cfg["zones"]["policies"] = {
        label: {"helmet": label == "C", "vest": label == "C"} for label in "ABCD"
    }
    return cfg


@pytest.mark.parametrize("label", list("ABCD"))
def test_region_policy_keeps_inference_and_only_c_warns(cfg, label):
    cfg = region_config(cfg)
    zone_map = ZoneMap(np.ones((10, 10), dtype=np.uint8), 1.0, -2.0, -2.0, {1: label})
    policy = ZonePpePolicy(cfg, zone_map)
    policy.note_pose((5.0, 2.9, 0.0), 100)
    assert policy.current_zone(100) == label
    required = DEFAULT_PPE if label == "C" else ()
    assert policy.required(100) == required
    model = FakeModel(
        [Detection("no_helmet", 0.9, (1, 20, 20, 40)), Detection("no_vest", 0.9, (1, 60, 20, 100))]
    )
    detector = PpeDetector(cfg, model)
    detector.set_requirements(policy.required(100))
    image = np.zeros((480, 640, 3), np.uint8)
    tracks = (Track(1, (100, 20, 300, 470), 0.9, 0),)
    for at in (0, 1000, 1500):
        verdict = detector.observe(image, tracks, at)
    assert model.calls == 3, "비필수 구역에서도 착용 여부 추론은 계속한다"
    assert verdict.state == (VIOLATION if label == "C" else OK)
    assert verdict.confirmed == (label == "C")
    assert [region.label for region in verdict.regions] == ["no_helmet", "no_vest"]


@pytest.mark.parametrize("pose", [(20, 20, 0), (math.nan, 0, 0), (0, math.inf, 0)])
def test_unknown_region_keeps_default_requirements(cfg, pose):
    policy = ZonePpePolicy(region_config(cfg), ZoneMap(np.ones((2, 2)), 1, 0, 0, {1: "A"}))
    policy.note_pose(pose, 100)
    assert policy.current_zone(100) is None
    assert policy.required(100) == DEFAULT_PPE


def test_optional_gear_still_displays_unknown_head_and_detected_vest(cfg):
    model = FakeModel([Detection("no_vest", 0.9, (1, 60, 20, 100))])
    detector = PpeDetector(cfg, model)
    detector.set_requirements(())
    verdict = detector.observe(
        np.zeros((480, 640, 3), np.uint8), (Track(1, (100, 0, 300, 470), 0.9, 0),), 100
    )
    assert model.calls == 1
    assert verdict.state == OK and not verdict.confirmed
    assert [region.label for region in verdict.regions] == ["UNDETERMINED", "no_vest"]
    assert verdict.regions[0].reason == "머리 클리핑"


def test_unlabelled_missing_unknown_and_stale_regions_keep_defaults(cfg):
    cfg = region_config(cfg)
    policy = ZonePpePolicy(cfg, ZoneMap(np.array([[0, 1, 2]]), 1, 0, 0, {1: "A", 2: "Z"}))
    for x in (0, 2):
        policy.note_pose((x, 0, 0), 100)
        assert policy.current_zone(100) is None
        assert policy.required(100) == DEFAULT_PPE
    policy.note_pose((1, 0, 0), 100)
    assert policy.required(100) == ()
    assert policy.required(99) == DEFAULT_PPE
    assert policy.required(101 + cfg["localization"]["pose_timeout_ms"]) == DEFAULT_PPE
    missing = ZonePpePolicy(cfg)
    missing.note_pose((1, 0, 0), 100)
    assert missing.required(100) == DEFAULT_PPE


def regional_runtime(config, clock, label):
    runtime, navigator, vision = _camera_aim_runtime(region_config(config), clock)
    zone_map = ZoneMap(np.ones((60, 60), dtype=np.uint8), 0.1, -1, -1, {1: label})
    navigator.zone_map = runtime._zone_ppe.zone_map = zone_map
    runtime._zone_inspector._zone_at = runtime._zone_ppe.current_zone
    # 정지점 (1, 1)은 이 앵커의 도착 반경 밖이다.
    runtime._zone_inspector._anchors = (Zone(label, 4, 4, aim_deg=90),)
    vlm = SlotVlm()
    runtime._vlm = runtime._zone_inspector._vlm = runtime._fall._vlm = vlm
    return runtime, navigator, vision, vlm


@pytest.mark.usefixtures("unlock_modes")
@pytest.mark.parametrize("label", list("ABC"))
@pytest.mark.parametrize("point_label", [None, "D"])
def test_route_dwell_inspects_actual_region_after_route_heading(config, clock, label, point_label):
    runtime, navigator, vision, vlm = regional_runtime(config, clock, label)
    assert navigator.start_route(
        route(RoutePoint(x=1, y=1, aim_deg=-90, dwell_s=1, label=point_label)), clock.ms
    )[0]
    for _ in range(2):
        _camera_aim_tick(runtime, navigator, vision, clock)
    assert runtime.behavior.state == "PATROL"
    assert vlm.submitted == []
    assert not runtime._zone_inspector.hazards_allowed(clock.ms)
    for _ in range(3):
        _camera_aim_tick(runtime, navigator, vision, clock, yaw=-math.pi / 2)
    assert runtime.behavior.state == "ZONE_INSPECT"
    assert runtime._zone_inspector._zone == label
    assert runtime._zone_inspector._aligned
    assert navigator.arrival.zone == label
    assert vlm.keys == [(*ZONE_KEYS, HAZARD_ITEM) if label == "A" else ZONE_KEYS]
    assert runtime._zone_inspector.hazards_allowed(clock.ms) == (label == "A")
    assert runtime.nav_status()["zone"] == label
    assert runtime.nav_status()["ppe_required"] == (list(DEFAULT_PPE) if label == "C" else [])


@pytest.mark.usefixtures("unlock_modes")
def test_route_moving_through_anchor_does_not_start_region_inspection(config, clock):
    runtime, navigator, vision, vlm = regional_runtime(config, clock, "A")
    runtime._zone_inspector._anchors = (Zone("A", 1, 1),)
    assert navigator.start_route(route(RoutePoint(x=2, y=2, dwell_s=1)), clock.ms)[0]
    for _ in range(3):
        _camera_aim_tick(runtime, navigator, vision, clock)
    assert navigator.route_status()["phase"] == "moving"
    assert runtime.behavior.state == "PATROL"
    assert vlm.submitted == []
    assert not runtime._zone_inspector.hazards_allowed(clock.ms)


@pytest.mark.usefixtures("unlock_modes")
def test_hazard_stops_on_region_exit_or_pose_expiry(config, clock):
    runtime, navigator, vision, _ = regional_runtime(config, clock, "A")
    assert navigator.start_route(route(RoutePoint(x=1, y=1, aim_deg=0, dwell_s=1)), clock.ms)[0]
    for _ in range(3):
        _camera_aim_tick(runtime, navigator, vision, clock)
    assert runtime._zone_inspector.hazards_allowed(clock.ms)
    assert not runtime._zone_inspector.hazards_allowed(clock.ms + runtime._zone_ppe.timeout + 1)
    runtime.note_pose((20, 20, 0), clock.ms)
    assert not runtime._zone_inspector.hazards_allowed(clock.ms)
    assert runtime._zone_ppe.required(clock.ms) == DEFAULT_PPE


@pytest.mark.usefixtures("unlock_modes")
def test_region_dwell_waits_for_camera_and_reopens_on_same_region_repeat(config, clock):
    runtime, navigator, vision, _ = regional_runtime(config, clock, "B")
    assert navigator.start_route(route(RoutePoint(x=1, y=1), repeat=2), clock.ms)[0]
    _camera_aim_tick(runtime, navigator, vision, clock)
    for _ in range(3):
        _camera_aim_tick(runtime, navigator, vision, clock, new_frame=False)
    assert navigator.route_active
    assert navigator.route_status()["cycle"] == 1
    assert runtime.behavior.state == "PATROL"
    _camera_aim_tick(runtime, navigator, vision, clock)
    assert runtime.behavior.state == "ZONE_INSPECT"
    first_visit = navigator.route_visit
    runtime._zone_inspector._concluded = True
    runtime.apply_external(Event.ZONE_CLEAR)
    for _ in range(5):
        _camera_aim_tick(runtime, navigator, vision, clock)
        if runtime.behavior.state == "ZONE_INSPECT":
            break
    assert navigator.route_visit != first_visit
    assert navigator.route_status()["cycle"] == 2
    assert runtime._zone_inspector._zone == "B"


@pytest.mark.usefixtures("unlock_modes")
def test_person_and_fall_records_share_region_and_ppe_policy(config, clock, tmp_path):
    runtime, navigator, vision, _ = regional_runtime(config, clock, "B")
    record_config = deepcopy(config)
    record_config["logging"]["blackbox_dir"] = str(tmp_path / "blackbox")
    blackbox = runtime.incidents.blackbox = EventBlackbox(record_config)
    _camera_aim_tick(runtime, navigator, vision, clock)
    for event in ("person_found", "fall_review_required"):
        runtime.incidents.record_scene(event, vision.result)
    assert len(blackbox.feed()) == 2
    assert all(entry.judgement["zone"] == "B" for entry in blackbox.feed())
    assert all(entry.judgement["ppe_required"] == [] for entry in blackbox.feed())
