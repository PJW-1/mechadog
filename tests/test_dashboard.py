"""관제 서버: 실물 접근 없이 순수 상태·ASGI·실제 loopback TCP를 검증한다."""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect

from host.common.protocol import TelemetryEncoder
from host.dashboard.server import (
    DEFAULT_STATIC_DIR,
    MAX_CLIENTS,
    VISION_POLL_PERIOD_S,
    TelemetryHub,
    VisionHub,
    _send_updates,
    create_app,
    encode_vision_frame,
    running_server,
)
from host.dashboard.state import DashboardState
from host.runtime import Runtime, build_parser


def packet(*, device="mechdog-01", seq=1, boot="test-boot"):
    encoder = TelemetryEncoder(device_id=device, boot_id=boot, clock=lambda: 100)
    message = encoder.build(
        state="PATROL",
        batt_v=7.8,
        dist_cm=80,
        imu={"pitch": 1.5, "roll": -2.5, "yaw": 30},
        last_cmd_age_ms=12,
        flags={"tipped": False, "lowbatt": False, "link_ok": True},
    )
    message["seq"] = seq
    return json.dumps(message)


def state_at(clock):
    clock.ms = 0
    return DashboardState("mechdog-01", stale_after_ms=3000, clock=clock)


def test_initial_state_never_invents_healthy_readings(clock):
    snapshot = state_at(clock).snapshot()
    assert snapshot["telemetry"] is None
    assert snapshot["state"] is None
    assert snapshot["stale"] and snapshot["runtime_stale"]
    assert snapshot["telemetry_age_ms"] is None
    assert snapshot["link_rtt_ms"] is None


def test_snapshot_detaches_mutable_inputs_and_outputs(clock):
    state = state_at(clock)
    telemetry = {"imu": {"pitch": 5}}
    state.publish(telemetry=telemetry, state="IDLE", escalation="L0", mode="guard", received_at=0)
    telemetry["imu"]["pitch"] = 99
    first = state.snapshot()
    assert first["telemetry"]["imu"]["pitch"] == 5
    first["telemetry"]["imu"]["pitch"] = 88
    assert state.snapshot()["telemetry"]["imu"]["pitch"] == 5


@pytest.mark.parametrize("elapsed,stale", [(2999, False), (3000, True), (4000, True)])
def test_stale_boundary_uses_receive_time_not_broadcast(clock, elapsed, stale):
    state = state_at(clock)
    clock.ms = elapsed
    state.publish(telemetry={"seq": 1}, state="IDLE", escalation="L0", mode="guard", received_at=0)
    assert state.snapshot()["stale"] is stale
    assert state.snapshot()["telemetry_age_ms"] == elapsed
    assert state.snapshot()["runtime_stale"] is False


def test_runtime_stall_is_separate_from_telemetry(clock):
    state = state_at(clock)
    state.publish(telemetry={"seq": 1}, state="IDLE", escalation="L0", mode="guard", received_at=0)
    clock.ms = 3100
    assert state.snapshot()["runtime_stale"]
    assert state.snapshot()["runtime_age_ms"] == 3100


def test_invalid_stale_threshold():
    with pytest.raises(ValueError):
        DashboardState("test", stale_after_ms=0)


def test_runtime_preserves_angles_and_rejects_bad_foreign_replayed_packets(cfg, clock):
    state = state_at(clock)
    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock, dashboard=state)
    assert runtime.ingest(packet(), 0).accepted
    runtime.tick(0)
    first = state.snapshot()
    assert first["telemetry"]["imu"] == {"pitch": 1.5, "roll": -2.5, "yaw": 30}
    assert first["telemetry"]["batt_v"] == 7.8
    assert first["telemetry"]["last_cmd_age_ms"] == 12
    assert first["state"] == runtime.behavior.state
    for raw in [packet(device="other"), packet(), "broken"]:
        clock.ms += 100
        assert not runtime.ingest(raw, clock.ms).accepted
        runtime.tick(clock.ms)
    assert state.snapshot()["telemetry_age_ms"] == 300
    assert state.snapshot()["telemetry"] == first["telemetry"]
    assert runtime.ingest(packet(boot="second-boot"), clock.ms).accepted
    runtime.tick(clock.ms)
    assert state.snapshot()["telemetry"]["boot_id"] == "second-boot"
    assert state.snapshot()["telemetry_age_ms"] == 0


