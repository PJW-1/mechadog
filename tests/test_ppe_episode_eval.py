"""PPE 실측의 에피소드 기준 평가(`tools/ppe/episode_eval.py`) 검증.

⚠️ **확인불가를 정상으로 치지 않는다.** 확인불가만 있는 위반 에피소드는 놓친 것이고,
확인불가만 있는 정상 에피소드는 오경보는 아니지만 정답도 아니다. 그 둘이 수치에서
사라지지 않는지를 경계마다 본다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import ppe_live_check as ppe  # noqa: E402
from tools.ppe import episode_eval as ee  # noqa: E402

OK, BAD, UNK = ppe.STATE_OK, ppe.STATE_VIOLATION, ppe.STATE_UNKNOWN
REAL_SESSION = (
    Path(__file__).resolve().parents[1]
    / "TEST_MECHDOG"
    / "results"
    / "20260928_ppe-xiao"
    / "session.json"
)


def ev(t, seg="seg", exp=OK, ori="정면", states=(OK,), confirmed=False, hits=0):
    return {
        "t": round(t, 1),
        "tag": f"{int(round(t * 10)):05d}",
        "people": len(states),
        "states": list(states),
        "reasons": [],
        "labels": [],
        "confirmed": confirmed,
        "hits": hits,
        "segment": seg,
        "expected": exp if seg else None,
        "orientation": ori if seg else None,
    }


def run(start, end, step=0.5, **kw):
    """start 이상 end 미만을 step 간격으로 채운 이벤트."""
    out, t = [], start
    while t < end - 1e-9:
        out.append(ev(t, **kw))
        t += step
    return out


def session(events, sha="a" * 64, hits_required=3, window_ms=1500):
    return {
        "schema_version": 1,
        "source": "fake",
        "device": "mechdog-01",
        "scenario": "xiao",
        "duration_s": events[-1]["t"] if events else 0,
        "frames": len(events),
        "counts": {},
        "reasons": {},
        "settings": {"model_sha256": sha, "window_ms": window_ms, "hits_required": hits_required},
        "segments": {},
        "events": events,
    }


def confirm_at(events, t):
    """t 시각 프레임을 위반 확정(상승 에지)으로 바꾼다."""
    for e in events:
        if abs(e["t"] - t) < 1e-6:
            e.update(states=[BAD], confirmed=True, hits=3)
            return events
    raise AssertionError(f"{t} 프레임 없음")


def evaluate(events, **kw):
    kw.setdefault("timeout_s", 15.0)
    return ee.evaluate([("fake", session(events))], **kw)


# ── 에피소드 나누기 ──────────────────────────────────────────


def test_episodes_split_on_orientation_change_and_skip_unlabelled_frames():
    events = (
        run(0, 2, seg=None)
        + run(2, 10, ori="정면")
        + run(10, 18, ori="우측")
        + run(18, 20, seg=None)
    )
    episodes = ee.split_episodes(events)
    assert [(e[0]["orientation"], e[0]["t"], len(e)) for e in episodes] == [
        ("정면", 2.0, 16),
        ("우측", 10.0, 16),
    ]


def test_short_episode_below_confirmation_window_is_excluded_but_frames_still_count():
    # 1.2초짜리 에피소드는 1.5초 창을 채울 수 없다 — 평가 기회가 아니다.
    events = run(0, 10, ori="정면") + run(10, 11.2, ori="우측") + run(11.2, 12, seg=None)
    summary = evaluate(events)
    total = summary["total"]
    assert total["episode"]["episodes"] == 1
    assert total["episode"]["excluded"] == 1
    # 프레임 기준은 구간 전체를 센다 (ppe_live_check 의 구간 집계와 같게).
    assert total["frame"]["count"] == len([e for e in events if e["segment"]])
    assert [e["excluded"] for e in summary["episodes"]] == [None, "평가 창 1.5초 미만"]


# ── 위반 에피소드: 제한시간과 판정 시간 ───────────────────────


def test_violation_confirmed_exactly_at_timeout_counts_and_latency_is_measured():
    events = confirm_at(run(0, 20, exp=BAD, states=(UNK,)), 15.0)
    ep = evaluate(events)["total"]["episode"]
    assert ep["recall"] == 1.0
    assert ep["latency"] == {"n": 1, "mean_s": 15.0, "p95_s": 15.0, "untimed": 0, "carried": 0}


def test_violation_confirmed_right_after_timeout_is_a_miss_and_is_shown_as_late():
    events = confirm_at(run(0, 20, exp=BAD, states=(OK,), step=0.1), 15.1)
    summary = evaluate(events)
    ep = summary["total"]["episode"]
    assert ep["recall"] == 0.0
    assert ep["late"] == 1
    assert ep["latency"]["n"] == 0
    assert ep["latency"]["untimed"] == 1
    assert ep["latency"]["mean_s"] is None
    # 제한시간 안에서는 적합만 봤으므로 최종 판정은 적합 = 오답
    assert summary["episodes"][0]["final"] == OK
    assert ep["effective"] == 0.0


def test_timeout_is_configurable():
    events = confirm_at(run(0, 20, exp=BAD, states=(OK,), step=0.1), 15.1)
    assert evaluate(events, timeout_s=16.0)["total"]["episode"]["recall"] == 1.0


def test_latency_mean_and_p95_over_several_episodes():
    events = []
    for index, (ori, delay) in enumerate([("정면", 1.0), ("우측", 2.0), ("후면", 4.0)]):
        start = index * 10
        events += confirm_at(run(start, start + 10, exp=BAD, ori=ori, states=(UNK,)), start + delay)
    latency = evaluate(events)["total"]["episode"]["latency"]
    assert latency["n"] == 3
    assert latency["mean_s"] == pytest.approx(7 / 3)
    assert latency["p95_s"] == pytest.approx(3.8)  # 선형 보간 — 2 + 0.9 * (4 - 2)


# ── 확인불가만 있는 에피소드 ──────────────────────────────────


def test_unknown_only_violation_episode_is_a_miss_not_a_pass():
    summary = evaluate(run(0, 10, exp=BAD, states=(UNK,)))
    ep = summary["total"]["episode"]
    assert summary["episodes"][0]["final"] == UNK
    assert ep["recall"] == 0.0
    assert ep["unknown_rate"] == 1.0
    assert ep["coverage"] == 0.0
    assert ep["conditional"] is None  # 판정 가능 표본이 없다
    assert ep["effective"] == 0.0


def test_unknown_only_normal_episode_is_not_a_false_alarm_but_not_correct_either():
    summary = evaluate(run(0, 10, exp=OK, states=(UNK,)))
    ep = summary["total"]["episode"]
    assert ep["false_alarm_rate"] == 0.0
    assert ep["unknown_rate"] == 1.0
    assert ep["effective"] == 0.0
    assert summary["total"]["frame"]["unknown_rate"] == 1.0


def test_expected_unknown_episode_counts_as_correct_hold_and_stays_out_of_coverage():
    summary = evaluate(run(0, 10, exp=UNK, states=(UNK,)))
    ep = summary["total"]["episode"]
    assert ep["unknown_expected"] == 1
    assert ep["effective"] == 1.0
    assert ep["coverage"] is None  # 분모에서 빠진다 — segment_section 과 같은 규칙


def test_no_person_frames_make_the_episode_undeterminable():
    summary = evaluate(run(0, 10, exp=OK, states=()))
    assert summary["episodes"][0]["final"] == UNK


# ── 정상 에피소드: 오경보와 방향 전환 ─────────────────────────


def test_false_alarm_is_counted_anywhere_in_a_normal_episode_without_timeout():
    events = confirm_at(run(0, 25, exp=OK), 20.0)
    summary = evaluate(events)
    ep = summary["total"]["episode"]
    assert ep["false_alarm_rate"] == 1.0
    assert summary["episodes"][0]["final"] == BAD
    assert ep["latency"]["n"] == 0  # 정상 에피소드의 판정 시간은 정의하지 않는다


def test_alarm_latched_across_orientation_change_is_carried_not_a_new_alarm():
    # 창은 방향이 바뀔 때 비워지지 않는다(구간을 바꿀 때만 reset) — 정면의 확정이
    # 우측 초반까지 이어진다. 새 상승 에지가 없으면 우측의 오경보로 세지 않고 «이월» 로 둔다.
    front = confirm_at(run(0, 10, exp=OK, ori="정면"), 9.5)
    right = run(10, 20, exp=OK, ori="우측")
    right[0].update(states=[BAD], hits=2)
    right[1].update(hits=1)
    summary = evaluate(front + right)
    episodes = summary["episodes"]
    assert [e["alarms"] for e in episodes] == [1, 0]
    assert [e["carried"] for e in episodes] == [False, True]
    assert summary["total"]["episode"]["false_alarm_rate"] == 0.5
    assert summary["total"]["episode"]["carried"] == 1


def test_segment_change_resets_the_carry():
    first = confirm_at(run(0, 10, seg="a", exp=OK), 9.5)
    second = run(10, 20, seg="b", exp=OK)
    second[0].update(hits=2)
    episodes = evaluate(first + second)["episodes"]
    assert [e["carried"] for e in episodes] == [False, False]


def test_rows_group_by_segment_and_orientation():
    events = (
        confirm_at(run(0, 10, seg="ok", exp=OK, ori="정면"), 5.0)
        + run(10, 20, seg="ok", exp=OK, ori="우측")
        + confirm_at(run(20, 30, seg="bad", exp=BAD, ori="정면"), 22.0)
    )
    summary = evaluate(events)
    assert [(r["segment"], r["orientation"]) for r in summary["rows"]] == [
        ("ok", "정면"),
        ("ok", "우측"),
        ("bad", "정면"),
    ]
    assert [s["segment"] for s in summary["segments"]] == ["ok", "bad"]
    ok = summary["segments"][0]["episode"]
    assert ok["false_alarm_rate"] == 0.5
    assert ok["recall"] is None  # 위반 에피소드가 없다
    assert summary["segments"][1]["episode"]["recall"] == 1.0


# ── 프레임 기준은 ppe_live_check 와 같은 식이다 ─────────────────


def test_frame_tally_is_the_live_check_formula():
    events = run(0, 4, exp=OK, states=(OK, UNK)) + run(4, 8, exp=OK, ori="우측", states=(BAD,))
    frame = evaluate(events)["total"]["frame"]
    verdicts = {OK: 8, UNK: 8, BAD: 8}
    expected = ppe.verdict_tally(OK, verdicts)
    assert {k: frame[k] for k in expected} == expected


def test_segment_section_still_prints_the_same_numbers():
    specs = [
        {"key": "all", "condition": "전신", "wear": "전부 착용", "expected": OK},
        {"key": "clipped", "condition": "머리 잘림", "wear": "전부 착용", "expected": UNK},
    ]
    log = ppe.SegmentLog(specs, 15, ["정면"])
    log.select("all")
    log.observe([OK, UNK])
    log.select("clipped")
    log.observe([UNK])
    lines: list[str] = []
    ppe.segment_section(lines.append, log)
    assert "판정 가능률 50% · 실효 성공률 67%" in "\n".join(lines)

    events = [
        ev(0.0, seg="all", exp=OK, states=(OK, UNK)),
        ev(0.1, seg="clipped", exp=UNK, states=(UNK,)),
    ]
    frame = evaluate(events)["total"]["frame"]
    assert frame["coverage"] == pytest.approx(0.5)
    assert frame["effective"] == pytest.approx(2 / 3)


# ── 여러 세션 합치기 ──────────────────────────────────────────


def test_sessions_with_different_models_are_merged_with_a_warning():
    a = session(confirm_at(run(0, 10, exp=OK), 5.0), sha="a" * 64)
    b = session(run(0, 10, exp=OK), sha="b" * 64)
    summary = ee.evaluate([("a", a), ("b", b)], timeout_s=15.0)
    assert summary["total"]["episode"]["episodes"] == 2
    assert summary["total"]["episode"]["false_alarm_rate"] == 0.5
    assert any("모델" in w for w in summary["warnings"])


def test_sessions_with_the_same_model_merge_quietly():
    a = session(run(0, 10, exp=OK), sha="a" * 64)
    summary = ee.evaluate([("a", a), ("b", a)], timeout_s=15.0)
    assert summary["warnings"] == []


# ── 출력 ──────────────────────────────────────────────────────


def test_markdown_states_definitions_and_marks_undefined_values(tmp_path):
    events = run(0, 10, exp=OK)
    path = tmp_path / "session.json"
    path.write_text(json.dumps(session(events), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out.md"
    assert ee.main([str(path), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "에피소드" in text and "제한시간 15초" in text
    assert "해당 없음" in text  # 정상 에피소드뿐이라 판정 시간이 없다


def test_json_option_writes_machine_readable_summary(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(session(run(0, 10, exp=OK))), encoding="utf-8")
    out = tmp_path / "out.json"
    assert ee.main([str(path), "--json", "--timeout-s", "5", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["settings"]["timeout_s"] == 5.0
    assert data["total"]["episode"]["episodes"] == 1


def test_default_timeout_comes_from_the_acceptance_plan():
    plan = json.loads(ppe.DEFAULT_ACCEPTANCE_PLAN.read_text(encoding="utf-8"))
    assert ee.default_timeout_s() == float(plan["orientation_step_s"])


# ── 실제 세션 (2026-09-28 · v23b · 직립·전신·전부 착용) ─────────


@pytest.mark.skipif(not REAL_SESSION.is_file(), reason="실측 세션 파일 없음")
def test_real_session_20260928_has_four_false_alarms_in_four_normal_episodes():
    data = json.loads(REAL_SESSION.read_text(encoding="utf-8"))
    summary = ee.evaluate([(str(REAL_SESSION), data)], timeout_s=15.0)
    ep = summary["total"]["episode"]
    assert sum(e["alarms"] for e in summary["episodes"]) == 4
    assert ep["normal_episodes"] == 4
    assert ep["false_alarms"] == 4
    assert ep["false_alarm_rate"] == 1.0
    assert ep["violation_episodes"] == 0
    assert ep["recall"] is None
    # 구간을 한 번 더 눌러 생긴 1.2초 조각은 평가 기회가 아니다
    assert ep["excluded"] == 1
    # 프레임 기준은 세션에 저장된 구간 집계와 같아야 한다
    stored = data["segments"]["standing-all"]
    frame = summary["segments"][0]["frame"]
    assert {k: frame[k] for k in ("count", "right", "unknown")} == {
        "count": sum(stored["verdicts"].values()),
        "right": stored["verdicts"][OK],
        "unknown": stored["verdicts"][UNK],
    }
    for row in summary["rows"]:
        counts = stored["orientations"][row["orientation"]]
        assert row["frame"]["count"] == sum(counts.values())
