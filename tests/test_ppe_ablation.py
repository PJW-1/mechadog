"""PPE 전처리·후처리 ablation(`tools/ppe/ablation.py`) 검증.

모델과 프레임은 CI 에 없다. 판정·누적 규칙은 합성 검출값으로, 기준 재현은 저장소에 있는
2026-10-06 세션 원자료(`session.json`)로 본다.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.vision.detector import Detection  # noqa: E402
from tools.ppe import ablation as ab  # noqa: E402
from tools.ppe import ppe_live_check as ppe  # noqa: E402

OK, BAD, UNK = ppe.STATE_OK, ppe.STATE_VIOLATION, ppe.STATE_UNKNOWN
SESSION_1006 = (
    Path(__file__).resolve().parents[1]
    / "field_tests"
    / "results"
    / "20261006_ppe-xiao"
    / "session.json"
)
PLAN = ppe.DEFAULT_ACCEPTANCE_PLAN


def det(label, score=0.9, box=(10.0, 20.0, 40.0, 50.0)):
    return [label, score, *box]


def person(dets, origin=(0, 0)):
    return {"origin": list(origin), "dets": dets}


def frame(tag, persons, crops, full=None, shape=(480, 640)):
    """persons: [(score, box)], crops: {키: [person(...) | None]}"""
    return {
        "tag": tag,
        "shape": list(shape),
        "persons": [{"score": s, "box": list(b)} for s, b in persons],
        "crops": crops,
        "full": full or [],
    }


WORN = [det("helmet", box=(10, 20, 30, 40)), det("vest", box=(5, 60, 50, 120))]
NO_HELMET = [det("no_helmet", box=(10, 20, 30, 40)), det("vest", box=(5, 60, 50, 120))]


# ── 한 사람 판정 ─────────────────────────────────────────────


def test_conf_threshold_filters_cached_detections():
    dets = [
        det("helmet", 0.8, (10, 20, 30, 40)),
        det("no_helmet", 0.45, (10, 20, 30, 40)),
        det("vest", 0.9, (5, 60, 50, 120)),
    ]
    base = {"clip": "crop", "head_margin": 8, "origin": (0, 0), "person_box": (0, 50, 60, 200)}
    assert ab.judge_person(dets, conf=0.5, **base).state == OK
    assert ab.judge_person(dets, conf=0.4, **base).state == BAD
    assert ab.judge_person(dets, conf=0.95, **base).reason == "아무것도 미검출"


def test_clip_modes_crop_frame_off():
    """도구는 머리 상단을 크롭 좌표로, 런타임은 프레임 좌표로 잰다."""
    head_at_crop_top = [det("helmet", box=(10, 3, 30, 40)), det("vest", box=(5, 60, 50, 120))]
    kw = {"conf": 0.5, "head_margin": 8, "origin": (50, 100), "person_box": (60, 110, 120, 300)}
    assert ab.judge_person(head_at_crop_top, clip="crop", **kw).reason == "머리 클리핑"
    assert ab.judge_person(head_at_crop_top, clip="frame", **kw).state == OK
    assert ab.judge_person(head_at_crop_top, clip="off", **kw).state == OK


def test_frame_clip_uses_person_top_like_runtime():
    kw = {"conf": 0.5, "head_margin": 8, "origin": (0, 0), "person_box": (60, 2, 120, 300)}
    assert ab.judge_person(WORN, clip="frame", **kw).reason == "머리 클리핑"


def test_missing_crop_is_crop_failure():
    kw = {
        "conf": 0.5,
        "clip": "crop",
        "head_margin": 8,
        "origin": (0, 0),
        "person_box": (0, 0, 1, 1),
    }
    assert ab.judge_person(None, **kw).reason == "크롭 실패"


# ── 프레임 판정 ─────────────────────────────────────────────


def test_frame_judgements_gate_and_primary():
    small, tall = (0.9, (0, 100, 50, 200)), (0.9, (100, 50, 200, 400))
    fr = frame(
        "00001",
        [small, tall],
        {"0.08": [person(NO_HELMET), person(WORN)]},
        full=NO_HELMET,
    )
    base = ab.Variant("base")
    assert [j.state for j in ab.frame_judgements(fr, base, head_margin=8)] == [BAD, OK]
    primary = ab.Variant("p", primary_only=True)
    assert [j.state for j in ab.frame_judgements(fr, primary, head_margin=8)] == [OK]
    gate_off = ab.Variant("g", gate=False)
    assert [j.state for j in ab.frame_judgements(fr, gate_off, head_margin=8)] == [BAD]


def test_gate_off_judges_empty_frames_and_gate_on_skips_them():
    fr = frame("00001", [], {"0.08": []}, full=[det("no_vest")])
    assert ab.frame_judgements(fr, ab.Variant("base"), head_margin=8) == []
    states = [j.state for j in ab.frame_judgements(fr, ab.Variant("g", gate=False), head_margin=8)]
    assert states == [UNK]


# ── 누적 규칙 ───────────────────────────────────────────────


def test_clearing_window_hysteresis():
    """런타임은 적합 한 번에 누적을 지운다. N 회 연속 적합일 때만 지우면 확정된다."""
    seq = [BAD, BAD, OK, BAD]
    once = ab.ClearingWindow(1500, 3, clear_after_ok=1)
    assert [once.observe([s], i * 100) for i, s in enumerate(seq)] == [False] * 4
    twice = ab.ClearingWindow(1500, 3, clear_after_ok=2)
    assert [twice.observe([s], i * 100) for i, s in enumerate(seq)] == [False, False, False, True]


def test_clearing_window_resets_without_people_and_time_window_still_applies():
    w = ab.ClearingWindow(1500, 3, clear_after_ok=1)
    w.observe([BAD], 0)
    w.observe([BAD], 100)
    w.observe([], 200)
    assert w.hits == 0
    w.observe([BAD], 300)
    w.observe([BAD], 400)
    assert w.observe([BAD], 2000) is False  # 300 · 400 은 창 밖이다
    assert w.hits == 1


def test_clearing_window_rising_edge_only():
    w = ab.ClearingWindow(1500, 2, clear_after_ok=1)
    assert [w.observe([BAD], t) for t in (0, 100, 200)] == [False, True, False]


# ── 재생 ───────────────────────────────────────────────────


def labelled(t, tag, seg="standing-all", exp=OK, ori="정면"):
    return {"t": t, "tag": tag, "segment": seg, "expected": exp, "orientation": ori}


def test_replay_builds_session_from_cache():
    frames = [
        frame(f"{i:05d}", [(0.9, (0, 50, 60, 300))], {"0.08": [person(NO_HELMET)]})
        for i in range(1, 5)
    ]
    labels = [labelled(i * 0.1, f"{i:05d}", "standing-nohelmet", BAD) for i in range(1, 5)]
    out = ab.replay(frames, labels, ab.Variant("base"), head_margin=8, orientations=["정면"])
    assert [e["confirmed"] for e in out["events"]] == [False, False, True, False]
    assert out["segments"]["standing-nohelmet"]["verdicts"][BAD] == 4
    assert out["segments"]["standing-nohelmet"]["orientations"]["정면"][BAD] == 4
    assert out["settings"]["hits_required"] == 3


def test_replay_resets_window_on_segment_change():
    frames = [
        frame(f"{i:05d}", [(0.9, (0, 50, 60, 300))], {"0.08": [person(NO_HELMET)]})
        for i in range(1, 5)
    ]
    labels = [
        labelled(0.1, "00001", "standing-nohelmet", BAD),
        labelled(0.2, "00002", "standing-nohelmet", BAD),
        labelled(0.3, "00003", "standing-none", BAD),
        labelled(0.4, "00004", "standing-none", BAD),
    ]
    out = ab.replay(frames, labels, ab.Variant("base"), head_margin=8, orientations=["정면"])
    assert [e["hits"] for e in out["events"]] == [1, 2, 1, 2]


def test_replay_rejects_cache_that_does_not_match_labels():
    frames = [frame("00002", [], {"0.08": []})]
    with pytest.raises(ValueError, match="00001"):
        ab.replay(frames, [labelled(0.1, "00001")], ab.Variant("base"), 8, ["정면"])


# ── 기준 재현 (저장소에 있는 실측 원자료) ──────────────────────


@pytest.fixture(scope="module")
def recorded():
    return json.loads(SESSION_1006.read_text(encoding="utf-8"))


def test_recorded_replay_reproduces_summary(recorded):
    """기록된 판정만 다시 누적해도 summary.md 의 83% · 79% · C1~C3 가 그대로 나와야 한다."""
    replayed = ab.replay_recorded(recorded, ab.Variant("base"))
    for key, row in recorded["segments"].items():
        assert replayed["segments"][key]["verdicts"] == row["verdicts"]
        assert replayed["segments"][key]["orientations"] == row["orientations"]
    row = ab.evaluate_variant(replayed, PLAN)
    assert (row["determinate"], row["total"]) == (4880, 5913)
    assert round(row["decidable_rate"], 2) == 0.83
    assert round(row["effective_rate"], 2) == 0.79
    assert [c.passed for c in row["criteria"]] == [True, True, True]
    assert row["alarms_total"] == sum(e["confirmed"] for e in recorded["events"])


def test_recorded_replay_matches_recorded_window(recorded):
    """0.1초로 반올림된 t 로 다시 센 창이 기록된 창과 같은 상태(빔·미달·확정)여야 한다.

    히트 수 자체는 창 경계에서 반올림 때문에 1~3 씩 어긋난다(20 fps 에서 창 안 30여 회).
    경보와 이월을 가르는 것은 «비었나·기준 이상인가» 뿐이다.
    """
    replayed = ab.replay_recorded(recorded, ab.Variant("base"))

    def level(hits):
        return 0 if hits == 0 else (2 if hits >= 3 else 1)

    pairs = list(zip(replayed["events"], recorded["events"], strict=True))
    same = sum(level(a["hits"]) == level(b["hits"]) for a, b in pairs)
    assert same / len(pairs) > 0.99
    assert [a["tag"] for a, _ in pairs if a["confirmed"]] == [
        b["tag"] for _, b in pairs if b["confirmed"]
    ]


def test_evaluate_variant_reports_episode_metrics(recorded):
    row = ab.evaluate_variant(ab.replay_recorded(recorded, ab.Variant("base")), PLAN)
    assert row["recall"] == 1.0
    assert row["false_alarm_rate"] == 0.0
    assert row["latency_p95_s"] is not None
    assert row["reasons"]["머리 미검출"] == 1091
    # C2 는 이월을 확정으로 센다. 이월을 빼면 방향마다 제 경보가 있어야 한다.
    assert set(row["own_directions"]) == {"standing-nohelmet", "standing-novest", "standing-none"}
    assert sum(row["own_directions"].values()) <= row["alarms_total"]


def test_evaluate_variant_counts_only_judged_segments(recorded):
    """판정 대상이 아닌 구간이 세션에 있어도 분모는 C1·C3 와 같은 판정 대상 구간만 센다."""
    session = ab.replay_recorded(recorded, ab.Variant("base"))
    base = ab.evaluate_variant(session, PLAN)
    extra = {
        "verdicts": {BAD: 0, OK: 50, UNK: 50},
        "orientations": {},
        "frames": 100,
    }
    mixed = {**session, "segments": {**session["segments"], "crouching-all": extra}}
    row = ab.evaluate_variant(mixed, PLAN)
    for key in ("determinate", "total", "right", "count"):
        assert row[key] == base[key]
    assert row["decidable_rate"] == base["decidable_rate"]
    assert row["effective_rate"] == base["effective_rate"]


def test_breakdown_by_condition(recorded):
    rows = ab.breakdown(ab.replay_recorded(recorded, ab.Variant("base")))
    row = next(r for r in rows if r["segment"] == "standing-all" and r["orientation"] == "좌측")
    assert (row["count"], row["right"], row["unknown"]) == (449, 328, 121)
    people = {r["people"]: r for r in ab.breakdown_by_people(recorded)}
    assert set(people) == {1, 2}


# ── 추론 캐시 ───────────────────────────────────────────────


class FakeDetector:
    def __init__(self, result):
        self.result = result
        self.shapes = []

    def detect(self, image):
        self.shapes.append(image.shape[:2])
        return list(self.result)


def test_infer_frame_records_crops_scales_and_full_frame():
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    coco = FakeDetector(
        [Detection("person", 0.9, (100, 100, 200, 300)), Detection("cup", 0.9, (0, 0, 5, 5))]
    )
    ppe_fake = FakeDetector([Detection("helmet", 0.7, (10, 10, 20, 20))])
    rec = ab.infer_frame(
        image, "00001", coco, ppe_fake, person_label="person", pads=(0.0, 0.1), scales=(0.5,)
    )
    assert rec["shape"] == [480, 640]
    assert len(rec["persons"]) == 1
    assert set(rec["crops"]) == {"0", "0.1", "0@0.5", "0.1@0.5"}
    assert rec["crops"]["0.1"][0]["origin"] == [90, 80]
    # 절반 해상도로 넣은 크롭의 박스는 원래 크롭 좌표로 되돌린다.
    assert rec["crops"]["0@0.5"][0]["dets"][0][2:] == [20.0, 20.0, 40.0, 40.0]
    assert rec["full"][0][0] == "helmet"
    assert (100, 50) in ppe_fake.shapes  # 0.5 배로 줄인 0 여유 크롭


# ── 모델 비교 (같은 프레임·같은 사람 검출) ──────────────────────


CLIPPED = [det("no_helmet", box=(10, 0, 30, 20)), det("no_vest", box=(5, 60, 50, 120))]
NO_BOTH = [det("no_helmet", box=(10, 20, 30, 40)), det("no_vest", box=(5, 60, 50, 120))]


def judged_event(tag, states, reasons=(), seg="standing-none", exp=BAD, confirmed=False):
    return {
        "t": 0.1,
        "tag": tag,
        "segment": seg,
        "expected": exp,
        "orientation": "후면",
        "states": list(states),
        "reasons": list(reasons),
        "confirmed": confirmed,
    }


def test_segment_tally_counts_head_clipping_apart_from_other_unknowns():
    session = {
        "events": [
            judged_event("00001", [BAD, OK]),
            judged_event("00002", [UNK, UNK], ["머리 미검출", "머리 클리핑"], confirmed=True),
            judged_event("00003", [OK], seg="standing-nohelmet"),
            {"t": 0.4, "tag": "00004", "segment": None, "states": [BAD], "reasons": []},
        ]
    }
    tally = ab.segment_tally(session)
    assert tally["standing-none"] == {
        "expected": BAD,
        "count": 4,
        "right": 1,
        "wrong": 1,
        "unknown": 2,
        "clipped": 1,
        "alarms": 1,
    }
    assert tally["standing-nohelmet"]["wrong"] == 1
    assert set(tally) == {"standing-none", "standing-nohelmet"}


def test_paired_tally_matches_people_one_to_one():
    a = {"events": [judged_event("00001", [OK, BAD]), judged_event("00002", [UNK])]}
    b = {"events": [judged_event("00001", [BAD, BAD]), judged_event("00002", [BAD])]}
    pairs = ab.paired_tally(a, b)
    assert pairs["standing-none"] == {(OK, BAD): 1, (BAD, BAD): 1, (UNK, BAD): 1}


def test_paired_tally_rejects_sessions_with_different_people():
    a = {"events": [judged_event("00001", [OK, BAD])]}
    b = {"events": [judged_event("00001", [BAD])]}
    with pytest.raises(ValueError, match="00001"):
        ab.paired_tally(a, b)


def test_compare_replays_each_cache_with_base_and_writes_table(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "orientation_step_s": 60,
                "orientations": ["후면"],
                "scenarios": {
                    "xiao-rear": {
                        "segments": [
                            {"key": "standing-none", "expected": BAD},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    tags = [f"{i:05d}" for i in range(1, 5)]
    session = {
        "scenario": "xiao-rear",
        "events": [
            {**labelled(i * 0.1, tag, "standing-none", BAD, "후면"), "states": [BAD], "reasons": []}
            for i, tag in enumerate(tags, start=1)
        ],
    }
    (tmp_path / "session.json").write_text(json.dumps(session), encoding="utf-8")
    box = (0, 50, 60, 300)
    caches = {
        # 옛 모델: 두 장은 적합으로 오판하고 한 장은 머리가 크롭 위에 닿아 확인불가다.
        "old": [WORN, WORN, CLIPPED, NO_BOTH],
        "new": [NO_BOTH] * 4,
    }
    args = ["compare", str(tmp_path / "session.json"), "--plan", str(plan)]
    for name, dets in caches.items():
        frames = [
            frame(t, [(0.9, box)], {"0.08": [person(d)]}) for t, d in zip(tags, dets, strict=True)
        ]
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"meta": {"conf_floor": 0.1}, "frames": frames}))
        args += ["--cache", f"{name}={path}"]
    out = tmp_path / "compare.md"
    assert ab.main([*args, "--out", str(out)]) == 0

    text = out.read_text(encoding="utf-8")
    assert "| standing-none | 위반 | old | 4 | 1 | 2 | 1 | 1 |" in text
    assert "| standing-none | 위반 | new | 4 | 4 | 0 | 0 | 0 |" in text
    assert "old: 기록 판정과의 프레임별 판정 일치율 25.0%" in text
    assert "new: 기록 판정과의 프레임별 판정 일치율 100.0%" in text
    # 사람 단위 맞대기: old 의 적합 오판 2건과 클리핑 1건을 new 가 모두 위반으로 읽었다.
    assert "| standing-none | 적합 | 0 | 2 | 0 |" in text
    assert "| standing-none | 확인불가 | 0 | 1 | 0 |" in text


def test_compare_rejects_cache_argument_without_name(tmp_path):
    with pytest.raises(SystemExit):
        ab.main(["compare", "session.json", "--cache", str(tmp_path / "cache.json")])


def test_paired_tally_names_the_cause_when_tags_differ():
    a = {"events": [judged_event("00001", [OK])]}
    b = {"events": [judged_event("00002", [OK])]}
    with pytest.raises(ValueError, match="태그"):
        ab.paired_tally(a, b)


def test_paired_tally_rejects_sessions_with_different_segments():
    a = {"events": [judged_event("00001", [OK], seg="standing-none")]}
    b = {"events": [judged_event("00001", [OK], seg="standing-nohelmet")]}
    with pytest.raises(ValueError, match="구간"):
        ab.paired_tally(a, b)


def test_compare_rejects_cache_with_conf_floor_above_base(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"orientation_step_s": 60, "orientations": ["후면"]}))
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"scenario": "xiao-rear", "events": []}))
    cache = tmp_path / "high.json"
    cache.write_text(json.dumps({"meta": {"conf_floor": 0.7}, "frames": []}))
    with pytest.raises(SystemExit, match="high"):
        ab.main(["compare", str(session), "--plan", str(plan), "--cache", f"high={cache}"])


def test_paired_tally_rejects_sessions_with_different_event_counts():
    a = {"events": [judged_event("00001", [OK]), judged_event("00002", [OK])]}
    b = {"events": [judged_event("00001", [OK])]}
    with pytest.raises(ValueError, match="프레임 수"):
        ab.paired_tally(a, b)


def test_report_rejects_cache_with_conf_floor_above_base(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"orientation_step_s": 60, "orientations": ["후면"]}))
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"scenario": "xiao-rear", "events": []}))
    cache = tmp_path / "high.json"
    cache.write_text(json.dumps({"meta": {"conf_floor": 0.7}, "frames": []}))
    with pytest.raises(SystemExit, match="0.7"):
        ab.main(["report", str(session), "--plan", str(plan), "--cache", str(cache)])


def test_compare_creates_missing_parent_folder_of_out(tmp_path):
    out = tmp_path / "새" / "폴더" / "compare.md"
    ab._write_text("표\n", str(out))
    assert out.read_text(encoding="utf-8") == "표\n"


# ── 합격 기준 없는 계획 (위반 구간만 찍은 세션) ─────────────────


def _violation_only(tmp_path):
    """적합 구간 없이 위반 두 구간만 찍은 세션과 기준 없는 계획."""
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "orientation_step_s": 60,
                "orientations": ["후면"],
                "scenarios": {
                    "rear": {
                        "segments": [
                            {"key": "standing-all", "expected": OK},
                            {"key": "standing-nohelmet", "expected": BAD},
                            {"key": "standing-none", "expected": BAD},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    judged = [
        ("standing-nohelmet", [BAD], []),
        ("standing-nohelmet", [BAD], []),
        ("standing-nohelmet", [UNK], ["머리 클리핑"]),
        ("standing-nohelmet", [BAD], []),
        ("standing-nohelmet", [BAD], []),
        ("standing-nohelmet", [UNK], ["머리 미검출"]),
        ("standing-none", [BAD], []),
        ("standing-none", [BAD], []),
        ("standing-none", [BAD], []),
        ("standing-none", [OK], []),
    ]
    events = [
        {
            **labelled(round(i * 0.1, 1), f"{i:05d}", seg, BAD, "후면"),
            "states": states,
            "reasons": reasons,
        }
        for i, (seg, states, reasons) in enumerate(judged, start=1)
    ]
    raw = {"scenario": "rear", "events": events}
    session = ab.replay_recorded(raw, ab.Variant("base"), ["후면"])
    return plan, session


def test_evaluate_variant_without_criteria_counts_every_labelled_segment(tmp_path):
    plan, session = _violation_only(tmp_path)
    row = ab.evaluate_variant(session, plan)
    assert row["criteria"] is None
    # 분모는 세션에 있는 라벨 구간 전체다 (nohelmet 6 + none 4).
    assert (row["determinate"], row["total"]) == (8, 10)
    assert (row["right"], row["count"]) == (7, 10)
    # 스스로 낸 머리 클리핑 확인불가 1건을 분모에서 뺀다.
    assert row["clipped"] == 1
    assert row["clip_excluded_rate"] == pytest.approx(7 / 9)
    assert (row["detected"], row["violation_episodes"]) == (2, 2)
    assert row["latency_n"] == 2
    assert row["normal_episodes"] == 0
    assert row["false_alarm_rate"] is None


def test_report_without_criteria_writes_table_with_dashes(tmp_path):
    plan, session = _violation_only(tmp_path)
    path = tmp_path / "session.json"
    path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "report.md"
    assert ab.main(["report", str(path), "--plan", str(plan), "--out", str(out)]) == 0

    lines = out.read_text(encoding="utf-8").splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("| 변형 |"))
    assert "합격 기준 없음" in lines[header - 2]
    columns = [c.strip() for c in lines[header].strip("|").split("|")]
    base = next(line for line in lines if line.startswith("| base |"))
    cells = dict(zip(columns, (c.strip() for c in base.strip("|").split("|")), strict=True))
    assert cells["판정 가능률"] == "80.0% (8/10)"
    assert cells["실효 성공률"] == "70.0% (7/10)"
    assert (cells["C1 경보"], cells["C2 방향(이월)"], cells["C3"]) == ("-", "-", "-")
    assert cells["recall"] == "100.0% (2/2)"
    assert cells["오경보율"] == "-"
    assert cells["클리핑 제외 실효 성공률"] == "77.8% (7/9)"


def test_clip_excluded_column_is_appended_after_existing_columns(recorded):
    row = ab.evaluate_variant(ab.replay_recorded(recorded, ab.Variant("base")), PLAN)
    lines = ab.table([row], {})
    columns = [c.strip() for c in lines[0].strip("|").split("|")]
    assert columns[-1] == "클리핑 제외 실효 성공률"
    assert columns[-1 - len(ab.REASON_ORDER) : -1] == list(ab.REASON_ORDER)
    assert row["clip_excluded_rate"] == pytest.approx(
        row["right"] / (row["count"] - row["clipped"])
    )
    assert "PASS" in lines[2]  # 기준이 있으면 C1~C3 칸은 그대로다


# ── 단계 누적 (계획서 2단계 표) ─────────────────────────────────


def test_stages_add_one_step_at_a_time():
    """Raw → 사람 크롭 → 머리 클리핑 필터 → 시간 누적. 앞 단계와 한 축씩만 다르고 끝은 base 다."""
    raw, crop, clip, vote = ab.STAGES
    assert (raw.gate, raw.clip, raw.hits_required) == (False, "off", 1)
    assert (crop.gate, crop.clip, crop.hits_required) == (True, "off", 1)
    assert (clip.gate, clip.clip, clip.hits_required) == (True, "crop", 1)
    assert dataclasses.replace(vote, name=ab.BASE.name, note=ab.BASE.note) == ab.BASE
    for before, after in zip(ab.STAGES, ab.STAGES[1:], strict=False):
        changed = [
            f.name
            for f in dataclasses.fields(ab.Variant)
            if f.name not in ("name", "note") and getattr(before, f.name) != getattr(after, f.name)
        ]
        assert len(changed) == 1, (before.name, after.name, changed)


def test_report_with_cache_writes_stage_table(tmp_path):
    plan, session = _violation_only(tmp_path)
    path = tmp_path / "session.json"
    path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    keys = {v.crop for v in ab.VARIANTS}
    # 사람 크롭에서는 맨머리를 보고, 풀프레임에서는 아무것도 못 본다.
    frames = [
        frame(e["tag"], [(0.9, (0, 50, 60, 200))], {k: [person(NO_HELMET)] for k in keys})
        for e in session["events"]
    ]
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"meta": {"conf_floor": 0.1}, "frames": frames}), encoding="utf-8")
    out = tmp_path / "report.md"
    args = ["report", str(path), "--plan", str(plan), "--cache", str(cache), "--out", str(out)]
    assert ab.main(args) == 0

    lines = out.read_text(encoding="utf-8").splitlines()
    start = lines.index("## 단계 누적 (계획서 2단계 표)")
    rows = [line for line in lines[start:] if line.startswith("| stage-")]
    assert [r.split("|")[1].strip() for r in rows] == [v.name for v in ab.STAGES]
    assert "0.0% (0/10)" in rows[0]  # 풀프레임은 판정 못 함
    assert "100.0% (10/10)" in rows[1]  # 사람 크롭은 모두 위반으로 맞힘
    replayed = lines.index("## 다시 추론한 결과 (캐시)")
    base = next(line for line in lines[replayed:] if line.startswith("| base |"))
    assert rows[-1].split("|")[3:] == base.split("|")[3:]  # 마지막 단계 = base 재추론
