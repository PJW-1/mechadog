"""텔레메트리 수신 및 사건 변환 검증 (WBS 4.3.6 · FR-1.4).

**골든 픽스처를 그대로 물린다.** 수신기가 규약과 어긋나면 여기서 걸린다 —
로봇이 실제로 보낼 레코드와 같은 것을 보기 때문이다.
"""

import json

import pytest
from conftest import FIXTURES, load_jsonl

from host.behavior.commander import Commander
from host.behavior.fsm import Behavior, Event
from host.common.protocol import ONBOARD_STATES, TelemetryEncoder
from host.telemetry.receiver import (
    ONBOARD_EVENTS,
    RECOVERY_EVENTS,
    Reading,
    TelemetryReceiver,
)

#: 개체별 인코더를 **한 번만** 만든다.
#:
#: ⚠️ 매 호출마다 새 인코더를 만들면 `seq` 가 늘 1 에서 시작해 두 번째 레코드가
#: **중복으로 폐기된다.** 이 프로젝트에서 명령 쪽 헬퍼로 이미 한 번 겪은 버그이며,
#: 그때는 저전압 시험이 배터리와 무관하게 통과하는 형태로 나타났다.
_ENCODERS: dict[str, TelemetryEncoder] = {}


def record(**over) -> str:
    """정상 레코드 하나. 바꿀 필드만 넘긴다."""
    base = {
        "device_id": "mechdog-a",
        "boot_id": "boot-a-001",
        "state": "PATROL",
        "dist_cm": 180,
        "imu": {"pitch": 1.0, "roll": 0.0, "yaw": 180.0},
        "batt_v": 8.1,
        "last_cmd_age_ms": 30,
        "flags": {"lowbatt": False, "tipped": False, "link_ok": True},
    }
    base.update(over)
    device_id = base.pop("device_id")
    boot_id = base.pop("boot_id")
    flags = base.pop("flags")
    session = f"{device_id}/{boot_id}"
    encoder = _ENCODERS.setdefault(session, TelemetryEncoder(device_id=device_id, boot_id=boot_id))
    return encoder.encode(flags=flags, **base)


# ── 표 자체 ───────────────────────────────────────────────────
def test_only_onboard_states_produce_events() -> None:
    """**호스트 전용 상태는 사건을 내지 않는다.**

    `ALERT`·`TRACK` 은 우리가 `STATE` 로 내려보낸 값이 되돌아온 것이라 새 정보가
    없다. 그것으로 전이를 만들면 호스트가 자기 말을 듣고 다시 움직인다.
    """
    assert set(ONBOARD_EVENTS) <= set(ONBOARD_STATES)
    for src, dst in RECOVERY_EVENTS:
        assert src in ONBOARD_STATES and dst in ONBOARD_STATES


# ── 사건 변환 ─────────────────────────────────────────────────
def test_robot_reporting_failsafe_becomes_an_event() -> None:
    r = TelemetryReceiver()
    out = r.ingest(
        record(state="FAILSAFE", flags={"lowbatt": False, "tipped": True, "link_ok": True})
    )
    assert out.accepted
    assert Event.ONBOARD_FAILSAFE in out.events


def test_robot_reporting_avoid_becomes_an_event() -> None:
    r = TelemetryReceiver()
    out = r.ingest(record(state="AVOID", dist_cm=20))
    assert Event.ONBOARD_AVOID in out.events


@pytest.mark.parametrize(
    ("state", "flags"),
    [
        ("AVOID", {"lowbatt": False, "tipped": False, "link_ok": True}),
        ("FAILSAFE", {"lowbatt": False, "tipped": True, "link_ok": True}),
    ],
)
def test_repeated_onboard_state_is_edge_triggered(state: str, flags: dict[str, bool]) -> None:
    r = TelemetryReceiver()
    assert r.ingest(record(state=state, flags=flags)).events
    for _ in range(20):
        assert r.ingest(record(state=state, flags=flags)).events == ()


