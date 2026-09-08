"""호스트 운용 런타임 검증 (WBS 4.3.7).

**가짜 소켓으로 실시간 없이 검증한다.** `recvfrom` 안에서만 시각이 흐르게 만들면
실제 루프와 같은 순서가 되고, 60초 운용도 즉시 시험된다 (ENGINEERING_GUIDE 2.3).
"""

from __future__ import annotations

import pytest
from conftest import FakeClock

from host.behavior.fsm import Event
from host.common.config import ConfigError
from host.common.protocol import TelemetryEncoder
from host.runtime import Runtime

DEVICE = "mechdog-01"
PEER = ("127.0.0.1", 5001)


class FakeSocket:
    """`settimeout` 만큼 기다리는 것을 흉내낸다. **시각은 여기서만 흐른다.**

    받을 것이 없으면 마감까지 시계를 밀고 `TimeoutError` 를 낸다 — 실제 소켓과
    같은 모양이라 `serve()` 를 고치지 않고 그대로 물릴 수 있다.
    """

    def __init__(self, clock: FakeClock, inbox: list[tuple[int, bytes]] | None = None) -> None:
        self.clock = clock
        self.inbox = sorted(inbox or [])
        self.sent: list[tuple[int, bytes]] = []
        self._timeout = 0.0

    def settimeout(self, seconds: float) -> None:
        self._timeout = seconds

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        deadline = self.clock.ms + int(self._timeout * 1000)
        if self.inbox and self.inbox[0][0] <= deadline:
            at_ms, payload = self.inbox.pop(0)
            self.clock.ms = max(self.clock.ms, at_ms)
            return payload, PEER
        self.clock.ms = deadline
        # ⚠️ 대기 0 은 소켓을 **비차단으로 바꾸므로** 실제로는 TimeoutError 가 아니라
        # BlockingIOError 가 온다. 이 가짜가 그것을 흉내내지 않았을 때 실기에서
        # WinError 10035 로 죽었다 — 가짜가 실물보다 친절하면 시험이 통과한다.
        if self._timeout == 0:
            raise BlockingIOError
        raise TimeoutError

    def sendto(self, data: bytes, _peer: tuple[str, int]) -> None:
        self.sent.append((self.clock.ms, data))

    # 보낸 전문에서 타입만 뽑는다
    def types(self) -> list[str]:
        import json

        return [json.loads(line)["type"] for _at, line in self.sent]


@pytest.fixture
def config(cfg: dict) -> dict:
    """개체 파일 없이 런타임을 만들 수 있는 최소 설정."""
    merged = dict(cfg)
    merged["mechdog_ip"] = PEER[0]
    return merged


def telemetry(enc: TelemetryEncoder, state: str = "PATROL", **over) -> bytes:
    base = {
        "state": state,
        "dist_cm": 180,
        "imu": {"pitch": 0.0, "roll": 0.0, "yaw": 0.0},
        "batt_v": 8.1,
        "last_cmd_age_ms": 30,
        "flags": {"lowbatt": False, "tipped": False, "link_ok": True},
    }
    base.update(over)
    return enc.encode(**base).encode("utf-8")


