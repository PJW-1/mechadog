"""PPE 합격 기준 C1~C3 자동 판정(`tools/ppe/acceptance_judge.py`) 검증.

⚠️ **임계값은 측정 전에 고정했다(2026-10-05).** 판정 범위는 10-06 실측 뒤 사용자 결정으로
직립 4구간(`criteria.judged_segments`)으로 줄였고 C4(머리 잘림)는 없앴다. 임계값은 코드가 아니라
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
STANDING = ["standing-all", "standing-nohelmet", "standing-novest", "standing-none"]
JUDGED = [spec for spec in SPECS if spec["key"] in STANDING]
OUTSIDE = tuple(spec["key"] for spec in SPECS if spec["key"] not in STANDING)


def build_session(
    *,
    alarms: dict[tuple[str, str], int] | None = None,
    skip: tuple[str, ...] = (),
    skip_cells: tuple[tuple[str, str], ...] = (),
    frames: dict[tuple[str, str], int] | None = None,
) -> dict:
    """계획의 전 구간 × 4방향 세션. 기본은 위반 구간마다 전 방향 확정, 적합 구간은 무경보.

    alarms: (구간, 방향) → 그 칸의 확정 에지 수. 주면 기본값을 덮어쓴다.
    skip_cells: 아예 찍지 않은 (구간, 방향).
    frames: (구간, 방향) → 그 칸의 프레임 수(기본 10).
    """
    alarms = alarms or {}
    frames = frames or {}
    events: list[dict] = []
    segments: dict[str, dict] = {}
    t = 0.0
    for spec in SPECS:
        key, expected = spec["key"], spec["expected"]
        if key in skip:
            continue
        verdicts = {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0}
        for ori in ORIENTATIONS:
            if (key, ori) in skip_cells:
                continue
            default = 1 if expected == STATE_VIOLATION else 0
            count = alarms.get((key, ori), default)
            for i in range(frames.get((key, ori), 10)):
                t = round(t + 0.1, 1)
                state = expected
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
    assert set(result) == {"C1", "C2", "C3"}
    assert all(c.passed for c in result.values()), [c.detail for c in result.values()]


def test_thresholds_come_from_plan_file():
    criteria = aj.load_criteria(DEFAULT_ACCEPTANCE_PLAN, "xiao")
    assert criteria == {
        "window_ms": 1500,
        "hits_required": 3,
        "max_ok_segment_alarms": 1,
        "min_violation_directions": 3,
        "min_decidable_rate": 0.6,
        "judged_segments": STANDING,
    }


def test_judged_scope_is_standing_four_and_other_buttons_stay_in_plan():
    """판정은 직립 4구간만 한다(2026-10-06 사용자 결정). 나머지 버튼은 기록·재학습용으로 남는다."""
    criteria = aj.load_criteria(DEFAULT_ACCEPTANCE_PLAN, "xiao")
    judged = aj.judged_specs(SPECS, criteria)
    assert [spec["key"] for spec in judged] == STANDING
    assert [spec["expected"] for spec in judged] == [STATE_OK] + [STATE_VIOLATION] * 3
    assert {"crouching-all", "clipped-base", "pitch-up", "sit"} <= set(OUTSIDE)


def test_segments_outside_judged_scope_do_not_count():
    session = build_session(
        alarms={
            ("crouching-all", "정면"): 5,
            ("sit", "우측"): 3,
            ("clipped-base", "후면"): 2,
            **{("crouching-none", ori): 0 for ori in ORIENTATIONS},
        }
    )
    session["segments"]["crouching-all"]["verdicts"][STATE_UNKNOWN] = 10_000
    result = verdicts_of(session)
    assert all(c.passed for c in result.values()), [c.detail for c in result.values()]
    for word in ("crouching", "sit", "pitch-up", "clipped-base"):
        assert all(word not in c.detail for c in result.values())


def test_c1_boundary_one_false_alarm_passes_two_fail():
    one = verdicts_of(build_session(alarms={("standing-all", "정면"): 1}))
    assert one["C1"].passed
    two = verdicts_of(
        build_session(alarms={("standing-all", "정면"): 1, ("standing-all", "우측"): 1})
    )
    assert not two["C1"].passed
    assert "2회" in two["C1"].detail


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
    session = build_session(alarms={("standing-none", ori): 0 for ori in ORIENTATIONS})
    events = session["events"]
    first = next(
        i
        for i, e in enumerate(events)
        if e["segment"] == "standing-none" and e["orientation"] == "정면"
    )
    events[first]["confirmed"] = True
    for e in events:
        if e["segment"] == "standing-none":
            e["hits"] = 3  # 구간 내내 창이 켜진 채 이어진다
    assert verdicts_of(session)["C2"].passed


def test_c2_unobserved_violation_segment_fails():
    result = verdicts_of(build_session(skip=("standing-none",)))["C2"]
    assert not result.passed
    assert "standing-none" in result.detail


def _fixed_verdicts(session: dict, unknown: dict[str, int]) -> None:
    """직립 4구간의 판정 집계를 구간당 40건으로 맞추고, 구간별 확인불가 수를 넣는다."""
    for key in STANDING:
        n = unknown.get(key, 0)
        session["segments"][key]["verdicts"] = {
            STATE_VIOLATION: 0,
            STATE_OK: 40 - n,
            STATE_UNKNOWN: n,
        }


def test_c3_boundary_at_sixty_percent():
    # 직립 4구간 = 160건. 확인불가 64건 → 가능률 60.0% 통과, 65건 → 59.4% 실패.
    session = build_session()
    _fixed_verdicts(session, {"standing-all": 40, "standing-nohelmet": 24})
    result = verdicts_of(session)["C3"]
    assert result.passed, result.detail
    assert "96/160" in result.detail
    _fixed_verdicts(session, {"standing-all": 40, "standing-nohelmet": 25})
    assert not verdicts_of(session)["C3"].passed


def test_c3_ignores_unknowns_outside_judged_scope():
    session = build_session()
    for key in ("clipped-base", "crouching-all", "sit"):
        session["segments"][key]["verdicts"][STATE_UNKNOWN] = 10_000
    result = verdicts_of(session)["C3"]
    assert result.passed
    assert "160/160" in result.detail


def test_c3_without_judgements_fails_not_crashes():
    session = build_session()
    session["segments"] = {}
    assert not verdicts_of(session)["C3"].passed


@pytest.mark.parametrize("key", ["min_decidable_rate", "judged_segments"])
def test_missing_criteria_key_is_rejected(key, tmp_path):
    plan = json.loads(DEFAULT_ACCEPTANCE_PLAN.read_text(encoding="utf-8"))
    broken = copy.deepcopy(plan)
    del broken["scenarios"]["xiao"]["criteria"][key]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=key):
        aj.load_criteria(path, "xiao")


def test_scenario_without_criteria_is_rejected():
    with pytest.raises(ValueError, match="기준"):
        aj.load_criteria(DEFAULT_ACCEPTANCE_PLAN, "webcam")


def test_cli_prints_pass_fail_and_sets_exit_code(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(build_session(), ensure_ascii=False), encoding="utf-8")
    assert aj.main([str(good)]) == 0
    out = capsys.readouterr().out
    for key in ("C1", "C2", "C3"):
        assert f"{key} PASS" in out
    assert "C4" not in out
    assert "전체 PASS" in out

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            build_session(alarms={("standing-all", "정면"): 1, ("standing-all", "후면"): 1}),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert aj.main([str(bad)]) == 1
    out = capsys.readouterr().out
    assert "C1 FAIL" in out
    assert "전체 FAIL" in out


def test_cli_standing_only_session_passes(tmp_path, capsys):
    """직립 4구간만 찍은 세션(2026-10-06 실측 형태)은 커버리지 누락 없이 합격할 수 있다."""
    assert aj.main([str(_write(tmp_path, build_session(skip=OUTSIDE)))]) == 0
    out = capsys.readouterr().out
    assert "누락" not in out
    assert out.strip().splitlines()[-1] == "전체 PASS"


def test_c1_fails_when_ok_segment_was_never_observed():
    """관측 없는 구간은 «경보 0회» 로 통과하지 않는다 — 판정 구간은 전부 찍어야 한다."""
    result = verdicts_of(build_session(skip=("standing-all",)))
    assert not result["C1"].passed
    assert "관측 없음" in result["C1"].detail
    assert "standing-all" in result["C1"].detail


def test_c2_confirmation_after_timeout_does_not_count():
    """방향 15초가 지난 뒤에야 나온 확정은 그 방향의 확정이 아니다."""
    alarms = {("standing-nohelmet", ori): 1 for ori in ORIENTATIONS}
    session = build_session(alarms=alarms)
    for e in session["events"]:
        if e["segment"] == "standing-nohelmet" and e["orientation"] in ("후면", "좌측"):
            e["confirmed"] = False
            e["hits"] = 0
    # 후면의 확정을 제한시간 뒤로 민다.
    late = [
        e
        for e in session["events"]
        if e["segment"] == "standing-nohelmet" and e["orientation"] == "후면"
    ][-1]
    late["t"] = late["t"] + STEP_S + 1
    late["confirmed"] = True  # 창은 바로 풀린 것으로 둔다(hits 0) — 다음 방향으로 이월되지 않게
    result = verdicts_of(session)["C2"]
    assert not result.passed
    assert "standing-nohelmet 2방향" in result.detail


def test_c2_detail_marks_carried_directions():
    session = build_session(alarms={("standing-none", ori): 0 for ori in ORIENTATIONS})
    first = next(e for e in session["events"] if e["segment"] == "standing-none")
    first["confirmed"] = True
    for e in session["events"]:
        if e["segment"] == "standing-none":
            e["hits"] = 3
    assert "이월" in verdicts_of(session)["C2"].detail


def _write(tmp_path: Path, session: dict, name: str = "s.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    return path


def test_coverage_gaps_empty_for_full_session():
    assert aj.coverage_gaps(build_session(), JUDGED, ORIENTATIONS, STEP_S) == []


def test_coverage_ignores_segments_outside_judged_scope():
    assert aj.coverage_gaps(build_session(skip=OUTSIDE), JUDGED, ORIENTATIONS, STEP_S) == []


def test_coverage_gap_lists_missing_segment_direction_pairs():
    session = build_session(skip_cells=(("standing-all", "후면"), ("standing-all", "좌측")))
    gaps = aj.coverage_gaps(session, JUDGED, ORIENTATIONS, STEP_S)
    assert gaps == [("standing-all", "후면"), ("standing-all", "좌측")]


def test_coverage_gap_includes_segment_never_run():
    gaps = aj.coverage_gaps(build_session(skip=("standing-none",)), JUDGED, ORIENTATIONS, STEP_S)
    assert gaps == [("standing-none", ori) for ori in ORIENTATIONS]


def test_excluded_episode_does_not_count_as_coverage():
    """확정 기준보다 프레임이 적은 에피소드는 그 방향을 찍은 것으로 치지 않는다."""
    session = build_session(frames={("standing-all", "정면"): 2})
    gaps = aj.coverage_gaps(session, JUDGED, ORIENTATIONS, STEP_S)
    assert gaps == [("standing-all", "정면")]


def test_excluded_episode_does_not_count_as_confirmed_direction():
    session = build_session(
        alarms={("standing-novest", "좌측"): 0},
        frames={("standing-novest", "정면"): 2},
    )
    result = verdicts_of(session)["C2"]
    assert not result.passed
    assert "standing-novest 2방향" in result.detail


def test_excluded_episode_alarms_still_count_for_c1():
    session = build_session(
        alarms={("standing-all", "정면"): 1, ("standing-all", "우측"): 1},
        frames={("standing-all", "정면"): 2, ("standing-all", "우측"): 2},
    )
    assert not verdicts_of(session)["C1"].passed


def test_cli_one_direction_only_session_is_invalid(tmp_path, capsys):
    """standing-all 을 한 방향만 돌린 세션은 C1 이 초록이어도 합격이 아니다."""
    session = build_session(
        skip_cells=tuple(("standing-all", ori) for ori in ORIENTATIONS[1:]),
    )
    assert aj.main([str(_write(tmp_path, session))]) == 1
    out = capsys.readouterr().out
    assert "누락" in out
    assert "standing-all" in out
    assert out.strip().splitlines()[-1].startswith("전체 FAIL")
    assert "전체 PASS" not in out


def test_cli_final_line_is_fail_with_reason_for_operating_window(tmp_path, capsys):
    session = build_session()
    session["settings"]["window_ms"] = 3000
    assert aj.main([str(_write(tmp_path, session))]) == 1
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("전체 FAIL")
    assert "운용 창" in last


def test_cli_final_line_names_failed_criteria(tmp_path, capsys):
    session = build_session(alarms={("standing-all", "정면"): 1, ("standing-all", "좌측"): 1})
    assert aj.main([str(_write(tmp_path, session))]) == 1
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("전체 FAIL")
    assert "C1" in last


def test_cli_missing_file_exits_2_with_one_stderr_line(tmp_path, capsys):
    assert aj.main([str(tmp_path / "none.json")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1


def test_cli_broken_json_exits_2(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert aj.main([str(path)]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_cli_scenario_without_criteria_exits_2(tmp_path, capsys):
    path = _write(tmp_path, build_session())
    assert aj.main([str(path), "--scenario", "webcam"]) == 2
    err = capsys.readouterr().err
    assert "기준" in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.parametrize(
    "replace",
    [
        lambda scenario: scenario.update(criteria=5),
        lambda scenario: scenario.update(criteria=True),
        lambda scenario: scenario.update(criteria=list(aj.CRITERIA_KEYS)),
    ],
    ids=["number", "bool", "key-list"],
)
def test_cli_criteria_that_is_not_an_object_exits_2(replace, tmp_path, capsys):
    """합격 기준이 객체가 아니면 traceback 이 아니라 «입력 오류» 한 줄과 종료 2 다."""
    plan = json.loads(DEFAULT_ACCEPTANCE_PLAN.read_text(encoding="utf-8"))
    replace(plan["scenarios"]["xiao"])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    session = _write(tmp_path, build_session())
    assert aj.main([str(session), "--plan", str(plan_path)]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


@pytest.mark.parametrize("scenario", [["standing-all"], "xiao"])
def test_cli_scenario_that_is_not_an_object_exits_2(scenario, tmp_path, capsys):
    plan = json.loads(DEFAULT_ACCEPTANCE_PLAN.read_text(encoding="utf-8"))
    plan["scenarios"]["xiao"] = scenario
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    session = _write(tmp_path, build_session())
    assert aj.main([str(session), "--plan", str(plan_path)]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_cli_does_not_swallow_unexpected_errors(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("예상 밖")

    monkeypatch.setattr(aj, "judge_session", boom)
    with pytest.raises(RuntimeError):
        aj.main([str(_write(tmp_path, build_session()))])


@pytest.mark.parametrize(
    ("settings", "word"),
    [
        ({"window_ms": 3000, "hits_required": 3}, "window_ms"),
        ({"window_ms": 1500, "hits_required": 2}, "hits_required"),
        ({"window_ms": 1500, "hits_required": 3, "overridden": True}, "덮어"),
    ],
)
def test_session_with_non_operational_window_is_rejected(settings, word, tmp_path, capsys):
    """덮어쓴 창으로 잰 세션은 기준을 느슨하게 만든다 — 합격 판정에 쓰지 않는다."""
    session = build_session()
    session["settings"].update(settings)
    path = tmp_path / "s.json"
    path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    assert aj.main([str(path)]) == 1
    assert word in capsys.readouterr().out


@pytest.mark.parametrize(
    ("patch", "word"),
    [
        ({"min_decidable_rate": "0.6"}, "min_decidable_rate"),
        ({"judged_segments": "standing-all"}, "judged_segments"),
        ({"judged_segments": []}, "judged_segments"),
        ({"judged_segments": ["standing-all", "standing-all", "standing-none"]}, "중복"),
        ({"judged_segments": ["standing-all", "no-such-segment"]}, "no-such-segment"),
        ({"judged_segments": ["standing-all", "standing-none", "clipped-base"]}, "clipped-base"),
        ({"judged_segments": ["standing-nohelmet", "standing-none"]}, STATE_OK),
        ({"judged_segments": ["standing-all", "crouching-all"]}, STATE_VIOLATION),
        ({"max_clipped_alarms": 0}, "max_clipped_alarms"),
    ],
)
def test_malformed_criteria_values_are_rejected(patch, word, tmp_path):
    plan = json.loads(DEFAULT_ACCEPTANCE_PLAN.read_text(encoding="utf-8"))
    plan["scenarios"]["xiao"]["criteria"].update(patch)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=word):
        aj.load_criteria(path, "xiao")
