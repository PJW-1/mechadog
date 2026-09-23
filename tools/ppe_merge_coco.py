# 병합 COCO 데이터셋 빌더 — sim_factory_t15 + construction-ppe + SHWD
# 출력: ppe_coco_v2/{train,val}/ + annotations/instances_{train,val}.json
# 클래스(고정): 0 helmet · 1 no_helmet · 2 vest · 3 no_vest · 4 person_down
import argparse
import json
import random
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2

CLASSES = ["helmet", "no_helmet", "vest", "no_vest", "person_down"]

# 소스별 (이미지, 라벨, {소스클래스: 우리클래스}) — 매핑 없는 클래스는 버린다
def yolo_items(img_dir, lbl_dir, mapping):
    imgs = sorted(p for p in Path(img_dir).iterdir()
                  if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    for img in imgs:
        lbl = Path(lbl_dir) / f"{img.stem}.txt"
        if not lbl.exists():
            continue
        anns = []
        for line in lbl.read_text(encoding="utf-8").splitlines():
            p = line.split()
            if not p or int(p[0]) not in mapping:
                continue
            anns.append((mapping[int(p[0])], *map(float, p[1:5])))  # cx cy w h 정규화
        yield img, anns


def voc_items(img_dir, ann_dir, mapping, hat_only_ratio):
    """SHWD — hat 1개 이상 사진은 전부, 맨머리만 있는 사진은 hat_only_ratio 만큼만."""
    rng = random.Random(20260923)
    imgs = sorted(Path(img_dir).glob("*.jpg"))
    kept = skipped = 0
    for img in imgs:
        xml = Path(ann_dir) / f"{img.stem}.xml"
        if not xml.exists():
            continue
        root = ET.parse(xml).getroot()
        w = float(root.findtext("size/width", "0"))
        h = float(root.findtext("size/height", "0"))
        if w <= 0 or h <= 0:
            continue
        anns, has_hat = [], False
        for obj in root.findall("object"):
            name = obj.findtext("name", "").strip().lower()
            if name not in mapping:
                continue
            if name == "hat":
                has_hat = True
            b = obj.find("bndbox")
            x1, y1 = float(b.findtext("xmin")), float(b.findtext("ymin"))
            x2, y2 = float(b.findtext("xmax")), float(b.findtext("ymax"))
            anns.append((mapping[name],
                         ((x1 + x2) / 2) / w, ((y1 + y2) / 2) / h,
                         (x2 - x1) / w, (y2 - y1) / h))
        if not has_hat and rng.random() > hat_only_ratio:
            skipped += 1
            continue
        if anns:
            kept += 1
            yield img, anns
    print(f"  shwd kept={kept} skipped(no-hat sample-out)={skipped}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="ppe_coco_v2")
    ap.add_argument("--val-ratio", type=float, default=0.08)
    ap.add_argument("--no-hat-ratio", type=float, default=0.25)
    args = ap.parse_args()

    root = Path(r"C:\dev\mechadog-sim-view")
    ext = Path(r"C:\dev\ppe-ext-data")
    items = []

    # ① 시뮬 우리 화각 — 전부 (5클래스 그대로)
    for sess in ("sim_factory_t15", "sim_fallen_t15", "sim_fallen_t15b"):
        sim = list(yolo_items(root / f"datasets/ppe/images/train/{sess}",
                              root / f"datasets/ppe/labels/train/{sess}",
                              {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}))
        print(f"sim/{sess}: {len(sim)}")
        items += [("sim", *it) for it in sim]

    # ② construction-ppe — helmet/vest/no_helmet 만 (gloves·Person 등 제외)
    cp_map = {0: 0, 2: 2, 7: 1}
    for split in ("train", "val", "test"):
        sub = list(yolo_items(ext / f"construction-ppe/images/{split}",
                              ext / f"construction-ppe/labels/{split}", cp_map))
        items += [("cp", *it) for it in sub]
        print(f"cppe/{split}: {len(sub)}")

    # ③ SHWD — hat→helmet, person→no_helmet
    shwd = list(voc_items(ext / "shwd/VOC2028/JPEGImages",
                          ext / "shwd/VOC2028/Annotations",
                          {"hat": 0, "person": 1}, args.no_hat_ratio))
    items += [("shwd", *it) for it in shwd]

    # ④ Simuletic laying (YOLO-pose — 첫 5필드만 씀) — laying→person_down
    #    standing(1)은 우리 어휘에 없으니 버린다
    sml = list(yolo_items(ext / "simuletic_fall/laying_dataset/images",
                          ext / "simuletic_fall/laying_dataset/labels",
                          {0: 4}))
    print(f"simuletic: {len(sml)}")
    items += [("sml", *it) for it in sml]
    print(f"total: {len(items)}")

    rng = random.Random(42)
    rng.shuffle(items)
    n_val = max(30, round(len(items) * args.val_ratio))
    val_set, train_set = items[:n_val], items[n_val:]

    out = Path(args.out)
    for d in ("train", "val", "annotations"):
        (out / d).mkdir(parents=True, exist_ok=True)

    for name, subset in (("train", train_set), ("val", val_set)):
        images, anns, aid = [], [], 0
        for iid, (src, img, boxlist) in enumerate(subset, 1):
            fname = f"{src}_{img.name}"
            dst = out / name / fname
            if not dst.exists():
                shutil.copyfile(img, dst)
            im = cv2.imread(str(img))
            h, w = im.shape[:2]
            images.append({"id": iid, "file_name": fname, "width": w, "height": h})
            for cls, cx, cy, bw, bh in boxlist:
                anns.append({"id": aid, "image_id": iid, "category_id": cls,
                             "bbox": [(cx - bw / 2) * w, (cy - bh / 2) * h, bw * w, bh * h],
                             "area": bw * bh * w * h, "iscrowd": 0})
                aid += 1
        js = {"images": images, "annotations": anns,
              "categories": [{"id": i, "name": n} for i, n in enumerate(CLASSES)]}
        (out / "annotations" / f"instances_{name}.json").write_text(
            json.dumps(js), encoding="utf-8")
        from collections import Counter
        c = Counter(a["category_id"] for a in anns)
        print(f"{name}: {len(images)} imgs, {len(anns)} anns, "
              f"per-class={{{', '.join(f'{CLASSES[k]}:{v}' for k, v in sorted(c.items()))}}}")


if __name__ == "__main__":
    main()
