# ruff: noqa: N806 — 영상 크기 H·W·변환 M 은 OpenCV 관례 이름을 쓴다
"""로봇 카메라 사진(배경) 위에 라이터·보조배터리를 바닥 쪽에 작게 붙여 COCO 데이터셋을 만든다.

- 배경: 10-06 실기 기록 frames (실물 위험물이 놓였던 작업실 구간은 제외)
- 물체: cut/{lighter,powerbank}/*.png (공개 CC BY 4.0) + real/*.png (로봇 카메라 실물 크롭)
- 음성 예시: 아무것도 붙이지 않은 배경(의자·PC 본체·흰 쓰레기통 등)
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).parent
REC = Path(__import__("os").environ.get("HZ_REC_DIR", "field_records/2026-10-06"))
CLASSES = ("lighter", "powerbank")
BAD = {
    "lighter": {
        "12",
        "20",
        "23",
        "45",
        "46",
        "47",
        "48",
        "50",
        "52",
        "56",
        "57",
        "58",
        "64",
        "69",
        "75",
        "77",
    },
    "powerbank": {
        "2",
        "13",
        "15",
        "18",
        "23",
        "24",
        "26",
        "31",
        "41",
        "44",
        "47",
        "52",
        "54",
        "59",
        "61",
        "63",
        "64",
        "65",
        "74",
        "75",
        "76",
        "79",
    },
}
# 실물 위험물이 바닥에 놓였던 기록(작업실 시험) — 배경에서 뺀다
EXCLUDE_DIRS = ("리허설3_1805",)
SEED = 20261006


def load_rgba(p: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_UNCHANGED)


def hazard_windows(log: Path) -> list[tuple[int, int]]:
    """runtime.log 의 위험물 검출기 켜짐 시각(KST HH:MM:SS) 앞뒤 60초를 (초) 구간으로."""
    out = []
    if not log.exists():
        return out
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "hazard_detector_switched · enabled=True" in line:
            h, m, s = map(int, line[:8].split(":"))
            t = h * 3600 + m * 60 + s
            out.append((t - 60, t + 60))
    return out


def backgrounds() -> list[Path]:
    import datetime as dt

    files = []
    for d in sorted(REC.iterdir()):
        if not (d / "frames").is_dir() or any(x in d.name for x in EXCLUDE_DIRS):
            continue
        win = hazard_windows(d / "runtime.log")
        for f in (d / "frames").glob("*.jpg"):
            m = re.match(r"(\d{13})_", f.name)
            if not m:
                continue
            k = dt.datetime.utcfromtimestamp(int(m.group(1)) / 1000) + dt.timedelta(hours=9)
            t = k.hour * 3600 + k.minute * 60 + k.second
            if any(a <= t <= b for a, b in win):
                continue
            files.append(f)
    return files


def degrade(obj: np.ndarray, rng: random.Random) -> np.ndarray:
    rgb, a = obj[:, :, :3].astype(np.float32), obj[:, :, 3]
    rgb = rgb * rng.uniform(0.55, 1.05) + rng.uniform(-25, 15)  # 실내·역광으로 어둡게
    hsv = cv2.cvtColor(np.clip(rgb, 0, 255).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[:, :, 1] *= rng.uniform(0.6, 1.0)
    rgb = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR)
    return np.dstack([rgb, a])


def paste(bg: np.ndarray, obj: np.ndarray, rng: random.Random, occupied: list) -> list | None:
    H, W = bg.shape[:2]
    if rng.random() < 0.6:  # 바닥에 눕힌 모습: 가로로 돌린다
        obj = cv2.rotate(obj, rng.choice([cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE]))
    ang = rng.uniform(-20, 20)
    h, w = obj.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    obj = cv2.warpAffine(obj, M, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))
    # 크기: 화면 아래일수록(가까울수록) 크게. 긴 변 18~110px
    y_c = rng.uniform(0.62, 0.97) * H
    near = (y_c / H - 0.62) / 0.35
    long_side = rng.uniform(18, 45) + near * rng.uniform(10, 65)
    s = long_side / max(obj.shape[:2])
    obj = cv2.resize(
        obj,
        (max(4, int(obj.shape[1] * s)), max(4, int(obj.shape[0] * s))),
        interpolation=cv2.INTER_AREA,
    )
    obj = degrade(obj, rng)
    oh, ow = obj.shape[:2]
    x0 = int(rng.uniform(0.05, 0.95) * W - ow / 2)
    y0 = int(y_c - oh)
    x0, y0 = max(0, min(W - ow, x0)), max(0, min(H - oh, y0))
    box = [x0, y0, ow, oh]
    for b in occupied:
        if not (x0 + ow < b[0] or b[0] + b[2] < x0 or y0 + oh < b[1] or b[1] + b[3] < y0):
            return None
    a = obj[:, :, 3:4].astype(np.float32) / 255.0
    a = cv2.GaussianBlur(a, (3, 3), 0)[:, :, None] if min(oh, ow) > 6 else a
    roi = bg[y0 : y0 + oh, x0 : x0 + ow].astype(np.float32)
    # 바닥 그림자 (아래쪽에 옅은 어둠)
    sh = np.zeros_like(a)
    sh[int(oh * 0.6) :] = a[int(oh * 0.6) :] * 0.35
    roi = roi * (1 - sh)
    bg[y0 : y0 + oh, x0 : x0 + ow] = (roi * (1 - a) + obj[:, :, :3].astype(np.float32) * a).astype(
        np.uint8
    )
    return box


def camera_look(img: np.ndarray, rng: random.Random) -> np.ndarray:
    if rng.random() < 0.7:
        k = rng.choice([3, 3, 5])
        img = cv2.GaussianBlur(img, (k, k), 0)
    noise = np.random.default_rng(rng.randint(0, 10**9)).normal(0, rng.uniform(2, 7), img.shape)
    img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    q = rng.randint(35, 80)
    return cv2.imdecode(
        cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])[1], cv2.IMREAD_COLOR
    )


def main() -> None:
    rng = random.Random(SEED)
    objs = {
        c: [load_rgba(p) for p in sorted((HERE / "cut" / c).glob("*.png")) if p.stem not in BAD[c]]
        for c in CLASSES
    }
    for c in CLASSES:
        for p in sorted((HERE / "real").glob(f"{c}_*.png")):
            for _ in range(15):  # 실물은 가중
                objs[c].append(load_rgba(p))
    print({c: len(v) for c, v in objs.items()})
    bgs = backgrounds()
    rng.shuffle(bgs)
    print("backgrounds", len(bgs))
    out = HERE / "dataset"
    n_pos, n_neg = int(sys.argv[1]) if len(sys.argv) > 1 else 3000, 1200
    splits = {"train2017": [], "val2017": []}
    for i in range(n_pos + n_neg):
        bgp = bgs[i % len(bgs)]
        img = cv2.imdecode(np.fromfile(str(bgp), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        anns = []
        if i < n_pos:
            occupied = []
            for _ in range(rng.choice([1, 1, 1, 2, 2, 3])):
                c = rng.choice(CLASSES)
                box = paste(img, rng.choice(objs[c]), rng, occupied)
                if box:
                    occupied.append(box)
                    anns.append((CLASSES.index(c) + 1, box))
        img = camera_look(img, rng)
        split = "val2017" if i % 10 == 0 else "train2017"
        name = f"s{i:05d}.jpg"
        (out / split).mkdir(parents=True, exist_ok=True)
        cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tofile(str(out / split / name))
        splits[split].append((name, img.shape, anns))
    (out / "annotations").mkdir(exist_ok=True)
    for split, items in splits.items():
        coco = {
            "images": [],
            "annotations": [],
            "categories": [{"id": k + 1, "name": c} for k, c in enumerate(CLASSES)],
        }
        aid = 1
        for k, (name, shp, anns) in enumerate(items):
            coco["images"].append(
                {"id": k + 1, "file_name": name, "height": shp[0], "width": shp[1]}
            )
            for cid, b in anns:
                coco["annotations"].append(
                    {
                        "id": aid,
                        "image_id": k + 1,
                        "category_id": cid,
                        "bbox": b,
                        "area": b[2] * b[3],
                        "iscrowd": 0,
                    }
                )
                aid += 1
        tag = "train" if split.startswith("train") else "val"
        (out / "annotations" / f"instances_{tag}2017.json").write_text(
            json.dumps(coco), encoding="utf-8"
        )
        print(split, len(items), "anns", aid - 1)


if __name__ == "__main__":
    main()