# ── 조립 ─────────────────────────────────────────────────────
def test_period_comes_from_config(config: dict, clock: FakeClock) -> None:
    """10Hz 는 코드가 아니라 `network.cmd_rate_hz` 에서 온다 (NFR-3①)."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    assert r.commander.period_ms == 1000 // config["network"]["cmd_rate_hz"]


def test_zero_rate_is_refused(config: dict, clock: FakeClock) -> None:
    broken = dict(config, network=dict(config["network"], cmd_rate_hz=0))
    with pytest.raises(ConfigError):
        Runtime(broken, device_id=DEVICE, clock=clock)


# ── ⚠️ 다른 개체의 패킷은 링크를 살려주지 않는다 ──────────────
def test_foreign_telemetry_does_not_refresh_the_link(config: dict, clock: FakeClock) -> None:
    """**A 의 침묵이 B 의 패킷으로 가려지면 페일세이프가 걸리지 않는다.**

    개체 판별은 `device_id` 로 한다 — 보낸 IP 로 하지 않는다 (DR-17).
    """
    r = Runtime(config, device_id=DEVICE, clock=clock)
    mine = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    other = TelemetryEncoder(device_id="mechdog-02", boot_id="boot-1")

    r.ingest(telemetry(mine), clock.ms)  # 우리 로봇이 한 번 살아 있었다
    r.start_patrol(clock.ms)
    assert r.behavior.state == "PATROL"

    # 이후 우리 로봇은 조용하고 남의 패킷만 계속 온다
    for _ in range(40):
        clock.advance(100)
        r.ingest(telemetry(other), clock.ms)
        r.tick(clock.ms)

    assert r.stats.foreign == 40
    assert r.stats.accepted == 1
    assert r.behavior.state == "FAILSAFE", "남의 패킷은 링크 시각을 갱신하지 않는다"


def test_foreign_telemetry_produces_no_events(config: dict, clock: FakeClock) -> None:
    r = Runtime(config, device_id=DEVICE, clock=clock)
    other = TelemetryEncoder(device_id="mechdog-02", boot_id="boot-1")
    r.start_patrol(clock.ms)
    out = r.ingest(telemetry(other, state="FAILSAFE"), clock.ms)
    assert not out.accepted and out.events == ()
    assert r.behavior.state == "PATROL", "남의 로봇이 넘어져도 우리 상태는 그대로다"


def test_discarded_record_does_not_refresh_the_link(config: dict, clock: FakeClock) -> None:
    """깨진 패킷을 살아 있음으로 세면 페일세이프가 걸리지 않는다 (규칙 ③)."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    mine = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    r.ingest(telemetry(mine), clock.ms)
    r.start_patrol(clock.ms)
    for _ in range(40):
        clock.advance(100)
        r.ingest(b'{"seq":9,"device_id":"mechdog-01"', clock.ms)  # 잘린 JSON
        r.tick(clock.ms)
    assert r.stats.discarded == 40
    assert r.behavior.state == "FAILSAFE"


# ── 사건 적용 ────────────────────────────────────────────────
def test_robot_failsafe_report_drives_the_host(config: dict, clock: FakeClock) -> None:
    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    r.start_patrol(clock.ms)
    r.ingest(
        telemetry(enc, state="FAILSAFE", flags={"lowbatt": False, "tipped": True, "link_ok": True}),
        clock.ms,
    )
    assert r.behavior.state == "FAILSAFE"
    assert r.stats.transitions == 1


# ── 루프 ─────────────────────────────────────────────────────
def test_loop_sends_at_the_configured_rate(config: dict, clock: FakeClock) -> None:
    """1초 동안 10Hz — 양 끝(0s·1.0s)을 포함해 11회다."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    stats = r.serve(sock, duration_s=1.0, clock=clock)
    assert stats.ticks == 11
    # 반복 의도가 틱마다 하나, 거기에 `STATE` 알림이 1회 더 실린다.
    # **틱이 빠진 것이 아니라 한 번만 보낼 것이 하나 더 나간 것이다.**
    assert stats.sent == stats.ticks + 1


def test_loop_ends_with_estop(config: dict, clock: FakeClock) -> None:
    """**종료 전문은 ESTOP 이다.** 호스트가 사라진 뒤 로봇이 계속 걷지 않게 한다."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    r.serve(sock, duration_s=0.5, clock=clock)
    assert sock.types()[-1] == "ESTOP"


