"""호스트 운용 런타임 검증 (WBS 4.3.7).

**가짜 소켓으로 실시간 없이 검증한다.** `recvfrom` 안에서만 시각이 흐르게 만들면
실제 루프와 같은 순서가 되고, 60초 운용도 즉시 시험된다 (ENGINEERING_GUIDE 2.3).
"""

from __future__ import annotations

import pytest
from conftest import FakeClock

from host.behavior.escalation import Level
from host.behavior.fsm import Event
from host.common.config import ConfigError
from host.common.protocol import TelemetryEncoder
from host.runtime import Runtime, watch_console
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
) -> VisionResult:
    """런타임 통합 시험용 판정 결과."""
    return VisionResult(
        detections=(),
        frame_seq=seq,
        frame_received_ms=at_ms,
        completed_ms=at_ms,
        inference_ms=1.0,
        sighting=Sighting(
            present=present,
            changed=True,
            hits=hits,
            best_score=0.9 if hits else 0.0,
            last_seen_ms=last_seen_ms,
            box=(0.0, 0.0, 10.0, 20.0) if hits else None,
        ),
        # 추적 결과는 검출이 있을 때만 붙는다 — 보이지 않는 대상을 결과로 내보내지
        # 않는 것이 추적기의 계약이다 (`3.3.4`).
        tracks=(
            (Track(track_id=1, box=(0.0, 0.0, 10.0, 20.0), score=0.9, last_seen_ms=at_ms),)
            if hits
            else ()
        ),
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
    # 반복 의도가 틱마다 하나, 거기에 `STATE` 알림이 1회 더 실린다.
    # **틱이 빠진 것이 아니라 한 번만 보낼 것이 하나 더 나간 것이다.**
    assert stats.sent == stats.ticks + 1


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
    assert set(sock.types()) == {"STOP", "STATE", "ESTOP"}


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
    monkeypatch.setattr(runtime_module, "Runtime", CliRuntime)
    monkeypatch.setattr(runtime_module, "open_socket", lambda _port: CliSocket())

    result = runtime_module.main(["--device", DEVICE, "--duration", "0", "--xiao-ip", "192.0.2.10"])
    assert result == 0
    assert captured["vision"] is vision
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
