"""PPE 합격 기준 C1~C4 자동 판정(`tools/ppe/acceptance_judge.py`) 검증.

⚠️ **기준은 측정 전에 고정했다(2026-10-05).** 임계값은 코드가 아니라
`config/ppe_acceptance.json` 에 있고, 여기서는 합성 세션으로 경계마다 합·불을 본다.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ppe import acceptance_judge as aj  # noqa: E402
from tools.ppe.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
    load_acceptance_plan,
)

ORIENTATIONS = ["정면", "우측", "후면", "좌측"]
SPECS, STEP_S, _ = load_acceptance_plan(DEFAULT_ACCEPTANCE_PLAN, "xiao")


def build_session(
    *,
    alarms: dict[tuple[str, str], int] | None = None,
    unknown_in: dict[str, int] | None = None,
    skip: tuple[str, ...] = (),
) -> dict:
    """11구간 × 4방향 세션. 기본은 위반 구간마다 전 방향 확정, 적합 구간은 무경보.

    alarms: (구간, 방향) → 그 칸의 확정 에지 수. 주면 기본값을 덮어쓴다.
    unknown_in: 구간 → 확인불가 판정 수(나머지 10건은 기대 상태).
    """
    alarms = alarms or {}
    unknown_in = unknown_in or {}
    events: list[dict] = []
    segments: dict[str, dict] = {}
    t = 0.0
    for spec in SPECS:
        key, expected = spec["key"], spec["expected"]
        if key in skip:
            continue
        unknown = unknown_in.get(key, 0)
        verdicts = {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: unknown}
        for ori in ORIENTATIONS:
            default = 1 if expected == STATE_VIOLATION else 0
            count = alarms.get((key, ori), default)
            for i in range(10):
                t = round(t + 0.1, 1)
                state = STATE_UNKNOWN if unknown and i == 0 and ori == "정면" else expected
                if expected == STATE_UNKNOWN:
                    state = STATE_UNKNOWN
                verdicts[state] += 1
                events.append(
                    {
                        "t": t,
                        "tag": f"{len(events):05d}",
                        "people": 1,
                        "states": [state],
                        "reasons": [],
                        "labels": [],
                        "confirmed": i < count,
                        "hits": 3 if i < count else 0,
                        "segment": key,
                        "expected": expected,
                        "orientation": ori,
                    }
                )
        segments[key] = {"verdicts": verdicts, "frames": 40, "seconds": 60.0}
    return {
        "schema_version": 1,
        "device": "mechdog-01",
        "scenario": "xiao",
        "settings": {"model_sha256": "abc", "window_ms": 1500, "hits_required": 3},
        "segments": segments,
        "events": events,
    }


def verdicts_of(session: dict, criteria: dict | None = None) -> dict[str, aj.Criterion]:
    criteria = criteria or aj.load_criteria(DEFAULT_ACCEPTANCE_PLAN, "xiao")
    return {c.key: c for c in aj.judge_session(session, SPECS, criteria, STEP_S)}


def test_all_clean_passes_every_criterion():
    result = verdicts_of(build_session())
    assert set(result) == {"C1", "C2", "C3", "C4"}
    assert all(c.passed for c in result.values()), [c.detail for c in result.values()]


def test_thresholds_come_from_plan_file():
    criteria = aj.load_criteria(DEFAULT_ACCEPTANCE_PLAN, "xiao")
    assert criteria == {
        "max_ok_segment_alarms": 1,
        "min_violation_directions": 3,
        "min_decidable_rate": 0.6,
        "max_clipped_alarms": 0,
        "clipped_segment": "clipped-base",
    }


def test_c1_boundary_one_false_alarm_passes_two_fail():
    one = verdicts_of(build_session(alarms={("standing-all", "정면"): 1}))
    assert one["C1"].passed
    two = verdicts_of(build_session(alarms={("standing-all", "정면"): 1, ("sit", "우측"): 1}))
    assert not two["C1"].passed
    assert "2" in two["C1"].detail


def test_c1_counts_only_ok_segments_not_violation_segments():
    session = build_session(alarms={("standing-nohelmet", "정면"): 5})
    assert verdicts_of(session)["C1"].passed


def test_c2_three_of_four_directions_passes_two_fails():
    base = {(("standing-novest"), ori): 1 for ori in ORIENTATIONS}
    base[("standing-novest", "후면")] = 0
    assert verdicts_of(build_session(alarms=base))["C2"].passed
    base[("standing-novest", "좌측")] = 0
    result = verdicts_of(build_session(alarms=base))["C2"]
    assert not result.passed
    assert "standing-novest" in result.detail


def test_c2_alarm_carried_over_from_previous_direction_counts():
    """창이 방향 전환에서 이어지면 뒤 방향엔 새 에지가 없다 — 놓친 것이 아니다."""
    session = build_session(alarms={("crouching-none", ori): 0 for ori in ORIENTATIONS})
    events = session["events"]
    first = next(
        i
        for i, e in enumerate(events)
        if e["segment"] == "crouching-none" and e["orientation"] == "정면"
    )
    events[first]["confirmed"] = True
    for e in events:
        if e["segment"] == "crouching-none":
            e["hits"] = 3  # 구간 내내 창이 켜진 채 이어진다
    assert verdicts_of(session)["C2"].passed


def test_c2_unobserved_violation_segment_fails():
    result = verdicts_of(build_session(skip=("standing-none",)))["C2"]
    assert not result.passed
    assert "standing-none" in result.detail


def _fixed_verdicts(session: dict, unknown: int) -> None:
    """전신 구간 10개의 판정 집계를 구간당 40건으로 맞추고, 한 구간에 확인불가를 몰아 둔다."""
    for spec in SPECS:
        if spec["key"] == "clipped-base":
            continue
        session["segments"][spec["key"]]["verdicts"] = {
            STATE_VIOLATION: 0,
            STATE_OK: 40,
            STATE_UNKNOWN: 0,
        }
    session["segments"]["standing-all"]["verdicts"] = {
        STATE_VIOLATION: 0,
        STATE_OK: 40 - unknown,
        STATE_UNKNOWN: unknown,
    }


def test_c3_boundary_at_sixty_percent():
    # 전신 구간 10개 = 400건. 확인불가 160건(4구간) → 가능률 60.0% 통과, 200건 → 50% 실패.
    session = build_session()
    _fixed_verdicts(session, 40)
    session["segments"]["crouching-all"]["verdicts"] = {
        STATE_VIOLATION: 0,
        STATE_OK: 0,
        STATE_UNKNOWN: 40,
    }
    for key in ("standing-nohelmet", "standing-novest"):
        session["segments"][key]["verdicts"] = {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 40}
    result = verdicts_of(session)["C3"]
    assert result.passed, result.detail
    assert "240/400" in result.detail
    session["segments"]["crouching-novest"]["verdicts"] = {
        STATE_VIOLATION: 0,
        STATE_OK: 0,
        STATE_UNKNOWN: 40,
    }
    assert not verdicts_of(session)["C3"].passed


def test_c3_ignores_clipped_base_unknowns():
    session = build_session()
    session["segments"]["clipped-base"]["verdicts"][STATE_UNKNOWN] = 10_000
    assert verdicts_of(session)["C3"].passed


def test_c3_without_judgements_fails_not_crashes():
    session = build_session()
    session["segments"] = {}
    assert not verdicts_of(session)["C3"].passed


def test_c4_any_alarm_in_clipped_base_fails():
    assert verdicts_of(build_session())["C4"].passed
    result = verdicts_of(build_session(alarms={("clipped-base", "후면"): 1}))["C4"]
    assert not result.passed


def test_missing_criteria_key_is_rejected(tmp_path):
    plan = json.loads(DEFAULT_ACCEPTANCE_PLAN.read_text(encoding="utf-8"))
    broken = copy.deepcopy(plan)
    del broken["scenarios"]["xiao"]["criteria"]["min_decidable_rate"]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="min_decidable_rate"):
        aj.load_criteria(path, "xiao")


def test_scenario_without_criteria_is_rejected():
    with pytest.raises(ValueError, match="기준"):
        aj.load_criteria(DEFAULT_ACCEPTANCE_PLAN, "webcam")


def test_cli_prints_pass_fail_and_sets_exit_code(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(build_session(), ensure_ascii=False), encoding="utf-8")
    assert aj.main([str(good)]) == 0
    out = capsys.readouterr().out
    for key in ("C1", "C2", "C3", "C4"):
        assert f"{key} PASS" in out
    assert "전체 PASS" in out

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(build_session(alarms={("clipped-base", "정면"): 1}), ensure_ascii=False),
        encoding="utf-8",
    )
    assert aj.main([str(bad)]) == 1
    out = capsys.readouterr().out
    assert "C4 FAIL" in out
    assert "전체 FAIL" in out


def test_c1_and_c4_fail_when_their_segments_were_never_observed():
    """관측 없는 구간은 «경보 0회» 로 통과하지 않는다 — 11구간 전부 찍어야 한다."""
    result = verdicts_of(build_session(skip=("pitch-up", "clipped-base")))
    assert not result["C1"].passed
    assert "pitch-up" in result["C1"].detail
    assert not result["C4"].passed
    assert "관측 없음" in result["C4"].detail