def test_loop_receives_and_reacts(config: dict, clock: FakeClock) -> None:
    """수신 → 사건 → 전이 → 다음 틱의 명령까지 한 루프에서 이어진다."""
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    start = clock.ms
    inbox = [(start + 300, telemetry(enc, state="PATROL"))]
    inbox += [
        (
            start + 600,
            telemetry(
                enc,
                state="FAILSAFE",
                flags={"lowbatt": False, "tipped": True, "link_ok": True},
            ),
        )
    ]
    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock, inbox)
    r.start_patrol(clock.ms)
    stats = r.serve(sock, duration_s=1.0, clock=clock)
    assert stats.accepted == 2
    assert r.behavior.state == "FAILSAFE"
    assert stats.states.get("FAILSAFE"), "전이 뒤의 틱들은 FAILSAFE 상태로 나갔다"


def test_peer_is_learned_from_the_first_telemetry(cfg: dict, clock: FakeClock) -> None:
    """`mechdog_ip` 가 비어 있으면 첫 텔레메트리를 보낸 곳으로 답한다."""
    no_ip = dict(cfg)
    no_ip["mechdog_ip"] = None
    r = Runtime(no_ip, device_id=DEVICE, clock=clock)
    assert r.peer is None
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    sock = FakeSocket(clock, [(clock.ms + 100, telemetry(enc))])
    r.serve(sock, duration_s=0.5, clock=clock)
    assert r.peer == PEER


