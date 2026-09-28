"""운용 판정 경로 목업 — 정답이 있는 원본 사진에 런타임과 같은 경로를 돌려 모델을 비교한다.

경로는 `tools/ppe_live_check.process` 그대로다: COCO 사람 검출 → 사람 크롭(여유 0.08) →
PPE 검출 → `judge()` 세 상태(적합·위반·확인불가). 모델 파일만 바꿔 끼운다 —
`models/ppe.onnx`(런타임 모델)는 건드리지 않는다.

정답은 검출된 사람 박스마다 원본 라벨에서 만든다. 머리·몸통 라벨이 둘 다 그 사람 구간에
있어야 평가 대상이고(학습 크롭과 같은 `person_label_problem` 규칙), 하나라도 `no_*` 면
«위반», 아니면 «적합» 이다. 라벨이 없는 주변 인물은 정답을 모르므로 뺀다.

⚠️ **정지 사진 한 장씩이다.** 시간 창(1.5초 안 3회)은 없다. XIAO 영상의 에피소드 지표를
대신하지 않는다 — 실기 전에 모델끼리 같은 조건에서 비교하는 용도다.

⚠️ **가장 중요한 칸은 «적합 → 위반»** 이다. v23b 는 정상 착용자 오경고 4회로 기각됐다
(승격 조건 ④ ≤1, `config/config.yaml` vision.ppe 주석).
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ppe import mendeley_prepare as md  # noqa: E402
from tools.ppe.index_raw import imread_any  # noqa: E402
from tools.ppe.rf100_prepare import (  # noqa: E402
    Box,
    in_body_part,
    load_split,
    person_label_problem,
)
from tools.ppe_live_check import (  # noqa: E402
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
    process,
)

STATES = (STATE_OK, STATE_VIOLATION, STATE_UNKNOWN)


# ── 순수 함수 (시험 대상) ─────────────────────────────────────────────
def expected_state(person: Box, gt: Sequence[tuple[str, Box]]) -> str | None:
    """검출된 사람 박스의 정답 상태. 머리·몸통 라벨이 다 없으면 `None`(평가 제외)."""
    if person_label_problem(person, gt):
        return None
    mine = [name for name, box in gt if in_body_part(name, box, person)]
    return STATE_VIOLATION if any(n.startswith("no_") for n in mine) else STATE_OK


def summarize(pairs: Iterable[tuple[str, str, str]]) -> dict[str, Any]:
    """(정답, 판정, 사유) 목록 → 혼동표와 비율."""
    confusion: dict[str, collections.Counter] = {s: collections.Counter() for s in STATES[:2]}
    reasons: collections.Counter = collections.Counter()
    for truth, judged, reason in pairs:
        confusion[truth][judged] += 1
        if judged == STATE_UNKNOWN:
            reasons[reason] += 1
    ok, bad = confusion[STATE_OK], confusion[STATE_VIOLATION]
    n_ok, n_bad = sum(ok.values()), sum(bad.values())
    total = n_ok + n_bad

    def pct(a: int, b: int) -> float | None:
        return round(100 * a / b, 1) if b else None

    return {
        "persons": total,
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "violation_recall_pct": pct(bad[STATE_VIOLATION], n_bad),
        "compliant_false_violation_pct": pct(ok[STATE_VIOLATION], n_ok),
        "compliant_false_violation": ok[STATE_VIOLATION],
        "undetermined_pct": pct(ok[STATE_UNKNOWN] + bad[STATE_UNKNOWN], total),
        "undetermined_reasons": dict(reasons),
        "effective_accuracy_pct": pct(ok[STATE_OK] + bad[STATE_VIOLATION], total),
    }


# ── 정답 원본 ─────────────────────────────────────────────────────────
def rf_test_images(rf_raw: Path) -> list[tuple[Path, list[tuple[str, Box]]]]:
    return [(r["file"], r["ppe"]) for r in load_split(rf_raw / "test" / "_annotations.coco.json")]


def md_test_images(md_raw: Path, build: Path) -> list[tuple[Path, list[tuple[str, Box]]]]:
    """빌드의 `instances_test_md.json` 에 크롭이 남은 Mendeley 원본만 (중복 제거 뒤)."""
    ann = json.loads((build / "annotations" / md.MD_TEST_ANN).read_text(encoding="utf-8"))
    stems = sorted({im["file_name"][3:].rsplit("__p", 1)[0] for im in ann["images"]})
    names = md.map_names(md.read_names(md_raw / "data.yaml"))
    out = []
    for stem in stems:
        path = md_raw / "valid" / "images" / f"{stem}.jpg"
        h, w = imread_any(path, 0).shape[:2]
        text = (md_raw / "valid" / "labels" / f"{stem}.txt").read_text(encoding="utf-8")
        out.append((path, md.read_yolo(text, names, w, h)))
    return out


def run(
    images: Sequence[tuple[Path, list[tuple[str, Box]]]],
    model: Path,
    labels: Sequence[str],
    device: str,
    coco_model: Path,
) -> dict[str, Any]:
    import cv2

    from host.common.config import load_config
    from host.vision.coco_labels import COCO_CLASSES
    from host.vision.detector import Detector

    config = load_config(device)
    config["vision"]["coco"]["model_path"] = str(coco_model)
    config["vision"]["ppe"]["model_path"] = str(model)
    ppe_cfg = config["vision"]["ppe"]
    coco = Detector(config, section="coco", labels=COCO_CLASSES)
    ppe = Detector(config, section="ppe", labels=list(labels))
    person_label = config["vision"]["coco"]["person_class"]
    pairs = []
    for path, gt in images:
        image = imread_any(path, cv2.IMREAD_COLOR)
        for person, judged, _ in process(
            image,
            coco,
            ppe,
            person_label=person_label,
            pad=0.08,
            head_margin=int(ppe_cfg["head_margin_px"]),
            use_clip=bool(ppe_cfg["require_head_visible"]),
        ):
            truth = expected_state(tuple(person.box), gt)
            if truth is not None:
                pairs.append((truth, judged.state, judged.reason))
    return summarize(pairs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rf-raw", type=Path, required=True)
    parser.add_argument("--md-raw", type=Path, required=True)
    parser.add_argument(
        "--build", type=Path, required=True, help="instances_test_md.json 이 있는 빌드"
    )
    parser.add_argument("--coco-model", type=Path, required=True)
    parser.add_argument("--device", default="mechdog-01")
    parser.add_argument(
        "--model",
        action="append",
        required=True,
        metavar="NAME=PATH[::CLASSES]",
        help="비교할 모델. CLASSES 는 쉼표 목록 (기본 helmet,no_helmet,vest,no_vest)",
    )
    parser.add_argument("--out", type=Path, help="결과 JSON")
    args = parser.parse_args(argv)
    sets = {
        "roboflow_test": rf_test_images(args.rf_raw),
        "mendeley_test": md_test_images(args.md_raw, args.build),
    }
    result: dict[str, Any] = {"images": {k: len(v) for k, v in sets.items()}, "models": {}}
    for spec in args.model:
        name, rest = spec.split("=", 1)
        path, _, classes = rest.partition("::")
        labels = classes.split(",") if classes else ["helmet", "no_helmet", "vest", "no_vest"]
        result["models"][name] = {
            set_name: run(images, Path(path), labels, args.device, args.coco_model)
            for set_name, images in sets.items()
        }
        print(json.dumps({name: result["models"][name]}, ensure_ascii=False))
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
