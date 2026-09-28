"""여러 대를 한 프로세스로 — `host.fleet` 검증.

**가짜 소켓으로 실시간 없이 검증한다** (`test_runtime.py` 와 같은 방식). 여기서 보는 것은
판단이 아니라 *나눠 쓰기* 다 — 한 소켓에 섞여 들어온 datagram 이 제 로봇에게만 가고,
각 로봇의 명령이 제 주소로만 나가는가.
"""

from __future__ import annotations

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from host.common.config import ConfigError
from host.common.logging_setup import LogContext
from host.common.protocol import TelemetryEncoder
from host.dashboard.server import create_app, create_fleet_app
from host.dashboard.state import DashboardState
from host.fleet import Fleet, FleetLogContext, _acting, load_members, telemetry_device
from host.runtime import Runtime

A, B = "mechdog-01", "mechdog-02"
A_IP, B_IP = "127.0.0.2", "127.0.0.3"


class MixedSocket:
    """여러 로봇의 datagram 이 한 소켓으로 들어온다. 시각은 여기서만 흐른다."""

    def __init__(self, clock: FakeClock, inbox: list[tuple[int, bytes, str]]) -> None:
        self.clock = clock
        self.inbox = sorted(inbox)
        self.sent: list[tuple[int, bytes, tuple[str, int]]] = []
        self._timeout = 0.0

    def settimeout(self, value: float) -> None:
        self._timeout = value

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        deadline = self.clock.ms + int(self._timeout * 1000)
        if self.inbox and self.inbox[0][0] <= deadline:
            at_ms, payload, ip = self.inbox.pop(0)
            self.clock.ms = max(self.clock.ms, at_ms)
            return payload, (ip, 5001)
        self.clock.ms = deadline
        if self._timeout == 0:
            raise BlockingIOError
        raise TimeoutError

    def sendto(self, payload: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((self.clock.ms, payload, addr))


def _config(cfg: dict, ip: str | None) -> dict:
    merged = dict(cfg)
    merged["network"] = dict(cfg["network"], mechdog_ip=ip)
    return merged


def _telemetry(device: str, start_ms: int, ip: str, count: int = 10):
    enc = TelemetryEncoder(device_id=device, boot_id="boot-1")
    base = {
        "state": "IDLE",
        "dist_cm": 180,
        "imu": {"pitch": 0.0, "roll": 0.0, "yaw": 0.0},
        "batt_v": 8.1,
        "last_cmd_age_ms": 30,
        "flags": {"lowbatt": False, "tipped": False, "link_ok": True},
    }
    return [(start_ms + i * 100, enc.encode(**base).encode("utf-8"), ip) for i in range(count)]


def test_telemetry_is_split_by_device_id_not_by_address(cfg: dict, clock: FakeClock) -> None:
    """**남의 패킷이 우리 로봇의 링크를 살려 두면 안 된다** (DR-17).

    둘 다 주소를 모르는 상태로 시작한다 — 첫 수락 텔레메트리에서 각자 제 주소를 배운다.
    """
    a = Runtime(_config(cfg, None), device_id=A, clock=clock)
    b = Runtime(_config(cfg, None), device_id=B, clock=clock)
    inbox = _telemetry(A, clock.ms + 50, A_IP) + _telemetry(B, clock.ms + 60, B_IP)
    sock = MixedSocket(clock, inbox)

    Fleet([a, b]).serve(sock, duration_s=1.2, clock=clock)

    assert (a.stats.accepted, a.stats.foreign) == (10, 0)
    assert (b.stats.accepted, b.stats.foreign) == (10, 0)
    assert a.peer == (A_IP, 5001) and b.peer == (B_IP, 5001)
    to_a = [payload for _, payload, addr in sock.sent if addr[0] == A_IP]
    to_b = [payload for _, payload, addr in sock.sent if addr[0] == B_IP]
    assert to_a and to_b
    assert {addr[0] for _, _, addr in sock.sent} == {A_IP, B_IP}


def test_unknown_device_and_stray_ack_are_dropped(cfg: dict, clock: FakeClock) -> None:
    a = Runtime(_config(cfg, A_IP), device_id=A, clock=clock)
    fleet = Fleet([a])
    stray = _telemetry("mechdog-99", clock.ms, "127.0.0.9")[0][1]

    assert fleet.route(stray, ("127.0.0.9", 5001)) is None
    # 명령 응답은 개체 ID 가 없다 — 이미 그 주소로 명령을 보내는 로봇에게만 간다.
    assert fleet.route(b"ACK seq=3", (A_IP, 5001)) is a
    assert fleet.route(b"ACK seq=3", ("127.0.0.9", 5001)) is None
    assert telemetry_device(b"not json") is None


def test_shutdown_stops_every_robot_before_releasing_any(cfg: dict, clock: FakeClock) -> None:
    """**한 대의 비전 정리를 기다리는 동안 다른 로봇이 걸으면 안 된다.**"""
    order: list[str] = []

    class Vision:
        def __init__(self, name: str) -> None:
            self.name = name

        def set_ppe_enabled(self, _on: bool) -> None: ...
        def start(self) -> None: ...
        def stop(self) -> None:
            order.append(f"release:{self.name}")

        def latest(self):
            return None

        def healthy(self) -> bool:
            return True

        def stalled(self, _now: int) -> bool:
            return False

        def age_ms(self, _now: int):
            return None

    a = Runtime(_config(cfg, A_IP), device_id=A, clock=clock, vision=Vision(A))
    b = Runtime(_config(cfg, B_IP), device_id=B, clock=clock, vision=Vision(B))

    class RecordingSocket(MixedSocket):
        def sendto(self, payload: bytes, addr: tuple[str, int]) -> None:
            super().sendto(payload, addr)
            if b"ESTOP" in payload:
                order.append(f"estop:{addr[0]}")

    Fleet([a, b]).serve(RecordingSocket(clock, []), duration_s=0.3, clock=clock)

    first_release = next(i for i, step in enumerate(order) if step.startswith("release"))
    assert set(order[:first_release]) == {f"estop:{A_IP}", f"estop:{B_IP}"}


def test_log_records_carry_the_robot_being_handled(cfg: dict, clock: FakeClock) -> None:
    fleet_ctx = FleetLogContext(device_id="fleet")
    a = Runtime(_config(cfg, A_IP), device_id=A, clock=clock, context=LogContext(device_id=A))

    assert fleet_ctx.as_dict()["device_id"] == "fleet"
    with _acting(a):
        assert fleet_ctx.as_dict()["device_id"] == A
    # 루프 밖(관제 웹 스레드 등)은 틀린 로봇 대신 플릿으로 찍힌다.
    assert fleet_ctx.as_dict()["device_id"] == "fleet"


def test_members_get_their_own_blackbox_and_registration() -> None:
    members = load_members(
        [A, "mechdog-03"], mode=None, robot_ips={A: A_IP}, xiao_ips={}, log_level=None
    )
    by_id = {m.device_id: m for m in members}

    assert by_id[A].config["network"]["mechdog_ip"] == A_IP
    assert by_id[A].config["logging"]["blackbox_dir"].endswith(f"/{A}")
    assert by_id["mechdog-03"].config["logging"]["blackbox_dir"].endswith("/mechdog-03")
    # 실물이 없는 자리는 텔레메트리 개체 ID(MAC)가 없다 — 화면에 «미등록».
    assert by_id[A].registered is True
    assert by_id["mechdog-03"].registered is False


@pytest.mark.parametrize(
    ("devices", "robot_ips"),
    [([A, A], {}), ([A], {B: B_IP})],
)
def test_bad_member_lists_are_refused(devices, robot_ips) -> None:
    with pytest.raises(ConfigError):
        load_members(devices, mode=None, robot_ips=robot_ips, xiao_ips={}, log_level=None)


def test_fleet_app_mounts_each_robot_and_runs_their_broadcast_loops(monkeypatch) -> None:
    """**붙인 앱의 lifespan 은 Starlette 가 돌려 주지 않는다** — 직접 여는지 본다."""
    started: list[str] = []

    async def fake_run(self) -> None:
        started.append(self.state.snapshot()["device_id"])

    monkeypatch.setattr("host.dashboard.server.TelemetryHub.run", fake_run)
    apps = {
        device: create_app(DashboardState(device, stale_after_ms=3000))
        for device in (A, B, "mechdog-03")
    }
    fleet_app = create_fleet_app(apps, registered={A: True, B: True, "mechdog-03": False})

    with TestClient(fleet_app) as client:
        assert sorted(started) == sorted([A, B, "mechdog-03"])
        assert client.get("/health").json()["service"] == "telemetry"
        robots = client.get("/api/fleet").json()["robots"]
        assert [r["id"] for r in robots] == [A, B, "mechdog-03"]
        assert robots[2] == {"id": "mechdog-03", "base": "/robots/mechdog-03", "registered": False}
        assert client.get(f"/robots/{B}/health").json()["device_id"] == B
        assert client.get(f"/robots/{A}/api/telemetry").json()["device_id"] == A


def test_fleet_robot_commands_keep_the_origin_check(cfg: dict, clock: FakeClock) -> None:
    """명령 경로는 한 대짜리와 같은 코드다 — 붙인 뒤에도 남의 출처를 거절한다."""
    from host.runtime import dashboard_wiring

    runtime = Runtime(_config(cfg, A_IP), device_id=A, clock=clock)
    state = DashboardState(A, stale_after_ms=3000)
    app = create_app(state, **dashboard_wiring(runtime, cfg, vision=None, blackbox=None))
    with TestClient(create_fleet_app({A: app})) as client:
        rejected = client.post(
            f"/robots/{A}/api/command/estop", headers={"origin": "https://other.test"}
        )
        accepted = client.post(f"/robots/{A}/api/command/estop")
    assert rejected.status_code == 403
    assert accepted.json()["accepted"] is True


def test_stray_websocket_closes_instead_of_crashing_the_static_mount() -> None:
    """`/` 의 정적 마운트는 http 만 안다 — 예전 한 대짜리 화면의 `/ws/telemetry`
    재연결 같은 stray WS 가 assertion 으로 서버를 어지럽히면 안 된다."""
    apps = {A: create_app(DashboardState(A, stale_after_ms=3000))}
    with TestClient(create_fleet_app(apps)) as client:
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/telemetry"):
            pass
        # 로봇 범위의 WS 는 그대로 붙는다.
        with client.websocket_connect(f"/robots/{A}/ws/telemetry") as ws:
            assert ws.receive_json()["device_id"] == A
