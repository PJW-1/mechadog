"""위치 힌트 API: 구역/점 선택, 유효하지 않은 입력, 콜백 결과 보존."""

import pytest
from fastapi.testclient import TestClient

from host.behavior.commander import Commander
from host.behavior.fsm import behavior_from_config
from host.dashboard.commands import CommandService
from host.dashboard.server import create_app
from host.dashboard.state import DashboardState


@pytest.fixture
def locate_api(cfg):
    calls = []

    def locate_zone(zone):
        calls.append(("zone", zone))
        return True, "구역 힌트를 예약했다"

    def locate_point(x, y):
        calls.append(("point", x, y))
        if abs(x) > 2 or abs(y) > 2:
            return False, "지도 밖의 위치는 알려줄 수 없다"
        return True, "점 힌트를 예약했다"

    commander = Commander()
    commands = CommandService(
        behavior_from_config(commander, cfg),
        commander,
        lambda _line: None,
        locate_zone=locate_zone,
        locate_point=locate_point,
    )
    app = create_app(DashboardState("test-device", stale_after_ms=3000), commands)
    with TestClient(app) as client:
        yield client, calls


@pytest.mark.parametrize(
    "body,expected",
    [({"zone": "A"}, ("zone", "A")), ({"x": 0, "y": -0.5}, ("point", 0.0, -0.5))],
)
def test_locate_api_selects_exactly_one_callback(locate_api, body, expected):
    client, calls = locate_api
    response = client.post("/api/command/locate", json=body)
    assert response.status_code == 200
    assert response.json()["command"] == "locate"
    assert response.json()["accepted"] is True
    assert calls == [expected]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"zone": "A", "x": 0, "y": 0},
        {"zone": "A", "x": 0},
        {"zone": "A", "y": 0},
        {"zone": None},
        {"zone": 1},
        {"x": 0},
        {"y": 0},
        {"x": None, "y": 0},
        {"x": True, "y": 0},
        {"x": 0, "y": False},
        {"x": "NaN", "y": 0},
        {"x": 0, "y": "inf"},
        {"x": "bad", "y": 0},
    ],
)
def test_locate_api_rejects_invalid_input_before_callback(locate_api, body):
    client, calls = locate_api
    assert client.post("/api/command/locate", json=body).status_code == 400
    assert calls == []


@pytest.mark.parametrize("x", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_nonfinite_numbers_are_rejected(locate_api, x):
    client, calls = locate_api
    response = client.post(
        "/api/command/locate",
        content=f'{{"x": {x}, "y": 0}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert calls == []


def test_locate_api_preserves_runtime_rejection(locate_api):
    client, calls = locate_api
    response = client.post("/api/command/locate", json={"x": 3, "y": 0})
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert "지도 밖" in response.json()["detail"]
    assert calls == [("point", 3.0, 0.0)]


def test_locate_point_reports_unwired_service(cfg):
    commander = Commander()
    service = CommandService(behavior_from_config(commander, cfg), commander, lambda _line: None)
    result = service.locate_point(0, 0)
    assert result.command == "locate"
    assert result.accepted is False
    assert "연결되지 않았다" in result.detail


def test_locate_api_keeps_local_origin_gate(locate_api):
    client, calls = locate_api
    response = client.post(
        "/api/command/locate", json={"x": 0, "y": 0}, headers={"Origin": "https://example.invalid"}
    )
    assert response.status_code == 403
    assert calls == []
