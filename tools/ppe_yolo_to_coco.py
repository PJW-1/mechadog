# YOLO txt -> COCO json 변환 (YOLOX 학습용)
# 사용: python tools/ppe_yolo_to_coco.py --session sim_factory_t15 --val-ratio 0.15
import argparse
import json
import sys
from pathlib import Path

import cv2

CLASSES = ["helmet", "no_helmet", "vest", "no_vest", "person_down"]


def collect(img_dir: Path, lbl_dir: Path):
    items = []
    for img in sorted(img_dir.glob("*.jpg")):
        lbl = lbl_dir / f"{img.stem}.txt"
        if not lbl.exists():
            continue
        items.append((img, lbl))
    return items


def build(items, categories):
    images, anns = [], []
    aid = 0
    for iid, (img, lbl) in enumerate(items, 1):
        h, w = cv2.imread(str(img)).shape[:2]
        images.append({"id": iid, "file_name": img.name, "width": w, "height": h})
        for line in lbl.read_text(encoding="utf-8").splitlines():
            p = line.split()
            if not p:
                continue
            cls, cx, cy, bw, bh = int(p[0]), *map(float, p[1:5])
            x, y = (cx - bw / 2) * w, (cy - bh / 2) * h
            anns.append({
                "id": aid, "image_id": iid, "category_id": cls,
                "bbox": [x, y, bw * w, bh * h],
                "area": bw * bh * w * h, "iscrowd": 0,
            })
            aid += 1
    return {"images": images, "annotations": anns,
            "categories": [{"id": i, "name": n} for i, n in enumerate(categories)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="datasets/ppe")
    ap.add_argument("--session", default="sim_factory_t15")
    ap.add_argument("--val-ratio", type=float, default=0.15)
    args = ap.parse_args()

    root = Path(args.root)
    img_dir = root / "images/train" / args.session
    lbl_dir = root / "labels/train" / args.session
    items = collect(img_dir, lbl_dir)
    if not items:
        sys.exit("no images")

    n_val = max(1, round(len(items) * args.val_ratio))
    val, train = items[:n_val], items[n_val:]

    out = Path("ppe_coco")
    (out / "annotations").mkdir(parents=True, exist_ok=True)
    (out / "train").mkdir(parents=True, exist_ok=True)
    (out / "val").mkdir(parents=True, exist_ok=True)
    for name, subset in (("train", train), ("val", val)):
        dst = out / name
        for img, _ in subset:
            link = dst / img.name
            if not link.exists():
                link.write_bytes(img.read_bytes())
        js = build(subset, CLASSES)
        (out / "annotations" / f"instances_{name}.json").write_text(
            json.dumps(js), encoding="utf-8")
        print(f"{name}: {len(js['images'])} imgs, {len(js['annotations'])} anns")


if __name__ == "__main__":
    main()
