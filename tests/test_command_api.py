"""명령 API 검증 — 수동 오버라이드와 E-Stop (WBS 4.5.3 · FR-4.3/4.4).

완료 기준이 둘이다 — **수동 오버라이드 진입/해제**와 **E-Stop 이 모든 상태에서
최우선 처리**. 뒤엣것이 이 파일의 본론이라, FSM 이 갈 수 있는 상태를 훑으며
매번 눌러 본다.

전송은 리스트로 받는다. 소켓도 로봇도 필요 없다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from host.behavior.commander import Commander
from host.behavior.fsm import Event, behavior_from_config
from host.dashboard.commands import CommandService
from host.dashboard.server import create_app
from host.dashboard.state import DashboardState

#: FSM 이 실제로 들어갈 수 있는 상태와 거기까지 가는 사건들.
ROUTES: dict[str, tuple[Event, ...]] = {
    "IDLE": (),
    "PATROL": (Event.START_PATROL,),
    "ALERT": (Event.START_PATROL, Event.PERSON_FOUND),
    "TRACK": (Event.START_PATROL, Event.PERSON_FOUND, Event.TARGET_OFF_CENTER),
    "MANUAL": (Event.MANUAL_ON,),
    "FAILSAFE": (Event.ONBOARD_FAILSAFE,),
}


@pytest.fixture
def service(cfg):
    sent: list[str] = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    return CommandService(behavior, commander, sent.append), behavior, sent


def _state() -> DashboardState:
    return DashboardState("mechdog-01", stale_after_ms=3000)


def _drive_to(behavior, events: tuple[Event, ...]) -> None:
    for index, event in enumerate(events, start=1):
        behavior.event(event, now_ms=index * 1000)


# ── E-Stop — 모든 상태에서 최우선 ────────────────────────────────


@pytest.mark.parametrize("state,events", list(ROUTES.items()))
def test_estop_works_from_every_state(service, state, events):
    """**이것이 4.5.3 의 본론이다.** 조건을 하나라도 달면 그 조건이 틀렸을 때
    비상정지가 안 듣는다.
    """
    svc, behavior, sent = service
    _drive_to(behavior, events)
    assert behavior.state == state

    result = svc.estop()
    assert result.accepted is True
    assert behavior.state == "FAILSAFE"
    assert len(sent) == 1


def test_estop_telegram_goes_out_immediately(service):
    """틱을 기다리면 최악의 경우 100ms 늦는다. 큐에 넣지 않는다."""
    svc, _behavior, sent = service
    assert sent == []
    svc.estop()
    assert len(sent) == 1
    assert "ESTOP" in sent[0]


def test_estop_is_idempotent(service):
    """이미 FAILSAFE 여도 또 눌린다 — 누른 사람은 멈췄는지 알 수 없다."""
    svc, behavior, sent = service
    svc.estop()
    svc.estop()
    assert behavior.state == "FAILSAFE"
    assert len(sent) == 2


def test_estop_stops_the_repeating_intent_too(service):
    """래치 뒤에는 MOVE 가 차단되므로 의도도 정지여야 실제와 맞는다."""
    svc, behavior, _sent = service
    svc.manual_on()
    svc.drive(60, 10)
    svc.estop()
    assert svc._commander.intent.type_ == "STOP"


# ── 수동 오버라이드 진입/해제 ────────────────────────────────────


def test_manual_on_and_off_round_trip(service):
    svc, behavior, _sent = service
    assert svc.manual_on().accepted is True
    assert behavior.state == "MANUAL"
    assert svc.manual_off().accepted is True
    assert behavior.state == "IDLE"


def test_manual_on_is_refused_in_failsafe(service):
    """비상정지로 멈춘 로봇을 조이스틱으로 다시 움직이게 두지 않는다."""
    svc, behavior, _sent = service
    svc.estop()
    result = svc.manual_on()
    assert result.accepted is False
    assert behavior.state == "FAILSAFE"
    assert "받지 않는다" in result.detail


def test_manual_off_is_refused_when_not_manual(service):
    svc, _behavior, _sent = service
    result = svc.manual_off()
    assert result.accepted is False
    assert "수동 조종 중이 아니다" in result.detail


def test_entering_manual_halts_first(service):
    """조작자가 조이스틱을 잡기 전까지는 멈춰 있어야 한다."""
    svc, behavior, _sent = service
    behavior.event(Event.START_PATROL, now_ms=1000)
    svc.manual_on()
    assert svc._commander.intent.type_ == "STOP"


# ── 수동 조종 입력 ───────────────────────────────────────────────


def test_drive_is_refused_outside_manual(service):
    """자율 주행 중 조이스틱을 섞으면 FSM 지시와 사람 입력이 주기를 다툰다."""
    svc, behavior, _sent = service
    behavior.event(Event.START_PATROL, now_ms=1000)
    result = svc.drive(60, 0)
    assert result.accepted is False
    assert "오버라이드를 먼저" in result.detail


def test_drive_sets_the_repeating_intent(service):
    svc, _behavior, sent = service
    svc.manual_on()
    assert svc.drive(60, -12).accepted is True
    intent = svc._commander.intent
    assert intent.type_ == "MOVE"
    assert intent.fields == {"step": 60, "angle": -12}
    # 즉시 보내지 않는다 — 다음 틱에 반영된다.
    assert sent == []


# ── HTTP 경로 ────────────────────────────────────────────────────


@pytest.fixture
def client(cfg):
    sent: list[str] = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    svc = CommandService(behavior, commander, sent.append)
    app = create_app(_state(), svc)
    with TestClient(app) as http:
        yield http, behavior, sent


def test_estop_endpoint_reports_failsafe(client):
    http, behavior, sent = client
    body = http.post("/api/command/estop").json()
    assert body["accepted"] is True
    assert body["state"] == "FAILSAFE"
    assert len(sent) == 1


def test_manual_endpoint_round_trips(client):
    http, _behavior, _sent = client
    assert http.post("/api/command/manual", json={"on": True}).json()["state"] == "MANUAL"
    assert http.post("/api/command/manual", json={"on": False}).json()["state"] == "IDLE"


def test_manual_endpoint_rejects_a_non_boolean(client):
    http, _behavior, _sent = client
    assert http.post("/api/command/manual", json={"on": "yes"}).status_code == 400


def test_drive_endpoint_requires_both_fields(client):
    http, _behavior, _sent = client
    http.post("/api/command/manual", json={"on": True})
    assert http.post("/api/command/drive", json={"step": 60}).status_code == 400


def test_commands_are_refused_from_a_foreign_origin(client):
    """이 경로는 로봇을 움직인다. 다른 탭이 순찰을 멈추게 두지 않는다."""
    http, behavior, sent = client
    response = http.post("/api/command/estop", headers={"origin": "http://evil.example"})
    assert response.status_code == 403
    assert behavior.state == "IDLE"
    assert sent == []


def test_local_origin_is_allowed(client):
    http, _behavior, _sent = client
    response = http.post(
        "/api/command/estop",
        headers={"origin": "http://127.0.0.1:" + str(http.base_url.port or 80)},
    )
    assert response.status_code == 200


def test_read_only_server_exposes_no_command_routes():
    """`commands` 를 넘기지 않으면 예전처럼 읽기 전용으로 뜬다."""
    with TestClient(create_app(_state())) as http:
        assert http.get("/health").json()["read_only"] is True
        assert http.post("/api/command/estop").status_code == 404


def test_health_reports_that_it_is_no_longer_read_only(client):
    http, _behavior, _sent = client
    assert http.get("/health").json()["read_only"] is False


def test_service_reports_the_current_state(service):
    """화면이 상태를 물을 때 쓰는 통로다."""
    svc, behavior, _sent = service
    assert svc.state == "IDLE"
    behavior.event(Event.START_PATROL, now_ms=1000)
    assert svc.state == "PATROL" == behavior.state
