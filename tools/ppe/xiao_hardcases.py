"""`ppe_live_check` 세션에서 **착용 정답을 아는 구간의 틀린 프레임**을 뽑아 학습용 라벨을 만든다.

PPE 재학습의 데이터 2단계다. 입력은 `tools/ppe_live_check.py --segments --session ...
--save-raw-dir <세션>/raw` 로 남긴 세션 폴더(`session.json` + `raw/NNNNN.jpg`)다. `raw/` 가
없으면 `--save-dir` 의 `frames/` 를 읽지만, 판정을 그린 프레임이면 멈춘다.

    python tools/ppe/xiao_hardcases.py --device mechdog-01 \\
        --session TEST_MECHDOG/results/20260928_ppe-xiao \\
        --ppe-model models/ppe_v23b.onnx \\
        --build datasets/ppe/build/ppe4_cs_v1 --merge

출력 (모두 `--build` 아래 — `datasets/` 밖이면 멈춘다)
    train2017/ val2017/ xiao__<세션>__<프레임>.jpg     사람 크롭
    annotations/xiao_<세션>_{train,val}.json           이 세션만의 주석
    annotations/instances_{train,val}_mix.json         `--merge` — 공개 세트 + xiao_* 전부
    xiao_<세션>_card.json                              선택 규칙·건수·모델 sha256
    review/xiao_<세션>/sheet_NN.jpg                    사람이 검토할 접촉 시트

학습에 섞으려면 `PPE_TRAIN_ANN=instances_train_mix.json PPE_VAL_ANN=instances_val_mix.json`
으로 exp 를 띄운다. ⚠️ **접촉 시트를 사람이 먼저 본다** — 라벨은 모델 출력이다.

⚠️ **착용 상태를 구간 정의(`config/ppe_acceptance.json` 의 `wear`·`expected`)에서 읽는다.**
전부 착용 · 안전모만 · 조끼만 · 둘 다 없음 × 직립 · 웅크림 구간을 쓴다. 머리 박스
(helmet·no_helmet)는 그 구간의 안전모 정답으로, 몸통 박스(vest·no_vest)는 조끼 정답으로
이름을 정한다 — **모델이 낸 이름이 아니라 구간의 정답이 라벨이다.** 모델은 박스 위치만
준다. 그래서 **위치를 검사한다** — 머리 박스는 사람 박스 머리 구간(위 0~30%), 몸통 박스는
몸통 구간(25~80%)에 IoA 0.5 이상 들어야 하고(`rf100_prepare.in_body_part` 그대로), 이름을
바꾼 뒤 머리·몸통 박스가 같은 자리에 겹치면 둘 다 버린다. 버린 박스는 카드에 사유별로
센다. 기대=확인불가 구간(머리 잘림)과 로봇 자세 구간(pitch_up·sit)은 쓰지 않는다 — 이유는
`segment_truths` 가 카드에 남긴다. 머리나 몸통 박스가 없는 프레임은 버린다 — 없는 쪽이
배경으로 학습되기 때문이다 (`rf100_prepare.py` 의 부분 라벨 규칙과 같다).

⚠️ **구간 정답은 버튼 누른 시각에 묶인다.** 옷을 갈아입는 중에 버튼이 눌려 있으면 그 몇
초는 정답이 틀린다. 접촉 시트에서 구간 첫머리 칸을 특히 본다.

⚠️ **이 세션은 학습·검증에만 쓴다. test 로 내보내지 않는다.** 같은 사람·같은 방이라
test 에 넣으면 성능이 부풀려진다. 세션 하나 안에서 나눌 수밖에 없어 **시간 블록**(기본
10초) 단위로 train/val 을 나눈다 — 프레임 단위로 나누면 이웃 프레임이 양쪽에 들어간다.

⚠️ **저장 프레임에 판정 박스가 그려져 있으면 멈춘다.** `ppe_live_check` 는 박스·글자를
그린 뒤에 `--save-dir` 로 저장한다. 그 위에서 학습하면 모델이 «초록 테두리 + 글자» 를
단서로 배운다. 사람 박스 테두리의 판정색 비율로 가려내며, 알고도 쓰려면
`--allow-annotated` 를 준다 (카드에 남는다).
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import hashlib
import json
import math
import random
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host.vision.detector import Detection, nms  # noqa: E402
from tools.ppe.index_raw import imread_any  # noqa: E402
from tools.ppe.rf100_prepare import (  # noqa: E402
    CLASSES,
    DATASETS,
    HEAD_CLASSES,
    IMAGE_DIRS,
    TORSO_CLASSES,
    Box,
    add_image,
    area,
    empty_coco,
    ensure_under_datasets,
    in_body_part,
    intersect,
    sha256_file,
    write_jpeg,
)
from tools.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_COLOR,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
    crop_person,
    load_acceptance_plan,
)

#: 구간 정의의 착용 문구 → (안전모, 조끼) 착용 여부.
#: ⚠️ 이 표에 없는 문구의 구간은 쓰지 않는다 — 정답을 추측하지 않는다.
WEAR_TRUTH = {
    "전부 착용": (True, True),
    "안전모 미착용": (False, True),
    "조끼 미착용": (True, False),
    "둘 다 미착용": (False, False),
}
#: 조건 문구에 이것이 있으면 로봇 자세 구간이다 (`pitch-up`·`sit`·`clipped-base`).
ROBOT_POSE_MARKS = ("pitch_up", "sit", "기본 자세")

#: 이름이 같아진 박스끼리 겹치면 하나만 남긴다 (한 머리에 helmet·no_helmet 이 함께 뜬 경우).
DEDUPE_IOU = 0.45
#: 이름을 바꾼 뒤 머리 박스와 몸통 박스가 이만큼 겹치면 «같은 자리의 두 계열» 로 보고 둘 다 버린다.
CROSS_IOU = 0.45
KEEP_RATIO = 0.2
MIN_GAP_S = 0.5
BLOCK_S = 10.0
VAL_RATIO = 0.25
SEED = 20260928

#: 사람 박스 테두리에서 판정색이 이 비율 이상이면 «그려진 프레임» 으로 본다 (중앙값).
OVERLAY_LIMIT = 0.5
#: JPEG 압축 뒤 색 허용 오차(채널별)와 테두리 탐색 폭(px).
OVERLAY_TOL = 60
OVERLAY_BAND = 3

#: 라벨에서 버린 박스의 사유 (카드 `dropped_boxes`).
DROP_HEAD_PLACE = "머리 박스가 머리 구간 밖"
DROP_TORSO_PLACE = "몸통 박스가 몸통 구간 밖"
DROP_CROSS = "머리·몸통 박스가 같은 자리"

SHEET_COLS, SHEET_ROWS = 6, 5
CELL_W, CELL_H = 200, 300
DRAW_COLORS = {
    "helmet": (0, 200, 0),
    "no_helmet": (0, 0, 255),
    "vest": (200, 200, 0),
    "no_vest": (255, 0, 255),
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """라벨을 붙일 수 있는 프레임 하나."""

    tag: str
    t: float
    hard: bool
    person: tuple[float, float, float, float]
    boxes: tuple[tuple[str, tuple[float, float, float, float]], ...]
    segment: str = ""


@dataclass(frozen=True, slots=True)
class SegmentTruth:
    """구간 하나의 정답 — 안전모·조끼 착용 여부와 구간 정의의 기대 판정."""

    helmet: bool
    vest: bool
    expected: str


def segment_truths(
    specs: Iterable[dict[str, str]],
) -> tuple[dict[str, SegmentTruth], dict[str, str]]:
    """구간 정의에서 착용 정답을 읽는다. (쓰는 구간 → 정답, 뺀 구간 → 이유)

    ⚠️ **착용을 확실히 아는 구간만 쓴다.** 애매하면 빼고 이유를 카드에 남긴다.
    """
    used: dict[str, SegmentTruth] = {}
    excluded: dict[str, str] = {}
    for spec in specs:
        key, wear, expected = spec["key"], spec.get("wear", ""), spec.get("expected")
        worn = WEAR_TRUTH.get(wear)
        if worn is None:
            excluded[key] = f"착용 문구를 모른다 («{wear}») — 정답을 추측하지 않는다"
        elif expected == STATE_UNKNOWN:
            excluded[key] = (
                "기대=확인불가 — 머리·몸통이 다 보인다는 보장이 없어(머리 잘림) "
                "박스 정답을 세울 수 없다"
            )
        elif any(mark in spec.get("condition", "") for mark in ROBOT_POSE_MARKS):
            excluded[key] = (
                "로봇 자세 구간 — 자세 전환 중 흔들린 프레임이 섞이고 사람 자세 조건이 "
                "정해져 있지 않다 (수집 세션은 로봇을 움직이지 않는다)"
            )
        elif expected != (STATE_OK if all(worn) else STATE_VIOLATION):
            excluded[key] = f"기대 판정({expected})과 착용({wear})이 어긋난다"
        else:
            used[key] = SegmentTruth(worn[0], worn[1], expected)
    return used, excluded


def truth_events(
    events: Iterable[dict[str, Any]], truths: dict[str, SegmentTruth]
) -> list[dict[str, Any]]:
    """정답을 아는 구간에서 사람이 잡힌 프레임만.

    ⚠️ 세션이 기록한 기대 판정이 지금 구간 정의와 다르면 **정의가 세션 뒤에 바뀐 것**이다.
    그 정의로 라벨을 붙이면 틀린다 — 멈춘다.
    """
    out = []
    for e in events:
        truth = truths.get(e.get("segment") or "")
        if truth is None:
            continue
        if e.get("expected") != truth.expected:
            raise SystemExit(
                f"구간 {e['segment']}: 세션 기대 {e.get('expected')} ≠ 구간 정의 "
                f"{truth.expected} — 구간 정의가 세션 뒤에 바뀌었다"
            )
        if int(e.get("people", 0)) > 0:
            out.append(e)
    return out


def truth_names(helmet: bool, vest: bool) -> dict[str, str]:
    """모델이 낸 이름 → 구간 정답 이름. 머리 박스는 안전모, 몸통 박스는 조끼 정답을 따른다.

    ⚠️ 이 표에 없는 이름(5클래스 모델의 5번째 등)은 버린다.
    """
    head = "helmet" if helmet else "no_helmet"
    torso = "vest" if vest else "no_vest"
    return {**dict.fromkeys(HEAD_CLASSES, head), **dict.fromkeys(TORSO_CLASSES, torso)}


def iou(a: Box, b: Box) -> float:
    inter = area(intersect(a, b))
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def relabel_by_truth(
    detections: Sequence[Detection],
    helmet: bool,
    vest: bool,
    person: Box | None = None,
    drops: collections.Counter | None = None,
) -> list[Detection]:
    """박스 이름을 구간 정답으로 바꾸고 겹침을 걷어 낸다 (한 머리에 두 이름이 뜬 경우).

    `person` 을 주면 (PPE 박스와 같은 좌표계) 부위 밖 박스를 먼저 버린다 — 이름을 정답으로
    바꾸므로 몸통·배경에 그어진 머리 박스도 그대로 라벨이 되기 때문이다. 이름 NMS 뒤에는
    머리·몸통 두 계열이 같은 자리에 겹친 쌍을 버린다. 버린 수는 `drops` 에 사유별로 더한다.
    """
    drops = collections.Counter() if drops is None else drops
    names = truth_names(helmet, vest)
    renamed = [Detection(names[d.label], d.score, d.box) for d in detections if d.label in names]
    if person is not None:
        placed = []
        for d in renamed:
            if in_body_part(d.label, d.box, person):
                placed.append(d)
            else:
                drops[DROP_HEAD_PLACE if d.label in HEAD_CLASSES else DROP_TORSO_PLACE] += 1
        renamed = placed
    out: list[Detection] = []
    for name in dict.fromkeys(d.label for d in renamed):
        same = [d for d in renamed if d.label == name]
        boxes = np.array([d.box for d in same], dtype=np.float32)
        scores = np.array([d.score for d in same], dtype=np.float32)
        out.extend(same[i] for i in nms(boxes, scores, DEDUPE_IOU))
    crossed: set[int] = set()
    for i, h in enumerate(out):
        for j, t in enumerate(out):
            head_torso = h.label in HEAD_CLASSES and t.label in TORSO_CLASSES
            if head_torso and iou(h.box, t.box) >= CROSS_IOU:
                crossed |= {i, j}
    if crossed:
        drops[DROP_CROSS] += len(crossed)
    return [d for i, d in enumerate(out) if i not in crossed]


def is_hard(states: Sequence[str], raw: Sequence[Detection], truth: SegmentTruth) -> bool:
    """세션 판정이 기대와 반대였거나, 다시 추론한 박스 이름이 구간 정답과 다르면 «틀림».

    ⚠️ 확인불가는 틀림으로 세지 않는다 (예전 전부 착용 규칙과 같다).
    """
    opposite = STATE_VIOLATION if truth.expected == STATE_OK else STATE_OK
    names = truth_names(truth.helmet, truth.vest)
    return opposite in states or any(d.label in names and names[d.label] != d.label for d in raw)


def segment_table(
    truths: dict[str, SegmentTruth],
    excluded: dict[str, str],
    events: Sequence[dict[str, Any]],
    candidates: Sequence[Candidate],
    picked: Sequence[Candidate],
    splits: Sequence[str],
) -> dict[str, Any]:
    """세션 카드용 구간별 채택 수 — 프레임 → 후보 → 선택(train/val)."""
    frames = collections.Counter(e["segment"] for e in events)
    cands = collections.Counter(c.segment for c in candidates)
    hard = collections.Counter(c.segment for c in candidates if c.hard)
    chosen = collections.Counter((c.segment, s) for c, s in zip(picked, splits, strict=True))
    used = {
        key: {
            "wear": next(w for w, v in WEAR_TRUTH.items() if v == (truth.helmet, truth.vest)),
            "expected": truth.expected,
            "frames": frames[key],
            "candidates": cands[key],
            "candidates_hard": hard[key],
            "selected": {s: chosen[(key, s)] for s in ("train", "val")},
        }
        for key, truth in truths.items()
    }
    return {"used": used, "excluded": dict(excluded)}


def has_head_and_torso(detections: Sequence[Detection]) -> bool:
    labels = {d.label for d in detections}
    return bool(labels & set(HEAD_CLASSES)) and bool(labels & set(TORSO_CLASSES))


def select_frames(
    candidates: Sequence[Candidate], keep_ratio: float, min_gap_s: float, seed: int = SEED
) -> list[int]:
    """틀린 프레임은 전부, 맞힌 프레임은 `keep_ratio` 만큼 — **서로 `min_gap_s` 이상 떨어뜨려.**

    ⚠️ 틀린 프레임을 먼저 자리 잡게 한다. 간격 규칙으로 밀려날 때 버려지는 쪽이
    흔한 맞힌 프레임이어야 한다. 돌려주는 순서는 시간순이다.
    """
    rng = random.Random(seed)
    hard = [i for i, c in enumerate(candidates) if c.hard]
    easy = [i for i, c in enumerate(candidates) if not c.hard]
    easy = sorted(rng.sample(easy, round(len(easy) * keep_ratio)))
    taken: list[float] = []
    chosen: list[int] = []
    for index in hard + easy:
        t = candidates[index].t
        pos = bisect.bisect_left(taken, t)
        near = [taken[j] for j in (pos - 1, pos) if 0 <= j < len(taken)]
        if any(abs(t - other) < min_gap_s for other in near):
            continue
        taken.insert(pos, t)
        chosen.append(index)
    return sorted(chosen, key=lambda i: candidates[i].t)


def split_by_block(
    times: Sequence[float], block_s: float, val_ratio: float, seed: int = SEED
) -> list[str]:
    """시간 블록 단위로 `train`/`val` 만 낸다. **`test` 는 절대 내지 않는다.**"""
    blocks = sorted({int(t // block_s) for t in times})
    rng = random.Random(seed)
    shuffled = blocks[:]
    rng.shuffle(shuffled)
    n_val = min(len(blocks) - 1, math.ceil(len(blocks) * val_ratio)) if len(blocks) > 1 else 0
    val = set(shuffled[:n_val])
    return ["val" if int(t // block_s) in val else "train" for t in times]


def overlay_fraction(
    image: np.ndarray,
    box: tuple[float, float, float, float],
    colors: Iterable[tuple[int, int, int]] = tuple(STATE_COLOR.values()),
    tol: int = OVERLAY_TOL,
    band: int = OVERLAY_BAND,
) -> float:
    """사람 박스 네 변 가운데 **판정색 선이 가장 뚜렷한 변**의 비율 (0~1).

    변 위 각 위치에서 ±`band` px 안에 판정색 픽셀이 있으면 그 위치를 센다. 자연 사진에서
    한 변의 절반 넘게 순색 초록·빨강·주황 선이 이어지는 일은 드물다.
    """
    height, width = image.shape[:2]
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    x1, x2 = max(0, min(width - 1, x1)), max(0, min(width - 1, x2))
    y1, y2 = max(0, min(height - 1, y1)), max(0, min(height - 1, y2))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return 0.0
    signed = image.astype(np.int16)
    mask = np.zeros((height, width), dtype=bool)
    for color in colors:
        mask |= (np.abs(signed - np.array(color, dtype=np.int16)) <= tol).all(axis=2)

    def rows(y: int) -> float:
        strip = mask[max(0, y - band) : y + band + 1, x1 : x2 + 1]
        return float(strip.any(axis=0).mean())

    def cols(x: int) -> float:
        strip = mask[y1 : y2 + 1, max(0, x - band) : x + band + 1]
        return float(strip.any(axis=1).mean())

    return max(rows(y1), rows(y2), cols(x1), cols(x2))


def merge_coco(base: dict[str, Any], extras: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """COCO 여러 개를 하나로. id 를 새로 매기고 **범주가 다르면 멈춘다.**"""
    merged = {**base, "images": [], "annotations": []}
    for part in (base, *extras):
        if part["categories"] != base["categories"]:
            raise SystemExit("범주 목록이 다른 주석은 합치지 않는다")
        remap: dict[int, int] = {}
        for image in part["images"]:
            remap[image["id"]] = len(merged["images"]) + 1
            merged["images"].append({**image, "id": remap[image["id"]]})
        for ann in part["annotations"]:
            merged["annotations"].append(
                {**ann, "id": len(merged["annotations"]) + 1, "image_id": remap[ann["image_id"]]}
            )
    return merged


def merge_build(build: Path) -> dict[str, str]:
    """`instances_{train,val}.json` + `xiao_*_{train,val}.json` → `instances_{split}_mix.json`."""
    ann_dir = build / "annotations"
    written: dict[str, str] = {}
    for split in ("train", "val"):
        base = json.loads((ann_dir / f"instances_{split}.json").read_text(encoding="utf-8"))
        extras = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(ann_dir.glob(f"xiao_*_{split}.json"))
        ]
        out = ann_dir / f"instances_{split}_mix.json"
        # ASCII 로 쓴다 — pycocotools 가 cp949 로 읽는다 (rf100_prepare 와 같다)
        out.write_text(json.dumps(merge_coco(base, extras)), encoding="utf-8")
        written[split] = f"{out.name} · sha256 {sha256_file(out)}"
    return written


def draw_cell(crop: np.ndarray, cand: Candidate, split: str) -> np.ndarray:
    """접촉 시트 한 칸. 틀렸던 프레임은 빨간 테두리."""
    import cv2

    scale = min(CELL_W / crop.shape[1], (CELL_H - 30) / crop.shape[0])
    small = cv2.resize(
        crop, (max(1, int(crop.shape[1] * scale)), max(1, int(crop.shape[0] * scale)))
    )
    cell = np.full((CELL_H, CELL_W, 3), 40, dtype=np.uint8)
    cell[: small.shape[0], : small.shape[1]] = small
    for name, (bx1, by1, bx2, by2) in cand.boxes:
        p1 = (int(bx1 * scale), int(by1 * scale))
        p2 = (int(bx2 * scale), int(by2 * scale))
        cv2.rectangle(cell, p1, p2, DRAW_COLORS[name], 1)
    text = f"{cand.tag} {cand.t:.1f}s {split}" + (" HARD" if cand.hard else "")
    cv2.putText(cell, text, (2, CELL_H - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1)
    # 어느 구간(정답)의 라벨인지 — 맨머리 구간에 helmet 이 보이면 라벨이 틀린 것이다
    cv2.putText(
        cell, cand.segment, (2, CELL_H - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1
    )
    if cand.hard:
        cv2.rectangle(cell, (0, 0), (CELL_W - 1, CELL_H - 1), (0, 0, 255), 2)
    return cell


def write_sheets(cells: Sequence[np.ndarray], folder: Path) -> int:
    per = SHEET_COLS * SHEET_ROWS
    for n, start in enumerate(range(0, len(cells), per), 1):
        chunk = list(cells[start : start + per])
        chunk += [np.zeros((CELL_H, CELL_W, 3), dtype=np.uint8)] * (per - len(chunk))
        rows = [np.hstack(chunk[r * SHEET_COLS : (r + 1) * SHEET_COLS]) for r in range(SHEET_ROWS)]
        write_jpeg(folder / f"sheet_{n:02d}.jpg", np.vstack(rows))
    return math.ceil(len(cells) / per)


def load_frame(session: Path, tag: str) -> np.ndarray | None:
    import cv2

    # ⚠️ 왜: `frames/` 는 판정을 그린 프레임이다. `--save-raw-dir <세션>/raw` 원본이 있으면
    # 그것을 쓴다 — 없을 때만 `frames/` 로 돌아가고, 그림 여부는 `collect` 가 따로 막는다.
    raw = session / "raw" / f"{tag}.jpg"
    return imread_any(raw if raw.is_file() else session / "frames" / f"{tag}.jpg", cv2.IMREAD_COLOR)


def build_detectors(device: str, ppe_model: Path, coco_model: Path | None):
    """런타임과 같은 설정으로 두 검출기를 만든다. 모델 경로만 바꾼다."""
    from host.common.config import load_config
    from host.vision.coco_labels import COCO_CLASSES
    from host.vision.detector import Detector

    config = load_config(device)
    config["vision"]["ppe"]["model_path"] = str(ppe_model.resolve())
    if coco_model is not None:
        config["vision"]["coco"]["model_path"] = str(coco_model.resolve())
    coco = Detector(config, section="coco", labels=COCO_CLASSES)
    # ⚠️ 이름은 4개만 준다. 5클래스 모델의 5번째는 `class_4` 로 나와 `truth_names` 에서 버려진다.
    ppe = Detector(config, section="ppe", labels=CLASSES)
    coco.open()
    ppe.open()
    return config, coco, ppe


def collect(
    session: Path, events, truths: dict[str, SegmentTruth], config, coco, ppe
) -> tuple[list[Candidate], dict[str, Any]]:
    """정답을 아는 프레임마다 다시 추론해 라벨 후보를 만든다."""
    person_label = config["vision"]["coco"]["person_class"]
    pad = float(config["vision"]["ppe"]["crop_pad"])
    skipped: collections.Counter = collections.Counter()
    dropped: collections.Counter = collections.Counter()
    overlays: list[float] = []
    out: list[Candidate] = []
    for event in events:
        image = load_frame(session, event["tag"])
        if image is None:
            skipped["프레임 파일 없음"] += 1
            continue
        people = [d for d in coco.detect(image) if d.label == person_label]
        if not people:
            skipped["사람 미검출"] += 1
            continue
        # ⚠️ 런타임(`PpeDetector.observe`)처럼 **가장 큰(높은) 사람 하나**만 본다.
        person = max(people, key=lambda d: d.box[3] - d.box[1])
        overlays.append(overlay_fraction(image, person.box))
        crop, (ox, oy) = crop_person(image, person.box, pad)
        if crop is None:
            skipped["크롭 실패"] += 1
            continue
        truth = truths[event["segment"]]
        raw = ppe.detect(crop)
        # ⚠️ PPE 박스는 크롭 좌표다. 사람 박스를 크롭 원점만큼 옮겨 같은 좌표계에서 본다.
        x1, y1, x2, y2 = person.box
        in_crop = (x1 - ox, y1 - oy, x2 - ox, y2 - oy)
        labels = relabel_by_truth(raw, truth.helmet, truth.vest, person=in_crop, drops=dropped)
        if not has_head_and_torso(labels):
            skipped["머리·몸통 박스 없음"] += 1
            continue
        out.append(
            Candidate(
                tag=event["tag"],
                t=float(event["t"]),
                hard=is_hard(event.get("states", []), raw, truth),
                person=person.box,
                boxes=tuple((d.label, d.box) for d in labels),
                segment=event["segment"],
            )
        )
    stats = {
        "skipped": dict(skipped),
        "dropped_boxes": dict(dropped),
        "overlay_median": round(statistics.median(overlays), 3) if overlays else 0.0,
    }
    return out, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True, help="개체 프로파일 (예: mechdog-01)")
    parser.add_argument("--session", type=Path, required=True, help="session.json 이 든 폴더")
    parser.add_argument("--ppe-model", type=Path, required=True, help="다시 추론할 PPE onnx")
    parser.add_argument("--coco-model", type=Path, help="기본은 설정의 models/coco.onnx")
    parser.add_argument("--build", type=Path, required=True, help="rf100_prepare 출력 폴더")
    parser.add_argument(
        "--plan", type=Path, default=DEFAULT_ACCEPTANCE_PLAN, help="구간 정의 (착용 정답)"
    )
    parser.add_argument(
        "--keep-ratio", type=float, default=KEEP_RATIO, help="맞힌 프레임 표본 비율"
    )
    parser.add_argument("--min-gap-s", type=float, default=MIN_GAP_S)
    parser.add_argument("--block-s", type=float, default=BLOCK_S)
    parser.add_argument("--val-ratio", type=float, default=VAL_RATIO)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--merge", action="store_true", help="instances_{train,val}_mix.json 생성")
    parser.add_argument(
        "--allow-annotated", action="store_true", help="판정 박스가 그려진 프레임도 쓴다"
    )
    args = parser.parse_args(argv)

    build = ensure_under_datasets(args.build)
    if not (build / "annotations" / "instances_train.json").is_file():
        raise SystemExit(f"rf100_prepare 출력이 아니다: {build}")
    session_file = args.session / "session.json"
    data = json.loads(session_file.read_text(encoding="utf-8"))
    scenario = data.get("scenario") or "xiao"
    specs, _, _ = load_acceptance_plan(args.plan, scenario)
    truths, excluded = segment_truths(specs)
    events = truth_events(data.get("events", []), truths)
    if not events:
        raise SystemExit(f"착용 정답을 아는 구간 프레임이 없다 (뺀 구간 {excluded})")
    name = args.session.resolve().name

    config, coco, ppe = build_detectors(args.device, args.ppe_model, args.coco_model)
    print(f"착용 정답을 아는 프레임 {len(events)}장 — 다시 추론")
    candidates, stats = collect(args.session, events, truths, config, coco, ppe)
    if stats["overlay_median"] >= OVERLAY_LIMIT and not args.allow_annotated:
        raise SystemExit(
            f"저장 프레임에 판정 박스가 그려져 있다 (테두리 판정색 중앙값 "
            f"{stats['overlay_median']}). 원본 프레임으로 다시 찍거나 --allow-annotated"
        )

    chosen = select_frames(candidates, args.keep_ratio, args.min_gap_s, args.seed)
    picked = [candidates[i] for i in chosen]
    splits = split_by_block([c.t for c in picked], args.block_s, args.val_ratio, args.seed)
    assert "test" not in splits  # ⚠️ 세션 프레임은 test 로 새지 않는다

    pad = float(config["vision"]["ppe"]["crop_pad"])
    cocos = {s: empty_coco(s, f"xiao_{name}") for s in ("train", "val")}
    cells: list[np.ndarray] = []
    for cand, split in zip(picked, splits, strict=True):
        image = load_frame(args.session, cand.tag)
        crop, _ = crop_person(image, cand.person, pad)
        file_name = f"xiao__{name}__{cand.tag}.jpg"
        write_jpeg(build / IMAGE_DIRS[split] / file_name, crop)
        add_image(cocos[split], file_name, crop.shape[1], crop.shape[0], list(cand.boxes))
        cells.append(draw_cell(crop, cand, split))

    ann_dir = build / "annotations"
    files: dict[str, str] = {}
    for split, coco_json in cocos.items():
        path = ann_dir / f"xiao_{name}_{split}.json"
        path.write_text(json.dumps(coco_json), encoding="utf-8")  # ASCII — 위 merge 와 같다
        files[split] = f"{path.name} · sha256 {sha256_file(path)}"
    sheets = write_sheets(cells, build / "review" / f"xiao_{name}")
    merged = merge_build(build) if args.merge else {}

    counts = {
        split: {
            "images": len(c["images"]),
            "hard": sum(1 for p, s in zip(picked, splits, strict=True) if s == split and p.hard),
            "boxes": dict(
                collections.Counter(CLASSES[a["category_id"] - 1] for a in c["annotations"])
            ),
        }
        for split, c in cocos.items()
    }
    card = {
        "session": {
            "name": name,
            "session_json_sha256": sha256_file(session_file),
            "source": data.get("source"),
            "scenario": data.get("scenario"),
            "recorded_model_sha256": data.get("settings", {}).get("model_sha256"),
        },
        "models": {
            "ppe": {"path": args.ppe_model.as_posix(), "sha256": sha256_file(args.ppe_model)},
            "coco": config["vision"]["coco"]["model_path"],
        },
        "plan": {
            "path": args.plan.as_posix(),
            "sha256": sha256_file(args.plan),
            "scenario": scenario,
        },
        "rule": {
            "frames": "착용 정답을 아는 구간 · 가장 큰 사람 · crop_pad 런타임값",
            "relabel": "머리 박스(helmet·no_helmet) → 구간 안전모 정답, "
            "몸통 박스(vest·no_vest) → 구간 조끼 정답 (모델 이름 무시)",
            "place": "머리 박스는 사람 박스 머리 구간(0~30%), 몸통 박스는 몸통 구간(25~80%)에 "
            f"IoA≥0.5 (rf100_prepare.in_body_part) · 머리·몸통 박스 IoU≥{CROSS_IOU} 이면 둘 다 버림",
            "hard": "세션 판정이 기대와 반대였거나 다시 추론한 이름이 구간 정답과 다름",
            "keep_ratio": args.keep_ratio,
            "min_gap_s": args.min_gap_s,
            "block_s": args.block_s,
            "val_ratio": args.val_ratio,
            "seed": args.seed,
            "crop_pad": pad,
            "test": "내보내지 않음",
        },
        "annotated_frames": stats["overlay_median"] >= OVERLAY_LIMIT,
        "overlay_median": stats["overlay_median"],
        "labelled_frames": len(events),
        "candidates": len(candidates),
        "candidates_hard": sum(1 for c in candidates if c.hard),
        "skipped": stats["skipped"],
        "dropped_boxes": stats["dropped_boxes"],
        "splits": counts,
        "segments": segment_table(truths, excluded, events, candidates, picked, splits),
        "annotations": files,
        "merged": merged,
        "review_sheets": sheets,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "generator": {
            "script": "tools/ppe/xiao_hardcases.py",
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    }
    card_path = build / f"xiao_{name}_card.json"
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"후보 {len(candidates)}장(틀림 {card['candidates_hard']}) → 선택 {len(picked)}장 {counts}"
    )
    print(f"건너뜀 {stats['skipped']} · 접촉 시트 {sheets}장 → {build / 'review'}")
    print(f"기록: {card_path.relative_to(DATASETS.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