def test_new_boot_does_not_inherit_previous_onboard_state() -> None:
    r = TelemetryReceiver()
    r.ingest(record(state="AVOID", dist_cm=20, boot_id="boot-a-001"))
    rebooted = r.ingest(record(state="PATROL", boot_id="boot-a-002"))
    assert rebooted.accepted
    assert rebooted.events == ()
    assert r.last_onboard_state("mechdog-a") == "PATROL"


def test_leaving_avoid_reports_recovery() -> None:
    """**회피가 끝난 것을 호스트는 이 방법으로만 알 수 있다.**

    `AVOID → PATROL` 전이가 곧 *"전방이 clear 되어 시퀀스가 끝났다"* 는 신호다.
    """
    r = TelemetryReceiver()
    r.ingest(record(state="AVOID", dist_cm=20))
    out = r.ingest(record(state="PATROL", dist_cm=150))
    assert out.events == (Event.AVOID_CLEARED,)


def test_staying_in_patrol_produces_no_events() -> None:
    """10Hz 로 같은 상태가 계속 오는 것이 정상이다 — 매번 사건을 내면 안 된다."""
    r = TelemetryReceiver()
    r.ingest(record(state="PATROL"))
    for _ in range(20):
        assert r.ingest(record(state="PATROL")).events == ()


def test_host_only_state_in_between_does_not_lose_the_recovery() -> None:
    """`AVOID → ALERT → PATROL` 에서도 회복을 놓치지 않아야 한다.

    중간의 호스트 전용 상태를 기억해 버리면 `ALERT → PATROL` 로 보게 되고
    회복 사건이 사라진다.
    """
    r = TelemetryReceiver()
    r.ingest(record(state="AVOID", dist_cm=20))
    assert r.ingest(record(state="ALERT")).events == ()
    assert r.last_onboard_state("mechdog-a") == "AVOID", "호스트 전용 상태는 기억하지 않는다"
    assert r.ingest(record(state="PATROL")).events == (Event.AVOID_CLEARED,)


# ── ⚠️ 호스트가 안전을 판정하지 않는다 (아키텍처 1.2) ──────────
def test_low_voltage_alone_does_not_trigger_failsafe() -> None:
    """**전압이 낮아도 호스트가 페일세이프를 만들지 않는다.**

    Tier 1 을 호스트로 옮기는 것이 되고, 호스트만 `FAILSAFE` 인데 로봇은 걷는
    어긋난 상태가 된다. 값은 `Reading` 으로 올려보내고 표시는 대시보드가 한다.
    """
    r = TelemetryReceiver()
    out = r.ingest(
        record(
            state="PATROL", batt_v=6.5, flags={"lowbatt": True, "tipped": False, "link_ok": True}
        )
    )
    assert out.accepted
    assert out.events == (), "판정은 온보드 몫이다"
    assert out.reading is not None and out.reading.batt_v == 6.5
    assert out.reading.lowbatt is True, "관측값은 그대로 올려보낸다"


def test_close_obstacle_alone_does_not_trigger_avoid() -> None:
    """거리가 가깝다고 호스트가 `AVOID` 를 만들지 않는다 — 로봇이 보고해야 한다."""
    r = TelemetryReceiver()
    out = r.ingest(record(state="PATROL", dist_cm=10))
    assert out.events == ()
    assert out.reading is not None and out.reading.dist_cm == 10


# ── 폐기된 레코드는 사건을 내지 않는다 ───────────────────────
def test_discarded_records_produce_no_events() -> None:
    """**깨진 데이터로 상태를 옮기면 검증 규칙이 무의미해진다.**"""
    r = TelemetryReceiver()
    out = r.ingest(b'{"seq":1,"device_id":"a"')  # 잘린 JSON
    assert not out.accepted and out.events == () and out.discarded


