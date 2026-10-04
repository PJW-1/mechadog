"""동선 저장 API·출처·낡은 수정 방지와 명령 거절 전달을 검사한다."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from host.behavior.commander import Commander
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.routes import Route, load_routes
from host.common.config import load_config
from host.dashboard.commands import CommandService
from host.dashboard.planning import PlanError, PlanningService, RouteInput
from host.dashboard.server import create_app
from host.dashboard.state import DashboardState
from host.slam.occupancy import OccupancyGrid


@pytest.fixture
def planning(tmp_path):
    root = Path(__file__).resolve().parents[1]
    devices = tmp_path / "devices"
    devices.mkdir()
    (devices / "mechdog-02.yaml").write_bytes(
        (root / "config/devices/mechdog-02.yaml").read_bytes()
    )
    base = yaml.safe_load((root / "config/config.yaml").read_text(encoding="utf-8"))
    base["lidar"]["maps_dir"] = str(tmp_path / "maps")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    config = load_config("mechdog-02", config_path=config_path, devices_dir=devices)
    grid = OccupancyGrid.blank(resolution=0.1, span_cells=80)
    grid.cells.fill(-5)
    grid.save(tmp_path / "maps")
    return PlanningService(config, "mechdog-02", config_path=config_path, devices_dir=devices)


def proposal(service, route_id="test"):
    return RouteInput(
        revision=service.routes_snapshot()["revision"],
        route=Route.model_validate(
            {
                "id": route_id,
                "name": "합성 동선",
                "repeat": 2,
                "points": [{"x": -1, "y": 0}, {"x": 1, "y": 0, "aim_deg": 0, "label": "A"}],
            }
        ),
    )


def test_saved_and_startup_routes_are_distinct_with_roundtrip_and_delete(planning):
    original = planning.snapshot()["revision"]
    result = planning.save_route(proposal(planning))
    assert result["pending_restart"] and result["active"] == []
    assert result["explicit_start_uses_saved"]
    assert result["saved"][0]["points"][1]["aim_deg"] == 0
    assert planning.snapshot()["revision"] != original
    restarted = PlanningService(
        planning.config,
        planning.device,
        config_path=planning.config_path,
        devices_dir=planning.devices_dir,
    )
    assert not restarted.routes_snapshot()["pending_restart"]
    assert restarted.routes_snapshot()["active"] == result["saved"]
    planning.save_route(proposal(planning, "second"))
    assert list(load_routes(planning.maps)) == ["test", "second"]
    result = planning.delete_route("test", planning.routes_snapshot()["revision"])
    assert [route["id"] for route in result["saved"]] == ["second"]


def test_stale_and_blocked_edits_preserve_saved_bytes(planning):
    initial = proposal(planning)
    planning.save_route(initial)
    before = planning.routes_path.read_bytes()
    with pytest.raises(PlanError) as error:
        planning.delete_route("test", initial.revision)
    assert error.value.status == 409
    grid = OccupancyGrid.load(planning.maps)
    grid.cells[:, 40] = 5
    grid.save(planning.maps)
    updated = proposal(planning)
    preview = planning.preview_route(updated)
    assert not preview["valid"] and all(not segment["valid"] for segment in preview["segments"])
    with pytest.raises(PlanError, match="벽"):
        planning.save_route(updated)
    assert planning.routes_path.read_bytes() == before


def test_explicit_start_reads_latest_saved_and_rechecks_changed_map(planning):
    saved = proposal(planning)
    planning.save_route(saved)
    assert planning.route_for_start("test") == saved.route
    assert planning.routes_snapshot()["active"] == []
    grid = OccupancyGrid.load(planning.maps)
    grid.cells[:, 40] = 5
    grid.save(planning.maps)
    with pytest.raises(PlanError, match="벽"):
        planning.route_for_start("test")
    with pytest.raises(PlanError) as error:
        planning.route_for_start("missing")
    assert error.value.status == 404


def test_confirmation_digest_rejects_changed_route_without_unrelated_edits_conflict(planning):
    plan = proposal(planning)
    snapshot = planning.save_route(plan)
    digest = snapshot["digests"]["test"]
    planning.save_route(proposal(planning, "other"))
    assert planning.route_for_start("test", digest) == plan.route
    modified = proposal(planning)
    modified.route = modified.route.model_copy(update={"repeat": 3})
    planning.save_route(modified)
    with pytest.raises(PlanError, match="변경") as error:
        planning.route_for_start("test", digest)
    assert error.value.status == 409


def test_route_api_crud_preview_origin_and_missing_command(planning):
    app = create_app(DashboardState("mechdog-02", stale_after_ms=3000), planning=planning)
    with TestClient(app) as client:
        assert client.get("/api/planning/routes").json()["saved"] == []
        payload = proposal(planning).model_dump(mode="json")
        assert client.post("/api/planning/routes/preview", json=payload).json()["valid"]
        assert (
            client.post(
                "/api/planning/routes", json=payload, headers={"origin": "https://other.example"}
            ).status_code
            == 403
        )
        result = client.post("/api/planning/routes", json=payload)
        assert result.status_code == 200
        assert client.post("/api/planning/routes", json=payload).status_code == 409
        assert (
            client.request(
                "DELETE", "/api/planning/routes/test", json={"revision": result.json()["revision"]}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/command/route", json={"action": "start", "route_id": "test"}
            ).status_code
            == 404
        )


def test_route_command_delegates_runtime_safety_result_and_stop(cfg):
    calls = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)

    def start(route_id):
        calls.append(route_id)
        return False, "위치를 다시 확인하세요"

    def stop():
        calls.append("stop")
        return True, "동선 정지"

    commands = CommandService(
        behavior, commander, lambda _: None, start_route=start, stop_route=stop
    )
    app = create_app(DashboardState("mechdog-02", stale_after_ms=3000), commands=commands)
    with TestClient(app) as client:
        assert client.post("/api/command/route", json={"action": "start"}).status_code == 400
        result = client.post("/api/command/route", json={"action": "start", "route_id": "test"})
        assert not result.json()["accepted"] and "위치" in result.json()["detail"]
        assert client.post("/api/command/route", json={"action": "stop"}).json()["accepted"]
        assert (
            client.post(
                "/api/command/route",
                json={"action": "stop"},
                headers={"origin": "https://other.example"},
            ).status_code
            == 403
        )
    assert calls == ["test", "stop"]


@pytest.mark.parametrize("started", [False, True])
def test_existing_stop_button_uses_immediate_stop_callback_including_idle_queue(cfg, started):
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    if started:
        behavior.event(Event.START_PATROL)
    stopped = []

    def stop():
        stopped.append(True)
        return True, "예약과 동선을 즉시 중지했다"

    commands = CommandService(behavior, commander, lambda _: None, stop_route=stop)
    app = create_app(DashboardState("mechdog-02", stale_after_ms=3000), commands=commands)
    with TestClient(app) as client:
        result = client.post("/api/command/patrol", json={"action": "stop"}).json()
    assert result["accepted"] and result["command"] == "patrol_stop"
    assert stopped == [True]


@pytest.mark.parametrize("event", [Event.MANUAL_ON, Event.ESTOP])
def test_existing_stop_button_keeps_manual_and_failsafe_refusal(cfg, event):
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    behavior.event(event)
    stopped = []
    commands = CommandService(
        behavior,
        commander,
        lambda _: None,
        stop_route=lambda: (stopped.append(True) is None, "stop"),
    )
    assert not commands.patrol_stop().accepted
    assert stopped == []


def test_route_command_passes_confirmation_digest_and_rejects_malformed_digest(cfg):
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    calls = []

    def start(route_id, expected_digest=None):
        calls.append((route_id, expected_digest))
        return False, "동선이 변경되었습니다"

    commands = CommandService(behavior, commander, lambda _: None, start_route=start)
    app = create_app(DashboardState("mechdog-02", stale_after_ms=3000), commands=commands)
    body = {"action": "start", "route_id": "test", "expected_digest": "a" * 64}
    with TestClient(app) as client:
        response = client.post("/api/command/route", json=body)
        assert not response.json()["accepted"]
        body["expected_digest"] = "broken"
        assert client.post("/api/command/route", json=body).status_code == 400
    assert calls == [("test", "a" * 64)]
