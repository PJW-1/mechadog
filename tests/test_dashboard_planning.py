"""관제 계획은 실제 설정 형식에 저장하되 로봇 명령 없이 검증한다."""

import json
from copy import deepcopy
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from host.behavior.zone_map import ZoneMap
from host.behavior.zone_policy import DEFAULT_PPE, ZonePpePolicy
from host.behavior.zones import ZoneStore
from host.common.config import ConfigError, load_config, validate_base_config
from host.dashboard.planning import PlanError, PlanInput, PlanningService
from host.dashboard.server import DEFAULT_STATIC_DIR, create_app, create_fleet_app
from host.dashboard.state import DashboardState
from host.slam.occupancy import OccupancyGrid


@pytest.fixture
def planning(tmp_path):
    root = Path(__file__).resolve().parents[1]
    devices = tmp_path / "devices"
    devices.mkdir()
    # 기체 설정이 저장소 지도(`maps/…`)를 가리키므로 시험 지도 폴더로 바꿔 쓴다 (10-06 지도 커밋 이후).
    device = yaml.safe_load((root / "config/devices/mechdog-02.yaml").read_text(encoding="utf-8"))
    device.setdefault("lidar", {})["maps_dir"] = str(tmp_path / "maps")
    device.pop("zones", None)  # 현장 구역 순서·정책은 시험의 기준 구성을 덮지 않게 뺀다
    (devices / "mechdog-02.yaml").write_text(
        yaml.safe_dump(device, allow_unicode=True), encoding="utf-8"
    )
    base = yaml.safe_load((root / "config/config.yaml").read_text(encoding="utf-8"))
    base["lidar"]["maps_dir"] = str(tmp_path / "maps")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    config = load_config("mechdog-02", config_path=config_path, devices_dir=devices)
    return PlanningService(config, "mechdog-02", config_path=config_path, devices_dir=devices)


def proposal(service):
    snapshot = service.snapshot()
    return PlanInput(
        revision=snapshot["revision"],
        zones=[
            {
                "id": "B",
                "name": "조립실",
                "x": -1,
                "y": 0,
                "yaw_deg": 90,
                "helmet": False,
                "vest": True,
            },
            {"id": "A", "name": "출입구", "x": 1, "y": 0, "hazard": True},
        ],
    )


def grid_for(service):
    grid = OccupancyGrid.blank(resolution=0.1, span_cells=80)
    grid.cells.fill(-5)
    grid.save(service.maps)
    return grid


def test_save_roundtrips_runtime_order_policy_and_keeps_other_overrides(planning):
    planning.local_path.write_text("network:\n  cmd_port: 5099\n", encoding="utf-8")
    saved = planning.save(proposal(planning))
    assert saved["pending_restart"]
    assert saved["active"]["zones"][0]["id"] == "A"
    loaded = planning._saved_config()
    assert loaded["network"]["cmd_port"] == 5099
    assert loaded["zones"]["ids"] == ["B", "A"]
    assert loaded["zones"]["hazard_ids"] == ["A"]
    assert loaded["zones"]["policies"]["B"]["helmet"] is False
    anchors = ZoneStore.load(planning.maps, loaded["zones"]["ids"])
    assert anchors.labels == ("B", "A")
    assert anchors.get("B").yaw == pytest.approx(np.pi / 2)
    restarted = PlanningService(
        loaded, planning.device, config_path=planning.config_path, devices_dir=planning.devices_dir
    )
    assert restarted.snapshot()["pending_restart"] is False


def test_stale_revision_rejects_without_overwriting(planning):
    plan = proposal(planning)
    planning.save(plan)
    previous = planning.local_path.read_bytes()
    with pytest.raises(PlanError, match="다른 화면"):
        planning.save(plan)
    assert planning.local_path.read_bytes() == previous


def test_second_file_failure_restores_anchors(planning, monkeypatch):
    import host.dashboard.planning as module

    planning.save(proposal(planning))
    previous = planning.zones_path.read_bytes()
    plan = proposal(planning)
    plan.zones[0].x = 2
    original = module._atomic_write

    def write(path, content):
        if path == planning.local_path:
            raise OSError("disk full")
        original(path, content)

    monkeypatch.setattr(module, "_atomic_write", write)
    with pytest.raises(OSError, match="disk full"):
        planning.save(plan)
    assert planning.zones_path.read_bytes() == previous


def test_map_absence_does_not_invent_positions_or_routes(planning):
    assert not planning.snapshot()["map"]["available"]
    assert all(z["x"] is None for z in planning.snapshot()["saved"]["zones"])
    with pytest.raises(PlanError, match="지도"):
        planning.preview(proposal(planning))


