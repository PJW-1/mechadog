"""Mendeley PPE2286 을 사람 크롭 4클래스로 바꿔 기존 빌드(ppe4_cs_v1)에 합친다.

출처 — Mei-Ling Huang · Ying Cheng, «Dataset of Personal Protective Equipment (PPE)»,
Mendeley Data v6, DOI 10.17632/zkzghjvpn2.6, CC BY 4.0. 640×640 이미지 2,286장, YOLO 형식,
클래스 Helmet · NoHelmet · NoVest · Vest. **person 박스가 없다.**

⚠️ **사람 박스는 런타임의 COCO 검출기로 만든다.** 이 세트에는 사람 라벨이 없다. 런타임도
사람 박스를 COCO 검출기에서 얻어 자르므로, 같은 검출기·같은 임계값으로 만든 박스가
정답 사람 박스보다 오히려 실제 입력에 가깝다. 검출 결과는 캐시에 남겨 다시 돌리지 않는다.

⚠️ **Roboflow 세트와 같은 사진이 섞여 있을 수 있다.** 설명에 «GitHub·Kaggle·Roboflow 에서
모았다» 고 적혀 있고 모두 640×640 으로 다시 저장돼 있어 바이트 해시로는 못 잡는다. 그래서
dHash(64비트) 해밍 거리로 본다. Roboflow 원본(분할 무관)과 닮은 Mendeley 이미지는 통째로
뺀다 — 기존 test 가 학습으로 새지 않게 하는 게 목적이다. Mendeley 안에서는 test→val→train
순서로 먼저 남은 쪽을 지킨다.

분할 — Mendeley `train` 은 우리 train, Mendeley `valid` 는 시드로 반씩 나눠 우리 val 과
Mendeley 전용 test 로 쓴다. 기존 `instances_test.json`(Roboflow test)은 **그대로 둔다** —
v1 과 같은 잣대로 비교해야 한다. Mendeley test 는 `instances_test_md.json` 으로 따로 잰다.

머리·몸통 제외 규칙, 크롭 식, 박스 이동 규칙은 `rf100_prepare` 의 함수를 그대로 부른다.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import random
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ppe.index_raw import imread_any  # noqa: E402
from tools.ppe.rf100_prepare import (  # noqa: E402
    CLASSES,
    IMAGE_DIRS,
    MIN_PERSON_PX,
    REASON_CROP,
    REASON_SMALL,
    RUNTIME_PAD,
    SEED,
    TRAIN_PAD_RANGE,
    Box,
    add_image,
    crop_rect,
    ensure_under_datasets,
    person_label_problem,
    sha256_file,
    to_crop,
    write_jpeg,
)

ROOT = Path(__file__).resolve().parents[2]

SOURCE = {
    "name": "Mendeley Data — Dataset of Personal Protective Equipment (PPE) v6",
    "authors": "Mei-Ling Huang, Ying Cheng",
    "doi": "10.17632/zkzghjvpn2.6",
    "url": "https://data.mendeley.com/datasets/zkzghjvpn2/6",
    "license": "CC BY 4.0",
}

#: data.yaml 이름 → 우리 이름. 표에 없는 이름이 나오면 멈춘다 (빠진 클래스는 배경으로 학습된다).
MAPPING: dict[str, str] = {
    "Helmet": "helmet",
    "NoHelmet": "no_helmet",
    "Vest": "vest",
    "NoVest": "no_vest",
}

#: dHash 해밍 거리가 이 값 이하면 같은 사진으로 본다. 재압축·리사이즈는 보통 0~4 에 든다.
DHASH_MAX = 6
#: Mendeley `valid` 중 test 로 떼는 비율.
MD_TEST_SHARE = 0.5
MD_TEST_ANN = "instances_test_md.json"
REASON_NEAR_DUP_RF = "Roboflow 와 같은 사진"
REASON_NEAR_DUP_MD = "Mendeley 안 중복"


# ── 순수 함수 (시험 대상) ─────────────────────────────────────────────
def read_names(data_yaml: Path) -> list[str]:
    """`names: ['A', 'B']` 한 줄을 읽는다. PyYAML 없이 — 이 파일 형식만 받는다."""
    for line in data_yaml.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("names:"):
            body = line.split(":", 1)[1].strip().strip("[]")
            names = [n.strip().strip("'\"") for n in body.split(",") if n.strip()]
            # ⚠️ 블록 목록(`names:` 다음 줄에 `- A`)은 여기서 빈 목록이 된다. 빈 채로
            # 넘기면 첫 라벨에서 IndexError 로 죽으므로 이유를 말하고 멈춘다.
            if not names:
                raise SystemExit(f"names 가 한 줄 목록이 아니다: {data_yaml}")
            return names
    raise SystemExit(f"names 줄이 없다: {data_yaml}")


def map_names(names: Sequence[str]) -> list[str]:
    unknown = [n for n in names if n not in MAPPING]
    if unknown:
        raise SystemExit(f"매핑표에 없는 클래스: {unknown} — MAPPING 에 더한다")
    return [MAPPING[n] for n in names]


def read_yolo(text: str, names: Sequence[str], w: int, h: int) -> list[tuple[str, Box]]:
    """YOLO 한 파일(`cls cx cy bw bh`, 0~1) → [(이름, 픽셀 xyxy)]."""
    out: list[tuple[str, Box]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 5:
            raise SystemExit(f"bbox 가 아닌 줄(세그먼트?): {line!r}")
        cls, cx, cy, bw, bh = int(parts[0]), *(float(p) for p in parts[1:])
        out.append(
            (
                names[cls],
                ((cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h),
            )
        )
    return out


def dhash(image, size: int = 8) -> int:
    """회색조 (size+1)×size 로 줄여 가로 이웃 밝기 비교 → 64비트 정수."""
    import cv2

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def near(value: int, pool: Sequence[int], limit: int = DHASH_MAX) -> bool:
    return any(hamming(value, other) <= limit for other in pool)


def hashes_with_flip(image) -> tuple[int, int]:
    """원본과 좌우 반전의 dHash. 공개 세트는 반전 증강본을 원본처럼 싣기도 한다."""
    import cv2

    return dhash(image), dhash(cv2.flip(image, 1))


def near_any(value: int, pool: Sequence[tuple[int, int]], limit: int = DHASH_MAX) -> bool:
    """`value` 가 풀의 원본이나 반전 중 하나와 닮았는지."""
    return any(hamming(value, a) <= limit or hamming(value, b) <= limit for a, b in pool)


def check_contiguous(coco: dict[str, Any], where: str) -> None:
    """이어 붙일 COCO 의 id 가 1..N 인지. `add_image` 가 `len+1` 로 id 를 매기기 때문이다."""
    for key in ("images", "annotations"):
        ids = sorted(int(x["id"]) for x in coco[key])
        if ids != list(range(1, len(ids) + 1)):
            raise SystemExit(f"{where} 의 {key} id 가 1..N 이 아니다 — 이어 붙이면 id 가 겹친다")


def check_unique_names(paths: Sequence[Path]) -> None:
    """사람 박스 캐시는 파일 이름으로 찾는다. 분할이 달라도 이름이 겹치면 박스가 섞인다."""
    seen: dict[str, Path] = {}
    for path in paths:
        if path.name in seen:
            raise SystemExit(f"파일 이름이 겹친다: {seen[path.name]} · {path}")
        seen[path.name] = path


def split_valid(stems: Sequence[str], seed: int, share: float = MD_TEST_SHARE) -> dict[str, str]:
    """Mendeley valid 를 이미지 단위로 val/test 로 나눈다. 같은 시드면 같은 결과."""
    ordered = sorted(stems)
    random.Random(seed).shuffle(ordered)
    cut = int(round(len(ordered) * share))
    return {s: ("test" if i < cut else "val") for i, s in enumerate(ordered)}


# ── 입출력 ───────────────────────────────────────────────────────────
def list_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})


def rf_hashes(rf_raw: Path) -> list[tuple[int, int]]:
    import cv2

    out = []
    for sub in ("train", "valid", "test"):
        for path in list_images(rf_raw / sub):
            image = imread_any(path, cv2.IMREAD_COLOR)
            if image is not None:
                out.append(hashes_with_flip(image))
    return out


def detect_people(
    paths: Sequence[Path], cache: Path, device: str, coco_model: Path
) -> dict[str, Any]:
    """이미지마다 COCO 사람 박스. 캐시가 같은 검출기·같은 임계값이면 다시 돌리지 않는다."""
    import cv2

    from host.common.config import load_config
    from host.vision.coco_labels import COCO_CLASSES
    from host.vision.detector import Detector

    config = load_config(device)
    coco_cfg = config["vision"]["coco"]
    coco_cfg["model_path"] = str(coco_model)
    key = {
        "model_sha256": sha256_file(coco_model),
        "conf": float(coco_cfg["conf_threshold"]),
        "iou": float(coco_cfg.get("iou_threshold", 0.45)),
        "person_class": coco_cfg["person_class"],
    }
    if cache.is_file():
        stored = json.loads(cache.read_text(encoding="utf-8"))
        if stored.get("key") == key and all(p.name in stored["boxes"] for p in paths):
            return stored
    detector = Detector(config, section="coco", labels=COCO_CLASSES)
    boxes: dict[str, list[list[float]]] = {}
    for path in paths:
        image = imread_any(path, cv2.IMREAD_COLOR)
        found = detector.detect(image)
        boxes[path.name] = [list(d.box) for d in found if d.label == key["person_class"]]
    stored = {"key": key, "boxes": boxes}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(stored), encoding="utf-8")
    return stored


def load_coco(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def box_counts(coco: dict[str, Any]) -> dict[str, int]:
    return dict(collections.Counter(CLASSES[a["category_id"] - 1] for a in coco["annotations"]))


def prepare(
    md_raw: Path,
    rf_raw: Path,
    base: Path,
    out: Path,
    version: str,
    *,
    device: str,
    coco_model: Path,
    seed: int = SEED,
) -> dict[str, Any]:
    import cv2

    names = map_names(read_names(md_raw / "data.yaml"))
    if out.exists():
        raise SystemExit(f"출력 폴더가 이미 있다 — 지우고 다시: {out}")
    shutil.copytree(base, out, ignore=shutil.ignore_patterns("data_card.json"))

    train_imgs = list_images(md_raw / "train" / "images")
    valid_imgs = list_images(md_raw / "valid" / "images")
    assign = split_valid([p.stem for p in valid_imgs], seed)
    records: list[tuple[str, Path]] = [(assign[p.stem], p) for p in valid_imgs]
    records += [("train", p) for p in train_imgs]
    order = {"test": 0, "val": 1, "train": 2}
    records.sort(key=lambda r: (order[r[0]], r[1].name))
    check_unique_names([p for _, p in records])

    people = detect_people(
        [p for _, p in records], out.parent / f"{version}_people_cache.json", device, coco_model
    )
    rf_pool = rf_hashes(rf_raw)

    cocos = {
        "train": load_coco(out / "annotations" / "instances_train.json"),
        "val": load_coco(out / "annotations" / "instances_val.json"),
    }
    for name, coco in cocos.items():
        check_contiguous(coco, f"{base.name} {name}")
    base_counts = {
        k: {"images": len(v["images"]), "boxes": box_counts(v)} for k, v in cocos.items()
    }
    test_md = load_coco(out / "annotations" / "instances_test.json")
    test_md["images"], test_md["annotations"] = [], []
    test_md["info"] = {
        **test_md.get("info", {}),
        "description": f"Mendeley PPE2286 test — {version}",
    }
    cocos["test"] = test_md

    rng = random.Random(seed)
    kept_hashes: list[tuple[int, int]] = []
    stats: dict[str, dict[str, Any]] = {
        s: {
            "source_images": 0,
            "images_without_person": 0,
            "persons": 0,
            "excluded_images": collections.Counter(),
            "excluded": collections.Counter(),
            "boxes_dropped_partial": 0,
        }
        for s in ("test", "val", "train")
    }
    for split, path in records:
        st = stats[split]
        st["source_images"] += 1
        image = imread_any(path, cv2.IMREAD_COLOR)
        if image is None:
            raise SystemExit(f"이미지를 읽지 못했다: {path}")
        digest, flipped = hashes_with_flip(image)
        if near_any(digest, rf_pool):
            st["excluded_images"][REASON_NEAR_DUP_RF] += 1
            continue
        if near_any(digest, kept_hashes):
            st["excluded_images"][REASON_NEAR_DUP_MD] += 1
            continue
        kept_hashes.append((digest, flipped))
        h, w = image.shape[:2]
        label = path.parent.parent / "labels" / f"{path.stem}.txt"
        ppe = read_yolo(label.read_text(encoding="utf-8"), names, w, h)
        persons = [tuple(b) for b in people["boxes"][path.name]]
        if not persons:
            st["images_without_person"] += 1
            continue
        for k, person in enumerate(persons):
            st["persons"] += 1
            if person[3] - person[1] < MIN_PERSON_PX:
                st["excluded"][REASON_SMALL] += 1
                continue
            reason = person_label_problem(person, ppe)
            if reason:
                st["excluded"][reason] += 1
                continue
            pad = rng.uniform(*TRAIN_PAD_RANGE) if split == "train" else RUNTIME_PAD
            rect = crop_rect(w, h, person, pad)
            if rect is None:
                st["excluded"][REASON_CROP] += 1
                continue
            boxes: list[tuple[str, Box]] = []
            for name, box in ppe:
                moved = to_crop(box, rect)
                if moved is None:
                    st["boxes_dropped_partial"] += 1
                    continue
                boxes.append((name, moved))
            x1, y1, x2, y2 = (int(v) for v in rect)
            file_name = f"md_{path.stem}__p{k:02d}.jpg"
            write_jpeg(out / IMAGE_DIRS[split] / file_name, image[y1:y2, x1:x2])
            add_image(cocos[split], file_name, x2 - x1, y2 - y1, boxes)

    ann_names = {"train": "instances_train.json", "val": "instances_val.json", "test": MD_TEST_ANN}
    for split, coco in cocos.items():
        path = out / "annotations" / ann_names[split]
        path.write_text(json.dumps(coco), encoding="utf-8")
        st = stats[split]
        st["excluded_images"] = dict(st["excluded_images"])
        st["excluded"] = dict(st["excluded"])
        st["annotation_file"] = path.relative_to(out).as_posix()
        st["annotation_sha256"] = sha256_file(path)
        st["merged_images"] = len(coco["images"])
        st["merged_boxes"] = box_counts(coco)

    base_card = json.loads((base / "data_card.json").read_text(encoding="utf-8"))
    card = {
        "dataset_version": version,
        "base": {
            "version": base_card["dataset_version"],
            "dir": base.as_posix(),
            "splits": base_counts,
            "source": base_card["source"],
        },
        "added_source": {**SOURCE, "raw_dir": md_raw.as_posix(), "mapping": MAPPING},
        "classes": list(CLASSES),
        "person_boxes": {
            "detector": "host.vision.detector (vision.coco)",
            **people["key"],
            "device_profile": device,
        },
        "crop_rule": base_card["crop_rule"],
        "exclusion_rule": base_card["exclusion_rule"],
        "dedupe": {
            "method": f"dHash 64bit (원본·좌우 반전), 해밍 거리 ≤ {DHASH_MAX}",
            "vs_roboflow": "Roboflow 원본(train·valid·test)과 닮은 Mendeley 이미지는 통째로 제외",
            "within_mendeley": "test→val→train 순서로 먼저 남은 쪽 유지",
        },
        "splits": {
            "train": "Roboflow train + Mendeley train",
            "val": "Roboflow val + Mendeley valid 의 절반 (체크포인트 선택용)",
            "test": "instances_test.json = Roboflow test 그대로 (v1 과 같은 잣대)",
            "test_md": f"{MD_TEST_ANN} = Mendeley valid 의 나머지 절반",
            "test2017_folder": "Roboflow test 크롭과 Mendeley test 크롭(md_ 접두사)이 함께 있다 — "
            "폴더를 통째로 읽지 말고 주석 파일로 고른다",
        },
        "mendeley_stats": stats,
        "seed": seed,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "generator": {
            "script": "tools/ppe/mendeley_prepare.py",
            "sha256": sha256_file(Path(__file__)),
        },
    }
    (out / "data_card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return card


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--md-raw", type=Path, required=True, help="압축을 푼 폴더 (data.yaml 이 있는 곳)"
    )
    parser.add_argument(
        "--rf-raw", type=Path, required=True, help="Roboflow construction-safety 원본"
    )
    parser.add_argument("--base", type=Path, required=True, help="합칠 기존 빌드 (예: ppe4_cs_v1)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--device", default="mechdog-01", help="COCO 검출기 설정을 읽을 개체 프로파일"
    )
    parser.add_argument("--coco-model", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    out = ensure_under_datasets(args.out)
    card = prepare(
        args.md_raw,
        args.rf_raw,
        args.base,
        out,
        args.version,
        device=args.device,
        coco_model=args.coco_model,
        seed=args.seed,
    )
    print(json.dumps({"splits": card["mendeley_stats"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