def test_sequence_reversal_is_discarded_per_device() -> None:
    """개체별 seq 게이트 — 한 개체의 역전이 다른 개체를 막으면 안 된다."""
    r = TelemetryReceiver()
    enc_a = TelemetryEncoder(device_id="mechdog-a", boot_id="boot-a-001")
    enc_b = TelemetryEncoder(device_id="mechdog-b", boot_id="boot-b-001")
    common = {
        "state": "PATROL",
        "dist_cm": 100,
        "imu": {"pitch": 0.0, "roll": 0.0, "yaw": 0.0},
        "batt_v": 8.0,
        "last_cmd_age_ms": 30,
        "flags": {"lowbatt": False, "tipped": False, "link_ok": True},
    }
    for _ in range(3):
        assert r.ingest(enc_a.encode(**common)).accepted
    assert r.ingest(enc_b.encode(**common)).accepted, "다른 개체는 자기 순번으로 판정한다"


def test_unknown_state_is_discarded_with_a_warning() -> None:
    """규약에 없는 상태는 폐기 + WARN — 상태 추가를 하위 호환으로 만드는 장치다."""
    r = TelemetryReceiver()
    raw = json.loads(record(state="PATROL"))
    raw["state"] = "TELEPORTING"
    out = r.ingest(json.dumps(raw))
    assert not out.accepted and out.warns


# ── 골든 픽스처 전수 ─────────────────────────────────────────
def test_every_valid_fixture_is_accepted() -> None:
    """**로봇이 실제로 보낼 레코드와 같은 것을 본다.**"""
    r = TelemetryReceiver()
    seen_states: set[str] = set()
    for row in load_jsonl(FIXTURES / "telemetry_samples.jsonl"):
        case = row.get("_case", "")
        out = TelemetryReceiver().ingest(json.dumps(row))
        assert out.accepted, f"{case}: {out.discarded}"
        seen_states.add(out.reading.state if out.reading else "")
    assert seen_states >= ONBOARD_STATES, f"온보드 상태 전부가 픽스처에 있어야 한다: {seen_states}"
    assert r is not None


def test_every_invalid_fixture_is_rejected() -> None:
    """**수신기 하나로 파일 순서대로 먹인다.**

    일부 레코드는 **혼자서는 유효하고 순서 때문에 무효다** — `seq` 역전 사례가
    그렇다. 매번 새 수신기를 쓰면 그 사례가 통과해 버리고, 그러면 순서 게이트가
    검증되지 않는 채로 시험이 초록불이 된다.
    """
    receiver = TelemetryReceiver()
    for row in load_jsonl(FIXTURES / "telemetry_invalid.jsonl"):
        out = receiver.ingest(json.dumps(row))
        assert not out.accepted, f"{row.get('_case', '')}: 폐기돼야 한다"
        assert out.events == ()


# ── FSM 과의 연결 ────────────────────────────────────────────
def test_events_drive_the_fsm(clock) -> None:
    """수신 → 사건 → 전이가 한 줄로 이어지는지 본다."""
    from host.common.protocol import CommandEncoder

    b = Behavior(Commander(CommandEncoder(clock=clock), period_ms=100))
    r = TelemetryReceiver()
    b.event(Event.START_PATROL)
    assert b.state == "PATROL"

    for raw in (record(state="AVOID", dist_cm=20), record(state="PATROL", dist_cm=150)):
        out = r.ingest(raw)
        b.note_telemetry(clock.ms)
        for event in out.events:
            b.event(event)
        clock.advance(100)
    assert b.state == "PATROL", "회피에 들어갔다가 돌아와야 한다"

    out = r.ingest(
        record(state="FAILSAFE", flags={"lowbatt": False, "tipped": True, "link_ok": True})
    )
    for event in out.events:
        b.event(event)
    assert b.state == "FAILSAFE"


def test_reading_is_immutable() -> None:
    with pytest.raises(AttributeError):
        Reading("a", "boot-a", 1, "PATROL", 8.0, 100, False, False, True).state = "ALERT"  # type: ignore[misc]