def test_real_planner_previews_loop_and_reports_wall(planning):
    grid = grid_for(planning)
    route = planning.preview(proposal(planning))
    assert route["all_reachable"] and route["total_m"] >= 4
    assert [(s["from"], s["to"]) for s in route["segments"]] == [("B", "A"), ("A", "B")]
    grid.cells[:, 40] = 5
    grid.save(planning.maps)
    blocked = planning.preview(proposal(planning))
    assert not blocked["all_reachable"]
    assert all(not s["points"] for s in blocked["segments"])


def test_map_png_preserves_world_y_orientation(planning):
    grid = grid_for(planning)
    grid.cells[0, 0] = 5
    grid.save(planning.maps)
    image = cv2.imdecode(np.frombuffer(planning.map_png(), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert image[-1, 0] == 62 and image[0, 0] == 250


def test_replacing_map_invalidates_old_route_and_save(planning):
    grid = grid_for(planning)
    plan = proposal(planning)
    grid.cells[0, 0] = 5
    grid.save(planning.maps)
    with pytest.raises(PlanError, match="지도"):
        planning.preview(plan)
    with pytest.raises(PlanError):
        planning.save(plan)


@pytest.mark.parametrize(
    "zones",
    [
        [{"id": "A", "x": 1}],
        [{"id": "A", "x": True, "y": 2}],
        [{"id": "A", "x": float("nan"), "y": 2}],
        [{"id": "A", "yaw_deg": 90}],
        [{"id": "A"}, {"id": "A"}],
        [{"id": "../a"}],
        [{"id": "A", "helmet": "false"}],
    ],
)
def test_invalid_plan_rejected(zones):
    with pytest.raises(ValidationError):
        PlanInput(zones=zones)


def test_api_saves_on_local_origin_and_exposes_no_robot_commands(planning):
    app = create_app(
        DashboardState("mechdog-02", stale_after_ms=3000), planning=planning, static_dir=None
    )
    with TestClient(app) as client:
        assert client.get("/api/planning").status_code == 200
        plan = proposal(planning).model_dump()
        assert (
            client.post(
                "/api/planning", json=plan, headers={"origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert client.post("/api/planning", json=plan).status_code == 200
        assert client.post("/api/planning", json=plan).status_code == 409
        assert (
            client.post("/api/planning", json={"zones": [{"id": "A"}, {"id": "A"}]}).status_code
            == 400
        )
        assert client.post("/api/command/patrol", json={"action": "start"}).status_code == 404
        assert client.get("/health").json()["read_only"] is True


def test_fleet_root_and_direct_index_are_white():
    with TestClient(create_fleet_app({})) as client:
        root = client.get("/", follow_redirects=False)
        assert root.headers["location"] == "/glass-preview/"
        assert client.get("/glass-preview/theme.css").status_code == 200
        assert 'id="glass-theme"' in client.get("/index.html").text
        assert 'id="glass-theme"' in client.get("/static/index.html").text
        assert (
            client.get("/?view=zones", follow_redirects=False).headers["location"]
            == "/glass-preview/?view=zones"
        )
        assert client.get("/planning-panel.js").status_code == 200
    assert 'id="glass-theme"' in (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_ppe_policy_uses_fresh_region_labels(cfg):
    config = deepcopy(cfg)
    config["zones"]["policies"] = {
        "A": {"helmet": False, "vest": True},
        "B": {"helmet": True, "vest": False},
    }
    # 앵커 반경과 무관하게 셀 영역이 정책을 고른다. 0은 구역 불명이다.
    zone_map = ZoneMap(np.array([[1, 2, 0]]), 1.0, 0.0, 0.0, {1: "A", 2: "B"})
    policy = ZonePpePolicy(config, zone_map)
    assert policy.required(100) == DEFAULT_PPE
    policy.note_pose((0, 0, 0), 100)
    assert policy.required(100) == ("vest",)
    policy.note_pose((0.9, 0.9, 0), 200)
    assert policy.current_zone(200) == "A"
    assert policy.required(200) == ("vest",)
    policy.note_pose((1.0, 0.9, 0), 250)
    assert policy.current_zone(250) == "B"
    assert policy.required(250) == ("helmet",)
    policy.note_pose((0, 0, 0), 300)
    assert policy.required(301 + config["localization"]["pose_timeout_ms"]) == DEFAULT_PPE
    assert policy.required(299) == DEFAULT_PPE
    policy.note_pose((3, 0, 0), 400)
    assert policy.required(400) == DEFAULT_PPE


def test_policy_schema_rejects_unsafe_values(cfg):
    config = deepcopy(cfg)
    config["zones"]["policies"] = {"A": {"helmet": "no"}}
    with pytest.raises(ConfigError):
        validate_base_config(config)


def test_removed_policy_does_not_resurrect_from_base(planning):
    raw = yaml.safe_load(planning.config_path.read_text())
    raw["zones"]["policies"] = {"C": {"helmet": False}}
    planning.config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    planning.save(proposal(planning))
    assert set(planning._saved_config()["zones"]["policies"]) == {"A", "B"}
    assert set(json.loads(planning.zones_path.read_text())) == {"A", "B"}


def test_shared_map_anchors_outside_this_plan_are_preserved(planning):
    planning.maps.mkdir()
    planning.zones_path.write_text('{"OTHER": {"x": 8, "y": 9}}', encoding="utf-8")
    planning.save(proposal(planning))
    assert json.loads(planning.zones_path.read_text())["OTHER"] == {"x": 8, "y": 9}


def camera_zone_plan(planning):
    planning.maps.mkdir(exist_ok=True)
    content = {
        "meta": {"resolution": 0.05},
        "pose_frame": {"rotate_deg": 23},
        "zones": [
            {"id": "A", "index": 1, "patrol": {"x": 1, "y": 0}, "plan": {"x": 9, "y": 8}},
            {"id": "B", "index": 2, "patrol": {"x": -1, "y": 0}},
            {"id": "OTHER", "index": 3, "aim_deg": 15},
        ],
    }
    planning.zone_plan_path.write_text(json.dumps(content), encoding="utf-8")
    return content


def test_camera_aim_api_roundtrips_both_zone_files_and_runtime(planning):
    original = camera_zone_plan(planning)
    app = create_app(
        DashboardState("mechdog-02", stale_after_ms=3000), planning=planning, static_dir=None
    )
    plan = proposal(planning).model_dump()
    plan["zones"][0]["aim_deg"] = 0
    plan["zones"][1]["aim_deg"] = -45.5
    with TestClient(app) as client:
        response = client.post("/api/planning", json=plan)
        assert response.status_code == 200
        assert [z["aim_deg"] for z in response.json()["saved"]["zones"]] == [0, -45.5]
        assert response.json()["saved"]["zones"][0]["yaw_deg"] == 90
    anchors = json.loads(planning.zones_path.read_text())
    assert anchors["B"]["aim_deg"] == 0
    saved_plan = json.loads(planning.zone_plan_path.read_text())
    original["zones"][0]["aim_deg"] = -45.5
    original["zones"][1]["aim_deg"] = 0
    assert saved_plan == original
    runtime = ZoneStore.load(planning.maps, ("A", "B"))
    assert runtime.get("A").aim_deg == -45.5
    assert runtime.get("B").aim_deg == 0
    assert runtime.get("B").yaw == pytest.approx(np.pi / 2)
    restarted = PlanningService(
        planning._saved_config(),
        planning.device,
        config_path=planning.config_path,
        devices_dir=planning.devices_dir,
    )
    assert restarted.snapshot()["pending_restart"] is False


def test_clearing_camera_aim_does_not_resurrect_saved_plan_direction(planning):
    camera_zone_plan(planning)
    plan = proposal(planning)
    plan.zones[0].aim_deg = 90
    planning.save(plan)
    cleared = proposal(planning)
    cleared.zones[0].aim_deg = None
    planning.save(cleared)
    assert ZoneStore.load(planning.maps, ("B",)).get("B").aim_deg is None
    assert "aim_deg" not in json.loads(planning.zone_plan_path.read_text())["zones"][1]
    assert planning.snapshot()["saved"]["zones"][0]["yaw_deg"] == 90


@pytest.mark.parametrize("failure", ["zone_plan_path", "local_path"])
def test_camera_aim_save_failure_restores_all_written_files(planning, monkeypatch, failure):
    import host.dashboard.planning as module

    camera_zone_plan(planning)
    planning.save(proposal(planning))
    paths = [planning.zones_path, planning.zone_plan_path, planning.local_path]
    before = {path: path.read_bytes() for path in paths}
    plan = proposal(planning)
    plan.zones[0].aim_deg = 30
    original = module._atomic_write

    def write(path, content):
        if path == getattr(planning, failure):
            raise OSError("disk full")
        original(path, content)

    monkeypatch.setattr(module, "_atomic_write", write)
    with pytest.raises(OSError, match="disk full"):
        planning.save(plan)
    assert {path: path.read_bytes() for path in paths} == before


def test_zone_plan_direction_change_invalidates_stale_draft(planning):
    original = camera_zone_plan(planning)
    plan = proposal(planning)
    original["zones"][0]["aim_deg"] = 0
    planning.zone_plan_path.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(PlanError, match="다른 화면"):
        planning.save(plan)


@pytest.mark.parametrize("value", [True, "90", float("nan"), float("inf"), -181, 181])
def test_api_camera_aim_requires_a_finite_number(value):
    with pytest.raises(ValidationError):
        PlanInput(zones=[{"id": "A", "x": 1, "y": 2, "aim_deg": value}])


def test_api_camera_aim_requires_an_anchor_and_legacy_yaw_is_not_promoted(planning):
    with pytest.raises(ValidationError, match="위치"):
        PlanInput(zones=[{"id": "A", "aim_deg": 0}])
    saved = planning.save(proposal(planning))
    assert saved["saved"]["zones"][0]["aim_deg"] is None
    assert saved["saved"]["zones"][0]["yaw_deg"] == 90
