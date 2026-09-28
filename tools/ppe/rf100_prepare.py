"""Roboflow COCO 내보내기(construction-safety)를 **사람 크롭 4클래스 학습 세트**로 바꾼다.

PPE 재학습(YOLOX-S 4클래스)의 데이터 1단계다. 원본은 고치지 않는다.

    python tools/ppe/rf100_prepare.py --version ppe4_cs_v1
    python tools/ppe/rf100_prepare.py --version ppe4_cs_v1 --force   # 같은 버전을 다시 만든다

입력  `datasets/ppe/raw/construction-safety/{train,valid,test}/_annotations.coco.json` + 이미지
출력  `datasets/ppe/build/<version>/`
        annotations/instances_{train,val,test}.json   YOLOX `COCODataset` 이 읽는 주석
        train2017/ val2017/ test2017/                  사람 크롭 이미지
        data_card.json                                 출처·라이선스·규칙·건수

학습은 이 폴더를 `PPE_DATA_DIR` 로 넘긴다 (`tools/ppe/yolox_exp_ppe_s.py` 참고).

⚠️ **런타임과 같은 사람 크롭을 만든다.** PPE 모델은 풀프레임이 아니라 가장 큰 사람의
크롭(`crop_pad` 0.08 여유, 경계 클램프)만 본다. 풀프레임으로 학습하면 모델이 본 적 없는
배율·구도로 판정하게 된다. 그래서 자르는 식은 `tools/ppe_live_check.crop_person` 을
그대로 부른다 — 식이 둘이면 언젠가 어긋난다.

⚠️ **학습 분할만 여유를 흔든다 (0.0~0.2).** 현장 박스는 사람 검출기의 흔들림만큼 매번
조금씩 다르다. 검증·시험은 런타임 값(0.08) 그대로 두어 실제 입력과 같은 조건으로 잰다.

⚠️ **부분 라벨 사람은 뺀다.** 머리에 helmet/no_helmet 이, 몸통에 vest/no_vest 가 없는
사람 크롭을 넣으면 **실제 맨머리나 몸통이 배경으로 학습된다** (PPE_Train.md 1.6 결함과
같은 형태). 없는 쪽이 하나라도 있으면 그 사람 크롭은 제외하고 사유별로 센다.

⚠️ **이름 매핑표에 없는 범주가 나오면 멈춘다.** 빠진 클래스는 조용히 배경으로 학습된다
(`index_raw.py` 와 같은 원칙). Roboflow 가 끼워 넣는 상위 범주(보통 id 0)도 표에 `None`
으로 적어야 통과한다 — 실제 이름은 받은 파일을 보고 표에 더한다.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import random
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.ppe.index_raw import imread_any  # noqa: E402
from tools.ppe_live_check import crop_person  # noqa: E402

DATASETS = ROOT / "datasets"
RAW = DATASETS / "ppe" / "raw" / "construction-safety"
BUILD = DATASETS / "ppe" / "build"

#: 우리 4클래스. **이 순서가 곧 카테고리 id(1~4)와 모델 출력 순서다.**
CLASSES = ("helmet", "no_helmet", "vest", "no_vest")
HEAD_CLASSES = ("helmet", "no_helmet")
TORSO_CLASSES = ("vest", "no_vest")
#: 크롭과 부분 라벨 검사에만 쓰고 출력하지 않는다 — 사람은 ①번 검출기가 담당한다(ADR-12).
PERSON = "person"

#: 정규화한 범주 이름 → 우리 이름. `None` 은 **의도적으로 버리는 것**이다.
#: ⚠️ 대조는 `normalize_name` 을 거친 뒤에 한다 (`no-helmet`·`No Helmet` → `no_helmet`).
MAPPING: dict[str, str | None] = {
    "workers": None,  # Roboflow 상위 범주 자리표시자 (id 0) — 이름은 내보내기마다 다르다
    "construction_safety": None,  # 2026-09-28 실제 내보내기(j2-p/construction-safety-gsnvb-h1yhi)
    "helmet": "helmet",
    "no_helmet": "no_helmet",
    "vest": "vest",
    "no_vest": "no_vest",
    "person": PERSON,
}

#: Roboflow 분할 → 우리 분할. **처리 순서이기도 하다** — 아래 중복 제거 참고.
#: ⚠️ test 는 공개 test 다. XIAO 실기 test 가 아니다.
SPLITS: tuple[tuple[str, str], ...] = (("test", "test"), ("valid", "val"), ("train", "train"))

#: YOLOX `COCODataset` 의 기본 이미지 폴더 이름. ⚠️ 이 이름이어야 exp 가 `name` 을
#: 따로 넘기지 않아도 읽는다 (train→train2017, 평가→val2017/test2017).
IMAGE_DIRS = {"train": "train2017", "val": "val2017", "test": "test2017"}

#: 런타임 `vision.ppe.crop_pad`. 검증·시험 분할은 이 값으로 자른다.
RUNTIME_PAD = 0.08
#: 학습 분할의 여유 범위(균등 분포).
TRAIN_PAD_RANGE = (0.0, 0.2)
#: 크롭 안에 원래 면적의 이 비율 미만이 남는 박스는 버린다.
MIN_KEEP_AREA = 0.5
#: 사람 박스 세로 비율로 본 머리·몸통 영역. 몸통은 `augment_torso.py` 와 같은 25~80%.
HEAD_REGION = (0.0, 0.30)
TORSO_REGION = (0.25, 0.80)
#: PPE 박스 면적 중 영역 안에 든 비율(IoA)이 이 값 이상이어야 «그 부위 라벨» 로 본다.
MIN_IOA = 0.5
#: 사람 박스 높이가 이 픽셀 미만이면 뺀다. ⚠️ 원거리 군중의 작은 사람을 640 으로
#: 키우면 뭉개진 그림이 된다 — 1~3m 앞 사람을 보는 우리 카메라에는 없는 입력이다
#: (`build_final.py` 가 원거리 군중 사진을 뺀 것과 같은 판단).
MIN_PERSON_PX = 48
#: 이보다 작은 박스는 COCO 변환 뒤 폭·높이 0 에 가까워 학습에 쓰지 않는다.
MIN_BOX_PX = 1.0
JPEG_QUALITY = 95
SEED = 20260928
LICENSE = "CC BY 4.0"

REASON_NO_HEAD = "머리 라벨 없음"
REASON_NO_TORSO = "몸통 라벨 없음"
REASON_CROP = "크롭 실패"
REASON_SMALL = "사람이 너무 작음"

Box = tuple[float, float, float, float]


def normalize_name(name: str) -> str:
    """범주 이름 표기 차이(대소문자·하이픈·공백)를 지운다."""
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def map_categories(categories: Iterable[dict[str, Any]], source: str = "") -> dict[int, str | None]:
    """COCO `categories` → {범주 id: 우리 이름 | None}. **표에 없는 이름이면 멈춘다.**"""
    out: dict[int, str | None] = {}
    unknown: list[str] = []
    for cat in categories:
        key = normalize_name(str(cat["name"]))
        if key not in MAPPING:
            unknown.append(f"{cat['name']!r}(id={cat['id']}, super={cat.get('supercategory')!r})")
            continue
        out[int(cat["id"])] = MAPPING[key]
    if unknown:
        raise SystemExit(
            f"{source}: 매핑표에 없는 범주 {unknown} — 무엇인지 확인하고 MAPPING 에 더한다"
        )
    return out


def xywh_to_xyxy(bbox: Sequence[float]) -> Box:
    x, y, w, h = (float(v) for v in bbox)
    return (x, y, x + w, y + h)


def area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def intersect(a: Box, b: Box) -> Box:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def ioa(box: Box, region: Box) -> float:
    """`box` 면적 중 `region` 안에 든 비율."""
    own = area(box)
    return area(intersect(box, region)) / own if own > 0 else 0.0


def band(person: Box, span: tuple[float, float]) -> Box:
    """사람 박스의 세로 구간 `span`(비율)을 박스로."""
    x1, y1, x2, y2 = person
    h = y2 - y1
    return (x1, y1 + h * span[0], x2, y1 + h * span[1])


def person_label_problem(person: Box, ppe: Sequence[tuple[str, Box]]) -> str | None:
    """사람의 머리·몸통 라벨이 다 있는지. 빠졌으면 사유, 다 있으면 `None`.

    ⚠️ 박스 중심이 아니라 IoA 로 본다 — 안전모 챙이 사람 박스 위로 삐져나와도
    대부분이 머리 영역 안이면 그 사람의 머리 라벨이다.
    """
    head, torso = band(person, HEAD_REGION), band(person, TORSO_REGION)
    if not any(c in HEAD_CLASSES and ioa(b, head) >= MIN_IOA for c, b in ppe):
        return REASON_NO_HEAD
    if not any(c in TORSO_CLASSES and ioa(b, torso) >= MIN_IOA for c, b in ppe):
        return REASON_NO_TORSO
    return None


def crop_rect(width: int, height: int, person: Box, pad: float) -> Box | None:
    """런타임과 같은 크롭 사각형 (x1, y1, x2, y2). 너무 작으면 `None`.

    ⚠️ 식을 새로 쓰지 않고 `crop_person` 에 빈 배열을 넣어 얻는다. 버림(`int`)·클램프·
    최소 크기(8px) 규칙까지 런타임과 한 몸이어야 한다.
    """
    import numpy as np

    blank = np.empty((height, width, 0), dtype=np.uint8)
    crop, (left, top) = crop_person(blank, person, pad)
    if crop is None:
        return None
    return (float(left), float(top), float(left + crop.shape[1]), float(top + crop.shape[0]))


def to_crop(box: Box, rect: Box) -> Box | None:
    """원본 좌표 박스를 크롭 좌표로. **원래 면적의 50% 미만이 남으면 `None`.**

    ⚠️ 반쯤 잘린 머리를 그대로 두면 «머리 일부» 가 helmet 으로 학습된다. 반대로 다
    버리면 그 자리가 배경이 되지만, 크롭 가장자리의 잘린 이웃이라 영향이 작다.
    """
    clipped = intersect(box, rect)
    own = area(box)
    if own <= 0 or area(clipped) < own * MIN_KEEP_AREA:
        return None
    x1, y1, x2, y2 = (
        clipped[0] - rect[0],
        clipped[1] - rect[1],
        clipped[2] - rect[0],
        clipped[3] - rect[1],
    )
    if x2 - x1 < MIN_BOX_PX or y2 - y1 < MIN_BOX_PX:
        return None
    return (x1, y1, x2, y2)


def load_split(ann_path: Path) -> list[dict[str, Any]]:
    """주석 파일 하나 → 이미지별 {file, w, h, persons, ppe}."""
    data = json.loads(ann_path.read_text(encoding="utf-8"))
    cats = map_categories(data["categories"], str(ann_path))
    boxes: dict[int, list[tuple[str, Box]]] = collections.defaultdict(list)
    for ann in data["annotations"]:
        name = cats[int(ann["category_id"])]
        if name is None:
            continue
        boxes[int(ann["image_id"])].append((name, xywh_to_xyxy(ann["bbox"])))
    out = []
    for image in sorted(data["images"], key=lambda im: str(im["file_name"])):
        found = boxes.get(int(image["id"]), [])
        out.append(
            {
                "file": ann_path.parent / str(image["file_name"]),
                "w": int(image["width"]),
                "h": int(image["height"]),
                "persons": [b for c, b in found if c == PERSON],
                "ppe": [(c, b) for c, b in found if c != PERSON],
            }
        )
    return out


def empty_coco(split: str, version: str) -> dict[str, Any]:
    return {
        "info": {"description": f"PPE 4클래스 사람 크롭 — {version} {split}", "version": version},
        "licenses": [{"id": 1, "name": LICENSE}],
        "images": [],
        "annotations": [],
        "categories": [
            {"id": i + 1, "name": name, "supercategory": "ppe"} for i, name in enumerate(CLASSES)
        ],
    }


def add_image(coco: dict[str, Any], file_name: str, w: int, h: int, boxes: list[tuple[str, Box]]):
    """COCO 에 이미지 하나와 박스를 붙인다. id 는 1 부터 이어진다."""
    image_id = len(coco["images"]) + 1
    coco["images"].append({"id": image_id, "file_name": file_name, "width": w, "height": h})
    for name, (x1, y1, x2, y2) in boxes:
        w_box, h_box = x2 - x1, y2 - y1
        coco["annotations"].append(
            {
                "id": len(coco["annotations"]) + 1,
                "image_id": image_id,
                "category_id": CLASSES.index(name) + 1,
                "bbox": [round(x1, 2), round(y1, 2), round(w_box, 2), round(h_box, 2)],
                "area": round(w_box * h_box, 2),
                "iscrowd": 0,
            }
        )


def write_jpeg(path: Path, image) -> None:
    """⚠️ `cv2.imwrite` 를 쓰지 않는다 — 한글 경로에서 조용히 실패한다(`imread_any` 참고)."""
    import cv2

    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError(f"JPEG 인코딩 실패: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(path))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_source_notes(raw: Path) -> dict[str, str | None]:
    """Roboflow 내보내기에 딸린 README 에서 URL·라이선스 줄을 찾는다. 없으면 `None`."""
    text = ""
    for name in ("README.roboflow.txt", "README.dataset.txt"):
        path = raw / name
        if path.is_file():
            text += path.read_text(encoding="utf-8", errors="replace") + "\n"
    url = re.search(r"https://universe\.roboflow\.com/\S+", text)
    lic = re.search(r"^.*License:.*$", text, flags=re.MULTILINE | re.IGNORECASE)
    return {
        "url": url.group(0).rstrip(".,)") if url else None,
        "license_line": lic.group(0).strip() if lic else None,
    }


def license_matches(line: str) -> bool:
    """`License: CC BY 4.0` 류의 줄이 CC BY 4.0 인지. `CC BY-NC 4.0` 은 아니다."""

    def squash(text: str) -> str:
        return re.sub(r"[\s\-]+", "", text.lower())

    return squash(LICENSE) in squash(line)


def prepare(
    raw: Path,
    out: Path,
    version: str,
    *,
    seed: int = SEED,
    source_url: str | None = None,
) -> dict[str, Any]:
    """변환 전체. 데이터 카드 dict 를 돌려주고 `out/data_card.json` 에도 쓴다."""
    import cv2

    notes = read_source_notes(raw)
    if notes["license_line"] and not license_matches(notes["license_line"]):
        # ⚠️ NC 세트는 쓰지 않는다 (datasets/README). 다른 라이선스로 조용히 만들지 않는다.
        raise SystemExit(f"라이선스가 {LICENSE} 가 아님: {notes['license_line']!r}")
    url = source_url or notes["url"]
    if not url:
        raise SystemExit("출처 URL 을 README 에서 못 찾았다 — --source-url 로 넘긴다")

    rng = random.Random(seed)
    seen: dict[str, str] = {}
    stats: dict[str, dict[str, Any]] = {}
    for rf_split, split in SPLITS:
        ann_path = raw / rf_split / "_annotations.coco.json"
        if not ann_path.is_file():
            raise SystemExit(f"주석 파일 없음: {ann_path}")
        coco = empty_coco(split, version)
        counts: dict[str, Any] = {
            "source_images": 0,
            "duplicates": 0,
            "images_without_person": 0,
            "persons": 0,
            "excluded": collections.Counter(),
            "boxes_dropped_partial": 0,
        }
        for record in load_split(ann_path):
            counts["source_images"] += 1
            # ⚠️ 중복은 **바이트 해시(sha256)** 로 본다. 처리 순서가 test→val→train 이라
            # 분할 사이에 겹치면 평가 쪽이 남고 학습 쪽이 빠진다 — 평가 누수를 막는다.
            digest = sha256_file(record["file"])
            if digest in seen:
                counts["duplicates"] += 1
                continue
            seen[digest] = f"{rf_split}/{record['file'].name}"
            if not record["persons"]:
                counts["images_without_person"] += 1
                continue
            image = None
            for k, person in enumerate(record["persons"]):
                counts["persons"] += 1
                if person[3] - person[1] < MIN_PERSON_PX:
                    counts["excluded"][REASON_SMALL] += 1
                    continue
                reason = person_label_problem(person, record["ppe"])
                if reason:
                    counts["excluded"][reason] += 1
                    continue
                pad = rng.uniform(*TRAIN_PAD_RANGE) if split == "train" else RUNTIME_PAD
                rect = crop_rect(record["w"], record["h"], person, pad)
                if rect is None:
                    counts["excluded"][REASON_CROP] += 1
                    continue
                boxes: list[tuple[str, Box]] = []
                for name, box in record["ppe"]:
                    moved = to_crop(box, rect)
                    if moved is None:
                        counts["boxes_dropped_partial"] += 1
                        continue
                    boxes.append((name, moved))
                if image is None:
                    image = imread_any(record["file"], cv2.IMREAD_COLOR)
                    if image is None:
                        raise SystemExit(f"이미지를 읽지 못했다: {record['file']}")
                x1, y1, x2, y2 = (int(v) for v in rect)
                file_name = f"{record['file'].stem}__p{k:02d}.jpg"
                write_jpeg(out / IMAGE_DIRS[split] / file_name, image[y1:y2, x1:x2])
                add_image(coco, file_name, x2 - x1, y2 - y1, boxes)
        ann_out = out / "annotations" / f"instances_{split}.json"
        ann_out.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ 왜 ASCII: pycocotools 가 인코딩 없이 open() 해서 Windows(cp949)에서 한글에 멈춘다
        ann_out.write_text(json.dumps(coco), encoding="utf-8")
        counts["excluded"] = dict(counts["excluded"])
        counts["images"] = len(coco["images"])
        counts["boxes"] = dict(
            collections.Counter(CLASSES[a["category_id"] - 1] for a in coco["annotations"])
        )
        counts["annotation_file"] = ann_out.relative_to(out).as_posix()
        counts["annotation_sha256"] = sha256_file(ann_out)
        stats[split] = counts

    card = {
        "dataset_version": version,
        "source": {
            "name": "Roboflow construction-safety (COCO 내보내기)",
            "url": url,
            "license": LICENSE,
            "license_line": notes["license_line"],
            "raw_dir": raw.as_posix(),
        },
        "classes": list(CLASSES),
        "categories": {name: i + 1 for i, name in enumerate(CLASSES)},
        "mapping": MAPPING,
        "crop_rule": {
            "target": "사람 박스마다 1장 (런타임은 가장 큰 사람 1명)",
            "function": "tools/ppe_live_check.crop_person",
            "pad_val_test": RUNTIME_PAD,
            "pad_train_uniform": list(TRAIN_PAD_RANGE),
            "min_keep_area": MIN_KEEP_AREA,
        },
        "exclusion_rule": {
            "head_region": list(HEAD_REGION),
            "torso_region": list(TORSO_REGION),
            "min_ioa": MIN_IOA,
            "min_person_px": MIN_PERSON_PX,
            "note": "머리에 helmet/no_helmet, 몸통에 vest/no_vest 가 없으면 그 사람 크롭 제외",
        },
        "dedupe": "sha256 · 순서 test→val→train (겹치면 평가 쪽을 남긴다)",
        "yolox": {"image_dirs": IMAGE_DIRS, "annotations": "annotations/instances_{split}.json"},
        "splits": stats,
        "seed": seed,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "generator": {
            "script": "tools/ppe/rf100_prepare.py",
            "sha256": sha256_file(Path(__file__)),
        },
    }
    (out / "data_card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return card


def ensure_under_datasets(path: Path) -> Path:
    """⚠️ 얼굴이 담긴 산출물이다. **깃이 무시하는 `datasets/` 밖에는 쓰지 않는다.**"""
    resolved = path.resolve()
    if not resolved.is_relative_to(DATASETS.resolve()):
        raise SystemExit(f"출력은 datasets/ 아래여야 한다: {resolved}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, default=RAW, help="Roboflow COCO 내보내기 폴더")
    parser.add_argument("--version", required=True, help="출력 폴더 이름 = dataset_version")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--source-url", help="README 에 URL 이 없을 때만")
    parser.add_argument("--force", action="store_true", help="같은 버전 폴더를 지우고 다시 만든다")
    args = parser.parse_args(argv)

    out = ensure_under_datasets(BUILD / args.version)
    if out.exists():
        if not args.force:
            raise SystemExit(f"이미 있다: {out} — 새 버전 이름을 쓰거나 --force")
        shutil.rmtree(out)
    card = prepare(args.raw, out, args.version, seed=args.seed, source_url=args.source_url)
    for split, counts in card["splits"].items():
        print(
            f"{split:5s} 원본 {counts['source_images']}장 · 중복 {counts['duplicates']} · "
            f"사람 {counts['persons']} · 크롭 {counts['images']}장 · 박스 {counts['boxes']} · "
            f"제외 {counts['excluded']}"
        )
    print(f"기록: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