def test_nothing_is_sent_before_the_peer_is_known(cfg: dict, clock: FakeClock) -> None:
    """상대를 모르면 보내지 않는다 — 조용히 버리는 것이 맞다."""
    no_ip = dict(cfg)
    no_ip["mechdog_ip"] = None
    r = Runtime(no_ip, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    r.serve(sock, duration_s=0.5, clock=clock)
    assert sock.sent == []


# ── 타이머가 루프 안에서도 도는지 ────────────────────────────
def test_patrol_timer_fires_inside_the_loop(config: dict, clock: FakeClock) -> None:
    """설정의 순찰 10초·스캔 3초가 실제 루프에서 동작한다 (WBS 3.4.3)."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    r.start_patrol(clock.ms)
    r.serve(sock, duration_s=10.5, clock=clock)
    assert r.behavior.state == "SCAN"
    assert r.stats.states.get("SCAN"), "SCAN 상태로도 명령이 나갔다"


def test_start_patrol_is_explicit(config: dict, clock: FakeClock) -> None:
    """기동만으로 걷지 않는다 — `--patrol` 을 줘야 순찰이 시작된다."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    r.serve(sock, duration_s=0.5, clock=clock)
    assert r.behavior.state == "IDLE"
    assert set(sock.types()) == {"STOP", "STATE", "ESTOP"}


def test_estop_event_is_not_forged_by_the_runtime(config: dict, clock: FakeClock) -> None:
    """런타임이 `ESTOP` **사건**을 만들지 않는다 — 사람이 누른 것만 전문이 된다."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    r.start_patrol(clock.ms)
    low = telemetry(enc, batt_v=6.4, flags={"lowbatt": True, "tipped": False, "link_ok": True})
    r.ingest(low, clock.ms)
    assert r.behavior.state == "PATROL", "전압 판정은 온보드 몫이다 (아키텍처 1.2)"
    assert r.behavior.fsm.can(Event.ESTOP), "사람이 누르면 받을 준비는 되어 있다"


# ── ⚠️ 세션 개시 (PROTOCOL 4절) ───────────────────────────────
def test_first_datagram_is_stop_seq_one(config: dict, clock: FakeClock) -> None:
    """**호스트 재시작 뒤 로봇이 우리를 다시 듣게 만드는 유일한 신호다.**

    로봇은 seq 역전을 폐기하므로, seq 가 1 로 돌아간 새 프로세스는 이전 세션의 최대
    seq 를 넘을 때까지 통째로 무시된다 — 10분 운용했다면 그 뒤 10분 동안 `ESTOP`
    조차 닿지 않는다. 규약은 `STOP` seq=1 을 세션 개시로 정해 두었다.

    실제로 첫 전문이 `RESET_SAFE` 가 되어 목업이 우리 명령을 159건 폐기했다.
    """
    import json

    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    r.serve(sock, duration_s=0.3, clock=clock)
    first = json.loads(sock.sent[0][1])
    assert first["type"] == "STOP" and first["seq"] == 1


def test_session_open_survives_a_reset_request(config: dict, clock: FakeClock) -> None:
    """`--reset-on-start` 여도 세션 개시가 먼저다 — 이게 뒤집혀서 버그가 났다."""
    import json

    r = Runtime(config, device_id=DEVICE, clock=clock)
    r.request_reset()
    sock = FakeSocket(clock)
    r.serve(sock, duration_s=0.3, clock=clock)
    types = sock.types()
    first = json.loads(sock.sent[0][1])
    assert first["type"] == "STOP" and first["seq"] == 1
    assert "RESET_SAFE" in types, "리셋은 그 다음에 나간다"
    assert types.index("RESET_SAFE") > 0


def test_session_open_waits_for_an_unknown_peer(cfg: dict, clock: FakeClock) -> None:
    """상대를 늦게 알았어도 **첫 datagram 은 여전히 세션 개시**여야 한다."""
    import json

    no_ip = dict(cfg)
    no_ip["mechdog_ip"] = None
    r = Runtime(no_ip, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    sock = FakeSocket(clock, [(clock.ms + 300, telemetry(enc))])
    r.serve(sock, duration_s=1.0, clock=clock)
    first = json.loads(sock.sent[0][1])
    assert first["type"] == "STOP" and first["seq"] == 1


# ── 로봇이 우리 명령을 폐기하는 것을 알아챈다 ────────────────
def test_warns_when_the_robot_keeps_ignoring_commands(config, clock, caplog) -> None:
    """조용한 고장이라 경고가 유일한 단서다 — 로봇만 폐기 카운터를 올린다."""
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    r.serve(FakeSocket(clock), duration_s=0.2, clock=clock)  # 세션 개시를 내보낸다
    with caplog.at_level(logging.WARNING, logger="mechadog.runtime"):
        for _ in range(30):
            clock.advance(100)
            r.ingest(telemetry(enc, last_cmd_age_ms=9000), clock.ms)
    assert any("받아들이지 않는다" in m for m in caplog.messages)


def test_healthy_uptake_is_silent(config, clock, caplog) -> None:
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    r.serve(FakeSocket(clock), duration_s=0.2, clock=clock)
    with caplog.at_level(logging.WARNING, logger="mechadog.runtime"):
        for _ in range(30):
            clock.advance(100)
            r.ingest(telemetry(enc, last_cmd_age_ms=40), clock.ms)
    assert not [m for m in caplog.messages if "받아들이지 않는다" in m]


# ── 리셋 핸드셰이크 ──────────────────────────────────────────
def test_reset_waits_for_the_robot_latch_report(config: dict, clock: FakeClock) -> None:
    """**패킷 수락과 실제 안전 해제는 다르다** (PROTOCOL 2절)."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    latched = {"lowbatt": False, "tipped": True, "link_ok": True}
    r.ingest(telemetry(enc, state="FAILSAFE", flags=latched, safety_latched=True), clock.ms)
    assert r.behavior.state == "FAILSAFE"

    r.request_reset()
    clock.advance(100)
    r.ingest(telemetry(enc, state="FAILSAFE", flags=latched, safety_latched=True), clock.ms)
    assert r.behavior.state == "FAILSAFE", "로봇이 아직 잠겨 있다"

    clock.advance(100)
    clear = {"lowbatt": False, "tipped": False, "link_ok": True}
    r.ingest(telemetry(enc, state="PATROL", flags=clear, safety_latched=False), clock.ms)
    assert r.behavior.state == "IDLE", "로봇이 풀렸다고 보고한 뒤에 복귀한다"
