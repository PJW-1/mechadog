"""공개 제품 사진에서 물체만 오려 RGBA PNG 로 저장한다 (GrabCut, 가장자리 여백 = 배경)."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).parent


def cutout(img: np.ndarray) -> np.ndarray | None:
    h, w = img.shape[:2]
    scale = 400 / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    m = int(0.04 * min(h, w)) + 2
    mask = np.zeros((h, w), np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(img, mask, (m, m, w - 2 * m, h - 2 * m), bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg)
    if n <= 1:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    fg = np.where(lab == k, 255, 0).astype(np.uint8)
    area = fg.sum() / 255 / (h * w)
    if not 0.03 <= area <= 0.85:
        return None
    x, y, bw, bh = cv2.boundingRect(fg)
    if x <= 1 and y <= 1 and bw >= w - 2 and bh >= h - 2:
        return None  # 테두리까지 꽉 찬 것은 배경을 못 뗀 것
    fg = cv2.GaussianBlur(fg, (3, 3), 0)
    return np.dstack([img, fg])[y : y + bh, x : x + bw]


def main() -> None:
    for cls in ("lighter", "powerbank"):
        out = HERE / "cut" / cls
        out.mkdir(parents=True, exist_ok=True)
        ok = 0
        for f in sorted((HERE / "src" / cls).glob("*.jpg")):
            img = cv2.imdecode(np.fromfile(str(f), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            c = cutout(img)
            if c is None:
                continue
            cv2.imencode(".png", c)[1].tofile(str(out / f"{f.stem}.png"))
            ok += 1
        print(cls, ok)
    # 확인용 모음
    for cls in ("lighter", "powerbank"):
        tiles = []
        for p in sorted((HERE / "cut" / cls).glob("*.png"))[:80]:
            c = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_UNCHANGED)
            t = np.full((100, 100, 3), 200, np.uint8)
            s = 90 / max(c.shape[:2])
            c = cv2.resize(c, (max(1, int(c.shape[1] * s)), max(1, int(c.shape[0] * s))))
            a = c[:, :, 3:4] / 255.0
            y0, x0 = (100 - c.shape[0]) // 2, (100 - c.shape[1]) // 2
            roi = t[y0 : y0 + c.shape[0], x0 : x0 + c.shape[1]]
            roi[:] = (roi * (1 - a) + c[:, :, :3] * a).astype(np.uint8)
            cv2.putText(t, p.stem, (2, 12), 0, 0.35, (0, 0, 255), 1)
            tiles.append(t)
        while len(tiles) % 10:
            tiles.append(np.full((100, 100, 3), 255, np.uint8))
        rows = [np.hstack(tiles[i : i + 10]) for i in range(0, len(tiles), 10)]
        cv2.imencode(".jpg", np.vstack(rows))[1].tofile(str(HERE / f"sheet_{cls}.jpg"))


if __name__ == "__main__":
    sys.exit(main())
