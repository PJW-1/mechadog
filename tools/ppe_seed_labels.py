"""PPE 초안 라벨러 — 색상 휴리스틱으로 **검수용 초안**을 만든다 (WBS 3.7.1).

    python tools/ppe_seed_labels.py --images datasets/ppe/images/train
                                    --labels datasets/ppe/labels/train
                                    --preview datasets/ppe/preview/train

흐름 — ① `coco.onnx`(YOLOX-S)로 `person` 박스를 뽑고 ② 박스 안에서
HSV 색 비율로 `helmet`/`vest` 초안을 제안한다. 주황 안전모와 형광 조끼는
학습 없이도 색으로 잘 분리되므로, 사람이 박스를 처음부터 그리는 대신
**제안을 확인하고 고치는** 작업으로 라벨링 공수를 줄인다.

⚠️ **이 도구의 출력은 정답이 아니라 초안이다.** 확신이 안 서는 경우
(`_uncertain` 버킷)는 라벨을 쓰지 않고 사람에게 넘긴다. 색으로 결론을
박는 순간 "조명 때문에 주황이 아닌데 안전모" 같은 조용한 오류가
학습 데이터에 들어간다 — **사람 검수 없이 학습에 넣지 않는다.**

⚠️ **머리가 프레임 위로 잘린 사람은 헬멧 판정을 보류한다**
(`vision.ppe.require_head_visible` 와 같은 규칙). 보이지 않는 곳에
라벨을 달면 모델이 "머리가 없으면 위반"을 배운다.

라벨 형식 — YOLO 정규화 좌표 `class cx cy w h`, 클래스 순서는
`config.yaml` 의 `vision.ppe.classes` 와 같다:

    0 helmet · 1 no_helmet · 2 vest · 3 no_vest
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.vision.detector import Detector, ModelMissingError  # noqa: E402

#: config.yaml `vision.ppe.classes` 순서와 반드시 일치시킨다.
CLASS_IDS = {"helmet": 0, "no_helmet": 1, "vest": 2, "no_vest": 3}
CLASS_NAMES = tuple(CLASS_IDS)

# ── 색상 범위 (OpenCV HSV: H 0-179) ─────────────────────────
#: 주황 안전모 — 실측 후 조정한다. 형광 물체는 S·V 가 높다.
ORANGE = ((5, 30), (150, 255), (140, 255))
#: 연두 형광 조끼 — 노랑~초록 경계. 청록·순수 초록은 밖으로 둔다.
LIME = ((30, 90), (150, 255), (160, 255))

#: person 박스를 나누는 비율 — 상단 25% 는 머리대, 25~80% 는 몸통.
HEAD_FRACTION = (0.0, 0.25)
TORSO_FRACTION = (0.25, 0.80)
#: 수평은 가운데 60% 만 본다 — 박스 가장자리 배경이 섞이는 것을 막는다.
SIDE_MARGIN = 0.20


@dataclass(frozen=True, slots=True)
class Band:
    """색 비율이 이 대역 안이면 **어느 쪽으로도 못 정한다**."""

    on_ratio: float  # 이 이상이면 물건이 있다
    off_ratio: float  # 이 이하이면 없다 — 사이는 검수 행


#: 기본 대역 — 자체 촬영분으로 캘리브레이션 후 config 에 옮긴다.
DEFAULT_BANDS = {"helmet": Band(0.15, 0.02), "vest": Band(0.20, 0.03)}


def _hsv_ratio(region_bgr: np.ndarray, hsv_range: tuple) -> float:
    """영역 안에서 HSV 범위에 드는 픽셀 비율. 빈 영역은 0."""
    if region_bgr.size == 0:
        return 0.0
    import cv2

    hsv = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([hsv_range[0][0], hsv_range[1][0], hsv_range[2][0]], dtype=np.uint8)
    hi = np.array([hsv_range[0][1], hsv_range[1][1], hsv_range[2][1]], dtype=np.uint8)
    return float(np.count_nonzero(cv2.inRange(hsv, lo, hi))) / hsv.shape[0] / hsv.shape[1]


def _zone(
    box: tuple[float, float, float, float],
    frac: tuple[float, float, float],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    """person 박스 안의 세로 대역 + 가운데 부분을 픽셀 좌표로."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    zx1 = int(np.clip(x1 + w * SIDE_MARGIN, 0, width - 1))
    zx2 = int(np.clip(x2 - w * SIDE_MARGIN, 0, width))
    zy1 = int(np.clip(y1 + h * frac[0], 0, height - 1))
    zy2 = int(np.clip(y1 + h * frac[1], 0, height))
    return zx1, zy1, zx2, zy2


def _yolo_line(class_id: int, box: tuple[int, int, int, int], width: int, height: int) -> str:
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2 / width, (y1 + y2) / 2 / height
    return f"{class_id} {cx:.6f} {cy:.6f} {(x2 - x1) / width:.6f} {(y2 - y1) / height:.6f}"


