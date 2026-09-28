"""PPE 재학습 도구(`tools/ppe/`)의 순수 규칙 검증.

⚠️ **torch·yolox·모델 파일 없이 돈다.** 학습 환경은 저장소 밖에 있고 CI 에는 없다.
여기서 지키는 것은 추론이 아니라 **데이터 규칙**이다 — 매핑표에 없는 이름이면 멈추는가,
크롭이 런타임과 같은가, 부분 라벨 사람을 빼는가, 세션 프레임이 test 로 새지 않는가.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.vision.detector import Detection  # noqa: E402
from tools.ppe import export_ppe, rf100_prepare, xiao_hardcases  # noqa: E402
from tools.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_OK,
    STATE_VIOLATION,
    crop_person,
    load_acceptance_plan,
)

PERSON = (10.0, 20.0, 50.0, 120.0)  # 폭 40 · 높이 100
HELMET = (22.0, 18.0, 38.0, 34.0)  # 머리 — 사람 박스 위로 조금 삐져나옴
VEST = (14.0, 50.0, 46.0, 90.0)  # 몸통 55~70% 구간


# ── rf100_prepare: 이름 매핑 ─────────────────────────────────
def test_mapping_normalizes_roboflow_spellings():
    cats = [
        {"id": 0, "name": "workers", "supercategory": "none"},
        {"id": 1, "name": "Helmet", "supercategory": "workers"},
        {"id": 2, "name": "No-Helmet", "supercategory": "workers"},
        {"id": 3, "name": "no vest", "supercategory": "workers"},
        {"id": 4, "name": "vest", "supercategory": "workers"},
        {"id": 5, "name": "person", "supercategory": "workers"},
    ]
    assert rf100_prepare.map_categories(cats) == {
        0: None,
        1: "helmet",
        2: "no_helmet",
        3: "no_vest",
        4: "vest",
        5: "person",
    }


def test_mapping_stops_on_unknown_name():
    cats = [{"id": 1, "name": "helmet"}, {"id": 2, "name": "gloves"}]
    with pytest.raises(SystemExit, match="gloves"):
        rf100_prepare.map_categories(cats, "x.json")


# ── rf100_prepare: 크롭 좌표와 50% 규칙 ──────────────────────
def test_crop_rect_matches_runtime_crop():
    image = np.zeros((200, 100, 3), dtype=np.uint8)
    for pad in (0.0, 0.08, 0.2):
        crop, (left, top) = crop_person(image, PERSON, pad)
        rect = rf100_prepare.crop_rect(100, 200, PERSON, pad)
        assert rect == (left, top, left + crop.shape[1], top + crop.shape[0])
    # 경계 클램프
    assert rf100_prepare.crop_rect(100, 200, (0, 0, 100, 200), 0.08) == (0, 0, 100, 200)
    # 너무 작으면 런타임처럼 실패
    assert rf100_prepare.crop_rect(100, 200, (10, 10, 14, 14), 0.0) is None


def test_to_crop_shifts_and_drops_boxes_under_half():
    rect = (10.0, 20.0, 60.0, 120.0)
    assert rf100_prepare.to_crop((20, 30, 30, 40), rect) == (10, 10, 20, 20)
    # 60% 남음 → 잘라서 남긴다
    assert rf100_prepare.to_crop((54, 30, 64, 40), rect) == (44, 10, 50, 20)
    # 40% 남음 → 버린다
    assert rf100_prepare.to_crop((56, 30, 66, 40), rect) is None


# ── rf100_prepare: 부분 라벨 검사 ────────────────────────────
def test_person_label_check_requires_head_and_torso():
    check = rf100_prepare.person_label_problem
    assert check(PERSON, [("helmet", HELMET), ("no_vest", VEST)]) is None
    assert check(PERSON, [("vest", VEST)]) == rf100_prepare.REASON_NO_HEAD
    assert check(PERSON, [("no_helmet", HELMET)]) == rf100_prepare.REASON_NO_TORSO
    # 몸통 라벨이 머리 자리에 있으면 몸통 라벨로 보지 않는다
    assert check(PERSON, [("helmet", HELMET), ("vest", HELMET)]) == rf100_prepare.REASON_NO_TORSO
    # 다른 사람의 머리(가로로 벗어남)는 이 사람 머리가 아니다
    far_head = (70.0, 18.0, 86.0, 34.0)
    assert check(PERSON, [("helmet", far_head), ("vest", VEST)]) == rf100_prepare.REASON_NO_HEAD


# ── rf100_prepare: 가짜 COCO 로 끝까지 ──────────────────────
def _write_image(path: Path, seed: int) -> None:
    import cv2

    rng = np.random.default_rng(seed)
    image = rng.integers(0, 255, (200, 100, 3), dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), image)


def _fake_split(folder: Path, images: list[tuple[str, int, list]]) -> None:
    cats = [
        {"id": 0, "name": "workers", "supercategory": "none"},
        {"id": 1, "name": "helmet", "supercategory": "workers"},
        {"id": 2, "name": "no-vest", "supercategory": "workers"},
        {"id": 3, "name": "vest", "supercategory": "workers"},
        {"id": 4, "name": "person", "supercategory": "workers"},
    ]
    coco = {"images": [], "annotations": [], "categories": cats}
    for image_id, (name, seed, anns) in enumerate(images, 1):
        _write_image(folder / name, seed)
        coco["images"].append({"id": image_id, "file_name": name, "width": 100, "height": 200})
        for cat, (x1, y1, x2, y2) in anns:
            coco["annotations"].append(
                {
                    "id": len(coco["annotations"]) + 1,
                    "image_id": image_id,
                    "category_id": cat,
                    "bbox": [x1, y1, x2 - x1, y2 - y1],
                }
            )
    (folder / "_annotations.coco.json").write_text(json.dumps(coco), encoding="utf-8")


def _fake_raw(root: Path, license_line: str = "License: CC BY 4.0") -> Path:
    raw = root / "raw"
    full = [(4, PERSON), (1, HELMET), (3, VEST)]
    no_head = [(4, PERSON), (2, VEST)]
    _fake_split(raw / "test", [("t1.jpg", 1, full)])
    _fake_split(raw / "valid", [("v1.jpg", 2, full)])
    # 1번 사진은 test 와 바이트가 같다 → 학습 쪽에서 빠진다. 2번은 머리 라벨이 없다.
    _fake_split(raw / "train", [("a.jpg", 1, full), ("b.jpg", 3, no_head), ("c.jpg", 4, full)])
    (raw / "README.roboflow.txt").write_text(
        f"https://universe.roboflow.com/example/construction-safety\n{license_line}\n",
        encoding="utf-8",
    )
    return raw


def test_prepare_builds_yolox_layout_and_card(tmp_path):
    raw = _fake_raw(tmp_path)
    out = tmp_path / "build" / "v0"
    card = rf100_prepare.prepare(raw, out, "v0", seed=1)

    for split, folder in rf100_prepare.IMAGE_DIRS.items():
        ann = out / "annotations" / f"instances_{split}.json"
        # pycocotools 는 인코딩 없이 open() 한다 — Windows(cp949)에서 한글이 있으면 학습이 멈춘다
        ann.read_bytes().decode("ascii")
        coco = json.loads(ann.read_text("utf-8"))
        # 카테고리 id 순서 = 우리 4클래스 순서 (YOLOX 는 id 를 정렬해 클래스 번호로 쓴다)
        assert [c["name"] for c in sorted(coco["categories"], key=lambda c: c["id"])] == list(
            rf100_prepare.CLASSES
        )
        for image in coco["images"]:
            assert (out / folder / image["file_name"]).is_file()
    stats = card["splits"]
    assert stats["train"]["duplicates"] == 1
    assert stats["train"]["excluded"] == {rf100_prepare.REASON_NO_HEAD: 1}
    assert stats["train"]["images"] == 1
    assert stats["test"]["boxes"] == {"helmet": 1, "vest": 1}
    assert card["source"]["license"] == "CC BY 4.0"
    assert (out / "data_card.json").is_file()

    # 검증·시험은 런타임 여유(0.08) 그대로 자른다
    val = json.loads((out / "annotations" / "instances_val.json").read_text("utf-8"))
    rect = rf100_prepare.crop_rect(100, 200, PERSON, rf100_prepare.RUNTIME_PAD)
    assert (val["images"][0]["width"], val["images"][0]["height"]) == (
        rect[2] - rect[0],
        rect[3] - rect[1],
    )


def test_prepare_refuses_non_commercial_license(tmp_path):
    raw = _fake_raw(tmp_path, "License: CC BY-NC 4.0")
    with pytest.raises(SystemExit, match="라이선스"):
        rf100_prepare.prepare(raw, tmp_path / "out", "v0")


# ── xiao_hardcases ──────────────────────────────────────────
def test_relabel_all_worn_truth_turns_violations_into_worn_and_dedupes():
    found = [
        Detection("no_helmet", 0.8, (10, 10, 30, 30)),
        Detection("helmet", 0.6, (11, 10, 31, 30)),  # 같은 머리
        Detection("no_vest", 0.7, (5, 40, 40, 90)),
        Detection("class_4", 0.9, (0, 0, 50, 100)),  # 5클래스 모델의 5번째
    ]
    out = xiao_hardcases.relabel_by_truth(found, helmet=True, vest=True)
    assert sorted((d.label, d.score) for d in out) == [
        ("helmet", pytest.approx(0.8)),
        ("vest", pytest.approx(0.7)),
    ]
    assert xiao_hardcases.has_head_and_torso(out)
    assert not xiao_hardcases.has_head_and_torso(out[:1])


def _xiao_truths():
    specs, _, _ = load_acceptance_plan(DEFAULT_ACCEPTANCE_PLAN, "xiao")
    return xiao_hardcases.segment_truths(specs)


def test_segment_truths_reads_wear_from_acceptance_plan():
    """착용 조합 8구간(직립·웅크림 × 4)을 쓰고, 확인불가·로봇 자세 구간은 이유와 함께 뺀다."""
    used, excluded = _xiao_truths()
    assert set(used) == {
        f"{pose}-{wear}"
        for pose in ("standing", "crouching")
        for wear in ("all", "nohelmet", "novest", "none")
    }
    assert (used["standing-all"].helmet, used["standing-all"].vest) == (True, True)
    assert (used["crouching-nohelmet"].helmet, used["crouching-nohelmet"].vest) == (False, True)
    assert (used["standing-novest"].helmet, used["standing-novest"].vest) == (True, False)
    assert (used["crouching-none"].helmet, used["crouching-none"].vest) == (False, False)
    assert used["standing-none"].expected == STATE_VIOLATION
    assert set(excluded) == {"clipped-base", "pitch-up", "sit"}
    assert "확인불가" in excluded["clipped-base"]
    assert "로봇 자세" in excluded["pitch-up"] and "로봇 자세" in excluded["sit"]


def test_segment_truths_refuses_unknown_wear_and_inconsistent_expectation():
    specs = [
        {"key": "a", "condition": "직립·전신", "wear": "모자만", "expected": STATE_VIOLATION},
        {"key": "b", "condition": "직립·전신", "wear": "안전모 미착용", "expected": STATE_OK},
        {"key": "c", "condition": "직립·전신", "wear": "전부 착용", "expected": STATE_VIOLATION},
    ]
    used, excluded = xiao_hardcases.segment_truths(specs)
    assert used == {}
    assert "착용 문구" in excluded["a"]
    assert "어긋" in excluded["b"] and "어긋" in excluded["c"]


def test_truth_events_keeps_known_segments_with_people():
    used, _ = _xiao_truths()
    events = [
        {"t": 1.0, "tag": "00001", "people": 1, "segment": None, "expected": None},
        {"t": 2.0, "tag": "00002", "people": 1, "segment": "standing-all", "expected": STATE_OK},
        {"t": 3.0, "tag": "00003", "people": 0, "segment": "standing-none", "expected": "위반"},
        {"t": 4.0, "tag": "00004", "people": 1, "segment": "standing-none", "expected": "위반"},
        {"t": 5.0, "tag": "00005", "people": 1, "segment": "pitch-up", "expected": STATE_OK},
        {"t": 6.0, "tag": "00006", "people": 1, "segment": "clipped-base", "expected": "확인불가"},
    ]
    assert [e["tag"] for e in xiao_hardcases.truth_events(events, used)] == ["00002", "00004"]


def test_truth_events_stops_when_plan_disagrees_with_session():
    """세션이 기록한 기대 판정과 지금 구간 정의가 다르면 정의가 바뀐 것이다 — 멈춘다."""
    used, _ = _xiao_truths()
    events = [{"t": 1.0, "tag": "1", "people": 1, "segment": "standing-novest", "expected": "적합"}]
    with pytest.raises(SystemExit):
        xiao_hardcases.truth_events(events, used)


def test_relabel_by_truth_names_boxes_from_segment_not_model():
    found = [
        Detection("helmet", 0.8, (10, 10, 30, 30)),  # 모델은 착용이라 했지만 맨머리 구간
        Detection("no_helmet", 0.6, (11, 10, 31, 30)),  # 같은 머리
        Detection("no_vest", 0.7, (5, 40, 40, 90)),  # 조끼는 입은 구간
        Detection("class_4", 0.9, (0, 0, 50, 100)),
    ]
    out = xiao_hardcases.relabel_by_truth(found, helmet=False, vest=True)
    assert sorted((d.label, d.score) for d in out) == [
        ("no_helmet", pytest.approx(0.8)),
        ("vest", pytest.approx(0.7)),
    ]
    both_off = xiao_hardcases.relabel_by_truth(found, helmet=False, vest=False)
    assert sorted(d.label for d in both_off) == ["no_helmet", "no_vest"]
    assert xiao_hardcases.has_head_and_torso(both_off)


def test_is_hard_marks_session_or_model_disagreement_with_truth():
    helmet = Detection("helmet", 0.9, (0, 0, 1, 1))
    no_helmet = Detection("no_helmet", 0.9, (0, 0, 1, 1))
    vest = Detection("vest", 0.9, (0, 2, 1, 3))
    truth_ok = xiao_hardcases.SegmentTruth(True, True, STATE_OK)
    truth_bare = xiao_hardcases.SegmentTruth(False, True, STATE_VIOLATION)
    # 전부 착용 — 예전 규칙: 세션 위반 또는 모델이 no_* 를 냈으면 틀림
    assert not xiao_hardcases.is_hard([STATE_OK], [helmet, vest], truth_ok)
    assert xiao_hardcases.is_hard([STATE_VIOLATION], [helmet, vest], truth_ok)
    assert xiao_hardcases.is_hard([STATE_OK], [no_helmet, vest], truth_ok)
    assert not xiao_hardcases.is_hard(["확인불가"], [helmet, vest], truth_ok)
    # 맨머리 — 세션이 적합이라 했거나 모델이 helmet 을 냈으면 틀림
    assert not xiao_hardcases.is_hard([STATE_VIOLATION], [no_helmet, vest], truth_bare)
    assert xiao_hardcases.is_hard([STATE_OK], [no_helmet, vest], truth_bare)
    assert xiao_hardcases.is_hard([STATE_VIOLATION], [helmet, vest], truth_bare)


def test_relabel_by_truth_drops_boxes_outside_their_body_part():
    """모델 이름 계열만 보면 몸통·배경에 그어진 머리 박스도 라벨이 된다 — 부위로 막는다."""
    import collections

    found = [
        Detection("no_helmet", 0.7, HELMET),  # 제자리 머리
        Detection("helmet", 0.9, (14.0, 60.0, 46.0, 90.0)),  # 몸통에 그어진 «머리»
        Detection("vest", 0.8, VEST),  # 제자리 몸통
        Detection("vest", 0.6, (60.0, 20.0, 90.0, 60.0)),  # 사람 밖 배경의 «몸통»
    ]
    drops: collections.Counter = collections.Counter()
    out = xiao_hardcases.relabel_by_truth(found, True, True, person=PERSON, drops=drops)
    assert sorted((d.label, d.box) for d in out) == [("helmet", HELMET), ("vest", VEST)]
    assert drops == {xiao_hardcases.DROP_HEAD_PLACE: 1, xiao_hardcases.DROP_TORSO_PLACE: 1}


def test_relabel_by_truth_drops_head_and_torso_on_the_same_spot():
    """이름을 바꾼 뒤 같은 자리에 머리·몸통 두 계열이 있으면 어느 쪽인지 모른다 — 둘 다 버린다."""
    import collections

    spot = (12.0, 35.0, 48.0, 60.0)  # 머리·몸통 구간 경계 — 두 구간 IoA 모두 0.5 이상
    found = [
        Detection("helmet", 0.9, HELMET),
        Detection("no_helmet", 0.8, spot),
        Detection("vest", 0.7, (12.0, 36.0, 48.0, 61.0)),
    ]
    drops: collections.Counter = collections.Counter()
    out = xiao_hardcases.relabel_by_truth(found, True, True, person=PERSON, drops=drops)
    assert [(d.label, d.box) for d in out] == [("helmet", HELMET)]
    assert drops == {xiao_hardcases.DROP_CROSS: 2}
    assert not xiao_hardcases.has_head_and_torso(out)


class _FixedDetector:
    def __init__(self, results):
        self.results = results
        self.inputs = []

    def detect(self, image):
        self.inputs.append(image.shape[:2])
        return list(self.results)


def test_collect_checks_body_part_in_crop_coordinates(tmp_path):
    """PPE 박스는 크롭 좌표다 — 사람 박스를 크롭 원점만큼 옮겨서 부위를 본다."""
    frame = np.full((300, 400, 3), 90, dtype=np.uint8)
    rf100_prepare.write_jpeg(tmp_path / "raw" / "00001.jpg", frame)
    person = (100.0, 50.0, 200.0, 250.0)  # pad 0.08 → 크롭 원점 (92, 34), 크롭 안 (8,16,108,216)
    coco = _FixedDetector([Detection("person", 0.9, person)])
    ppe = _FixedDetector(
        [
            Detection("no_helmet", 0.9, (40.0, 10.0, 70.0, 40.0)),  # 크롭 안 머리
            Detection("vest", 0.8, (20.0, 90.0, 90.0, 160.0)),  # 크롭 안 몸통
            Detection("helmet", 0.7, (40.0, 100.0, 70.0, 130.0)),  # 몸통 위 «머리»
        ]
    )
    config = {"vision": {"coco": {"person_class": "person"}, "ppe": {"crop_pad": 0.08}}}
    used, _ = _xiao_truths()
    events = [{"t": 1.0, "tag": "00001", "segment": "standing-novest", "states": ["위반"]}]
    cands, stats = xiao_hardcases.collect(tmp_path, events, used, config, coco, ppe)
    assert ppe.inputs == [(232, 116)]  # 크롭이 들어갔다
    assert len(cands) == 1
    assert sorted(cands[0].boxes) == [
        ("helmet", (40.0, 10.0, 70.0, 40.0)),
        ("no_vest", (20.0, 90.0, 90.0, 160.0)),
    ]
    assert stats["dropped_boxes"] == {xiao_hardcases.DROP_HEAD_PLACE: 1}


def test_segment_table_counts_per_segment():
    used, excluded = _xiao_truths()
    events = [
        {"t": 1.0, "tag": "1", "segment": "standing-all"},
        {"t": 2.0, "tag": "2", "segment": "standing-all"},
        {"t": 3.0, "tag": "3", "segment": "standing-none"},
    ]
    cands = [
        xiao_hardcases.Candidate("1", 1.0, False, (0, 0, 1, 1), (), "standing-all"),
        xiao_hardcases.Candidate("3", 3.0, True, (0, 0, 1, 1), (), "standing-none"),
    ]
    table = xiao_hardcases.segment_table(used, excluded, events, cands, cands, ["train", "val"])
    row = table["used"]["standing-none"]
    assert row["frames"] == 1 and row["candidates"] == 1 and row["candidates_hard"] == 1
    assert row["selected"] == {"train": 0, "val": 1}
    assert row["wear"] == "둘 다 미착용"
    assert table["used"]["standing-all"]["frames"] == 2
    assert table["used"]["standing-all"]["selected"] == {"train": 1, "val": 0}
    assert table["used"]["crouching-all"]["frames"] == 0
    assert table["excluded"] == excluded


def _cand(t: float, hard: bool) -> xiao_hardcases.Candidate:
    return xiao_hardcases.Candidate(f"{int(t * 10):05d}", t, hard, (0, 0, 1, 1), ())


def test_select_frames_keeps_hard_and_enforces_gap():
    cands = [_cand(i * 0.1, hard=i in (3, 4, 30)) for i in range(100)]  # 0.0~9.9초
    chosen = xiao_hardcases.select_frames(cands, keep_ratio=0.3, min_gap_s=0.5, seed=7)
    times = [cands[i].t for i in chosen]
    assert times == sorted(times)
    assert all(b - a >= 0.5 - 1e-9 for a, b in zip(times, times[1:], strict=False))
    # 틀린 프레임은 간격이 허락하는 한 들어간다 (3·4 는 0.1초 차이라 하나만)
    hard_taken = [cands[i].tag for i in chosen if cands[i].hard]
    assert "00003" in hard_taken and "00030" in hard_taken and "00004" not in hard_taken
    assert any(not cands[i].hard for i in chosen)


def test_block_split_never_emits_test():
    times = [i * 0.5 for i in range(200)]  # 100초 · 10초 블록 10개
    splits = xiao_hardcases.split_by_block(times, block_s=10, val_ratio=0.25, seed=3)
    assert set(splits) == {"train", "val"}
    by_block: dict[int, set[str]] = {}
    for t, s in zip(times, splits, strict=True):
        by_block.setdefault(int(t // 10), set()).add(s)
    assert all(len(v) == 1 for v in by_block.values())  # 블록 하나는 한쪽에만
    assert sum(1 for v in by_block.values() if v == {"val"}) == 3
    # 블록이 하나뿐이면 전부 학습 — 검증을 만들려고 test 로 새지 않는다
    assert set(xiao_hardcases.split_by_block([1.0, 2.0], 10, 0.5)) == {"train"}


def test_overlay_fraction_detects_drawn_judgement_box():
    import cv2

    rng = np.random.default_rng(0)
    clean = rng.integers(60, 180, (240, 320, 3), dtype=np.uint8)
    drawn = clean.copy()
    cv2.rectangle(drawn, (100, 40), (180, 220), (0, 200, 0), 2)
    ok, buf = cv2.imencode(".jpg", drawn, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    drawn = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    box = (101.0, 42.0, 179.0, 218.0)  # 다시 추론한 박스는 몇 px 어긋난다
    assert xiao_hardcases.overlay_fraction(drawn, box) > 0.9
    assert xiao_hardcases.overlay_fraction(clean, box) < 0.2


def test_merge_coco_renumbers_and_checks_categories():
    base = rf100_prepare.empty_coco("train", "v0")
    rf100_prepare.add_image(base, "a.jpg", 10, 20, [("helmet", (1, 1, 5, 5))])
    extra = rf100_prepare.empty_coco("train", "xiao")
    rf100_prepare.add_image(
        extra, "x.jpg", 10, 20, [("vest", (1, 1, 5, 5)), ("helmet", (2, 2, 4, 4))]
    )
    merged = xiao_hardcases.merge_coco(base, [extra])
    assert [i["id"] for i in merged["images"]] == [1, 2]
    assert [a["id"] for a in merged["annotations"]] == [1, 2, 3]
    assert [a["image_id"] for a in merged["annotations"]] == [1, 2, 2]
    bad = {**extra, "categories": extra["categories"][:2]}
    with pytest.raises(SystemExit):
        xiao_hardcases.merge_coco(base, [bad])


# ── export_ppe ──────────────────────────────────────────────
def test_expected_output_shape_is_raw_4class_yolox():
    assert export_ppe.expected_output_shape(4) == [1, 8400, 9]


def test_load_frame_prefers_raw_over_drawn(tmp_path):
    """`--save-raw-dir <세션>/raw` 원본이 있으면 판정을 그린 `frames/` 대신 그것을 읽는다."""
    import cv2

    (tmp_path / "frames").mkdir()
    (tmp_path / "raw").mkdir()
    cv2.imwrite(str(tmp_path / "frames" / "00001.jpg"), np.full((8, 8, 3), 255, np.uint8))
    assert xiao_hardcases.load_frame(tmp_path, "00001").min() > 200
    cv2.imwrite(str(tmp_path / "raw" / "00001.jpg"), np.zeros((8, 8, 3), np.uint8))
    assert xiao_hardcases.load_frame(tmp_path, "00001").max() < 30


def test_exp_falls_back_to_standard_cocoeval():
    """학습 중 평가가 C++ 즉석 빌드(`where cl`)에 막히지 않게 표준 COCOeval 을 쓴다."""
    pytest.importorskip("yolox")  # 학습 환경(C:\dev\ppe-train\.venv)에서만 돈다
    import importlib.util

    import yolox.layers
    from pycocotools.cocoeval import COCOeval

    path = Path(__file__).resolve().parents[1] / "tools" / "ppe" / "yolox_exp_ppe_s.py"
    spec = importlib.util.spec_from_file_location("yolox_exp_ppe_s", path)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert yolox.layers.COCOeval_opt is COCOeval
