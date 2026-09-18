"""ds2 의 빠진 몸통 라벨을 **확신할 수 있는 것만** 채운다.

ds2 에는 조끼 라벨이 없다. 그대로 합치면 조끼 입은 몸통이 배경으로 학습되므로
그냥 쓸 수 없고, 색상 휴리스틱으로 전부 채우기에는 정확도가 모자란다
(`calibrate_vest.py` 실측: 최적 임계 0.14 에서 양쪽 재현율 0.80).

⚠️ **그래서 전부 채우지 않는다.** 라벨이 있는 세트로 잰 결과 임계 0.30 위에서는
`vest` 정밀도가 0.96 이었다. 그 구간만 쓰고 애매한 것은 **이미지째 버린다** —
한 장 안에 라벨 못 붙인 몸통이 하나라도 남으면 그 장은 쓸 수 없다.

몸통 위치는 `Person` 박스에서 얻는다. 사람 박스 세로의 25~80% 구간이 몸통이다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "datasets" / "ppe" / "raw"
INDEX = ROOT / "datasets" / "ppe" / "index.json"
OUT = ROOT / "datasets" / "ppe" / "ds2_torso.json"

#: `calibrate_vest.py` 가 라벨 있는 데이터로 잰 값이다. **감으로 정한 값이 아니다.**
VEST_MIN = 0.30  # 이 위는 vest 정밀도 0.96
NOVEST_MAX = 0.02  # 이 아래만 no_vest 로 본다 (보수적)

#: 사람 박스에서 몸통이 차지하는 세로 구간과 가로 비율.
TORSO_TOP, TORSO_BOTTOM, TORSO_WIDTH = 0.25, 0.80, 0.85

BANDS = ((5, 45),)
SAT_MIN, VAL_MIN = 90, 90


def imread_any(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None


def hivis_ratio(img, box) -> float:
    x, y, w, h = (int(round(v)) for v in box)
    crop = img[max(0, y) : y + h, max(0, x) : x + w]
    if crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hh, ss, vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = (ss >= SAT_MIN) & (vv >= VAL_MIN)
    band = np.zeros_like(mask)
    for lo, hi in BANDS:
        band |= (hh >= lo) & (hh <= hi)
    return float((mask & band).mean())


def torso_of(person_box: list[float]) -> list[float]:
    x, y, w, h = person_box
    return [
        x + w * (1 - TORSO_WIDTH) / 2,
        y + h * TORSO_TOP,
        w * TORSO_WIDTH,
        h * (TORSO_BOTTOM - TORSO_TOP),
    ]


def main() -> int:
    records = json.loads(INDEX.read_text(encoding="utf-8"))
    targets = [
        r for r in records if r["ds"] == "ds2" and any(b["cls"] == "person" for b in r["boxes"])
    ]
    print(f"ds2 사람 박스 있는 이미지: {len(targets)}장")

    resolved, dropped = [], 0
    for i, r in enumerate(targets, 1):
        if i % 1000 == 0:
            print(f"  {i}/{len(targets)}")
        img = imread_any(RAW / r["file"])
        if img is None:
            dropped += 1
            continue
        torsos, ok = [], True
        for b in r["boxes"]:
            if b["cls"] != "person":
                continue
            tb = torso_of(b["bbox"])
            ratio = hivis_ratio(img, tb)
            if ratio >= VEST_MIN:
                torsos.append({"cls": "vest", "bbox": tb, "ratio": round(ratio, 3)})
            elif ratio <= NOVEST_MAX:
                torsos.append({"cls": "no_vest", "bbox": tb, "ratio": round(ratio, 3)})
            else:
                ok = False
                break
        if not ok or not torsos:
            dropped += 1
            continue
        resolved.append({"file": r["file"], "torsos": torsos})

    OUT.write_text(json.dumps(resolved, ensure_ascii=False), encoding="utf-8")
    kept_vest = sum(1 for r in resolved if any(t["cls"] == "vest" for t in r["torsos"]))
    print(f"\n확신 라벨 성공 {len(resolved)}장 · 애매해서 버림 {dropped}장")
    print(f"  그중 vest 포함 {kept_vest}장")
    print(f"기록: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