def test_dashboard_does_not_change_commands(cfg, clock):
    state = state_at(clock)
    observed = Runtime(cfg, device_id="mechdog-01", clock=clock, dashboard=state)
    baseline = Runtime(cfg, device_id="mechdog-01", clock=clock)
    for tick in range(600):
        clock.ms = tick * 100
        raw = packet(seq=tick + 1)
        observed.ingest(raw, clock.ms)
        baseline.ingest(raw, clock.ms)
        assert observed.tick(clock.ms) == baseline.tick(clock.ms)
    assert observed.stats.sent == baseline.stats.sent
    assert state.snapshot()["telemetry"]["seq"] == 600


def test_slow_subscriber_is_bounded_and_cannot_hold_up_another(clock):
    async def scenario():
        state = state_at(clock)
        hub = TelemetryHub(state)
        slow, fast = hub.subscribe(), hub.subscribe()
        for seq in range(100):
            state.publish(
                telemetry={"seq": seq}, state="IDLE", escalation="L0", mode="guard", received_at=0
            )
            hub.broadcast()
            assert fast.get_nowait()["telemetry"]["seq"] == seq
        assert slow.qsize() == 1
        assert slow.get_nowait()["telemetry"]["seq"] == 99
        assert hub.coalesced == 99

    asyncio.run(scenario())


def test_send_timeout_cancels_blocked_transport(monkeypatch):
    class BlockedSocket:
        cancelled = False

        async def send_json(self, _message):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    async def scenario():
        sock = BlockedSocket()
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait({})
        with pytest.raises(TimeoutError):
            await _send_updates(sock, queue)
        assert sock.cancelled

    monkeypatch.setattr("host.dashboard.server.SEND_TIMEOUT_S", 0.01)
    asyncio.run(scenario())


def test_http_and_multiple_websockets_cleanup(clock):
    app = create_app(state_at(clock))
    with TestClient(app) as client:
        assert client.get("/health").json()["read_only"]
        assert client.get("/api/telemetry").json()["stale"]
        with client.websocket_connect("/ws/telemetry") as first:
            with client.websocket_connect("/ws/telemetry") as second:
                assert first.receive_json() == second.receive_json()
                assert client.get("/health").json()["clients"] == 2
            assert client.get("/health").json()["clients"] == 1
        assert client.get("/health").json()["clients"] == 0
        assert client.post("/api/command", json={"type": "MOVE"}).status_code == 404


def test_read_only_socket_rejects_commands(clock):
    with (
        TestClient(create_app(state_at(clock))) as client,
        client.websocket_connect("/ws/telemetry") as ws,
    ):
        ws.send_json({"type": "RESET_SAFE"})
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                ws.receive_json()
        assert exc.value.code == 1008


def test_cross_origin_connection_rejected(clock):
    with (
        TestClient(create_app(state_at(clock))) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/telemetry", headers={"origin": "https://other.test"}),
    ):
        pytest.fail("Foreign origin accepted")


@pytest.mark.parametrize("origin", ["http://localhost", "http://127.0.0.1:80"])
def test_same_local_origin_is_accepted(clock, origin):
    with (
        TestClient(create_app(state_at(clock))) as client,
        client.websocket_connect("/ws/telemetry", headers={"origin": origin}) as ws,
    ):
        assert ws.receive_json()["type"] == "telemetry"


def test_connection_limit_and_reconnect(clock):
    app = create_app(state_at(clock))
    with TestClient(app) as client:
        with ExitStack() as stack:
            for _ in range(MAX_CLIENTS):
                stack.enter_context(client.websocket_connect("/ws/telemetry"))
            with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/telemetry"):
                pytest.fail("Too many connections accepted")
        assert client.get("/health").json()["clients"] == 0
        with client.websocket_connect("/ws/telemetry") as ws:
            assert ws.receive_json()["type"] == "telemetry"


def test_real_loopback_server_broadcasts_at_10hz_to_two_clients(cfg, record_property):
    # 포트 0을 OS에 할당받는다. 외부 주소나 로봇 UDP 소켓은 사용하지 않는다.
    state = DashboardState("mechdog-01", stale_after_ms=3000)
    runtime = Runtime(cfg, device_id="mechdog-01", dashboard=state)
    assert runtime.ingest(packet(), 0).accepted
    runtime.tick(0)
    with running_server(state, 0) as server:
        port = server.servers[0].sockets[0].getsockname()[1]

        def receive():
            with connect(f"ws://127.0.0.1:{port}/ws/telemetry", open_timeout=3) as ws:
                times = []
                for _ in range(16):
                    message = json.loads(ws.recv(timeout=2))
                    assert message["telemetry"]["imu"]["pitch"] == 1.5
                    assert message["telemetry"]["seq"] == 1
                    assert message["state"] == runtime.behavior.state
                    times.append(time.monotonic())
                return 15 / (times[-1] - times[0])

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(receive) for _ in range(2)]
            rates = [future.result(timeout=6) for future in futures]
        assert all(8 <= rate <= 12 for rate in rates), rates
        record_property("client_1_hz", rates[0])
        record_property("client_2_hz", rates[1])
    assert not any(t.name == "dashboard" for t in threading.enumerate())