def seed_labels(
    image: np.ndarray,
    detections: list,
    *,
    head_margin_px: int = 8,
    bands: dict[str, Band] = DEFAULT_BANDS,
) -> tuple[list[str], list[dict]]:
    """프레임 하나 → (YOLO 라벨 행들, 판정 근거 행들).

    판정 근거를 함께 내보내는 이유 — 검수자가 "왜 이 라벨이 달렸는가"를
    CSV 로 보면서 잘못 제안된 것을 빨리 찾을 수 있게 한다.
    """
    height, width = image.shape[:2]
    lines: list[str] = []
    evidence: list[dict] = []

    for person in (d for d in detections if d.label == "person"):
        pbox = person.box
        head_clipped = pbox[1] <= head_margin_px

        head_zone = _zone(pbox, HEAD_FRACTION, width, height)
        torso_zone = _zone(pbox, TORSO_FRACTION, width, height)
        head_ratio = _hsv_ratio(
            image[head_zone[1] : head_zone[3], head_zone[0] : head_zone[2]], ORANGE
        )
        torso_ratio = _hsv_ratio(
            image[torso_zone[1] : torso_zone[3], torso_zone[0] : torso_zone[2]], LIME
        )

        for kind, ratio, zone, clipped in (
            ("helmet", head_ratio, head_zone, head_clipped),
            ("vest", torso_ratio, torso_zone, False),
        ):
            band = bands[kind]
            if clipped:
                verdict = "deferred"  # 머리가 프레임 밖 — 라벨 없음
            elif ratio >= band.on_ratio:
                verdict, cid = kind, CLASS_IDS[kind]
            elif ratio <= band.off_ratio:
                verdict, cid = f"no_{kind}", CLASS_IDS[f"no_{kind}"]
            else:
                verdict = "uncertain"  # 대역 안 — 사람에게 넘김
            if verdict in CLASS_IDS:
                lines.append(_yolo_line(cid, zone, width, height))
            evidence.append(
                {
                    "class": verdict,
                    "ratio": round(ratio, 4),
                    "score": round(person.score, 3),
                    "zone": zone,
                }
            )

    return lines, evidence


def _make_console_survivable() -> None:
    """한국어 Windows 콘솔(cp949)에서 `—` 등을 못 찍어 죽는 것을 막는다.

    `tools/fetch_models.py` 와 같은 조치 — 검증 도구가 출력 때문에 죽으면
    검증을 못 한다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _make_console_survivable()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--preview", type=Path, help="박스를 그린 확인용 이미지 출력 경로")
    parser.add_argument("--model", type=Path, default=ROOT / "models" / "coco.onnx")
    parser.add_argument(
        "--head-margin",
        type=int,
        default=8,
        help="person 박스 상단이 프레임 경계 이 px 이내면 헬멧 판정 보류",
    )
    args = parser.parse_args(argv)

    if not args.images.is_dir():
        parser.error(f"이미지 폴더가 없습니다: {args.images}")
    if not args.model.is_file():
        raise ModelMissingError(f"{args.model} — python tools/fetch_models.py")

    args.labels.mkdir(parents=True, exist_ok=True)
    if args.preview:
        args.preview.mkdir(parents=True, exist_ok=True)
        import cv2

    detector = Detector(
        model_path=args.model,
        model_family="yolox",
        input_size=640,
        conf_threshold=0.5,
        iou_threshold=0.45,
        providers=None,
    )
    detector.open()

    frames = sorted(
        p for p in args.images.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )
    rows: list[dict] = []
    counts = {"deferred": 0, "uncertain": 0}
    for frame_path in frames:
        image = cv2.imread(str(frame_path))
        if image is None:
            print(f"읽기 실패, 건너뜀: {frame_path}")
            continue
        lines, evidence = seed_labels(
            image, detector.detect(image), head_margin_px=args.head_margin
        )
        rel = frame_path.relative_to(args.images)
        out = args.labels / rel.with_suffix(".txt")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")

        for ev in evidence:
            counts[ev["class"]] = counts.get(ev["class"], 0) + 1
            rows.append({"image": str(rel), **ev})

        if args.preview:
            vis = image.copy()
            for ev in evidence:
                x1, y1, x2, y2 = ev["zone"]
                cv2.rectangle(
                    vis,
                    (x1, y1),
                    (x2, y2),
                    (0, 0, 255) if ev["class"] in ("deferred", "uncertain") else (0, 255, 0),
                    2,
                )
                cv2.putText(
                    vis,
                    f"{ev['class']} {ev['ratio']:.2f}",
                    (x1, max(y1 - 4, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.4,
                    (255, 255, 255),
                    1,
                )
            cv2.imwrite(str(args.preview / rel.name), vis)

    report = args.labels / "_seed_report.csv"
    with report.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["image", "class", "ratio", "score", "zone"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"프레임 {len(frames)}장 처리 — 라벨은 초안이며 사람 검수가 필요합니다")
    print(f"판정 보류 {counts.get('deferred', 0)}건 · 확신 없음 {counts.get('uncertain', 0)}건")
    print(f"근거표: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
