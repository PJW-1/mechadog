"""4개 공개 세트를 하나로 합쳐 **위반 케이스 4분류 폴더**로 내보낸다.

FR-9 의 알람은 `PPE_VIOLATION` 하나이고 위반 클래스는 `{no_helmet, no_vest}` 다.
그래서 사진을 나누는 축도 "어떤 위반이 찍혀 있는가" 로 잡는다.

    1_compliant    위반 클래스 없음 — helmet·vest 만
    2_no_helmet    no_helmet 만
    3_no_vest      no_vest 만
    4_no_both      둘 다

⚠️ **버킷 상한을 두는 이유는 상관관계를 끊기 위해서다.** 정상 착용 사진이 압도적이면
모델이 "머리에 안전모가 있으면 몸통에도 조끼가 있다" 를 배운다. 그러면 한쪽만 위반한
사람에게서 틀린다 — 현장에서 가장 흔한 경우다.

⚠️ **원거리 군중 사진을 뺀다.** 우리 카메라는 15cm 높이에서 1~3m 앞의 사람을 본다.
박스가 화면의 0.1% 도 안 되는 사진은 그 조건과 너무 달라 학습을 흐린다.
"""

from __future__ import annotations

import collections
import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "datasets" / "ppe" / "raw"
INDEX = ROOT / "datasets" / "ppe" / "index.json"
AUG = ROOT / "datasets" / "ppe" / "ds2_torso.json"
DEST = Path.home() / "OneDrive" / "바탕 화면" / "PPE_dataset_v1"

CLASSES = ("helmet", "no_helmet", "vest", "no_vest")

#: 버킷별 상한. 가장 적은 `2_no_helmet`(252장)의 4배 언저리로 맞춘다.
CAPS = {"1_compliant": 1000, "2_no_helmet": 10**9, "3_no_vest": 1000, "4_no_both": 10**9}

#: 이 면적(화면 대비 %) 미만의 박스만 있는 사진은 뺀다.
MIN_BOX_AREA_PCT = 0.1

SEED = 20260918


def bucket_of(classes: set[str]) -> str:
    nh, nv = "no_helmet" in classes, "no_vest" in classes
    if nh and nv:
        return "4_no_both"
    if nh:
        return "2_no_helmet"
    if nv:
        return "3_no_vest"
    return "1_compliant"


def load_merged() -> list[dict]:
    records = json.loads(INDEX.read_text(encoding="utf-8"))
    aug = {r["file"]: r["torsos"] for r in json.loads(AUG.read_text(encoding="utf-8"))}
    out = []
    for r in records:
        if r["ds"] == "ds2":
            if r["file"] not in aug:
                continue  # 몸통을 확신 못 한 ds2 사진은 버린다
            boxes = [b for b in r["boxes"] if b["cls"] != "person"]
            boxes += [{"cls": t["cls"], "bbox": t["bbox"]} for t in aug[r["file"]]]
        else:
            boxes = [b for b in r["boxes"] if b["cls"] != "person"]
        if not boxes:
            continue
        out.append({**r, "boxes": boxes})
    return out


def main() -> int:
    merged = load_merged()
    print(f"통합: {len(merged)}장")

    kept = []
    tiny = 0
    for r in merged:
        area = r["w"] * r["h"]
        if all(b["bbox"][2] * b["bbox"][3] / area * 100 < MIN_BOX_AREA_PCT for b in r["boxes"]):
            tiny += 1
            continue
        kept.append(r)
    print(f"원거리 군중 제외: {tiny}장 → {len(kept)}장")

    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in kept:
        groups[bucket_of({b["cls"] for b in r["boxes"]})].append(r)

    rng = random.Random(SEED)
    final: dict[str, list[dict]] = {}
    for name, items in sorted(groups.items()):
        cap = CAPS[name]
        if len(items) <= cap:
            final[name] = items
            continue
        # 세트별 비율을 유지하며 줄인다 — 한 세트만 남으면 배경이 단조로워진다.
        by_ds: dict[str, list[dict]] = collections.defaultdict(list)
        for r in items:
            by_ds[r["ds"]].append(r)
        picked: list[dict] = []
        for sub in by_ds.values():
            rng.shuffle(sub)
            picked.extend(sub[: max(1, round(cap * len(sub) / len(items)))])
        rng.shuffle(picked)
        final[name] = picked[:cap]

    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    total_inst: collections.Counter = collections.Counter()
    print("\n내보내기")
    for name in ("1_compliant", "2_no_helmet", "3_no_vest", "4_no_both"):
        items = final.get(name, [])
        folder = DEST / name
        folder.mkdir()
        images, annotations, inst = [], [], collections.Counter()
        for i, r in enumerate(items, 1):
            src = RAW / r["file"]
            fname = r["file"].replace("/", "__")
            shutil.copy2(src, folder / fname)
            images.append({"id": i, "file_name": fname, "width": r["w"], "height": r["h"]})
            for b in r["boxes"]:
                x, y, w, h = b["bbox"]
                annotations.append(
                    {
                        "id": len(annotations) + 1,
                        "image_id": i,
                        "category_id": CLASSES.index(b["cls"]) + 1,
                        "bbox": [round(v, 2) for v in (x, y, w, h)],
                        "area": round(w * h, 2),
                        "iscrowd": 0,
                    }
                )
                inst[b["cls"]] += 1
        coco = {
            "info": {"description": f"PPE {name} — mechadog FR-9", "version": "1"},
            "licenses": [{"id": 1, "name": "CC BY 4.0 / Public Domain (출처별 상이)"}],
            "images": images,
            "annotations": annotations,
            "categories": [
                {"id": i + 1, "name": c, "supercategory": "ppe"} for i, c in enumerate(CLASSES)
            ],
        }
        (folder / "_annotations.coco.json").write_text(
            json.dumps(coco, ensure_ascii=False), encoding="utf-8"
        )
        total_inst += inst
        src_mix = dict(collections.Counter(r["ds"] for r in items))
        print(f"  {name:14s} {len(items):5d}장  박스 {dict(inst)}  출처 {src_mix}")

    print(f"\n합계 {sum(len(v) for v in final.values())}장 · 박스 {dict(total_inst)}")
    print(f"위치: {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