def test_occupied_port_fails_before_start(clock):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        with pytest.raises(OSError), running_server(state_at(clock), sock.getsockname()[1]):
            pytest.fail("Occupied port accepted")


def test_server_lifespan_can_restart(clock):
    state = state_at(clock)
    for _ in range(2):
        with running_server(state, 0) as server:
            assert server.started


def test_dashboard_is_opt_in():
    assert build_parser().parse_args(["--device", "mechdog-01"]).dashboard_port is None


def test_cli_passes_state_and_closes_server_after_runtime(cfg, monkeypatch):
    import host.runtime as module

    events = []

    class FakeSocket:
        def close(self):
            events.append("socket_closed")

    class FakeRuntime:
        telemetry_port = 5101

        def __init__(self, _config, **kwargs):
            self.dashboard = kwargs["dashboard"]
            self.behavior = None
            self.commander = None
            self.send_immediate = lambda _line: None
            self.ask_reset = lambda: None
            # 명령은 런타임의 `_apply` 경로로 들어간다 — 대응 단계와 전이 로그가
            # 거기 묶여 있다 (2026-09-14 실기).
            self.apply_external = lambda _event: True
            self.ask_patrol = lambda: None
            self.set_mode = lambda _mode: None
            # 음성 암구호는 시도를 세는 경로로 들어간다 (FR-10.3).
            self.note_voice_auth = lambda _ok: (True, "")

        def serve(self, _sock, **_kwargs):
            assert self.dashboard is captured[0]
            events.append("runtime_finished")

    captured = []

    @contextmanager
    def fake_server(state, port, **_kwargs):
        assert port == 8000
        captured.append(state)
        events.append("server_started")
        yield
        events.append("server_closed")

    monkeypatch.setattr(module, "load_config", lambda _device: cfg)
    monkeypatch.setattr(module, "setup_logging", lambda *_args, **_kw: None)
    monkeypatch.setattr(module, "EventBlackbox", lambda _cfg: None)
    monkeypatch.setattr(module, "Runtime", FakeRuntime)
    monkeypatch.setattr(module, "open_socket", lambda _port: FakeSocket())
    monkeypatch.setattr(module.sys, "stdin", None)
    monkeypatch.setattr("host.dashboard.server.running_server", fake_server)
    assert module.main(["--device", "test", "--no-vision", "--dashboard-port", "8000"]) == 0
    assert events == ["server_started", "runtime_finished", "server_closed", "socket_closed"]


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_cli_rejects_invalid_port_before_loading_config(port, monkeypatch):
    import host.runtime as module

    def unexpected(_device):
        pytest.fail("Loaded hardware profile before rejecting port")

    monkeypatch.setattr(module, "load_config", unexpected)
    with pytest.raises(SystemExit, match="dashboard-port"):
        module.main(["--device", "test", "--dashboard-port", port])


def test_dashboard_ships_its_page_and_three_without_install(clock):
    """관제 화면과 three.js 가 저장소에 함께 실려 있다 — npm 설치 없이 떠야 한다.

    예전에는 three.js 를 `node_modules` 에서 찾았고, 설치를 빠뜨리면 app.js 가 첫
    import 에서 실패해 **화면 조작이 통째로 죽었다**(3D 만 비는 것이 아니었다).
    """
    state = DashboardState("mechdog-01", stale_after_ms=1000, clock=clock)
    with TestClient(create_app(state, static_dir=DEFAULT_STATIC_DIR)) as client:
        for path in (
            "/",
            "/app.js",
            "/factory-layout.json",
            "/vendor/three.module.js",
            "/vendor/three.core.js",
            "/vendor/addons/controls/OrbitControls.js",
        ):
            assert client.get(path).status_code == 200, path
        # 정적 마운트가 API 를 가리지 않는다.
        assert client.get("/api/telemetry").status_code == 200


