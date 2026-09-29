"""상황 서술 문장 생성 시험 (WBS 4.8.1).

입력은 **합성 사건**이다 — `runtime.py` 가 실제로 만드는 `judgement` dict 형태
그대로 꾸며서 준다. `3.6` 이 Phase 2 로 넘어가 실제 변화 사건이 Phase 1 에서
나지 않기 때문에(2026-09-25 결정), 여기서는 코드가 만드는 형식을 흉내 낸다.
"""

from __future__ import annotations

from host.report.situation import describe

# ── person_fallen — 쓰러짐 확정 (`runtime._observe_fallen` · `FallMonitor._confirm`) ──


def test_person_fallen_non_factory_shape() -> None:
    """비공장 판(`_observe_fallen`) 형태 — `aspect`·`still_ms`·`confirm_ms`."""
    sentence = describe(
        "person_fallen",
        {"fallen": True, "aspect": "supine", "still_ms": 3000, "confirm_ms": 3000},
    )
    assert sentence is not None
    assert "쓰러" in sentence
    assert "확인" in sentence


def test_person_fallen_factory_shape() -> None:
    """공장 판(`FallMonitor._confirm`) 형태 — `vlm_yes`·`suspect_ms`·`raw`. 문장은 같다."""
    sentence = describe(
        "person_fallen",
        {"fallen": True, "vlm_yes": 2, "suspect_ms": 5000, "raw": "yes"},
    )
    assert sentence is not None
    assert "쓰러" in sentence


def test_person_fallen_empty_judgement() -> None:
    """빈 judgement 여도 죽지 않고 문장을 낸다."""
    assert describe("person_fallen", {}) is not None


def test_person_fallen_none_judgement() -> None:
    """judgement 가 `None` 이어도 죽지 않고 문장을 낸다."""
    assert describe("person_fallen", None) is not None


# ── zone_changed — 넘어짐·통로 막힘 확정 (`_leave_zone` hazards) ──────────────


def test_zone_changed_fallen_object() -> None:
    sentence = describe(
        "zone_changed",
        {
            "zone": "A",
            "grid": [3, 3],
            "changes": [{"kind": "fallen_object", "source": "vlm"}],
            "baseline_ms": 1000,
            "baseline_snapshot": None,
        },
    )
    assert sentence == "A 구역에서 적재물이 무너졌습니다. 확인이 필요합니다."


def test_zone_changed_collapsed_load_future_name() -> None:
    """`fallen_object` 가 `collapsed_load` 로 바뀐 뒤에도 같은 문구를 낸다."""
    sentence = describe(
        "zone_changed",
        {"zone": "B", "changes": [{"kind": "collapsed_load", "source": "vlm"}]},
    )
    assert sentence == "B 구역에서 적재물이 무너졌습니다. 확인이 필요합니다."


def test_zone_changed_blocked_path() -> None:
    sentence = describe(
        "zone_changed",
        {"zone": "C", "changes": [{"kind": "blocked_path", "source": "vlm"}]},
    )
    assert sentence == "C 구역에서 통로가 막혔습니다. 확인이 필요합니다."


def test_zone_changed_two_hazards_are_separate_sentences() -> None:
    """위험이 둘이면 기호로 잇지 않고 문장을 나눈다 — 스피커로 읽힌다."""
    sentence = describe(
        "zone_changed",
        {
            "zone": "A",
            "changes": [
                {"kind": "collapsed_load", "source": "vlm"},
                {"kind": "blocked_path", "source": "vlm"},
            ],
        },
    )
    assert sentence == "A 구역에서 적재물이 무너졌습니다. 통로가 막혔습니다. 확인이 필요합니다."


def test_zone_changed_mixed_with_removed_added() -> None:
    """반출·반입이 같은 방문에서 함께 확정돼도 위험 문구만 말한다 (`_leave_zone` 주석)."""
    sentence = describe(
        "zone_changed",
        {
            "zone": "A",
            "changes": [
                {"kind": "removed", "label": "backpack", "count": 1, "cell": [0, 0]},
                {"kind": "added", "label": "box", "count": 1, "cell": [1, 1]},
                {"kind": "blocked_path", "source": "vlm"},
            ],
        },
    )
    assert sentence == "A 구역에서 통로가 막혔습니다. 확인이 필요합니다."


def test_zone_changed_unknown_hazard_kind() -> None:
    """표에 없는 새 위험 종류는 일반 문구로 처리한다."""
    sentence = describe(
        "zone_changed",
        {"zone": "A", "changes": [{"kind": "smoke_detected", "source": "vlm"}]},
    )
    assert sentence == "A 구역에서 위험이 감지되었습니다. 확인이 필요합니다."


def test_zone_changed_empty_judgement() -> None:
    """빈 judgement 여도 죽지 않는다 — 구역 이름 없이 일반 문구만."""
    assert describe("zone_changed", {}) == "위험이 감지되었습니다. 확인이 필요합니다."


def test_zone_changed_none_judgement() -> None:
    assert describe("zone_changed", None) is not None


# ── zone_notice — 반출 가벼운 경고 (`_leave_zone` Z2) ────────────────────────


def test_zone_notice_with_zone() -> None:
    sentence = describe(
        "zone_notice",
        {
            "zone": "B",
            "changes": [{"kind": "removed", "label": "toolbox", "count": 1, "cell": [2, 0]}],
        },
    )
    assert sentence == "B 구역에서 물건이 반출된 것으로 보입니다."


def test_zone_notice_empty_judgement() -> None:
    assert describe("zone_notice", {}) == "물건이 반출된 것으로 보입니다."


def test_zone_notice_none_judgement() -> None:
    assert describe("zone_notice", None) is not None


# ── 대상이 아닌 사건 — None ──────────────────────────────────────────────────


def test_non_target_events_return_none() -> None:
    for event_type in ("PPE_VIOLATION", "person_found", "zone_reading", "fallen_changed"):
        assert describe(event_type, {"anything": "여기 있어도"}) is None


def test_unknown_event_type_returns_none() -> None:
    assert describe("no_such_event", None) is None


def test_every_runtime_hazard_has_a_phrase() -> None:
    """런타임 위험 종류가 바뀌면(예: `collapsed_load`) 문구 표도 같이 바뀌어야 한다 —
    놓치면 문장이 조용히 일반 문구로 떨어진다."""
    from host.report.situation import _HAZARD_PHRASES
    from host.runtime import ZONE_HAZARDS

    assert set(ZONE_HAZARDS) <= set(_HAZARD_PHRASES)
