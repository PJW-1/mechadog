"""SERVICE·ACTION 가드 프로브의 판정 로직 (WBS 3.2.4 · 실기 확인 도구).

**로봇 없이 검증한다.** 소켓은 레코드를 나르기만 하고 판정은 전부 `ingest` 와
`ack_for` 에 있다 (CONTRIBUTING 5.2 HAL 분리). 이 도구가 틀리면 재부팅한 기체를
"통과" 로 적는다 — 검증 도구가 거짓말하면 검증 자체가 없는 것보다 나쁘다.
"""

from __future__ import annotations

import pytest

from tools.service_action_probe import Tap, ack_for


@pytest.fixture
def tap() -> Tap:
    """포트 0 = 임시 포트. 소켓은 열되 **패킷은 주고받지 않는다.**"""
    made = Tap(port=0)
    yield made
    made.close()


def row(boot: str = "b1", **fields) -> dict:
    return {"boot_id": boot, "seq": 1, "flags": fields.pop("flags", {}), **fields}


# ── 재부팅 계수 ───────────────────────────────────────────────


def test_same_boot_id_counts_as_one_boot(tap: Tap) -> None:
    """같은 값이 10Hz 로 계속 와도 부팅은 한 번이다."""
    for i in range(30):
        tap.ingest(row("b1"), float(i))
    assert tap.boot_ids == ["b1"]


def test_changed_boot_id_is_a_reboot(tap: Tap) -> None:
    """⚠️ **이 한 줄이 이 도구의 전부다.**

    `boot_id` 가 바뀌었다는 것은 펌웨어가 다시 부팅했다는 뜻이다. SERVICE 중
    ACTION 이 워치독을 넘겨 보드를 재부팅시키면 정확히 여기서 잡힌다.
    """
    for boot in ("b1", "b1", "b2", "b2"):
        tap.ingest(row(boot), 0.0)
    assert tap.boot_ids == ["b1", "b2"]
    assert len(tap.boot_ids) - 1 == 1, "재부팅 1회"


def test_missing_boot_id_is_not_counted(tap: Tap) -> None:
    """`boot_id` 가 빠진 레코드를 재부팅으로 읽으면 멀쩡한 기체가 실패로 남는다."""
    tap.ingest(row("b1"), 0.0)
    tap.ingest({"seq": 2}, 0.1)
    tap.ingest(row("b1"), 0.2)
    assert tap.boot_ids == ["b1"]


# ── 필드 읽기 ─────────────────────────────────────────────────


def test_field_reads_the_nested_flag(tap: Tap) -> None:
    """`service` 는 최상위가 아니라 `flags` 안에 있다 — 최상위만 보면 영영 `None`."""
    tap.ingest(row("b1", flags={"service": True, "link_ok": True}), 0.0)
    assert tap.field("service") is True
    assert tap.field("boot_id") == "b1"


def test_field_prefers_the_top_level_key(tap: Tap) -> None:
    tap.ingest(row("b1", state="IDLE", flags={"state": "STALE"}), 0.0)
    assert tap.field("state") == "IDLE"


def test_field_without_any_record(tap: Tap) -> None:
    """텔레메트리가 한 건도 안 왔으면 `None` 이다 — 기본값으로 때우지 않는다."""
    assert tap.field("service") is None


def test_field_reads_the_latest_record(tap: Tap) -> None:
    """SERVICE 이탈 판정은 최신 한 건을 본다."""
    tap.ingest(row("b1", flags={"service": True}), 0.0)
    tap.ingest(row("b1", flags={"service": False}), 0.1)
    assert tap.field("service") is False


# ── 관찰 창 ───────────────────────────────────────────────────


def test_since_keeps_only_records_after_the_mark(tap: Tap) -> None:
    """ACTION 이전 레코드가 섞이면 "끊기지 않았다" 가 거짓이 된다."""
    for at in (0.0, 1.0, 2.0, 3.0):
        tap.ingest(row("b1"), at)
    assert len(tap.since(1.5)) == 2
    assert len(tap.since(0.0)) == 4


# ── ACK 고르기 ────────────────────────────────────────────────


def test_ack_for_ignores_the_stop_stream() -> None:
    """⚠️ **`applied` 만 세면 안 된다.**

    이 도구는 10Hz 로 STOP 을 계속 보내고 그 ACK 는 전부 `applied=true` 다.
    그 안에서 `applied=true` 를 세면 ACTION 이 거부됐든 말든 늘 통과한다 —
    실제로 2026-09-16 검증 중에 그렇게 한 번 잘못 셌다.
    """
    acks = [
        {"type": "STOP", "applied": True},
        {"type": "ACTION", "applied": False},
        {"type": "STOP", "applied": True},
    ]
    found = ack_for(acks, "ACTION")
    assert found is not None
    assert found["applied"] is False


def test_ack_for_takes_the_first_match() -> None:
    """한 번만 보낸 명령의 ACK 는 하나다. 뒤에 같은 형이 또 오면 다른 시행이다."""
    acks = [{"type": "SERVICE", "service_mode": True}, {"type": "SERVICE", "service_mode": False}]
    assert ack_for(acks, "SERVICE")["service_mode"] is True


def test_ack_for_returns_none_when_absent() -> None:
    """응답이 없으면 `None` 이다 — 없는 것을 통과로 읽으면 안 된다."""
    assert ack_for([{"type": "STOP", "applied": True}], "ACTION") is None
    assert ack_for([], "ACTION") is None