# ── 검출 오버레이 (WBS 4.5.2) ───────────────────────────────────────────


def _vision_result(seq=7, jpeg=b"JPEG-BYTES"):
    """워커 결과 모양만 흉내낸다 — 서버는 속성만 읽는다."""
    return SimpleNamespace(
        frame_seq=seq,
        frame_width=640,
        frame_height=480,
        completed_ms=1234,
        jpeg=jpeg,
        detections=(
            SimpleNamespace(label="person", score=0.91234, box=(10.04, 20.0, 110.0, 300.26)),
        ),
        tracks=(SimpleNamespace(track_id=3, score=0.9, box=(10.0, 20.0, 110.0, 300.0)),),
    )


def _decode_vision(message: bytes):
    size = int.from_bytes(message[:4], "big")
    return json.loads(message[4 : 4 + size]), message[4 + size :]


def test_vision_frame_keeps_boxes_with_the_jpeg_they_were_computed_on():
    """박스와 JPEG 가 한 메시지다 — 둘로 나누면 송신 취소 때 다음 사진에 박스가 붙는다."""
    result = _vision_result()
    header, jpeg = _decode_vision(encode_vision_frame(result))
    assert jpeg == result.jpeg
    assert header == {
        "type": "vision",
        "frame_seq": 7,
        "width": 640,
        "height": 480,
        "completed_ms": 1234,
        "detections": [{"label": "person", "score": 0.912, "box": [10.0, 20.0, 110.0, 300.3]}],
        "tracks": [{"track_id": 3, "score": 0.9, "box": [10.0, 20.0, 110.0, 300.0]}],
    }


def test_vision_hub_sends_each_inference_once_and_keeps_only_the_latest():
    async def scenario():
        current = {"result": None}
        hub = VisionHub(lambda: current["result"])
        slow, fast = hub.subscribe(), hub.subscribe()
        assert not hub.broadcast()  # 결과가 아직 없다
        current["result"] = _vision_result(jpeg=b"")
        assert not hub.broadcast()  # 사진 없는 결과는 보내지 않는다
        for seq in range(5):
            current["result"] = _vision_result(seq=seq)
            assert hub.broadcast()
            assert not hub.broadcast()  # 같은 추론을 다시 보내지 않는다
            assert _decode_vision(fast.get_nowait())[0]["frame_seq"] == seq
        assert slow.qsize() == 1
        assert _decode_vision(slow.get_nowait())[0]["frame_seq"] == 4
        assert hub.coalesced == 4
        # 늦게 붙은 화면은 다음 추론을 기다리지 않고 마지막 프레임부터 받는다.
        late = hub.subscribe()
        assert _decode_vision(late.get_nowait())[0]["frame_seq"] == 4

    asyncio.run(scenario())


def test_vision_poll_is_faster_than_inference():
    """확인 주기가 추론 주기보다 길면 그 사이의 결과가 버려져 화면 fps 가 묶인다."""
    config = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    inference_fps = yaml.safe_load(config.read_text(encoding="utf-8"))["vision"]["inference_fps"]
    inference_period_s = 1.0 / inference_fps
    assert inference_period_s / 2 >= VISION_POLL_PERIOD_S


def test_vision_socket_streams_boxes_with_their_jpeg(clock):
    result = _vision_result()
    app = create_app(state_at(clock), vision=lambda: result)
    with TestClient(app) as client, client.websocket_connect("/ws/vision") as ws:
        header, jpeg = _decode_vision(ws.receive_bytes())
        assert jpeg == result.jpeg
        assert header["detections"][0]["label"] == "person"
        assert client.get("/health").json()["vision_clients"] == 1


def test_vision_channel_is_absent_without_a_vision_worker(clock):
    with TestClient(create_app(state_at(clock))) as client:
        assert client.get("/health").json()["vision_clients"] is None
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/vision"):
            pytest.fail("vision channel opened without a worker")


def test_vision_socket_rejects_foreign_origin(clock):
    app = create_app(state_at(clock), vision=_vision_result)
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/vision", headers={"origin": "https://other.test"}),
    ):
        pytest.fail("Foreign origin accepted")


# ── 사건 피드 (WBS 4.4.3 · FR-3.9) ───────────────────────────────


def _event(n: int) -> dict:
    return {"event": "person_found", "ts_ms": 1000 + n, "entry": f"e{n}"}


