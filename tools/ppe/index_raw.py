"""공개 PPE 데이터셋 3종을 4클래스 규약으로 통합하고 중복을 걸러 색인한다.

WBS 3.7.1 · FR-9.1 의 전처리 1단계다. 이 스크립트는 **원본을 고치지 않는다** —
`datasets/ppe/raw/<ds>/` 를 읽어 `datasets/ppe/index.json` 하나를 쓴다.

⚠️ **데이터셋마다 클래스 이름이 다르다.** 이름을 손으로 맞추면 언젠가 하나가
빠지고, 빠진 클래스는 조용히 배경으로 학습된다. 그래서 매핑표를 여기 한 곳에
두고 **표에 없는 이름이 나오면 멈춘다.**

⚠️ **ds2 에는 조끼 라벨이 아예 없다.** 그대로 합치면 조끼를 입은 몸통이 배경으로
학습된다. 그래서 여기서는 `torso_source` 로 표시만 해 두고, 보강은 다음 단계
(`augment_torso.py`)가 한다.
"""

from __future__ import annotations

import collections
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "datasets" / "ppe" / "raw"
OUT = ROOT / "datasets" / "ppe" / "index.json"

#: 우리 4클래스. `person` 은 ①번 검출기가 담당하므로 학습 대상이 아니다 (FR-9.1.1).
CLASSES = ("helmet", "no_helmet", "vest", "no_vest")

#: 데이터셋별 클래스 이름 → 우리 이름. `None` 은 **의도적으로 버리는 것**이다.
#: ⚠️ 하네스는 조끼가 아니다 (라벨링 규약). 장갑·고글·장화는 FR-9 대상이 아니다.
MAPPING: dict[str, dict[str, str | None]] = {
    "ds1": {
        "helmet-vest": None,  # 슈퍼카테고리 자리표시자
        "helmet": "helmet",
        "no-helmet": "no_helmet",
        "vest": "vest",
        "no-vest": "no_vest",
        "boots": None,
        "no-boots": None,
        "gloves": None,
        "no-gloves": None,
        "goggles": None,
        "no-goggles": None,
    },
    "ds2": {
        "PPE": None,
        "Helmet": "helmet",
        "No_Helmet": "no_helmet",
        "Person": "person",  # 학습 대상 아님 — 몸통 위치 추정에만 쓴다
        "Glove": None,
        "No_Glove": None,
        "Goggles": None,
        "No_Goggles": None,
        "Safety_Harness": None,
        "No_Harness": None,
        "BreathingApparatus": None,
        "No_BreathingApparatus": None,
        "boots": None,
        "no boots": None,
    },
    "ds3": {
        "vest-novest": None,
        "helmet": "helmet",
        "no-helmet": "no_helmet",
        "vest": "vest",
        "no-vest": "no_vest",
    },
    "ds4": {
        "objects": None,
        "No Helmet": "no_helmet",
        "No Vest": "no_vest",
        "Person": "person",
    },
}

#: 몸통 라벨의 출처. ds2 만 보강 대상이다.
#: ⚠️ ds4 는 **위반 사진만 모은 세트**라 `helmet`·`vest` 가 아예 없다. 미라벨이
#: 아니라 그런 대상이 없는 것으로 본다 — 세트 성격상 전원이 미착용이다.
TORSO_SOURCE = {"ds1": "annotated", "ds2": "missing", "ds3": "annotated", "ds4": "annotated"}


def imread_any(path: Path, flags: int):
    """⚠️ **`cv2.imread` 를 쓰지 않는다.** Windows 에서 경로에 한글이 있으면 조용히
    `None` 을 돌려준다 — 오류가 아니라 빈 값이라 검사가 통째로 무력화된다.
    이 저장소의 실제 경로가 `바탕 화면` 이므로 여기서 실제로 걸렸다."""
    import cv2
    import numpy as np

    buf = np.fromfile(str(path), dtype=np.uint8)
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, flags)


def dhash(path: Path, size: int = 8) -> str:
    """지각 해시. **바이트 해시만으로는 부족하다** — 같은 사진이 데이터셋마다
    다시 인코딩돼 들어와서 바이트가 달라진다."""
    import cv2

    img = imread_any(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return ""
    small = cv2.resize(img, (size + 1, size))
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return "".join("1" if b else "0" for b in bits)


def load_split(ds: str, split: str) -> list[dict]:
    ann_path = RAW / ds / split / "_annotations.coco.json"
    if not ann_path.exists():
        return []
    data = json.loads(ann_path.read_text(encoding="utf-8"))
    cats = {c["id"]: c["name"] for c in data["categories"]}

    unknown = set(cats.values()) - set(MAPPING[ds])
    if unknown:
        raise SystemExit(f"{ds}/{split}: 매핑표에 없는 클래스 {sorted(unknown)}")

    boxes: dict[int, list] = collections.defaultdict(list)
    for a in data["annotations"]:
        name = MAPPING[ds][cats[a["category_id"]]]
        if name is None:
            continue
        x, y, w, h = a["bbox"]
        boxes[a["image_id"]].append({"cls": name, "bbox": [x, y, w, h]})

    out = []
    for im in data["images"]:
        out.append(
            {
                "ds": ds,
                "split": split,
                "file": f"{ds}/{split}/{im['file_name']}",
                "w": im["width"],
                "h": im["height"],
                "boxes": boxes.get(im["id"], []),
                "torso_source": TORSO_SOURCE[ds],
            }
        )
    return out


def main() -> int:
    #: 해시 재계산은 19,000장 디코드라 몇 분이 든다. 한 번 계산한 값을 재사용한다.
    cache_path = ROOT / "datasets" / "ppe" / "hashes.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    records: list[dict] = []
    for ds in ("ds1", "ds2", "ds3", "ds4"):
        for split in ("train", "valid", "test"):
            records.extend(load_split(ds, split))
    print(f"원본 합계: {len(records)}장")

    seen_bytes: dict[str, str] = {}
    seen_hash: dict[str, str] = {}
    kept: list[dict] = []
    dup_exact = dup_near = 0

    for i, r in enumerate(records, 1):
        if i % 2000 == 0:
            print(f"  중복 검사 {i}/{len(records)}")
        path = RAW / r["file"]
        hit = cache.get(r["file"])
        digest = hit["md5"] if hit else hashlib.md5(path.read_bytes()).hexdigest()
        if digest in seen_bytes:
            dup_exact += 1
            continue
        seen_bytes[digest] = r["file"]

        ph = hit["dh"] if hit else dhash(path)
        cache[r["file"]] = {"ds": r["ds"], "md5": digest, "dh": ph}
        if ph and ph in seen_hash:
            dup_near += 1
            continue
        if ph:
            seen_hash[ph] = r["file"]

        r["dhash"] = ph
        kept.append(r)

    print(f"바이트 중복 {dup_exact}장 · 지각 중복 {dup_near}장 제거 → {len(kept)}장")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    print(f"기록: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
