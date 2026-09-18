"""호스트 운용 런타임 검증 (WBS 4.3.7).

**가짜 소켓으로 실시간 없이 검증한다.** `recvfrom` 안에서만 시각이 흐르게 만들면
실제 루프와 같은 순서가 되고, 60초 운용도 즉시 시험된다 (ENGINEERING_GUIDE 2.3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeClock

from host.behavior.escalation import Level
from host.behavior.fsm import Event
from host.common.blackbox import EventBlackbox
from host.common.config import ConfigError
from host.common.protocol import TelemetryEncoder
from host.runtime import Runtime, watch_console
from host.vision.badge import Marker
from host.vision.detector import Detection
from host.vision.person import Sighting
from host.vision.tracker import Track
from host.vision.worker import VisionResult

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


class OversizedDatagramSocket(FakeSocket):
    """Windows의 WSAEMSGSIZE를 첫 수신에서 한 번 재현한다."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self._raised = False

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        if not self._raised:
            self._raised = True
            raise OSError(10040, "message too long")
        return super().recvfrom(size)


class FakeVision:
    """Runtime이 비전 워커의 수명을 실제로 소유하는지 확인하는 최소 가짜."""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.result: VisionResult | None = None
        self.alive = True
        self.is_stalled = False

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def latest(self):
        return self.result

    def healthy(self) -> bool:
        return self.alive

    def stalled(self, _now_ms: int) -> bool:
        return self.is_stalled

    def age_ms(self, _now_ms: int):
        return None if self.result is None else _now_ms - self.result.completed_ms


def vision_result(
    seq: int,
    at_ms: int,
    *,
    present: bool,
    hits: int,
    last_seen_ms: int | None,
    markers: tuple[Marker, ...] = (),
    # 기본은 **화면 중앙**이다. 구석에 두면 추종(`3.5.4`)이 켜져 `ALERT` 가
    # 아니라 `TRACK` 으로 가고, 사람 인지를 보려던 시험이 추종 시험이 된다.
    box: tuple[float, float, float, float] = (300.0, 200.0, 340.0, 400.0),
    frame_width: int = 640,
) -> VisionResult:
    """런타임 통합 시험용 판정 결과."""
    return VisionResult(
        detections=(Detection("person", 0.9, box),) if hits else (),
        jpeg=b"test-jpeg",
        frame_seq=seq,
        frame_width=frame_width,
        frame_height=480,
        frame_received_ms=at_ms,
        completed_ms=at_ms,
        inference_ms=1.0,
        sighting=Sighting(
            present=present,
            changed=True,
            hits=hits,
            best_score=0.9 if hits else 0.0,
            last_seen_ms=last_seen_ms,
            box=box if hits else None,
        ),
        # 추적 결과는 검출이 있을 때만 붙는다 — 보이지 않는 대상을 결과로 내보내지
        # 않는 것이 추적기의 계약이다 (`3.3.4`).
        tracks=((Track(track_id=1, box=box, score=0.9, last_seen_ms=at_ms),) if hits else ()),
        # 사원증은 기본적으로 보이지 않는다 — 인증 경로를 보는 시험만 넣어 준다.
        markers=markers,
    )


@pytest.fixture
def config(cfg: dict) -> dict:
    """개체 파일 없이 런타임을 만들 수 있는 최소 설정.

    ⚠️ **주소는 `network` 절 안에 넣는다.** 최상위에 넣어 주면 시험이 실물보다
    친절해져서, 코드가 최상위를 읽는 버그를 통과시킨다 — 실제로 그랬다.
    """
    merged = dict(cfg)
    merged["network"] = dict(cfg["network"], mechdog_ip=PEER[0])
    return merged


def _without_robot_ip(cfg: dict) -> dict:
    """주소를 모르는 상태 — 첫 텔레메트리로 배우는 경로를 시험한다."""
    merged = dict(cfg)
    merged["network"] = dict(cfg["network"], mechdog_ip=None)
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


def test_firmware_mac_name_is_our_robot(config: dict, clock: FakeClock) -> None:
    """⚠️ **펌웨어는 설정 이름이 아니라 MAC 으로 만든 이름을 보낸다** (`mechdog-<MAC>`).

    프로파일의 `telemetry_device_id` 로 잇지 않으면 우리 로봇의 텔레메트리를 전부 남의
    것으로 버린다 — 2026-09-12 실기에서 151건 전부가 foreign 이었다. 목업은 설정 이름을
    그대로 보내므로 그것도 받는다.
    """
    named = dict(config, telemetry_device_id="mechdog-3c8a1f333208")
    r = Runtime(named, device_id=DEVICE, clock=clock)
    firmware = TelemetryEncoder(device_id="mechdog-3c8a1f333208", boot_id="boot-1")
    mock = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    stranger = TelemetryEncoder(device_id="mechdog-aaaaaaaaaaaa", boot_id="boot-1")
    assert r.ingest(telemetry(firmware), clock.ms).accepted
    assert r.ingest(telemetry(mock), clock.ms).accepted
    assert not r.ingest(telemetry(stranger), clock.ms).accepted
    assert r.stats.foreign == 1


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


def test_command_ack_is_not_counted_as_discarded_telemetry(config: dict, clock: FakeClock) -> None:
    """⚠️ **로봇의 명령 응답은 같은 소켓으로 돌아온다** — 텔레메트리 폐기로 세지 않는다.

    세면 실기에서 폐기가 초당 10건씩 늘어 진짜 손상을 가린다. 목업은 응답을 보내지
    않아 드러나지 않았다. 전문은 펌웨어 `sendAck` 가 만드는 모양 그대로다.
    """
    r = Runtime(config, device_id=DEVICE, clock=clock)
    ack = (
        b'{"ok":true,"verdict":"ACCEPT","seq":3,"type":"MOVE","applied":true,'
        b'"safe_latched":false,"failsafe_count":0,"actuators":true}'
    )
    for _ in range(10):
        assert not r.ingest(ack, clock.ms).accepted
    assert r.stats.discarded == 0
    r.ingest(b'{"seq":9,"device_id":"mechdog-01"', clock.ms)  # 진짜 손상은 여전히 센다
    assert r.stats.discarded == 1


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
    # 순찰 시작(IDLE→PATROL)과 페일세이프 진입 둘 다 센다. **한쪽만 세면 로그도
    # 한쪽만 남는다** — 실제로 start_patrol 이 빠져 앞 6초가 IDLE 로 찍혔다.
    assert r.stats.transitions == 2