def test_events_reach_a_browser_that_connects_later(clock):
    """⚠️ **브라우저는 로봇보다 늦게 열리고 새로고침도 한다.**

    붙는 순간부터만 보내면 그 전에 있던 사람 감지가 화면에 영영 나오지 않는다.
    """
    state = state_at(clock)
    state.record_event(_event(1))
    state.record_event(_event(2))
    with TestClient(create_app(state)) as client, client.websocket_connect("/ws/events") as ws:
        first, second = ws.receive_json(), ws.receive_json()
        assert [first["entry"], second["entry"]] == ["e1", "e2"]
        assert first["type"] == "event" and first["seq"] == 1


def test_a_new_event_is_pushed_without_coalescing(clock):
    """⚠️ **사건은 합치지 않는다.** 텔레메트리처럼 최신만 남기면 기록이 사라진다."""
    state = state_at(clock)
    with TestClient(create_app(state)) as client, client.websocket_connect("/ws/events") as ws:
        for n in (1, 2, 3):
            state.record_event(_event(n))
        received = [ws.receive_json()["entry"] for _ in range(3)]
    assert received == ["e1", "e2", "e3"], "세 건 모두 도착해야 한다"


def test_a_dropped_event_is_announced_not_hidden(clock):
    """⚠️ **공백을 조용히 넘기지 않는다.**

    오래 끊겼으면 앞쪽이 버퍼에서 밀려 나가는데, 말하지 않으면 사람이 *"그 사이에
    아무 일도 없었다"* 고 읽는다. 사건 피드에서 그것은 거짓 안심이다.
    """
    from host.dashboard.state import EVENT_BUFFER

    state = state_at(clock)
    for n in range(EVENT_BUFFER + 5):
        state.record_event(_event(n))
    with TestClient(create_app(state)) as client, client.websocket_connect("/ws/events") as ws:
        gap = ws.receive_json()
        assert gap["type"] == "event_gap"
        assert gap["dropped"] == 5, "버퍼를 넘어 못 준 건수를 알려 준다"


def test_the_event_channel_is_read_only(clock):
    state = state_at(clock)
    with TestClient(create_app(state)) as client, client.websocket_connect("/ws/events") as ws:
        ws.send_text("drive")
        with pytest.raises(WebSocketDisconnect) as caught:
            ws.receive_json()
    assert caught.value.code == 1008


def test_health_reports_the_event_channel(clock):
    with TestClient(create_app(state_at(clock))) as client:
        health = client.get("/health").json()
    assert health["event_clients"] == 0
    assert health["events_overflowed"] == 0


def test_cli_wires_the_event_publisher_to_the_dashboard(cfg, monkeypatch):
    """⚠️ **어댑터가 있어도 CLI 가 꽂지 않으면 사건은 화면에 못 간다.**

    `4.4.3` 이 멈춰 있던 이유가 정확히 그것이다 — 블랙박스는 기록하고
    `event_publisher` 훅도 있었지만 아무도 넘기지 않아 발행이 죽어 있었다.
    어댑터를 직접 넣어 시험하면 이 구멍이 보이지 않으므로 **CLI 를 본다.**
    """
    import host.runtime as module

    captured: dict = {}

    class FakeSocket:
        def close(self):
            pass

    class FakeRuntime:
        telemetry_port = 5101

        def __init__(self, _config, **kwargs):
            captured["publisher"] = kwargs.get("event_publisher")
            captured["dashboard"] = kwargs["dashboard"]
            self.behavior = None
            self.commander = None
            self.send_immediate = lambda _line: None
            self.ask_reset = lambda: None
            self.apply_external = lambda _event: True
            self.ask_patrol = lambda: None
            self.set_mode = lambda _mode: None
            # 음성 암구호는 시도를 세는 경로로 들어간다 (FR-10.3).
            self.note_voice_auth = lambda _ok: (True, "")

        def serve(self, _sock, **_kwargs):
            pass

    @contextmanager
    def fake_server(_state, _port, **_kwargs):
        yield

    monkeypatch.setattr(module, "load_config", lambda _device: cfg)
    monkeypatch.setattr(module, "setup_logging", lambda *_args, **_kw: None)
    monkeypatch.setattr(module, "EventBlackbox", lambda _cfg: None)
    monkeypatch.setattr(module, "Runtime", FakeRuntime)
    monkeypatch.setattr(module, "open_socket", lambda _port: FakeSocket())
    monkeypatch.setattr(module.sys, "stdin", None)
    monkeypatch.setattr("host.dashboard.server.running_server", fake_server)
    assert module.main(["--device", "test", "--no-vision", "--dashboard-port", "8000"]) == 0

    publisher = captured["publisher"]
    assert callable(publisher), "CLI 가 발행 어댑터를 넘겨야 한다"
    # 실제로 그 대시보드에 꽂혔는지 본다 — 아무 데도 안 가는 함수면 의미가 없다.
    board = captured["dashboard"]
    before = board.event_seq
    publisher(
        SimpleNamespace(
            event_type="person_found",
            ts_ms=1,
            state="ALERT",
            escalation="L1",
            mode="guard",
            tracks=[{"track_id": 1}],
            detections=[],
            telemetry={},
            meta_path=Path("bb/entry-1/meta.json"),
            jpeg_path=Path("bb/entry-1/frame.jpg"),
        )
    )
    assert board.event_seq == before + 1, "넘긴 어댑터가 이 대시보드로 들어가야 한다"


