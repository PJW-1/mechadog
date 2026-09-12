"""관제 서버: 실물 접근 없이 순수 상태·ASGI·실제 loopback TCP를 검증한다."""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect

from host.common.protocol import TelemetryEncoder
from host.dashboard.server import (
    MAX_CLIENTS,
    TelemetryHub,
    _send_updates,
    create_app,
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
    state.publish(telemetry=telemetry, state="IDLE", escalation="L0", received_at=0)
    telemetry["imu"]["pitch"] = 99
    first = state.snapshot()
    assert first["telemetry"]["imu"]["pitch"] == 5
    first["telemetry"]["imu"]["pitch"] = 88
    assert state.snapshot()["telemetry"]["imu"]["pitch"] == 5


@pytest.mark.parametrize("elapsed,stale", [(2999, False), (3000, True), (4000, True)])
def test_stale_boundary_uses_receive_time_not_broadcast(clock, elapsed, stale):
    state = state_at(clock)
    clock.ms = elapsed
    state.publish(telemetry={"seq": 1}, state="IDLE", escalation="L0", received_at=0)
    assert state.snapshot()["stale"] is stale
    assert state.snapshot()["telemetry_age_ms"] == elapsed
    assert state.snapshot()["runtime_stale"] is False


def test_runtime_stall_is_separate_from_telemetry(clock):
    state = state_at(clock)
    state.publish(telemetry={"seq": 1}, state="IDLE", escalation="L0", received_at=0)
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
            state.publish(telemetry={"seq": seq}, state="IDLE", escalation="L0", received_at=0)
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

        def serve(self, _sock, **_kwargs):
            assert self.dashboard is captured[0]
            events.append("runtime_finished")

    captured = []

    @contextmanager
    def fake_server(state, port):
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
