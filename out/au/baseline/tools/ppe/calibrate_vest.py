"""형광 조끼 색상 휴리스틱의 임계값을 **라벨이 있는 데이터로 정한다**.

MODEL_PLAN 1.5 의 색상 휴리스틱을 ds2 몸통 보강에 쓰려면 임계값이 필요하다.
⚠️ **감으로 정하지 않는다.** ds1·ds3 에는 `vest`/`no_vest` 가 사람 손으로 라벨돼
있으므로, 그 박스 안의 형광 비율 분포를 재서 임계값을 고르고 **그 임계값의
정확도를 숫자로 남긴다.** 그래야 ds2 보강 라벨이 얼마나 믿을 만한지 말할 수 있다.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "datasets" / "ppe" / "raw"
INDEX = ROOT / "datasets" / "ppe" / "index.json"

#: 형광 조끼 색. OpenCV HSV 는 H 0~179 다.
#: 주황(H 5~25) · 노랑~연두(H 25~45). **채도와 명도 하한이 핵심이다** —
#: 그것이 없으면 살구색 벽이나 목재가 전부 조끼로 잡힌다.
BANDS = ((5, 45),)
SAT_MIN = 90
VAL_MIN = 90


def imread_any(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None


def hivis_ratio(img, box) -> float:
    """박스 안에서 형광색이 차지하는 비율."""
    x, y, w, h = (int(round(v)) for v in box)
    x, y = max(0, x), max(0, y)
    crop = img[y : y + h, x : x + w]
    if crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hh, ss, vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = (ss >= SAT_MIN) & (vv >= VAL_MIN)
    band = np.zeros_like(mask)
    for lo, hi in BANDS:
        band |= (hh >= lo) & (hh <= hi)
    return float((mask & band).mean())


def main(sample: int = 1200) -> int:
    records = json.loads(INDEX.read_text(encoding="utf-8"))
    labeled = [r for r in records if r["ds"] in ("ds1", "ds3")]
    random.seed(20260918)
    random.shuffle(labeled)

    ratios: dict[str, list[float]] = {"vest": [], "no_vest": []}
    used = 0
    for r in labeled:
        boxes = [b for b in r["boxes"] if b["cls"] in ratios]
        if not boxes:
            continue
        img = imread_any(RAW / r["file"])
        if img is None:
            continue
        for b in boxes:
            ratios[b["cls"]].append(hivis_ratio(img, b["bbox"]))
        used += 1
        if used >= sample:
            break

    v, nv = np.array(ratios["vest"]), np.array(ratios["no_vest"])
    print(f"표본: 이미지 {used}장 · vest {len(v)}개 · no_vest {len(nv)}개\n")
    for name, arr in (("vest", v), ("no_vest", nv)):
        q = np.percentile(arr, [10, 25, 50, 75, 90])
        print(f"{name:8s} 형광비율 10/25/50/75/90% = " + " ".join(f"{x:.3f}" for x in q))

    print("\n임계값별 성능 (형광비율 > t 이면 vest 로 판정)")
    print("  t       vest재현  no_vest재현  정확도")
    best = (0.0, -1.0)
    for t in np.arange(0.02, 0.45, 0.01):
        rv = float((v > t).mean())
        rn = float((nv <= t).mean())
        acc = (rv * len(v) + rn * len(nv)) / (len(v) + len(nv))
        if min(rv, rn) > best[1]:
            best = (float(t), min(rv, rn))
        if abs(t * 100 - round(t * 100)) < 1e-6 and round(t * 100) % 3 == 0:
            print(f"  {t:.2f}    {rv:.3f}     {rn:.3f}      {acc:.3f}")
    print(f"\n두 재현율의 최솟값이 가장 큰 임계값: t={best[0]:.2f} (min recall {best[1]:.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