def test_cli_omits_the_publisher_without_a_dashboard(cfg, monkeypatch):
    """대시보드가 없으면 발행할 곳도 없다 — 빈 어댑터를 만들지 않는다."""
    import host.runtime as module

    captured: dict = {}

    class FakeSocket:
        def close(self):
            pass

    class FakeRuntime:
        telemetry_port = 5101

        def __init__(self, _config, **kwargs):
            captured["publisher"] = kwargs.get("event_publisher")

        def serve(self, _sock, **_kwargs):
            pass

    monkeypatch.setattr(module, "load_config", lambda _device: cfg)
    monkeypatch.setattr(module, "setup_logging", lambda *_args, **_kw: None)
    monkeypatch.setattr(module, "EventBlackbox", lambda _cfg: None)
    monkeypatch.setattr(module, "Runtime", FakeRuntime)
    monkeypatch.setattr(module, "open_socket", lambda _port: FakeSocket())
    monkeypatch.setattr(module.sys, "stdin", None)
    assert module.main(["--device", "test", "--no-vision"]) == 0
    assert captured["publisher"] is None


def test_event_snapshot_route_serves_the_recorded_picture(clock):
    """사건 그림은 **지금 화면이 아니라 그때 장면**이다 (WBS 4.6.4)."""
    app = create_app(
        state_at(clock), event_snapshot=lambda entry: b"jpeg" if entry == "ok" else None
    )
    with TestClient(app) as client:
        found = client.get("/events/ok/snapshot.jpg")
        assert found.status_code == 200
        assert found.content == b"jpeg"
        assert found.headers["content-type"] == "image/jpeg"
        assert client.get("/events/nope/snapshot.jpg").status_code == 404


def test_event_snapshot_route_is_absent_without_a_resolver(clock):
    """읽기 전용으로 띄우면 사건 그림도 나가지 않는다 — 404 로 닫는다."""
    app = create_app(state_at(clock))
    with TestClient(app) as client:
        assert client.get("/events/anything/snapshot.jpg").status_code == 404


def test_event_snapshot_route_does_not_need_a_camera(clock):
    """⚠️ 카메라 없이도 지난 사건 그림은 보여야 한다 — 저장은 이미 끝났다."""
    app = create_app(state_at(clock), camera=None, event_snapshot=lambda _e: b"jpeg")
    with TestClient(app) as client:
        assert client.get("/events/x/snapshot.jpg").status_code == 200
        assert client.get("/camera/snapshot.jpg").status_code == 404


def test_events_http_polling_returns_events_after_cursor(clock):
    """음성 저널처럼 WS 를 못 쓰는 쪽의 폴링 경로다 (4.7.12)."""
    state = state_at(clock)
    state.record_event(_event(1))
    state.record_event(_event(2))
    with TestClient(create_app(state)) as client:
        body = client.get("/api/events?since=0").json()
        assert [e["entry"] for e in body["events"]] == ["e1", "e2"]
        assert body["latest"] == 2 and body["dropped"] == 0
        # 커서 뒤만 온다 — 재폴링은 중복 없이.
        body = client.get("/api/events?since=1").json()
        assert [e["entry"] for e in body["events"]] == ["e2"]


def test_events_http_reports_dropped(clock):
    """폴링 쪽도 공백을 숨기지 않는다 — WS 의 event_gap 과 같은 규약."""
    from host.dashboard.state import EVENT_BUFFER

    state = state_at(clock)
    for n in range(EVENT_BUFFER + 5):
        state.record_event(_event(n))
    with TestClient(create_app(state)) as client:
        body = client.get("/api/events?since=0").json()
        assert body["dropped"] == 5