# ── 루프 ─────────────────────────────────────────────────────
def test_loop_sends_at_the_configured_rate(config: dict, clock: FakeClock) -> None:
    """1초 동안 10Hz — 양 끝(0s·1.0s)을 포함해 11회다."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    stats = r.serve(sock, duration_s=1.0, clock=clock)
    assert stats.ticks == 11
    # 반복 의도가 틱마다 하나, 거기에 **한 번만 보낼 것이 둘** 더 실린다 —
    # `STATE` 알림과 첫 단계 색(`LED` · `4.7.3`)이다.
    # **틱이 빠진 것이 아니다.**
    assert stats.sent == stats.ticks + 2


def test_loop_ends_with_estop(config: dict, clock: FakeClock) -> None:
    """종료 ESTOP은 UDP 한 건 유실에도 남도록 같은 전문을 세 번 보낸다."""
    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = FakeSocket(clock)
    r.serve(sock, duration_s=0.5, clock=clock)
    assert sock.types()[-3:] == ["ESTOP", "ESTOP", "ESTOP"]
    assert len({payload for _at, payload in sock.sent[-3:]}) == 1


def test_oversized_windows_datagram_is_dropped(config: dict, clock: FakeClock, caplog) -> None:
    """WSAEMSGSIZE 한 건이 운용 루프와 종료 ESTOP을 막지 않는다."""
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    sock = OversizedDatagramSocket(clock)
    with caplog.at_level(logging.WARNING, logger="mechadog.runtime"):
        r.serve(sock, duration_s=0.2, clock=clock)

    assert "telemetry_datagram_too_large" in [
        getattr(record, "event", None) for record in caplog.records
    ]
    assert r.stats.discarded == 1
    assert sock.types()[-3:] == ["ESTOP", "ESTOP", "ESTOP"]


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
    no_ip = _without_robot_ip(cfg)
    r = Runtime(no_ip, device_id=DEVICE, clock=clock)
    assert r.peer is None
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    sock = FakeSocket(clock, [(clock.ms + 100, telemetry(enc))])
    r.serve(sock, duration_s=0.5, clock=clock)
    assert r.peer == PEER


def test_nothing_is_sent_before_the_peer_is_known(cfg: dict, clock: FakeClock) -> None:
    """상대를 모르면 보내지 않는다 — 조용히 버리는 것이 맞다."""
    no_ip = _without_robot_ip(cfg)
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
    # `LED` 는 걷는 것과 무관하다 — 기동 단계(L0)의 색을 한 번 내려보낸 것이다 (`4.7.3`).
    assert set(sock.types()) == {"STOP", "STATE", "ESTOP", "LED"}


def test_serve_owns_vision_worker_lifecycle(config: dict, clock: FakeClock) -> None:
    """실행 진입점이 워커를 넘기면 운용 루프가 시작과 정리를 빠뜨리지 않는다."""
    vision = FakeVision()
    r = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    r.serve(FakeSocket(clock), duration_s=0.2, clock=clock)
    assert vision.starts == 1
    assert vision.stops == 1


def test_gate_release_waits_for_target_lost_timeout(config: dict, clock: FakeClock) -> None:
    """300ms 게이트 해제를 5초 대상 상실 사건으로 잘못 쓰지 않는다."""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)

    vision.result = vision_result(1, 100, present=True, hits=3, last_seen_ms=100)
    runtime.tick(100)
    assert runtime.behavior.state == "ALERT"

    vision.result = vision_result(2, 401, present=False, hits=0, last_seen_ms=100)
    runtime.tick(401)
    assert runtime.behavior.state == "ALERT", "게이트 해제는 TARGET_LOST가 아니다"

    timeout_ms = int(config["fsm"]["target_lost_timeout_s"] * 1000)
    runtime.tick(100 + timeout_ms - 1)
    assert runtime.behavior.state == "ALERT"
    runtime.tick(100 + timeout_ms)
    assert runtime.behavior.state == "PATROL"


def test_vision_stall_marks_degraded_and_recovers(config: dict, clock: FakeClock) -> None:
    """첫 프레임이 없거나 워커가 죽어도 기능 저하 상태가 실제 FSM에 반영된다."""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)

    vision.is_stalled = True
    runtime.tick(2001)
    assert runtime.behavior.degraded is True
    assert runtime.behavior.state == "PATROL", "비전 단절은 순찰을 막지 않는다"

    vision.is_stalled = False
    vision.result = vision_result(1, 2100, present=False, hits=0, last_seen_ms=None)
    runtime.tick(2100)
    assert runtime.behavior.degraded is False


def test_stalled_camera_does_not_hold_alert_forever(config: dict, clock: FakeClock) -> None:
    """마지막 양성 결과가 슬롯에 남아도 5초 뒤에는 순찰로 복귀한다."""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)

    vision.result = vision_result(1, 100, present=True, hits=3, last_seen_ms=100)
    runtime.tick(100)
    assert runtime.behavior.state == "ALERT"

    vision.is_stalled = True
    runtime.tick(2101)
    assert runtime.behavior.degraded is True
    runtime.tick(100 + int(config["fsm"]["target_lost_timeout_s"] * 1000))
    assert runtime.behavior.state == "PATROL"


def test_confirmed_person_is_recorded_once_and_published(
    config: dict, clock: FakeClock, tmp_path
) -> None:
    """확정 엣지 한 번이 JPEG·텔레메트리 저장과 대시보드 사건 한 건이 된다."""
    local = dict(config)
    local["logging"] = dict(config["logging"], blackbox_dir=str(tmp_path / "blackbox"))
    blackbox = EventBlackbox(local)
    published = []
    vision = FakeVision()
    runtime = Runtime(
        local,
        device_id=DEVICE,
        clock=clock,
        vision=vision,
        blackbox=blackbox,
        event_publisher=published.append,
    )
    encoder = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime.start_patrol(clock.ms)
    runtime.ingest(telemetry(encoder), clock.ms)

    vision.result = vision_result(1, 100, present=True, hits=3, last_seen_ms=100)
    runtime.tick(100)
    runtime.tick(150)  # 같은 결과 슬롯을 다시 읽어도 중복 기록하지 않는다

    entries = blackbox.feed()
    assert len(entries) == 1
    assert published == entries
    assert entries[0].jpeg_path is not None
    assert entries[0].jpeg_path.read_bytes() == b"test-jpeg"
    assert entries[0].state == "ALERT"
    assert entries[0].escalation == "L1"
    assert entries[0].telemetry["device_id"] == DEVICE
    assert entries[0].telemetry["available"] is True
    assert entries[0].telemetry["seq"] == 1
    assert len(entries[0].tracks) == 1
    assert entries[0].detections[0]["label"] == "person"


# ── 대응 에스컬레이션 배선 (3.8.3) ──────────────────────────
#
# 단계기 자체는 `test_escalation.py` 가 전수로 본다. 여기서 보는 것은 **연결**이다 —
# 어느 축이 어느 사건을 실제로 받는지는 런타임을 지나야만 드러난다.


def _walk_in(runtime: Runtime, vision: FakeVision, *, seq: int, at_ms: int) -> None:
    """사람이 확정된 결과를 한 번 흘려 넣는다."""
    vision.result = vision_result(seq, at_ms, present=True, hits=3, last_seen_ms=at_ms)
    runtime.tick(at_ms)


def test_confirmed_person_raises_observe_level(config: dict, clock: FakeClock) -> None:
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    assert runtime.escalation.level is Level.L0

    _walk_in(runtime, vision, seq=1, at_ms=100)
    assert runtime.escalation.level is Level.L1
    assert runtime.behavior.state == "ALERT", "두 축이 함께 움직인다"


def test_standing_unauthenticated_person_reaches_auth_request(
    config: dict, clock: FakeClock
) -> None:
    """L2 승격은 **틱이 돌아야** 일어난다 — 사건이 아니라 시간이 정한다."""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    hold_ms = int(config["escalation"]["l1_to_l2_hold_s"]) * 1000
    for i in range(hold_ms // 100 + 1):
        _walk_in(runtime, vision, seq=i + 1, at_ms=100 + i * 100)
    assert runtime.escalation.level is Level.L2


def test_auth_timeout_raises_alarm_without_passing_through_apply(
    config: dict, clock: FakeClock
) -> None:
    """⚠️ **경보로 올라가는 유일한 실제 경로다.**

    `AUTH_FAILED` 는 `AUTH_WAIT` 의 30초 상태 타이머가 `Behavior` 안에서 만든다.
    `_apply` 를 지나지 않으므로, 전이의 트리거를 보고 단계 축에 넣지 않으면
    **인증 실패가 경보를 만들지 못한다.**
    """
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    runtime.start_patrol(clock.ms)
    runtime.behavior.event(Event.PERSON_FOUND, now_ms=1000)
    runtime.behavior.event(Event.AUTH_REQUIRED, now_ms=1000)
    assert runtime.behavior.state == "AUTH_WAIT"

    timeout_ms = int(config["auth"]["timeout_s"]) * 1000
    runtime.tick(1000)
    runtime.tick(1000 + timeout_ms)
    assert runtime.behavior.last_trigger is Event.AUTH_FAILED
    assert runtime.escalation.level is Level.L3


def test_person_near_an_idle_robot_raises_no_level(config: dict, clock: FakeClock) -> None:
    """⚠️ (잠정) **대기 중인 로봇 앞을 지나간 사람으로 경보가 뜨면 안 된다.**

    대기에서는 `AUTH_WAIT` 로 갈 수 없어, 10초 머물다 떠난 사람이 곧바로 L3 가 됐다 —
    시연 준비 중 팀원 때문에 빨간 경보가 뜨고 관리자 확인이 필요했다.
    """
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    assert runtime.behavior.state == "IDLE"
    hold_ms = int(config["escalation"]["l1_to_l2_hold_s"]) * 1000
    lost_ms = int(config["fsm"]["target_lost_timeout_s"]) * 1000
    last = 100 + hold_ms + 1000
    for i, at in enumerate(range(100, last + 1, 100)):
        _stand(runtime, vision, seq=i + 1, at_ms=at)
    vision.result = vision_result(9999, last + 100, present=False, hits=0, last_seen_ms=last)
    runtime.tick(last + 100)
    runtime.tick(last + lost_ms + 100)
    assert runtime.escalation.level is Level.L0


def test_manual_takeover_stands_down_but_keeps_an_alarm(config: dict, clock: FakeClock) -> None:
    """(잠정) 수동 조종으로 넘어가면 L1·L2 는 내리고 **L3 는 남긴다** (관리자 확인만)."""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    _walk_in(runtime, vision, seq=1, at_ms=100)
    assert runtime.escalation.level is Level.L1
    runtime.behavior.event(Event.MANUAL_ON, now_ms=200)
    runtime.tick(200)
    assert runtime.escalation.level is Level.L0

    runtime.escalation.note_event("PPE_VIOLATION", 300)
    runtime.tick(400)
    assert runtime.escalation.level is Level.L3


def test_alarm_survives_the_person_leaving(config: dict, clock: FakeClock) -> None:
    """FSM 은 순찰로 돌아가도 **경보는 남는다** — 그것이 별도 축인 이유다."""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    _walk_in(runtime, vision, seq=1, at_ms=100)
    runtime.behavior.event(Event.PPE_VIOLATION, now_ms=200)
    runtime.escalation.note_event("PPE_VIOLATION", 200)

    lost_ms = int(config["fsm"]["target_lost_timeout_s"]) * 1000
    vision.result = vision_result(2, 300, present=False, hits=0, last_seen_ms=100)
    runtime.tick(300)
    runtime.tick(100 + lost_ms)
    assert runtime.behavior.state == "PATROL"
    assert runtime.escalation.level is Level.L3


def test_confirm_alarm_is_the_way_back(config: dict, clock: FakeClock) -> None:
    """**해제 수단 없는 래치는 시연을 끝낸다.** 그 수단이 여기 있다."""
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    runtime.start_patrol(clock.ms)
    runtime.escalation.note_event("PPE_VIOLATION", clock.ms)
    assert runtime.confirm_alarm(clock.ms + 1) is True
    assert runtime.escalation.level is Level.L0
    assert runtime.confirm_alarm(clock.ms + 2) is False, "두 번 눌러도 흐트러지지 않는다"


def test_onboard_failsafe_raises_f_and_reset_returns(config: dict, clock: FakeClock) -> None:
    """F 해제는 **로봇이 래치를 풀었다고 보고할 때** 일어난다 (PROTOCOL 2절)."""
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime.start_patrol(clock.ms)
    runtime.ingest(telemetry(enc, state="FAILSAFE", safety_latched=True), clock.ms)
    assert runtime.escalation.level is Level.F

    runtime.request_reset()
    runtime.ingest(telemetry(enc, state="IDLE", safety_latched=False), clock.advance(100))
    assert runtime.behavior.state == "IDLE"
    assert runtime.escalation.level is Level.L0


def test_estop_cannot_be_used_to_clear_an_alarm(config: dict, clock: FakeClock) -> None:
    """비상정지를 눌렀다 푸는 것으로 경보가 지워지면 안 된다 — L3 로 되돌아온다."""
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime.start_patrol(clock.ms)
    runtime.escalation.note_event("PPE_VIOLATION", clock.ms)
    runtime.ingest(telemetry(enc, state="FAILSAFE", safety_latched=True), clock.ms)
    assert runtime.escalation.level is Level.F

    runtime.request_reset()
    runtime.ingest(telemetry(enc, state="IDLE", safety_latched=False), clock.advance(100))
    assert runtime.escalation.level is Level.L3


def test_log_context_carries_the_live_level(config: dict, clock: FakeClock, caplog) -> None:
    """로그의 `escalation` 은 상태에서 유도한 값이 아니라 **단계기의 지금 값**이다."""
    import logging

    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    runtime.start_patrol(clock.ms)
    runtime.escalation.note_event("AUTH_FAILED", clock.ms)
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        runtime.tick(clock.ms)
    assert runtime.context.as_dict()["escalation"] == "L3"
    assert runtime.context.state == "PATROL", "상태 축은 따로 움직인다"


def test_dead_vision_worker_marks_degraded(config: dict, clock: FakeClock) -> None:
    vision = FakeVision()
    vision.alive = False
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.tick(clock.ms)
    assert runtime.behavior.degraded is True


def test_cli_builds_and_injects_vision_by_default(config: dict, monkeypatch) -> None:
    """정식 CLI가 별도 수동 조립 없이 실제 비전 경로를 붙인다."""
    import host.runtime as runtime_module

    vision = FakeVision()
    captured: dict = {}

    class CliRuntime:
        telemetry_port = 5101

        def __init__(self, cfg, **kwargs) -> None:
            captured["config"] = cfg
            captured.update(kwargs)

        def serve(self, _sock, *, duration_s=None) -> None:
            captured["duration_s"] = duration_s

    class CliSocket:
        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(runtime_module, "load_config", lambda _device: config)
    monkeypatch.setattr(runtime_module, "setup_logging", lambda *_args, **_kw: None)
    monkeypatch.setattr(runtime_module, "build_worker", lambda _cfg: vision)
    blackbox = object()
    monkeypatch.setattr(runtime_module, "EventBlackbox", lambda _cfg: blackbox)
    monkeypatch.setattr(runtime_module, "Runtime", CliRuntime)
    monkeypatch.setattr(runtime_module, "open_socket", lambda _port: CliSocket())

    result = runtime_module.main(["--device", DEVICE, "--duration", "0", "--xiao-ip", "192.0.2.10"])
    assert result == 0
    assert captured["vision"] is vision
    assert captured["blackbox"] is blackbox
    assert captured["config"]["network"]["xiao_ip"] == "192.0.2.10"
    assert captured["duration_s"] == 0.0
    assert captured["closed"] is True


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

    no_ip = _without_robot_ip(cfg)
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
    events = [getattr(r, "event", None) for r in caplog.records]
    assert "commands_ignored" in events
    detail = next(r.detail for r in caplog.records if getattr(r, "event", "") == "commands_ignored")
    assert detail["last_cmd_age_ms"] == 9000, "왜 그렇게 판단했는지가 로그에 있어야 한다"


def test_healthy_uptake_is_silent(config, clock, caplog) -> None:
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    r.serve(FakeSocket(clock), duration_s=0.2, clock=clock)
    with caplog.at_level(logging.WARNING, logger="mechadog.runtime"):
        for _ in range(30):
            clock.advance(100)
            r.ingest(telemetry(enc, last_cmd_age_ms=40), clock.ms)
    assert "commands_ignored" not in [getattr(r, "event", None) for r in caplog.records]


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


# ── ⚠️ 로그의 state 가 실제와 어긋나면 안 된다 (4.4.2) ────────
def test_start_patrol_is_logged_like_any_other_transition(config, clock, caplog) -> None:
    """**처음에 이 경로만 로깅을 우회해서 앞 6초가 `IDLE` 로 찍혔다.**

    로봇은 순찰 중이었다. 로그가 거짓을 말하면 로그가 없는 것보다 나쁘다.
    """
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        r.start_patrol(clock.ms)
    row = next(rec for rec in caplog.records if getattr(rec, "event", "") == "fsm_transition")
    assert row.detail == {"from": "IDLE", "to": "PATROL", "trigger": "START_PATROL"}
    assert r.context.state == "PATROL", "로그 컨텍스트도 함께 움직여야 한다"


def test_failsafe_entry_is_logged_at_error(config, clock, caplog) -> None:
    """페일세이프 진입은 **기능 상실**이라 ERROR 다 (레벨 정책 1.2)."""
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    r.start_patrol(clock.ms)
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        r.ingest(
            telemetry(
                enc, state="FAILSAFE", flags={"lowbatt": False, "tipped": True, "link_ok": True}
            ),
            clock.ms,
        )
    row = next(rec for rec in caplog.records if getattr(rec, "event", "") == "fsm_transition")
    assert row.levelname == "ERROR"
    assert row.detail["trigger"] == "ONBOARD_FAILSAFE"
    assert r.context.escalation == "F"


def test_repeated_robot_state_is_not_logged_every_tick(config, clock, caplog) -> None:
    """같은 상태가 10Hz 로 오는 것이 정상이다 — 매번 찍으면 로그가 그것으로 덮인다."""
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        for _ in range(30):
            clock.advance(100)
            r.ingest(telemetry(enc, state="PATROL"), clock.ms)
    reported = [rec for rec in caplog.records if getattr(rec, "event", "") == "robot_state"]
    assert len(reported) == 1, "변화 없이 30건 받았으면 한 줄이다"


def test_periodic_summary_is_emitted_once_per_second(config, clock, caplog) -> None:
    """폐기가 30% 섞여도 초당 한 줄로 모인다 — 개별 사유가 아니라 비율이 알고 싶다."""
    import logging

    r = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        for _ in range(30):  # 3초
            clock.advance(100)
            r.ingest(telemetry(enc), clock.ms)
            r.ingest(b"{broken", clock.ms)
            r.tick(clock.ms)
    digests = [rec for rec in caplog.records if getattr(rec, "event", "") == "telemetry_summary"]
    # 첫 호출은 주기를 **시작만** 하므로 t=100 의 것은 요약을 내지 않는다. 그래서
    # 3초 동안 두 줄이고, 첫 줄에는 시작 이전에 모인 카운터도 함께 실린다.
    assert len(digests) == 2
    assert digests[0].detail["accepted"] == 11
    assert digests[0].detail["discarded"] == 11
    assert digests[1].detail["accepted"] == 10, "이후 창은 정확히 1초분이다"


def test_console_keys_map_to_the_two_confirmations(config: dict, clock: FakeClock) -> None:
    """⚠️ **두 확인은 다른 키다.** 하나로 묶으면 비상정지를 푸는 조작이 경보를 지운다.

    `stream` 을 주입해 키 대응을 시험한다 — 콘솔 없이 닫을 수 있는 부분을 `tty`
    핑계로 시험 밖에 두면, 조용히 어긋난 뒤 시연에서야 드러난다.
    """
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    runtime.start_patrol(clock.ms)
    runtime.escalation.note_event("PPE_VIOLATION", clock.ms)

    watch_console(runtime, ["c\n", "\n", "q\n"])
    assert runtime.escalation.level is Level.L3, "요청만 세우고 단계는 틱이 바꾼다"
    runtime.tick(clock.advance(100))
    assert runtime.escalation.level is Level.L0


def test_console_reset_key_asks_for_reset(config: dict, clock: FakeClock) -> None:
    import json

    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime.start_patrol(clock.ms)
    runtime.ingest(telemetry(enc, state="FAILSAFE", safety_latched=True), clock.ms)

    watch_console(runtime, ["R\n"])
    lines_out = runtime.tick(clock.advance(100))
    assert any(json.loads(line)["type"] == "RESET_SAFE" for line in lines_out), (
        "확인은 틱에 실려 나간다 — 틱을 앞지르는 것은 ESTOP 하나뿐이다"
    )
    runtime.ingest(telemetry(enc, state="IDLE", safety_latched=False), clock.advance(100))
    assert runtime.escalation.level is Level.L0


# ── 사원증 인증 배선 (3.8.1) ────────────────────────────────
#
# 판정기 자체는 `test_auth.py` 가 전수로 본다. 여기서 보는 것은 **연결**이다 —
# 특히 `3.8.3` 을 올릴 때 비워 둔 `AUTH_REQUIRED` 자리가 실제로 채워졌는가.


def _badge(marker_id: int) -> tuple[Marker, ...]:
    """기본 추적 박스 (300,200)~(340,400) 안에 있는 사원증 하나.

    ⚠️ **박스 밖에 두면 인증이 사람에게 귀속되지 않는다** (FR-3.6.2) — 시험이
    조용히 "인증 안 됨" 으로 바뀐다. 기본 박스를 옮기면 여기도 함께 옮긴다.
    """
    return (Marker(marker_id=marker_id, center=(320.0, 300.0)),)


def _stand(runtime: Runtime, vision: FakeVision, *, seq: int, at_ms: int, markers=()) -> None:
    vision.result = vision_result(
        seq, at_ms, present=True, hits=3, last_seen_ms=at_ms, markers=markers
    )
    runtime.tick(at_ms)


def test_auth_request_is_issued_when_the_level_reaches_l2(config: dict, clock: FakeClock) -> None:
    """⚠️ **`3.8.3` 이 비워 둔 자리다.**

    L2 에 올라도 `AUTH_REQUIRED` 를 내는 쪽이 없으면 `AUTH_WAIT` 에 못 들어가고,
    그러면 30초 타이머가 돌지 않아 `AUTH_FAILED` 를 낼 경로가 사라진다 — L2 가
    출구 없이 남는다.
    """
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    hold_ms = int(config["escalation"]["l1_to_l2_hold_s"]) * 1000
    for i in range(hold_ms // 100 + 1):
        _stand(runtime, vision, seq=i + 1, at_ms=100 + i * 100)
    assert runtime.escalation.level is Level.L2
    assert runtime.behavior.state == "AUTH_WAIT"


def test_full_walkthrough_person_to_authenticated(config: dict, clock: FakeClock) -> None:
    """사람 등장 → 관찰 → 인증 요구 → 사원증 제시 → 정상 복귀. **한 바퀴다.**"""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    hold_ms = int(config["escalation"]["l1_to_l2_hold_s"]) * 1000

    _stand(runtime, vision, seq=1, at_ms=100)
    assert (runtime.escalation.level, runtime.behavior.state) == (Level.L1, "ALERT")

    for i in range(1, hold_ms // 100 + 1):
        _stand(runtime, vision, seq=i + 1, at_ms=100 + i * 100)
    assert (runtime.escalation.level, runtime.behavior.state) == (Level.L2, "AUTH_WAIT")

    at = 100 + hold_ms + 100
    marker_id = next(iter(config["auth"]["badge_marker_map"]))
    _stand(runtime, vision, seq=999, at_ms=at, markers=_badge(marker_id))
    assert runtime.escalation.level is Level.L0, "인증 성공은 L2 를 L0 으로 내린다"
    assert runtime.behavior.state == "PATROL"
    assert runtime.auth.holder(1, at) == config["auth"]["badge_marker_map"][marker_id]


def test_unknown_badges_exhaust_attempts_and_alarm(config: dict, clock: FakeClock) -> None:
    """등록되지 않은 사원증 2장 → `AUTH_FAILED` → L3 (FR-10.3).

    미등록 마커는 안정성 게이트(1초 창 내 3프레임)를 통과해야 시도로 센다 —
    1프레임 ArUco 오검출이 시도를 소진시키는 것을 실기에서 확인했다.
    """
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    for i in range(3):
        _stand(runtime, vision, seq=1 + i, at_ms=100 + i * 100, markers=_badge(41))
    assert runtime.escalation.level is Level.L1, "한 번 실패로는 경보가 아니다"
    for i in range(3):
        _stand(runtime, vision, seq=10 + i, at_ms=400 + i * 100, markers=_badge(42))
    assert runtime.escalation.level is Level.L3


def test_expired_session_asks_again(config: dict, clock: FakeClock) -> None:
    """FR-10.2.4 — 유효 시간이 지나면 다시 인증을 요구한다.

    ⚠️ 인증 표시를 `AUTH_OK` **사건**만으로 유지하면 만료를 놓쳐 **그 사람 앞에서는
    영원히 승격되지 않는다.** 그래서 매번 상태를 물어 반영한다.
    """
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    marker_id = next(iter(config["auth"]["badge_marker_map"]))
    _stand(runtime, vision, seq=1, at_ms=100, markers=_badge(marker_id))
    assert runtime.escalation.authenticated is True

    valid_ms = int(config["auth"]["session_valid_s"]) * 1000
    hold_ms = int(config["escalation"]["l1_to_l2_hold_s"]) * 1000
    at = 100 + valid_ms
    for i in range(hold_ms // 100 + 2):  # 만료 뒤에도 계속 서 있다
        _stand(runtime, vision, seq=100 + i, at_ms=at + i * 100)
    assert runtime.escalation.authenticated is False
    assert runtime.escalation.level is Level.L2


def test_session_dies_with_the_track(config: dict, clock: FakeClock) -> None:
    """FR-3.6.3 — 추적 ID 가 사라지면 세션도 만료된다."""
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(clock.ms)
    marker_id = next(iter(config["auth"]["badge_marker_map"]))
    _stand(runtime, vision, seq=1, at_ms=100, markers=_badge(marker_id))
    assert runtime.auth.holder(1, 100) is not None

    vision.result = vision_result(2, 200, present=False, hits=0, last_seen_ms=100)
    runtime.tick(200)
    assert runtime.auth.holder(1, 200) is None


def test_patrol_survives_a_reset_on_start(config: dict, clock: FakeClock) -> None:
    """⚠️ **실기가 잡은 결함이다.** `--reset-on-start` 와 `--patrol` 을 함께 주면
    순찰이 조용히 취소됐다.

    기동 때 해제를 요청하고 곧바로 순찰을 시작하면, 로봇이 `safety_latched=true`
    를 보고해 호스트가 `FAILSAFE` 로 따라간 뒤 **해제가 정착하면서 `IDLE` 로
    내려온다.** 로그에는 순찰을 시작했다고 적혀 있어서 더 나쁘다.
    """
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime.request_reset()
    runtime.ask_patrol()

    # 래치된 로봇이 먼저 보고한다 — 호스트가 따라가 FAILSAFE 로 간다.
    runtime.ingest(telemetry(enc, state="FAILSAFE", safety_latched=True), clock.ms)
    assert runtime.behavior.state == "FAILSAFE"
    runtime.tick(clock.advance(100))

    # 로봇이 래치를 풀면 IDLE 로 내려오고, **그때** 순찰이 시작된다.
    runtime.ingest(telemetry(enc, state="IDLE", safety_latched=False), clock.advance(100))
    assert runtime.behavior.state == "IDLE"
    runtime.tick(clock.advance(100))
    assert runtime.behavior.state == "PATROL", "해제 뒤에 순찰이 살아나야 한다"


def test_asking_patrol_twice_does_not_double_fire(config: dict, clock: FakeClock) -> None:
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    runtime.ask_patrol()
    runtime.tick(clock.ms)
    before = runtime.stats.transitions
    runtime.tick(clock.advance(100))
    assert runtime.behavior.state == "PATROL"
    assert runtime.stats.transitions == before


# ── TRACK 락온 배선 (WBS 3.5.4 · FR-3.5) ─────────────────────────


def _move(lines: list[str]) -> dict | None:
    """전문 목록에서 마지막 `MOVE` 를 꺼낸다."""
    moves = [json.loads(line) for line in lines if '"type":"MOVE"' in line]
    return moves[-1] if moves else None


def _tracking_runtime(config: dict, clock: FakeClock) -> tuple[Runtime, FakeVision]:
    vision = FakeVision()
    runtime = Runtime(config, device_id=DEVICE, clock=clock, vision=vision)
    return runtime, vision


def _sighting(runtime: Runtime, vision: FakeVision, *, seq: int, at_ms: int, box) -> None:
    vision.result = vision_result(seq, at_ms, present=True, hits=3, last_seen_ms=at_ms, box=box)
    runtime.tick(at_ms)


def test_off_center_person_moves_alert_into_track(config: dict, clock: FakeClock) -> None:
    """⚠️ **이 사건을 내는 곳이 없었다.** `TARGET_OFF_CENTER` 는 전이표에 있었지만
    아무도 발행하지 않아 `TRACK` 은 도달 불가능한 상태였다 — 3.5.4 가 병합됐는데도
    추종은 한 번도 켜지지 않았다."""
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    _sighting(runtime, vision, seq=1, at_ms=100, box=(300.0, 200.0, 340.0, 400.0))
    assert runtime.behavior.state == "ALERT", "중앙이면 경계 자세에 머문다"
    _sighting(runtime, vision, seq=2, at_ms=200, box=(20.0, 200.0, 60.0, 400.0))
    assert runtime.behavior.state == "TRACK", "화면 왼쪽 끝이면 추종으로 간다"


def test_returning_to_center_leaves_track(config: dict, clock: FakeClock) -> None:
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    _sighting(runtime, vision, seq=1, at_ms=100, box=(20.0, 200.0, 60.0, 400.0))
    assert runtime.behavior.state == "TRACK"
    _sighting(runtime, vision, seq=2, at_ms=200, box=(300.0, 200.0, 340.0, 400.0))
    assert runtime.behavior.state == "ALERT"


def test_track_turns_toward_the_target(config: dict, clock: FakeClock) -> None:
    """부호가 뒤집히면 로봇이 **대상 반대쪽으로 돈다.**

    화면 x 는 오른쪽이 양수인데 `PROTOCOL` 은 양수가 좌회전이다. 둘을 그대로
    이으면 추종이 대상을 놓치는 방향으로 간다.
    """
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    _sighting(runtime, vision, seq=1, at_ms=100, box=(20.0, 200.0, 60.0, 400.0))
    assert runtime.behavior.sequence_for("TRACK") is not None, "등록 안 되면 명령이 안 나간다"
    left = _move(runtime.tick(200))
    assert left is not None, "추종 중에는 명령이 나가야 한다"
    assert left["angle"] > 0, "왼쪽에 있으면 좌회전(양수)이다"
    assert left["step"] > 0, "제자리 회전이 불가하므로 조향에 보폭이 따라붙는다 (DR-11)"

    _sighting(runtime, vision, seq=2, at_ms=300, box=(580.0, 200.0, 620.0, 400.0))
    right = _move(runtime.tick(400))
    assert right is not None
    assert right["angle"] < 0, "오른쪽이면 우회전(음수)"


def test_track_coasts_briefly_then_stops(config: dict, clock: FakeClock) -> None:
    """짧은 검출 공백은 마지막 지시로 이어 가고, **상한을 넘으면 정지한다.**

    ⚠️ **이어 가는 이유** — 4족 보행의 흔들림이 만드는 검출 공백은 물리라 0 이 될 수
    없다. 유효기간을 `cmd_timeout_ms`(0.3초)에 묶어 두었더니 끊길 때마다 정지가 나가
    **가다말다**가 됐다 (2026-09-18 실기 · `TRACK` 구간 초당 지시 중앙값 1/25).

    ⚠️ **상한이 있는 이유** — 없으면 대상이 사라져도 `FR-3.7` 의 5초 타이머가 `TRACK`
    을 빠져나갈 때까지 낡은 각도로 맴돈다. 정지를 **보내는** 것도 같은 이유다. 아무것도
    안 보내면 로봇이 `cmd_timeout_ms` 까지 직전 명령을 유지한다.
    """
    coast = int(config["fsm"]["track_coast_ms"])
    timeout = int(config["safety"]["cmd_timeout_ms"])
    assert timeout < coast, "이 시험의 전제 — 상한은 명령 타임아웃과 별개이고 더 길다"

    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    _sighting(runtime, vision, seq=1, at_ms=100, box=(20.0, 200.0, 60.0, 400.0))
    assert runtime.behavior.state == "TRACK"
    fresh = _move(runtime.tick(200))
    assert fresh is not None and fresh["angle"] > 0

    # 명령 타임아웃은 지났지만 상한 안이다 — 회전을 포함해 그대로 이어 간다.
    coasting = _move(runtime.tick(100 + timeout + 50))
    assert coasting is not None
    assert (coasting["step"], coasting["angle"]) == (fresh["step"], fresh["angle"])

    # 상한을 넘었다.
    stale = _move(runtime.tick(100 + coast + 50))
    assert stale is not None, "보내지 않으면 로봇이 직전 명령을 유지한다 — 정지를 보낸다"
    assert (stale["step"], stale["angle"]) == (0.0, 0.0), "낡은 지시로 돌지 않는다"


def test_track_gap_is_measured_within_one_episode(config: dict, clock: FakeClock, caplog) -> None:
    """공백의 **길이**를 남긴다 — `track_coast_ms` 가 맞는지 판정할 근거다.

    ⚠️ 추종을 벗어났다 다시 들어온 구간을 가로질러 재지 않는다. 그 사이는 공백이
    아니라 **추종을 하지 않은 시간**이고, 섞이면 분포가 통째로 오염된다.
    """
    import logging

    box = (20.0, 200.0, 60.0, 400.0)
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        runtime.tick(0)  # 요약 주기를 시작만 한다
        _sighting(runtime, vision, seq=1, at_ms=100, box=box)
        _sighting(runtime, vision, seq=2, at_ms=300, box=box)  # 200ms 공백 — 센다

        # 조작자가 수동을 잡았다 놓는다. 그동안은 추종이 아니다.
        runtime.apply_external(Event.MANUAL_ON)
        runtime.apply_external(Event.MANUAL_OFF)
        vision.result = vision_result(3, 400, present=False, hits=0, last_seen_ms=None)
        runtime.tick(400)
        runtime.start_patrol(500)

        # 재진입 — 300ms 에서 800ms 까지의 500ms 는 **공백이 아니다.**
        _sighting(runtime, vision, seq=4, at_ms=800, box=box)
        _sighting(runtime, vision, seq=5, at_ms=1000, box=box)  # 200ms 공백 — 센다
        runtime.tick(1100)
    digests = [rec for rec in caplog.records if getattr(rec, "event", "") == "telemetry_summary"]
    assert digests, "1초가 지났으면 요약이 나온다"
    assert digests[0].detail["track_gap_ms_avg"] == pytest.approx(200.0)


def test_no_box_raises_no_track_event(config: dict, clock: FakeClock) -> None:
    """대상이 사라진 판단은 5초 타이머 소관이다 (FR-3.7).

    여기서 `TARGET_CENTERED` 를 내면 **대상이 없어졌는데 중앙에 들어왔다고
    보고하는 꼴**이 되어 `TRACK` 이 `ALERT` 로 되돌아간다.
    """
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    _sighting(runtime, vision, seq=1, at_ms=100, box=(20.0, 200.0, 60.0, 400.0))
    assert runtime.behavior.state == "TRACK"
    vision.result = vision_result(2, 200, present=True, hits=0, last_seen_ms=100)
    runtime.tick(200)
    assert runtime.behavior.state == "TRACK", "박스가 없다고 중앙 정렬로 보지 않는다"


def test_track_summary_keeps_the_jitter_visible(config: dict, clock: FakeClock, caplog) -> None:
    """⚠️ **부호를 그대로 평균 내면 미세진동이 0 으로 상쇄된다.**

    좌우로 떠는 것이 DoD 가 확인하라는 바로 그것인데, 평균이 0 이면 원자료만 보고
    *"편차가 없었다"* 고 읽는다. 절대값을 넣어야 실기 근거가 된다 (`3.5.4`).
    """
    import logging

    left = (20.0, 200.0, 60.0, 400.0)
    right = (580.0, 200.0, 620.0, 400.0)
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        runtime.tick(0)  # 첫 호출은 요약 주기를 **시작만** 한다
        for i in range(12):
            box = left if i % 2 else right
            _sighting(runtime, vision, seq=i + 1, at_ms=100 * (i + 1), box=box)
    digests = [rec for rec in caplog.records if getattr(rec, "event", "") == "telemetry_summary"]
    assert digests, "1초가 지났으면 요약이 나온다"
    digest = digests[0].detail
    assert digest["track_dev_px_avg"] > 200, "좌우 280px 진동이 0 으로 상쇄되면 안 된다"
    assert digest["track_angle_deg_avg"] > 10, "조향각도 마찬가지다"
    assert digest["track_off_center"] >= 2, "데드존 밖에 몇 프레임 있었는지가 남는다"


# ── 눈 LED (WBS 4.7.3 · FR-10.4) ─────────────────────────────────


def _typed(lines: list[str], type_: str) -> list[dict]:
    return [json.loads(line) for line in lines if f'"type":"{type_}"' in line]


def test_eye_led_follows_the_escalation_level(config: dict, clock: FakeClock) -> None:
    """⚠️ **색은 계산돼 있었는데 보내는 곳이 없었다.**

    `escalation.presentation()` 이 단계별 색을 내는데 그것이 로그에만 쓰여, 실기에서
    눈이 펌웨어 기본값(파랑)에 머물렀다. `4.7.3` 이 진단 스케치로만 통과했던 이유다.
    """
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)

    first = _typed(runtime.tick(100), "LED")
    assert [m["color"] for m in first] == ["blue"], "순찰은 L0 파랑이다"
    assert first[0]["blink_hz"] == 0, "상시점등은 0 이다 — 규약이 `None` 을 받지 않는다"

    assert not _typed(runtime.tick(200), "LED"), "같은 단계를 10Hz 로 도배하지 않는다"

    vision.result = vision_result(1, 300, present=True, hits=3, last_seen_ms=300)
    after = _typed(runtime.tick(300), "LED")
    assert [m["color"] for m in after] == ["yellow"], "사람을 보면 L1 노랑으로 바뀐다"


# ── 구역 변화 감지 배선 (WBS 3.6.x · FR-8) ───────────────────────

ZONE_MARKER = 7


def _zone_config(config: dict, tmp_path: Path) -> dict:
    """구역 마커를 붙이고 기준을 임시 폴더에 쓰게 한다.

    ⚠️ **저장소에 쓰지 않는다.** 기준은 디스크에 남으므로, 기본 경로를 그대로
    두면 시험이 저장소를 더럽히고 다음 시험이 남의 기준과 견주게 된다.
    """
    from copy import deepcopy

    changed = deepcopy(config)
    changed["zones"]["marker_map"] = {ZONE_MARKER: "A"}
    changed["change_detect"]["snapshot_dir"] = str(tmp_path / "snapshots")
    return changed


def _zone_frame(seq: int, at_ms: int, *, detections, markers=()) -> VisionResult:
    """사람은 없고 물건만 있는 프레임."""
    return VisionResult(
        detections=tuple(detections),
        jpeg=b"zone-jpeg",
        frame_seq=seq,
        frame_width=640,
        frame_height=480,
        frame_received_ms=at_ms,
        completed_ms=at_ms,
        inference_ms=1.0,
        sighting=Sighting(
            present=False, changed=True, hits=0, best_score=0.0, last_seen_ms=None, box=None
        ),
        tracks=(),
        markers=markers,
    )


def _thing(label: str, x: float = 100.0) -> Detection:
    return Detection(label, 0.9, (x, 100.0, x + 40.0, 200.0))


def _zone_runtime(config: dict, clock: FakeClock, tmp_path: Path):
    cfg = _zone_config(config, tmp_path)
    vision = FakeVision()
    runtime = Runtime(cfg, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(0)
    return runtime, vision, cfg


def _see(runtime, vision, *, seq, at_ms, detections, marker=True) -> None:
    vision.result = _zone_frame(
        seq,
        at_ms,
        detections=detections,
        markers=(Marker(marker_id=ZONE_MARKER, center=(320.0, 240.0)),) if marker else (),
    )
    runtime.tick(at_ms)


def test_zone_marker_moves_patrol_into_inspect(config: dict, clock: FakeClock, tmp_path: Path):
    """⚠️ **이 사건을 내는 곳이 없었다.** `ZONE_ARRIVED`·`ZONE_CLEAR`·`ZONE_CHANGED`
    가 전이표에 다 있는데 아무도 발행하지 않아 `ZONE_INSPECT` 는 도달 불가능한
    상태였다 — 3.6.x 가 병합됐는데도 변화 감지는 한 번도 돌지 않았다."""
    runtime, vision, _ = _zone_runtime(config, clock, tmp_path)
    assert runtime.behavior.state == "PATROL"
    _see(runtime, vision, seq=1, at_ms=100, detections=[_thing("chair")])
    assert runtime.behavior.state == "ZONE_INSPECT"


def test_first_visit_registers_a_baseline_and_returns_to_patrol(
    config: dict, clock: FakeClock, tmp_path: Path
):
    """FR-8.1 — 기준 없이 견주면 처음 보는 물건이 전부 반입으로 잡힌다."""
    runtime, vision, cfg = _zone_runtime(config, clock, tmp_path)
    _see(runtime, vision, seq=1, at_ms=100, detections=[_thing("chair")])
    _see(runtime, vision, seq=2, at_ms=200, detections=[_thing("chair")])
    assert runtime.behavior.state == "PATROL", "기준을 뜬 뒤에는 순찰로 돌아간다"
    saved = Path(cfg["change_detect"]["snapshot_dir"]) / "A.json"
    assert saved.exists(), "기준이 디스크에 남아야 다음 순회에서 견줄 수 있다"


def test_a_new_object_is_confirmed_and_raises_an_alarm(
    config: dict, clock: FakeClock, tmp_path: Path
):
    runtime, vision, _ = _zone_runtime(config, clock, tmp_path)
    _see(runtime, vision, seq=1, at_ms=100, detections=[_thing("chair")])
    _see(runtime, vision, seq=2, at_ms=200, detections=[_thing("chair")])  # 기준 등록
    assert runtime.behavior.state == "PATROL"

    _see(runtime, vision, seq=3, at_ms=1000, detections=[_thing("chair")], marker=False)
    changed = [_thing("chair"), _thing("bottle", 400.0)]
    # 도착 프레임은 전이만 한다 — 비교는 다음 프레임부터다.
    _see(runtime, vision, seq=4, at_ms=1100, detections=changed)
    assert runtime.behavior.state == "ZONE_INSPECT"
    _see(runtime, vision, seq=5, at_ms=1200, detections=changed)
    assert runtime.behavior.state == "ZONE_INSPECT", "한 사이클로는 확정하지 않는다 (FR-8.4)"
    assert runtime.escalation.level is Level.L0, "확정 전에는 경보로 올리지 않는다"
    _see(runtime, vision, seq=6, at_ms=1300, detections=changed)
    assert runtime.behavior.state == "ALERT", "연속 2사이클이면 확정이다"
    assert runtime.escalation.level is Level.L3, "물체 변화 확정은 L3 다"


def test_an_unchanged_zone_returns_to_patrol(config: dict, clock: FakeClock, tmp_path: Path):
    runtime, vision, _ = _zone_runtime(config, clock, tmp_path)
    _see(runtime, vision, seq=1, at_ms=100, detections=[_thing("chair")])
    _see(runtime, vision, seq=2, at_ms=200, detections=[_thing("chair")])
    _see(runtime, vision, seq=3, at_ms=1000, detections=[_thing("chair")], marker=False)
    for seq, at in ((4, 1100), (5, 1200), (6, 1300), (7, 1400)):
        _see(runtime, vision, seq=seq, at_ms=at, detections=[_thing("chair")])
    assert runtime.behavior.state == "PATROL", "변화가 없으면 구역 앞에 서 있지 않는다"
    assert runtime.escalation.level is Level.L0


def test_two_zone_markers_pick_nothing(config: dict, clock: FakeClock, tmp_path: Path):
    """⚠️ 경계에 서면 두 장이 같이 잡힌다. 아무 쪽이나 고르면 **엉뚱한 구역의
    기준과 견주어** 물건이 통째로 사라졌다고 보고한다."""
    from copy import deepcopy

    cfg = deepcopy(_zone_config(config, tmp_path))
    cfg["zones"]["marker_map"] = {ZONE_MARKER: "A", ZONE_MARKER + 1: "B"}
    vision = FakeVision()
    runtime = Runtime(cfg, device_id=DEVICE, clock=clock, vision=vision)
    runtime.start_patrol(0)
    vision.result = _zone_frame(
        1,
        100,
        detections=[_thing("chair")],
        markers=(
            Marker(marker_id=ZONE_MARKER, center=(200.0, 240.0)),
            Marker(marker_id=ZONE_MARKER + 1, center=(440.0, 240.0)),
        ),
    )
    runtime.tick(100)
    assert runtime.behavior.state == "PATROL", "한 장만 보일 때까지 기다린다"


def test_inspection_holds_still(config: dict, clock: FakeClock, tmp_path: Path):
    """⚠️ 걸어 들어가면서 찍으면 기준과 현재가 다른 자리에서 찍힌다."""
    runtime, vision, _ = _zone_runtime(config, clock, tmp_path)
    vision.result = _zone_frame(
        1, 100, detections=[_thing("chair")], markers=(Marker(ZONE_MARKER, (320.0, 240.0)),)
    )
    lines = runtime.tick(100)
    assert runtime.behavior.state == "ZONE_INSPECT"
    move = _move(lines)
    assert move is not None, "보내지 않으면 로봇이 직전 순찰 명령을 유지한다"
    assert (move["step"], move["angle"]) == (0.0, 0.0)


# ── 사건이 관제 화면까지 닿는가 (WBS 4.4.3) ──────────────────────


def test_a_confirmed_person_reaches_the_dashboard_event_feed(
    config: dict, clock: FakeClock, tmp_path: Path
) -> None:
    """⚠️ **저장만으로는 완료가 아니다.**

    블랙박스는 디스크에 남기고 사람은 화면을 본다. 발행 연결이 없으면 기록은
    쌓이는데 아무도 모른다 — `4.4.3` 이 그 상태로 멈춰 있었다.

    여기서는 CLI 가 붙이는 어댑터(`_publish_event`)를 그대로 써서 **사람 확정 →
    블랙박스 기록 → 관제 사건 버퍼**가 이어지는지 본다.
    """
    from copy import deepcopy

    from host.common.blackbox import EventBlackbox
    from host.dashboard.state import DashboardState
    from host.runtime import _publish_event

    cfg = deepcopy(config)
    cfg["logging"]["blackbox_dir"] = str(tmp_path / "blackbox")
    blackbox = EventBlackbox(cfg)
    board = DashboardState("mechdog-02", stale_after_ms=3000)

    vision = FakeVision()
    runtime = Runtime(
        cfg,
        device_id=DEVICE,
        clock=clock,
        vision=vision,
        blackbox=blackbox,
        event_publisher=_publish_event(board),
    )
    runtime.start_patrol(clock.ms)
    _stand(runtime, vision, seq=1, at_ms=100)

    events, dropped = board.events_since(0)
    assert dropped == 0
    assert len(events) == 1, "확정 검출 한 번이 사건 하나가 된다"
    event = events[0]
    assert event["event"] == "person_found"
    assert event["type"] == "event" and event["seq"] == 1
    # 비주 대상도 함께 남는다 (FR-3.8.4).
    assert event["tracks"], "추적 목록이 비어 있으면 옆에 있던 사람이 기록에서 사라진다"
    # ⚠️ JPEG 바이트가 아니라 가리키는 이름이다 — 사건 소켓은 상태 전문과 같은 길이다.
    assert isinstance(event["snapshot"], str | type(None))
    assert "/" not in event["entry"] and "\\" not in event["entry"], "절대 경로를 보내지 않는다"
    assert not any(isinstance(v, bytes) for v in event.values()), "바이트를 싣지 않는다"


def test_the_gate_edge_publishes_one_event_not_one_per_tick(
    config: dict, clock: FakeClock, tmp_path: Path
) -> None:
    """10Hz 로 같은 사람을 보는 동안 사건이 쌓이면 피드가 쓸모없어진다."""
    from copy import deepcopy

    from host.common.blackbox import EventBlackbox
    from host.dashboard.state import DashboardState
    from host.runtime import _publish_event

    cfg = deepcopy(config)
    cfg["logging"]["blackbox_dir"] = str(tmp_path / "blackbox")
    board = DashboardState("mechdog-02", stale_after_ms=3000)
    vision = FakeVision()
    runtime = Runtime(
        cfg,
        device_id=DEVICE,
        clock=clock,
        vision=vision,
        blackbox=EventBlackbox(cfg),
        event_publisher=_publish_event(board),
    )
    runtime.start_patrol(clock.ms)
    for i in range(5):
        _stand(runtime, vision, seq=i + 1, at_ms=100 + i * 100)
    assert board.event_seq == 1, "게이트의 거짓→참 엣지에서만 한 번이다"


def test_alert_reentry_can_still_start_tracking(config: dict, clock: FakeClock) -> None:
    """추종 구간을 나갔다 들어오면 **첫 판정이 다시 사건이 된다.**

    ⚠️ 엣지 기억을 지우지 않으면 재진입 첫 판정이 *"변화 없음"* 으로 삼켜져
    `TARGET_OFF_CENTER` 가 나가지 않는다. `3.5.3` 이 미착수라 `ALERT` 에는 시퀀스가
    없어 로봇이 **정지한 채 갇힌다** — 2026-09-18 실기에서 편차 84px 대상을 앞에 두고
    33초를 서 있다가 `TARGET_LOST` 로 빠져나왔다.
    """
    off_centre = (20.0, 200.0, 60.0, 400.0)
    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    _sighting(runtime, vision, seq=1, at_ms=100, box=off_centre)
    assert runtime.behavior.state == "TRACK"

    # 조작자가 수동을 잡았다 놓는다 — 추종 구간을 벗어나는 가장 흔한 경로다.
    runtime.apply_external(Event.MANUAL_ON)
    runtime.apply_external(Event.MANUAL_OFF)
    vision.result = vision_result(2, 200, present=False, hits=0, last_seen_ms=None)
    runtime.tick(200)
    runtime.start_patrol(300)

    # 대상이 **데드존 밖 같은 쪽**에 다시 선다. 즉 `centered` 값이 직전과 같다.
    _sighting(runtime, vision, seq=3, at_ms=400, box=off_centre)
    assert runtime.behavior.state == "TRACK", "재진입 첫 판정이 삼켜지면 ALERT 에 갇힌다"


def test_engage_threshold_is_wider_than_the_steering_deadzone(
    config: dict, clock: FakeClock
) -> None:
    """⚠️ **경계에서 `ALERT ⇄ TRACK` 이 왕복하던 것을 막는다 (2026-09-18 실기).**

    bbox 중심이 데드존 경계를 ±5px 로 넘나들며 한 초에 3왕복했다. 진입 임계를
    이탈 임계보다 넓게 두면 그 사이 구간에서 상태가 유지된다.
    """
    deadzone = config["fsm"]["track_deadzone_px"]
    engage = config["fsm"]["track_engage_px"]
    assert deadzone < engage, "이 시험의 전제 — 진입이 이탈보다 넓다"
    between = (deadzone + engage) / 2  # 40 과 50 사이

    def box_at(offset: float) -> tuple[float, float, float, float]:
        centre = 320.0 + offset
        return (centre - 20.0, 200.0, centre + 20.0, 400.0)

    runtime, vision = _tracking_runtime(config, clock)
    runtime.start_patrol(0)
    _sighting(runtime, vision, seq=1, at_ms=100, box=box_at(0))
    assert runtime.behavior.state == "ALERT"

    # 두 임계 사이 — 아직 들어가지 않는다.
    _sighting(runtime, vision, seq=2, at_ms=200, box=box_at(between))
    assert runtime.behavior.state == "ALERT", "진입 임계를 넘어야 추종을 연다"

    # 진입 임계 밖 — 들어간다.
    _sighting(runtime, vision, seq=3, at_ms=300, box=box_at(engage + 10))
    assert runtime.behavior.state == "TRACK"

    # 다시 두 임계 사이 — 나오지 않는다. **떨림이 왕복을 만들지 못한다.**
    _sighting(runtime, vision, seq=4, at_ms=400, box=box_at(between))
    assert runtime.behavior.state == "TRACK", "이탈은 좁은 데드존이 정한다"


def test_yaw_rate_folds_the_compass_wrap(config: dict, clock: FakeClock, caplog) -> None:
    """방위가 359° 에서 1° 로 넘어가도 **+2° 회전**이지 −358° 가 아니다.

    ⚠️ yaw 를 그대로 평균 내면 경계 한 번에 요약이 통째로 망가진다. 좌우 대칭을
    보려고 싣는 값이라 **부호도 살아 있어야** 한다 (`3.5.4`).
    """
    import logging

    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        runtime.tick(0)  # 요약 주기를 시작만 한다
        for i, yaw in enumerate((359.0, 1.0, 3.0), start=1):
            runtime.ingest(telemetry(enc, imu={"pitch": 0.0, "roll": 0.0, "yaw": yaw}), 100 * i)
        runtime.tick(1100)
    digests = [rec for rec in caplog.records if getattr(rec, "event", "") == "telemetry_summary"]
    assert digests, "1초가 지났으면 요약이 나온다"
    # 359 -> 1 -> 3 은 100ms 마다 +2° 이므로 +20 °/s 다.
    assert digests[0].detail["yaw_rate_deg_s_avg"] == pytest.approx(20.0)
