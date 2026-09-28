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
from tools.ppe_live_check import STATE_OK, STATE_VIOLATION, crop_person  # noqa: E402

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
        coco = json.loads((out / "annotations" / f"instances_{split}.json").read_text("utf-8"))
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
def test_all_worn_events_uses_labeled_ok_segments_only():
    events = [
        {"t": 1.0, "tag": "00001", "people": 1, "segment": None, "expected": None},
        {"t": 2.0, "tag": "00002", "people": 1, "segment": "a", "expected": STATE_OK},
        {"t": 3.0, "tag": "00003", "people": 0, "segment": "a", "expected": STATE_OK},
        {"t": 4.0, "tag": "00004", "people": 1, "segment": "b", "expected": STATE_VIOLATION},
    ]
    assert [e["tag"] for e in xiao_hardcases.all_worn_events(events)] == ["00002"]


def test_relabel_all_worn_turns_violations_into_worn_and_dedupes():
    found = [
        Detection("no_helmet", 0.8, (10, 10, 30, 30)),
        Detection("helmet", 0.6, (11, 10, 31, 30)),  # 같은 머리
        Detection("no_vest", 0.7, (5, 40, 40, 90)),
        Detection("class_4", 0.9, (0, 0, 50, 100)),  # 5클래스 모델의 5번째
    ]
    out = xiao_hardcases.relabel_all_worn(found)
    assert sorted((d.label, d.score) for d in out) == [
        ("helmet", pytest.approx(0.8)),
        ("vest", pytest.approx(0.7)),
    ]
    assert xiao_hardcases.has_head_and_torso(out)
    assert not xiao_hardcases.has_head_and_torso(out[:1])


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
