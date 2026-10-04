"""명령 API 검증 — 수동 오버라이드와 E-Stop (WBS 4.5.3 · FR-4.3/4.4).

완료 기준이 둘이다 — **수동 오버라이드 진입/해제**와 **E-Stop 이 모든 상태에서
최우선 처리**. 뒤엣것이 이 파일의 본론이라, FSM 이 갈 수 있는 상태를 훑으며
매번 눌러 본다.

전송은 리스트로 받는다. 소켓도 로봇도 필요 없다.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from host.behavior.commander import Commander
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.mission import Mission
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


# ── 수동 자세 (B6) ───────────────────────────────────────────────


def _pose_service(cfg):
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    return CommandService(behavior, commander, lambda _line: None, pose=(-15.0, 500)), commander


def _poses(commander) -> list[dict]:
    return [i.fields for i in commander._pending if i.type_ == "POSE"]


def test_pose_is_refused_outside_manual(cfg):
    """자율 중에는 PPE 자세 상승이 같은 POSE 를 쓴다 — 섞지 않는다."""
    svc, commander = _pose_service(cfg)
    result = svc.pose("up")
    assert result.accepted is False
    assert "오버라이드를 먼저" in result.detail
    assert _poses(commander) == []


def test_pose_uses_only_the_verified_angles(cfg):
    svc, commander = _pose_service(cfg)
    svc.manual_on()
    assert svc.pose("up").accepted is True
    assert svc.pose("down").accepted is True
    assert svc.pose("tilt45").accepted is False, "임의 자세는 받지 않는다"
    assert [p["pitch"] for p in _poses(commander)] == [-15.0, 15.0]
    assert _poses(commander)[0]["dur"] == 500


def test_leaving_manual_levels_a_tilted_body(cfg):
    """⚠️ 기울인 채로 자율에 넘기면 순찰이 기울어진 채 걷는다."""
    svc, commander = _pose_service(cfg)
    svc.manual_on()
    svc.pose("up")
    svc.manual_off()
    assert [p["pitch"] for p in _poses(commander)] == [-15.0, 0.0]


def test_pose_without_a_wired_path_is_refused(service):
    svc, _behavior, _sent = service
    svc.manual_on()
    assert svc.pose("up").accepted is False


def test_pose_endpoint_round_trips(cfg):
    svc, _commander = _pose_service(cfg)
    with TestClient(create_app(_state(), svc)) as http:
        assert http.post("/api/command/pose", json={"preset": "up"}).json()["accepted"] is False
        http.post("/api/command/manual", json={"on": True})
        assert http.post("/api/command/pose", json={"preset": "up"}).json()["accepted"] is True
        assert http.post("/api/command/pose", json={"preset": 3}).status_code == 400


# ── 순찰 시작/정지 (WBS 4.7.11) ──────────────────────────────────


def test_patrol_reserves_through_the_runtime_hook(service):
    """`START_PATROL` 을 바로 넣지 않는다 — 기동 리셋과 순서가 어긋나면
    순찰이 조용히 취소되는 함정이 실기에서 드러났다 (`runtime.ask_patrol`)."""
    svc, _behavior, _sent = service
    asked: list[bool] = []
    svc._ask_patrol = lambda: asked.append(True)
    result = svc.patrol()
    assert result.accepted is True
    assert asked == [True]
    assert "예약" in result.detail


def test_patrol_is_refused_without_the_hook(service):
    """순찰 경로가 연결되지 않은 서비스는 거짓 성공을 하지 않는다."""
    svc, _behavior, _sent = service
    result = svc.patrol()
    assert result.accepted is False
    assert "연결되지 않았다" in result.detail


def test_patrol_stop_leaves_autonomy_and_parks_at_idle(service):
    """`PATROL → IDLE` 직행 사건은 없다 — 수동을 한 번 거쳐 정상 정지한다."""
    svc, behavior, _sent = service
    behavior.event(Event.START_PATROL, now_ms=1000)
    assert svc.patrol_stop().accepted is True
    assert behavior.state == "IDLE"
    # 수동 경유는 로봇을 멈추고 들어가는 경로다 — 멈춘 뒤 대기다.
    assert svc._commander.intent.type_ == "STOP"


def test_patrol_stop_is_refused_when_not_autonomous(service):
    svc, behavior, _sent = service
    result = svc.patrol_stop()
    assert result.accepted is False
    assert "자율 동작 중이 아니다" in result.detail
    assert behavior.state == "IDLE"


def test_patrol_stop_is_refused_in_failsafe(service):
    """FAILSAFE 에서는 '순찰 정지'가 아니라 리셋 확인이 필요하다."""
    svc, behavior, _sent = service
    svc.estop()
    result = svc.patrol_stop()
    assert result.accepted is False
    assert behavior.state == "FAILSAFE"


def test_patrol_endpoint_routes_start_and_stop(cfg):
    sent: list[str] = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    asked: list[bool] = []
    svc = CommandService(behavior, commander, sent.append, ask_patrol=lambda: asked.append(True))
    app = create_app(_state(), svc)
    with TestClient(app) as http:
        body = http.post("/api/command/patrol", json={"action": "start"}).json()
        assert body["accepted"] is True and asked == [True]
        behavior.event(Event.START_PATROL, now_ms=1000)
        body = http.post("/api/command/patrol", json={"action": "stop"}).json()
        assert body["accepted"] is True and body["state"] == "IDLE"
        assert http.post("/api/command/patrol", json={"action": "bogus"}).status_code == 400
        assert http.post("/api/command/patrol", json={}).status_code == 400


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


@pytest.mark.parametrize(
    "path", ["/api/command/manual", "/api/command/patrol", "/api/command/sound"]
)
@pytest.mark.parametrize("content", [b"{not json", b"", b"\xff\xfe", b"[]", b"null"])
def test_command_rejects_a_body_that_is_not_a_json_object(client, path, content):
    """깨진 JSON·빈 본문·객체가 아닌 본문은 다른 잘못된 본문처럼 400 이다(500 이 아니다)."""
    http, behavior, sent = client
    response = http.post(path, content=content, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"error": "body"}
    assert behavior.state == "IDLE"
    assert sent == []


def test_drive_rejects_malformed_json_with_its_own_reason(client):
    http, _behavior, _sent = client
    http.post("/api/command/manual", json={"on": True})
    response = http.post("/api/command/drive", content=b"{not json")
    assert response.status_code == 400
    assert response.json() == {"error": "fields"}


def test_foreign_origin_is_refused_before_the_body_is_read(client):
    http, _behavior, _sent = client
    response = http.post(
        "/api/command/manual", content=b"{not json", headers={"origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert response.json() == {"error": "origin"}


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


# ── 서비스 모드 (SERVICE 전문) ──────────────────────────────────


def test_service_enter_queues_a_one_shot_telegram(service):
    """진입은 다음 틱에 실어 보낸다 — 주차 명령이라 급하지 않다."""
    svc, _behavior, sent = service
    result = svc.service("enter")
    assert result.accepted is True
    assert sent == []  # tick 이 만들 때까지 전문이 나가지 않는다
    telegrams = svc._commander.tick(10_000)
    assert any('"type":"SERVICE"' in t and '"mode":"enter"' in t for t in telegrams)


def test_service_exit_queues_exit_mode(service):
    svc, _behavior, _sent = service
    assert svc.service("exit").accepted is True
    telegrams = svc._commander.tick(10_000)
    assert any('"mode":"exit"' in t for t in telegrams)


def test_service_rejects_an_unknown_mode(service):
    svc, _behavior, _sent = service
    result = svc.service("reboot")
    assert result.accepted is False
    telegrams = svc._commander.tick(10_000)
    assert not any('"SERVICE"' in t for t in telegrams)


def test_service_endpoint_round_trips(client):
    http, _behavior, _sent = client
    body = http.post("/api/command/service", json={"mode": "enter"}).json()
    assert body["accepted"] is True


def test_service_endpoint_rejects_a_non_string_mode(client):
    http, _behavior, _sent = client
    assert http.post("/api/command/service", json={"mode": 1}).status_code == 400


# ── 사건은 런타임의 `_apply` 경로로 들어간다 (2026-09-14 실기) ──


def test_commands_route_events_through_the_given_hook(cfg):
    """**`behavior.event()` 를 직접 부르면 대응 단계와 전이 로그가 함께 빠진다.**

    실기에서 E-Stop 이 로봇을 잠갔는데 단계가 `L0`(파랑) 에 머물러 눈 LED 가 흰색으로
    바뀌지 않았고(FR-10.4), `MANUAL` 26.7초의 전이도 로그에 한 줄도 남지 않았다.
    런타임이 `_apply` 에 그 둘을 묶어 두었으므로 명령도 그 경로로 들어가야 한다.
    """
    sent: list[str] = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    seen: list[Event] = []

    def applied(event: Event) -> bool:
        seen.append(event)
        return behavior.event(event)

    service = CommandService(behavior, commander, sent.append, apply_event=applied)
    service.manual_on()
    service.manual_off()
    service.estop()
    assert seen == [Event.MANUAL_ON, Event.MANUAL_OFF, Event.ESTOP]
    assert behavior.state == "FAILSAFE"


def test_runtime_wires_the_apply_hook_so_escalation_follows_estop(cfg, clock):
    """런타임에 붙었을 때 **E-Stop 이 대응 단계를 `F` 로 올려야 한다.**

    `+0.0s` 온보드 자체 래치는 `F` 까지 올라가는데 버튼으로 잠근 쪽만 `L0` 에
    머물렀던 것이 실기의 증상이다 — 차이는 잠긴 이유가 아니라 들어온 경로였다.
    """
    from host.runtime import Runtime

    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock)
    service = CommandService(
        runtime.behavior,
        runtime.commander,
        lambda _line: None,
        apply_event=runtime.apply_external,
    )
    assert runtime.escalation.level.value == "L0"
    service.estop()
    assert runtime.behavior.state == "FAILSAFE"
    assert runtime.escalation.level.value == "F", "E-Stop 이 단계를 올려야 눈 LED 가 흰색이 된다"


def test_failsafe_entry_reaches_the_event_feed(cfg, clock):
    """안전 잠금은 전이 로그에만 있었다 — 관제 사건 목록에도 올라간다 (B4)."""
    from host.runtime import Runtime

    board = _state()
    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock, dashboard=board)
    service = CommandService(
        runtime.behavior, runtime.commander, lambda _line: None, apply_event=runtime.apply_external
    )
    service.estop()
    events, _ = board.events_since(0)
    assert [(e["event"], e["trigger"], e["previous"]) for e in events] == [
        ("failsafe_entered", "ESTOP", "IDLE")
    ]


def test_policy_endpoint_serves_the_config_values(cfg):
    """설정 화면이 숫자를 지어내지 않게 판정 코드와 같은 키를 내보낸다 (B7)."""
    from host.runtime import policy_view

    with TestClient(create_app(_state(), policy=policy_view(cfg))) as http:
        body = http.get("/api/policy").json()
    assert body["l1_to_l2_hold_s"] == cfg["escalation"]["l1_to_l2_hold_s"]
    assert body["auth_timeout_s"] == cfg["auth"]["timeout_s"]
    assert body["target_lost_timeout_s"] == cfg["fsm"]["target_lost_timeout_s"]
    with TestClient(create_app(_state())) as http:
        assert http.get("/api/policy").status_code == 404


# ── PC 스피커 방송 음량·무음 (WBS 4.8.2) ─────────────────────────
#
# 로봇 스피커(`/api/command/sound`)와는 별개 경로다 — 이쪽은 관제 TTS
# (`host/cloud/broadcast.py`)가 Host PC 로 내는 소리만 다룬다.


def _fake_broadcaster():
    from host.cloud.broadcast import Broadcaster

    return Broadcaster(
        synth=lambda _t: (b"\x00\x00", 16000), play=lambda _p, _r: None, preload=False
    )


def test_broadcast_status_reports_unavailable_without_a_broadcaster():
    """piper 없음 등으로 방송기가 없으면 서버는 그래도 뜨고 «방송 없음» 을 말한다."""
    with TestClient(create_app(_state())) as http:
        assert http.get("/api/broadcast").json() == {"available": False}
        assert http.post("/api/broadcast", json={"volume": 50}).status_code == 404


def test_broadcast_status_and_update_round_trip():
    broadcaster = _fake_broadcaster()
    try:
        with TestClient(create_app(_state(), broadcast=broadcaster)) as http:
            body = http.get("/api/broadcast").json()
            assert body == {"available": True, "volume": 100, "muted": False}

            body = http.post("/api/broadcast", json={"volume": 30}).json()
            assert body == {"available": True, "volume": 30, "muted": False}
            assert broadcaster.volume == 30

            body = http.post("/api/broadcast", json={"muted": True}).json()
            assert body["muted"] is True
            assert broadcaster.muted is True
    finally:
        broadcaster.close()


def test_broadcast_update_rejects_a_bad_volume():
    broadcaster = _fake_broadcaster()
    try:
        with TestClient(create_app(_state(), broadcast=broadcaster)) as http:
            assert http.post("/api/broadcast", json={"volume": 101}).status_code == 400
            assert http.post("/api/broadcast", json={"volume": -1}).status_code == 400
            assert http.post("/api/broadcast", json={"volume": "50"}).status_code == 400
            # bool 을 정수로 받지 않는다 — 다른 명령 경로와 같은 규약.
            assert http.post("/api/broadcast", json={"volume": True}).status_code == 400
            assert broadcaster.volume == 100
    finally:
        broadcaster.close()


def test_broadcast_update_rejects_a_non_boolean_muted():
    broadcaster = _fake_broadcaster()
    try:
        with TestClient(create_app(_state(), broadcast=broadcaster)) as http:
            assert http.post("/api/broadcast", json={"muted": "yes"}).status_code == 400
            assert broadcaster.muted is False
    finally:
        broadcaster.close()


def test_broadcast_update_rejects_malformed_json():
    broadcaster = _fake_broadcaster()
    try:
        with TestClient(create_app(_state(), broadcast=broadcaster)) as http:
            response = http.post("/api/broadcast", content=b"{not json")
            assert response.status_code == 400
            assert response.json() == {"error": "body"}
            assert http.post("/api/broadcast", json={"volume": None}).json() == {"error": "volume"}
            assert broadcaster.volume == 100
    finally:
        broadcaster.close()


def test_broadcast_update_changes_nothing_when_any_field_is_bad():
    """검증을 모두 마친 뒤 적용한다 — 음량만 바뀌고 400 이 나가면 화면과 실제가 어긋난다."""
    broadcaster = _fake_broadcaster()
    try:
        with TestClient(create_app(_state(), broadcast=broadcaster)) as http:
            response = http.post("/api/broadcast", json={"volume": 30, "muted": "yes"})
            assert response.status_code == 400
            assert broadcaster.volume == 100
    finally:
        broadcaster.close()


def test_broadcast_update_rejects_a_non_object_body():
    broadcaster = _fake_broadcaster()
    try:
        with TestClient(create_app(_state(), broadcast=broadcaster)) as http:
            assert http.post("/api/broadcast", json=["volume"]).status_code == 400
            assert http.post("/api/broadcast", json="volume").status_code == 400
    finally:
        broadcaster.close()


def test_broadcast_update_is_refused_from_a_foreign_origin():
    broadcaster = _fake_broadcaster()
    try:
        with TestClient(create_app(_state(), broadcast=broadcaster)) as http:
            response = http.post(
                "/api/broadcast", json={"volume": 10}, headers={"origin": "http://evil.example"}
            )
            assert response.status_code == 403
            assert broadcaster.volume == 100
    finally:
        broadcaster.close()


# ── 운용 모드 전환 (WBS 3.4.4 · FR-4.7 · FR-11.3) ────────────────


@pytest.fixture
def mode_service(cfg):
    """모드 전환이 실제로 연결된 서비스. 전환 가부는 런타임처럼 FSM 상태가 정한다."""
    sent: list[str] = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    mission = Mission(cfg)
    svc = CommandService(
        behavior,
        commander,
        sent.append,
        set_mode=lambda target: mission.switch(target, state=behavior.state),
    )
    return svc, behavior, mission


@pytest.mark.usefixtures("unlock_modes")
def test_mode_switch_is_accepted_while_idle(mode_service):
    svc, _behavior, mission = mode_service
    result = svc.mission_mode("factory")
    assert result.accepted is True
    assert mission.mode == "factory"


@pytest.mark.parametrize(
    "state,events", [(s, e) for s, e in ROUTES.items() if s not in {"IDLE", "MANUAL"}]
)
@pytest.mark.usefixtures("unlock_modes")
def test_mode_switch_is_refused_outside_standstill(mode_service, state, events):
    """FR-11.3 — 대응 중에 판정 규칙이 바뀌면 진행 중인 시퀀스가 의미를 잃는다."""
    svc, behavior, mission = mode_service
    _drive_to(behavior, events)
    assert behavior.state == state

    result = svc.mission_mode("factory")
    assert result.accepted is False
    assert state in result.detail, "거절에는 사유가 붙는다"
    assert mission.mode == "guard", "거절됐으면 모드는 그대로다"


def test_mode_switch_without_a_wired_path_is_refused(service):
    """연결되지 않은 채로 «바꿨다» 고 답하면 화면이 거짓을 말한다."""
    svc, _behavior, _sent = service
    assert svc.mission_mode("factory").accepted is False


@pytest.mark.parametrize("value", ["", "   "])
def test_mode_switch_rejects_an_empty_name(mode_service, value):
    svc, _behavior, _mission = mode_service
    assert svc.mission_mode(value).accepted is False


@pytest.mark.usefixtures("unlock_modes")
def test_mode_endpoint_round_trips(cfg):
    sent: list[str] = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    mission = Mission(cfg)
    svc = CommandService(
        behavior,
        commander,
        sent.append,
        set_mode=lambda target: mission.switch(target, state=behavior.state),
    )
    with TestClient(create_app(_state(), svc)) as http:
        body = http.post("/api/command/mode", json={"mode": "factory"}).json()
        assert body["accepted"] is True
        assert mission.mode == "factory"

        assert http.post("/api/command/mode", json={"mode": 3}).status_code == 400


def test_health_lists_only_the_modes_that_can_be_chosen(client):
    """FR-11.7 — 서버가 **무엇을 고를 수 있는지** 스스로 말한다.

    `factory` 는 선행 구현(`3.7.3`)이 들어오면 자동으로
    목록에 들어온다 — 그래서 여기서는 «경비는 언제나 있다» 만 못 박는다.
    ⚠️ 화면은 이 값을 읽지 않고 버튼을 늘 띄운다. 거절 사유로 알리는 쪽을 골랐다.
    """
    http, _behavior, _sent = client
    modes = http.get("/health").json()["modes"]
    assert "guard" in modes
    assert set(modes) <= {"guard", "factory"}


# ── 음성 암구호 인증 결과 주입 (WBS 3.8.2 · FR-10.2) ────────────
#
# 대조 자체는 음성 파이프라인이 한다 — 이쪽은 판정 결과를 사건으로 옮기는
# 자리일 뿐이다. `AUTH_WAIT` 까지 가는 경로는 사원증 인증과 같다.

AUTH_WAIT_ROUTE: tuple[Event, ...] = (
    Event.START_PATROL,
    Event.PERSON_FOUND,
    Event.AUTH_REQUIRED,
)


def test_auth_ok_releases_auth_wait_to_patrol(service):
    """일치 판정이 오면 `AUTH_OK` — 인증된 방문객으로 간주하고 순찰로 돌아간다."""
    svc, behavior, _sent = service
    _drive_to(behavior, AUTH_WAIT_ROUTE)
    assert behavior.state == "AUTH_WAIT"

    result = svc.auth("ok")
    assert result.accepted is True
    assert behavior.state == "PATROL"


def test_auth_fail_returns_to_alert(service):
    """불일치 판정은 `AUTH_FAILED` — 재시도·초과 판단은 런타임이 한다."""
    svc, behavior, _sent = service
    _drive_to(behavior, AUTH_WAIT_ROUTE)

    result = svc.auth("fail")
    assert result.accepted is True
    assert behavior.state == "ALERT"


@pytest.mark.parametrize("state,events", [(s, e) for s, e in ROUTES.items() if s != "ALERT"])
def test_auth_is_refused_outside_auth_wait(service, state, events):
    """**인증 대기 중이 아니면 판정 결과를 받지 않는다** — 임의 시각에 `ok` 를
    밀어 넣어 인증을 통과하는 경로가 없어야 한다. (ALERT 는 AUTH_WAIT 의 전신이라
    별도 검증한다.)"""
    svc, behavior, _sent = service
    _drive_to(behavior, events)
    assert behavior.state == state

    result = svc.auth("ok")
    assert result.accepted is False
    assert behavior.state == state
    assert "AUTH_WAIT" in result.detail


def test_auth_is_refused_in_alert_before_auth_wait(service):
    """경보 단계에서 «인증 성공» 이 오면 안 된다 — 아직 묻지도 않았다."""
    svc, behavior, _sent = service
    _drive_to(behavior, (Event.START_PATROL, Event.PERSON_FOUND))
    assert behavior.state == "ALERT"

    result = svc.auth("ok")
    assert result.accepted is False
    assert behavior.state == "ALERT"


def test_auth_rejects_an_unknown_result(service):
    svc, behavior, _sent = service
    _drive_to(behavior, AUTH_WAIT_ROUTE)
    result = svc.auth("maybe")
    assert result.accepted is False
    assert behavior.state == "AUTH_WAIT"


def test_auth_endpoint_round_trips(cfg):
    sent: list[str] = []
    commander = Commander()
    behavior = behavior_from_config(commander, cfg)
    svc = CommandService(behavior, commander, sent.append)
    app = create_app(_state(), svc)
    with TestClient(app) as http:
        # 대기 중이 아니면 거절
        body = http.post("/api/command/auth", json={"result": "ok"}).json()
        assert body["accepted"] is False and body["state"] == "IDLE"

        _drive_to(behavior, AUTH_WAIT_ROUTE)
        body = http.post("/api/command/auth", json={"result": "fail"}).json()
        assert body["accepted"] is True and body["state"] == "ALERT"

        assert http.post("/api/command/auth", json={"result": "maybe"}).status_code == 400
        assert http.post("/api/command/auth", json={}).status_code == 400

        # `captured_at_ms` 는 선택이지만 **정수여야 한다.**
        assert (
            http.post(
                "/api/command/auth", json={"result": "ok", "captured_at_ms": "지금"}
            ).status_code
            == 400
        )
        # ⚠️ **`True` 를 정수로 받으면 안 된다.** `isinstance(True, int)` 가 참이라
        # 시각 1 로 들어가 **모든 발화가 창보다 오래된 것**이 되어 인증이 통째로 막힌다.
        assert (
            http.post(
                "/api/command/auth", json={"result": "ok", "captured_at_ms": True}
            ).status_code
            == 400
        )


def test_auth_endpoint_does_not_execute_speech(client):
    """인식 텍스트를 그대로 받는 엔드포인트가 아니다 — `text` 필드를 내도
    명령으로 실행되지 않는다 (임의 음성 → 로봇 명령 경로 차단)."""
    http, behavior, sent = client
    body = http.post("/api/command/auth", json={"result": "ok", "text": "순찰 시작해"}).json()
    assert body["accepted"] is False
    assert behavior.state == "IDLE"
    assert sent == []


def test_pending_is_refused_when_the_host_cannot_defer(service):
    """런타임이 붙지 않은 조합에서는 **조용히 성공한 척하지 않는다.**"""
    svc, behavior, _sent = service
    _drive_to(behavior, AUTH_WAIT_ROUTE)

    result = svc.auth("pending")

    assert result.accepted is False
    assert behavior.state == "AUTH_WAIT"


def test_auth_endpoint_accepts_pending(cfg, clock):
    """HTTP 로도 같은 말을 할 수 있어야 한다 — 파이프라인이 쓰는 문이다."""
    from host.runtime import Runtime

    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock)
    svc = CommandService(
        runtime.behavior,
        runtime.commander,
        lambda _line: None,
        apply_event=runtime.apply_external,
        note_voice_auth=runtime.note_voice_auth,
        note_voice_listening=runtime.note_voice_listening,
    )
    app = create_app(_state(), svc)
    with TestClient(app) as http:
        body = http.post("/api/command/auth", json={"result": "pending"}).json()
        assert body["accepted"] is False and body["state"] == "IDLE"

        for event in AUTH_WAIT_ROUTE:
            runtime.apply_external(event)
        body = http.post(
            "/api/command/auth", json={"result": "pending", "captured_at_ms": clock.ms}
        ).json()
        assert body["accepted"] is True and body["state"] == "AUTH_WAIT"
        assert runtime.behavior.timer_deferred_ms == int(cfg["auth"]["verdict_grace_s"]) * 1000


# ── 경보(L3) 확인 (FR-10.3.2 · 2026-09-22 실기) ──────────────────
#
# **헤드리스 런타임에는 L3 를 풀 방법이 없었다.** 콘솔 확인 키는 tty 를 요구하므로
# (`runtime.watch_console`) 백그라운드로 띄운 런타임에서는 아무도 누를 수 없다.
# 2026-09-22 실기에서 그것 때문에 **런타임을 세 번 재시작**했다 — 재시작은 확인이
# 아니라 증거 인멸에 가깝고, 그 사이의 사건 기록도 함께 끊긴다.


def _alarm_service(cfg, clock):
    """런타임이 붙은 서비스. **단계를 쥐고 있는 쪽이 런타임이라** 이 조합이어야 한다."""
    from host.runtime import Runtime

    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock)
    svc = CommandService(
        runtime.behavior,
        runtime.commander,
        lambda _line: None,
        request_reset=runtime.ask_reset,
        apply_event=runtime.apply_external,
        confirm_alarm=runtime.ask_alarm_confirm,
    )
    return svc, runtime


def _raise_alarm(runtime, clock):
    runtime.start_patrol(clock.ms)
    runtime.escalation.note_event("PPE_VIOLATION", clock.ms)
    assert runtime.escalation.level.value == "L3"


def test_alarm_confirm_releases_the_alarm_on_the_next_tick(cfg, clock):
    """**확인은 요청이고 해제는 틱이 한다.**

    그 자리에서 풀면 운용 루프가 전문을 만드는 중간에 단계가 바뀌어, 그 틱의
    명령이 어느 단계의 것인지 말할 수 없게 된다.
    """
    svc, runtime = _alarm_service(cfg, clock)
    _raise_alarm(runtime, clock)

    result = svc.alarm_confirm()

    assert result.accepted is True
    assert runtime.escalation.level.value == "L3", "요청만 세운다"
    runtime.tick(clock.advance(100))
    assert runtime.escalation.level.value == "L0"


def test_alarm_confirm_does_not_clear_the_failsafe(cfg, clock):
    """⚠️ **이것이 이 문을 따로 낸 이유다** — 경보 확인은 F 를 풀지 않는다.

    하나로 묶으면 **비상정지를 눌렀다 푸는 것으로 경보가 지워지고**, 반대로
    상황을 확인한 것이 물리 잠금까지 푼다 (ADR-26).
    """
    svc, runtime = _alarm_service(cfg, clock)
    runtime.apply_external(Event.ONBOARD_FAILSAFE)
    assert runtime.escalation.level.value == "F"

    assert svc.alarm_confirm().accepted is True
    runtime.tick(clock.advance(100))

    assert runtime.escalation.level.value == "F", "경보 확인으로 안전 잠금이 풀리면 안 된다"
    assert runtime.behavior.state == "FAILSAFE"


def test_alarm_confirm_is_harmless_when_there_is_no_alarm(cfg, clock):
    """경보가 없을 때 눌러도 **아무 일도 일어나지 않는다.** 단계를 내리지 않는다."""
    svc, runtime = _alarm_service(cfg, clock)
    runtime.start_patrol(clock.ms)
    runtime.apply_external(Event.PERSON_FOUND)
    before = runtime.escalation.level.value

    assert svc.alarm_confirm().accepted is True
    runtime.tick(clock.advance(100))

    assert runtime.escalation.level.value == before, "L3 가 아니면 확인이 단계를 내리지 않는다"


def test_alarm_confirm_is_refused_when_the_host_cannot_confirm(service):
    """런타임이 붙지 않은 조합에서는 **조용히 성공한 척하지 않는다.**"""
    svc, _behavior, _sent = service

    result = svc.alarm_confirm()

    assert result.accepted is False
    assert "연결되지 않았다" in result.detail


def test_alarm_endpoint_round_trips(cfg, clock):
    """**HTTP 로 풀 수 있어야 한다** — 헤드리스 런타임에 남은 유일한 문이다."""
    svc, runtime = _alarm_service(cfg, clock)
    app = create_app(_state(), svc)
    with TestClient(app) as http:
        _raise_alarm(runtime, clock)
        body = http.post("/api/command/alarm", json={}).json()
        assert body["accepted"] is True and body["command"] == "alarm"

        runtime.tick(clock.advance(100))
        assert runtime.escalation.level.value == "L0"


# ── 트랙 재생 (SOUND 전문 · WBS 4.7.21 ⑤) ───────────────────────


def _sounds(telegrams: list[str]) -> list[int]:
    return [m["track"] for m in map(json.loads, telegrams) if m["type"] == "SOUND"]


def test_sound_queues_a_one_shot_telegram(service):
    """급하지 않다 — `service` 처럼 다음 틱에 한 번만 싣는다."""
    svc, _behavior, sent = service
    result = svc.sound(17)
    assert result.accepted is True and result.command == "sound"
    assert sent == [], "즉시 송신은 ESTOP 만이다"
    assert _sounds(svc._commander.tick(10_000)) == [17]
    assert _sounds(svc._commander.tick(10_100)) == [], "한 번만 나간다"


@pytest.mark.parametrize("track", [-1, 3001, 10**6])
def test_sound_refuses_out_of_range_tracks(service, track):
    """잘라 받으면 다른 문장이 나간다 — 보내지 않고 사유를 돌려준다."""
    svc, _behavior, _sent = service
    result = svc.sound(track)
    assert result.accepted is False
    assert "범위" in result.detail
    assert _sounds(svc._commander.tick(10_000)) == []


@pytest.mark.parametrize("track", [True, False, 17.0, "17", None])
def test_sound_refuses_non_integers(service, track):
    """`isinstance(True, int)` 가 참이라 `True` 가 트랙 1 로 들어가면 안 된다."""
    svc, _behavior, _sent = service
    assert svc.sound(track).accepted is False
    assert _sounds(svc._commander.tick(10_000)) == []


@pytest.mark.parametrize("track", [0, 1, 3000])
def test_sound_endpoint_accepts_the_range_edges(client, track):
    """0 은 정지, 3000 은 상한 — 둘 다 받는다."""
    http, _behavior, _sent = client
    body = http.post("/api/command/sound", json={"track": track}).json()
    assert body["accepted"] is True and body["command"] == "sound"


@pytest.mark.parametrize("body", [{"track": True}, {"track": "17"}, {"track": 1.5}, {}])
def test_sound_endpoint_rejects_non_integer_tracks(client, body):
    http, _behavior, _sent = client
    response = http.post("/api/command/sound", json=body)
    assert response.status_code == 400
    assert response.json() == {"error": "track"}


@pytest.mark.parametrize("track", [-1, 3001])
def test_sound_endpoint_refuses_out_of_range_tracks(client, track):
    http, _behavior, _sent = client
    body = http.post("/api/command/sound", json={"track": track}).json()
    assert body["accepted"] is False


def test_sound_endpoint_keeps_the_origin_check(client):
    http, _behavior, _sent = client
    response = http.post(
        "/api/command/sound", json={"track": 17}, headers={"origin": "http://evil.example"}
    )
    assert response.status_code == 403


def test_sound_plays_under_the_safety_latch_without_touching_it(cfg, clock):
    """**래치 중에도 나가고, 래치를 풀지 않는다** (PROTOCOL `SOUND` 절).

    가상 로봇까지 이어 본다 — 목업은 ACK 를 보내지 않으므로(`runtime._is_command_ack`)
    적용 여부 대신 **디코더 수락 · 래치 유지 · RESET_SAFE 부재** 를 본다.
    """
    from host.runtime import Runtime
    from tools.mock.mock_mechdog import MockRobot

    robot = MockRobot("mechdog-01", cfg, start_ms=clock.ms)
    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock)
    svc = CommandService(
        runtime.behavior,
        runtime.commander,
        lambda line: robot.receive(line, clock.ms),
        request_reset=runtime.ask_reset,
        apply_event=runtime.apply_external,
    )
    svc.estop()
    assert runtime.behavior.state == "FAILSAFE"
    assert robot.state(clock.ms) == "FAILSAFE"

    assert svc.sound(17).accepted is True
    lines = runtime.tick(clock.advance(100))

    assert _sounds(lines) == [17]
    assert not any(json.loads(line)["type"] == "RESET_SAFE" for line in lines)
    for line in lines:
        assert robot.receive(line, clock.ms).accepted, line
    assert robot.state(clock.ms) == "FAILSAFE", "SOUND 가 래치를 풀면 안 된다"
    assert runtime.behavior.state == "FAILSAFE"


def test_sound_endpoint_reaches_the_runtime_through_the_real_wiring(cfg, clock):
    """`dashboard_wiring` 이 만든 서비스로 HTTP → 런타임 틱 전문까지 간다."""
    from host.runtime import Runtime, dashboard_wiring

    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock)
    app = create_app(_state(), **dashboard_wiring(runtime, cfg, vision=None, blackbox=None))
    with TestClient(app) as http:
        assert http.post("/api/command/sound", json={"track": 0}).json()["accepted"] is True
    assert _sounds(runtime.tick(clock.advance(100))) == [0]


def _zone_wired(cfg, clock):
    """설정의 구역(`zones.ids` A·B·C·D)을 쓰는 런타임, 실제 배선으로 만든 서버 인자."""
    from copy import deepcopy

    from host.runtime import Runtime, dashboard_wiring

    changed = deepcopy(cfg)
    assert set(changed["zones"]["ids"]) == {"A", "B", "C", "D"}
    runtime = Runtime(changed, device_id="mechdog-01", clock=clock)
    return runtime, dashboard_wiring(runtime, changed, vision=None, blackbox=None)


# ── 위치 알려주기 (2026-10-04) ────────────────────────────────────
# 들어 옮긴 뒤 집 안 비슷한 자리를 구별 못 할 때 사람이 «지금 이 구역» 을 알려준다.
# 측위 상태는 루프 스레드만 바꾼다 — 서버 스레드는 예약만 한다.


class _FakeNavigator:
    def __init__(self):
        self.hints = []

    def locate_zone_ids(self):
        return ("B", "C", "D", "A")

    def hint_zone(self, zone, now_ms):
        self.hints.append((zone, now_ms))
        return True


def test_locate_is_applied_on_the_next_tick(cfg, clock):
    runtime, wiring = _zone_wired(cfg, clock)
    navigator = _FakeNavigator()
    runtime._navigator = navigator
    result = wiring["commands"].locate("C")
    assert result.accepted is True and result.command == "locate"
    assert navigator.hints == [], "서버 스레드는 예약만 한다"
    runtime._drain_confirmations(clock.advance(100))
    assert [zone for zone, _ in navigator.hints] == ["C"]
    runtime._drain_confirmations(clock.advance(100))
    assert len(navigator.hints) == 1, "한 번 알려준 것은 한 번만 적용한다"


@pytest.mark.parametrize("zone", ["Z", "a", "", " C", "../C"])
def test_locate_refuses_unknown_zones(cfg, clock, zone):
    runtime, wiring = _zone_wired(cfg, clock)
    navigator = _FakeNavigator()
    runtime._navigator = navigator
    result = wiring["commands"].locate(zone)
    assert result.accepted is False
    runtime._drain_confirmations(clock.advance(100))
    assert navigator.hints == []


def test_locate_refused_without_lidar_navigator(cfg, clock):
    _runtime, wiring = _zone_wired(cfg, clock)
    result = wiring["commands"].locate("A")
    assert result.accepted is False
    assert "LiDAR" in result.detail


def test_locate_is_refused_when_unwired(service):
    svc, _behavior, _sent = service
    result = svc.locate("A")
    assert result.accepted is False
    assert "연결되지 않았다" in result.detail


def test_locate_endpoint_round_trips(cfg, clock):
    from host.dashboard.server import create_app

    runtime, wiring = _zone_wired(cfg, clock)
    runtime._navigator = _FakeNavigator()
    app = create_app(_state(), wiring["commands"])
    with TestClient(app) as http:
        assert http.post("/api/command/locate", json={"zone": "D"}).json()["accepted"] is True
        assert http.post("/api/command/locate", json={"zone": 3}).status_code == 400
        assert http.post("/api/command/locate", json={}).status_code == 400


def test_policy_lists_patrol_zones(cfg):
    from host.runtime import policy_view

    assert policy_view(cfg)["patrol_zones"] == [str(z) for z in cfg["zones"]["ids"]]


# ── 지도에서 찍은 곳으로 이동 · 자기 위치 (2026-10-04) ─────────────


class _GotoNavigator(_FakeNavigator):
    pose = (1.0, 2.0, 0.0)
    pose_verified = True
    pose_seeded = False
    phase = "PLANNING"
    target = None
    current_zone = "B"
    goal = None
    holding_goal = False
    match_frac = 0.8
    plan = None

    def __init__(self, accept=True):
        super().__init__()
        self.gotos = []
        self.cancels = []
        self.accept = accept

    def pose_stale(self, _now):
        return False

    def goto(self, x, y):
        self.gotos.append((x, y))
        return self.accept, "찍은 곳으로 간다" if self.accept else "길이 없다"

    def cancel_goal(self, reason):
        self.cancels.append(reason)


def test_goto_is_planned_on_the_next_tick_and_starts_patrol(cfg, clock):
    runtime, wiring = _zone_wired(cfg, clock)
    navigator = _GotoNavigator()
    runtime._navigator = navigator
    result = wiring["commands"].goto(1.5, -0.5)
    assert result.accepted is True and result.command == "goto"
    assert navigator.gotos == [], "서버 스레드는 예약만 한다"
    runtime._drain_confirmations(clock.advance(100))
    assert navigator.gotos == [(1.5, -0.5)]
    assert runtime._patrol_asked or runtime._behavior.state == "PATROL", (
        "순찰 중이 아니면 순찰 시작을 함께 예약한다"
    )
    assert runtime.nav_status().get("starting") is True, "첫 틱 전에는 지어내지 않는다"
    runtime._nav_snapshot = runtime._build_nav_snapshot(clock.advance(100))
    status = runtime.nav_status()
    assert status["goal_feedback"]["accepted"] is True
    assert status["pose"] == [1.0, 2.0, 0.0] and status["zone"] == "B"


def test_goto_refusal_is_reported_without_starting_patrol(cfg, clock):
    runtime, wiring = _zone_wired(cfg, clock)
    runtime._navigator = _GotoNavigator(accept=False)
    wiring["commands"].goto(9.0, 9.0)
    runtime._drain_confirmations(clock.advance(100))
    assert runtime._patrol_asked is False
    runtime._nav_snapshot = runtime._build_nav_snapshot(clock.advance(100))
    assert runtime.nav_status()["goal_feedback"]["accepted"] is False


def test_stopping_patrol_cancels_the_goal(cfg, clock):
    runtime, _wiring = _zone_wired(cfg, clock)
    navigator = _GotoNavigator()
    runtime._navigator = navigator
    runtime._mark_goal_cancel("PATROL", "ALERT")
    runtime._drain_confirmations(clock.advance(100))
    assert navigator.cancels == [], "경보로 잠시 나간 것은 취소가 아니다"
    runtime._mark_goal_cancel("PATROL", "IDLE")
    runtime._drain_confirmations(clock.advance(100))
    assert navigator.cancels == ["patrol_stopped"]


def test_goto_endpoint_validates_coordinates(cfg, clock):
    from host.dashboard.server import create_app

    runtime, wiring = _zone_wired(cfg, clock)
    runtime._navigator = _GotoNavigator()
    app = create_app(_state(), wiring["commands"], nav_status=runtime.nav_status)
    with TestClient(app) as http:
        assert http.post("/api/command/goto", json={"x": 1.0, "y": "2.5"}).json()["accepted"]
        assert http.post("/api/command/goto", json={"x": "nan", "y": 0}).status_code == 400
        assert http.post("/api/command/goto", json={"x": 1.0}).status_code == 400
        assert http.get("/api/nav").json()["available"] is True
        assert http.get("/api/map/meta").status_code == 404, "지도가 없으면 지어내지 않는다"


def test_goto_refused_without_navigator(cfg, clock):
    _runtime, wiring = _zone_wired(cfg, clock)
    assert wiring["commands"].goto(1.0, 1.0).accepted is False
    assert wiring["map_view"] is None and wiring["nav_status"] is None


def test_real_stop_path_cancels_the_goal_and_pending_start(cfg, clock):
    """실제 «순찰 정지» 는 PATROL → MANUAL → IDLE (Codex 검토 G P1). 리셋 정착(FAILSAFE→IDLE)은 취소가 아니다."""
    runtime, wiring = _zone_wired(cfg, clock)
    navigator = _GotoNavigator()
    runtime._navigator = navigator
    runtime._mark_goal_cancel("FAILSAFE", "IDLE")
    runtime._drain_confirmations(clock.advance(100))
    assert navigator.cancels == []
    runtime._mark_goal_cancel("PATROL", "MANUAL")
    runtime._drain_confirmations(clock.advance(100))
    assert navigator.cancels == ["patrol_stopped"], "수동 조종으로 넘어가도 옛 목표는 버린다"
    wiring["commands"].goto(1.0, 1.0)
    runtime._mark_goal_cancel("MANUAL", "IDLE")
    runtime._drain_confirmations(clock.advance(100))
    assert navigator.gotos == [], "정지 전에 들어온 이동 요청도 버린다"
    assert runtime._patrol_asked is False


def test_nav_status_is_one_loop_snapshot(cfg, clock):
    runtime, _wiring = _zone_wired(cfg, clock)
    navigator = _GotoNavigator()
    runtime._navigator = navigator
    runtime._nav_snapshot = runtime._build_nav_snapshot(clock.advance(100))
    navigator.pose = (9.0, 9.0, 0.0)  # 서버가 읽는 사이 루프가 바꿨다
    assert runtime.nav_status()["pose"] == [1.0, 2.0, 0.0], "다음 틱 전까지는 같은 시점의 묶음"
