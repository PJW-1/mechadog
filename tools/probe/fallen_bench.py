"""YOLOX-S 가 누운 사람을 «쓰러짐 의심 후보» 로 얼마나 잡는지 우리 사진으로 잰다 (FR-9 · ADR-42).

런타임은 `FallenGate` 의 `candidate` 한 번으로 쓰러짐 의심에 들어간다(`host/runtime.py`
`_observe_fallen`). 그 후보가 실제 누운 사람 사진에서 얼마나 서는지, 서 있는 사람·빈 장면에서
얼마나 잘못 서는지를 conf·종횡비를 바꿔 가며 본다. 모델을 학습하거나 설정을 바꾸지 않는다 —
로봇·카메라를 건드리지 않는 오프라인 계산이다.

    python tools/probe/fallen_bench.py <폴더> --confs 0.2,0.3,0.4,0.5 \
        --aspects 1.2,1.5,2.0 --out raw.json

⚠️ **사진을 저장소에 커밋하지 않는다** — 얼굴이 찍힐 수 있다. `<폴더>` 는 저장소 밖에 둔다.

입력 폴더

    `vlm_bench.py` 와 같은 폴더를 그대로 쓴다 — `capture --question person_down` 으로 찍은 것.

    <root>/person_down/<yes|no>/<장면>_NN.jpg

    같은 `<root>` 에 다른 질문키 폴더(`fallen_object` 등)가 있어도 `person_down` 만 쓴다.
    구조가 틀리거나 `person_down` 사진이 없으면 멈춘다(종료 코드 2).

판정 (런타임과 같다)

    `worker._run_one` 처럼 `decode_jpeg` → `Detector.detect` → 새 `PersonGate.observe` 가 고른
    대표 박스(**점수가 가장 높은 사람** — 가장 넓은 박스가 아니다) → 새 `FallenGate.observe`
    를 지난다. 첫 관측이라 이동량은 0 이므로 한 장만으로 종횡비(가로 ÷ 세로) ≥
    `vision.fallen.aspect_ratio` 면 후보다. 판정 규칙을 다시 짜지 않고 런타임 클래스를 쓴다.
    놓친 까닭은 `사람 미검출`(그 conf 이상의 사람이 없다)과 `종횡비 미달` 둘이다.

추론은 한 번

    `vision.coco.conf_threshold` 만 `--conf-floor`(기본 0.1)로 내린 검출기로 사진마다 한 번
    추론하고, 더 높은 conf 는 그 결과를 다시 걸러 잰다 — 클래스별 NMS 는 점수 내림차순 탐욕
    억제라 «억제 뒤 임계값» 과 «임계값 뒤 억제» 가 같은 박스를 남긴다
    (`tools/ppe/ablation.py` 와 같은 논리). 그래서 `--confs` 는 바닥값보다 낮을 수 없다.

채점

    프레임 단위와 장면 단위를 함께 낸다. 장면은 파일 이름 끝의 `_<숫자>` 를 뗀 것이고
    (`vlm_bench.scene_of`), **장면 안 프레임 하나라도 후보면** 그 장면을 «후보» 로 친다 —
    런타임은 후보 한 번으로 의심에 들어가기 때문이다. 운용 설정(설정 파일의
    `vision.coco.conf_threshold`·`vision.fallen.aspect_ratio`)은 훑기 값에 없어도 늘 보고한다.

    ⚠️ `vision.coco.conf_threshold` 는 쓰러짐만의 값이 아니다 — 내리면 사람 판정
    (`PersonGate`)·추적·PPE 게이팅도 함께 바뀐다.

출력

    표준 출력에 마크다운 표, `--out x.json` 에 사진별 사람 검출(바닥값 기준)·판정·요약.
    이미지는 담지 않는다. 모델 파일이 없으면 안내를 내고 멈춘다(종료 코드 1).
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.config import load_base_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.vision.coco_labels import COCO_CLASSES  # noqa: E402
from host.vision.detector import Detection, Detector, ModelMissingError  # noqa: E402
from host.vision.person import FallenGate, PersonGate  # noqa: E402
from host.vision.stream_client import decode_jpeg  # noqa: E402
from tools.probe.vlm_bench import LayoutError, Sample, collect, score  # noqa: E402

KEY = "person_down"
MISS_PERSON = "사람 미검출"
MISS_ASPECT = "종횡비 미달"
CONF_NOTE = (
    "⚠️ `vision.coco.conf_threshold` 를 내리면 쓰러짐 후보만이 아니라 사람 판정(`PersonGate`)"
    "·추적·PPE 게이팅도 함께 바뀐다."
)


class PersonDetector(Protocol):
    def detect(self, image: Any) -> list[Detection]: ...

    def model_summary(self) -> dict[str, Any]: ...


def build_detector(config: Mapping[str, Any]) -> PersonDetector:
    """런타임과 같은 COCO 검출기. 시험은 `main(detector_factory=...)` 로 갈아 끼운다."""
    return Detector(config, section="coco", labels=COCO_CLASSES)


def judge(
    people: Sequence[Detection], conf: float, aspect: float, *, config: Mapping[str, Any]
) -> dict[str, Any]:
    """사람 검출(바닥값 기준) 하나의 사진을 (conf, 종횡비) 로 판정한다 — 런타임 게이트 그대로.

    `PersonGate` 가 점수 최고를 대표 박스로 고르고, 첫 관측의 `FallenGate` 가 종횡비만 본다.
    """
    gate = PersonGate(config)
    sighting = gate.observe(0, [d for d in people if d.score >= conf])
    vision = config["vision"]
    tuned = {"vision": {**vision, "fallen": {**vision["fallen"], "aspect_ratio": aspect}}}
    verdict = FallenGate(tuned).observe(0, sighting.box, track_id=None)
    reason = None
    if not verdict.candidate:
        reason = MISS_PERSON if sighting.box is None else MISS_ASPECT
    return {
        "candidate": verdict.candidate,
        "reason": reason,
        "score": sighting.best_score if sighting.box is not None else None,
        "aspect": verdict.aspect,
    }


def infer(
    samples: Sequence[Sample], detector: PersonDetector, person_label: str
) -> list[dict[str, Any]]:
    """사진마다 한 번 추론해 사람 검출 전부(바닥값 기준)와 이미지 크기를 남긴다."""
    records: list[dict[str, Any]] = []
    for sample in samples:
        image = decode_jpeg(sample.path.read_bytes())
        people = [d for d in detector.detect(image) if d.label == person_label]
        records.append(
            {
                "label": sample.label,
                "file": sample.file,
                "scene": sample.scene,
                "frame": sample.frame,
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "people": people,
            }
        )
        print(f"  {sample.file}  사람 {len(people)}", file=sys.stderr, flush=True)
    return records


def _evaluate(
    records: Sequence[dict[str, Any]], conf: float, aspect: float, config: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """한 (conf, 종횡비) 조합의 사진별 판정과 프레임·장면 채점."""
    verdicts = [judge(r["people"], conf, aspect, config=config) for r in records]
    frames = score(
        (r["label"] == "yes", v["candidate"]) for r, v in zip(records, verdicts, strict=True)
    )
    scenes: dict[tuple[str, str], bool] = defaultdict(bool)
    for record, verdict in zip(records, verdicts, strict=True):
        key = (record["label"], record["scene"])
        scenes[key] = scenes[key] or verdict["candidate"]
    scored = score((label == "yes", hit) for (label, _), hit in scenes.items())
    return verdicts, {"frames": frames, "scenes": scored}


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def summarize(
    records: Sequence[dict[str, Any]],
    grid: Sequence[tuple[float, float]],
    operating: tuple[float, float],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """운용 설정·훑기·장면별 요약과 운용 설정의 사진별 판정."""
    verdicts, op = _evaluate(records, *operating, config)
    yes = [
        v
        for r, v in zip(records, verdicts, strict=True)
        if r["label"] == "yes" and not v["candidate"]
    ]
    op["missed"] = {
        MISS_PERSON: sum(v["reason"] == MISS_PERSON for v in yes),
        MISS_ASPECT: sum(v["reason"] == MISS_ASPECT for v in yes),
    }
    sweep = []
    for conf, aspect in grid:
        _, scored = _evaluate(records, conf, aspect, config)
        sweep.append({"conf": conf, "aspect": aspect, "operating": (conf, aspect) == operating})
        sweep[-1].update(scored)

    by_scene: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for record, verdict in zip(records, verdicts, strict=True):
        by_scene[record["label"], record["scene"]].append((record, verdict))
    scenes = []
    for (label, scene), rows in by_scene.items():
        boxed = [v for _, v in rows if v["score"] is not None]
        aspects = [v["aspect"] for v in boxed if v["aspect"] is not None]
        floor_best = [d.score for r, _ in rows for d in r["people"]]
        scenes.append(
            {
                "label": label,
                "scene": scene,
                "frames": len(rows),
                "person_frames": len(boxed),
                "candidate_frames": sum(v["candidate"] for _, v in rows),
                "max_score": _round(max((v["score"] for v in boxed), default=None)),
                "aspect_min": _round(min(aspects, default=None)),
                "aspect_max": _round(max(aspects, default=None)),
                "floor_best_score": _round(max(floor_best, default=None)),
            }
        )
    return {"operating": op, "sweep": sweep, "scenes": scenes}, verdicts


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _num(value: Any) -> str:
    return "-" if value is None else str(value)


def _counts(s: dict[str, Any]) -> str:
    return (
        f"{s['tp'] + s['fn']}/{s['fp'] + s['tn']} | {s['tp']} | {s['fn']} | {s['fp']} "
        f"| {s['tn']} | {_rate(s['recall'])} | {_rate(s['false_alarm'])}"
    )


def render(summary: dict[str, Any], operating: tuple[float, float]) -> str:
    """사람이 읽을 마크다운 표."""
    op = summary["operating"]
    conf, aspect = operating
    lines = [
        f"## 운용 설정 — conf {conf} · 종횡비 {aspect}",
        "",
        "| 단위 | 예/아니오 | TP | FN | FP | TN | 적중률 | 오경보율 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| 프레임 | {_counts(op['frames'])} |",
        f"| 장면 (한 장이라도 후보) | {_counts(op['scenes'])} |",
        "",
        f"놓친 «예» 프레임 — {MISS_PERSON} {op['missed'][MISS_PERSON]} · "
        f"{MISS_ASPECT} {op['missed'][MISS_ASPECT]}",
        "",
        "## 훑기",
        "",
        "| conf | 종횡비 | 프레임 적중률 | 프레임 오경보율 | 장면 적중률 | 장면 오경보율 | |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for row in summary["sweep"]:
        f, s = row["frames"], row["scenes"]
        lines.append(
            f"| {row['conf']} | {row['aspect']} | {_rate(f['recall'])} "
            f"| {_rate(f['false_alarm'])} | {_rate(s['recall'])} | {_rate(s['false_alarm'])} "
            f"| {'◀ 운용' if row['operating'] else ''} |"
        )
    lines += [
        "",
        "## 장면별 (운용 설정 · 바닥값 최고 점수는 conf 를 내리기 전의 사람 점수)",
        "",
        "| 예/아니오 | 장면 | 프레임 | 사람 검출 | 후보 | 대표 점수 최대 | 종횡비 범위 "
        "| 바닥값 사람 최고 점수 |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: |",
    ]
    for s in summary["scenes"]:
        span = "-" if s["aspect_min"] is None else f"{s['aspect_min']}–{s['aspect_max']}"
        lines.append(
            f"| {s['label']} | {s['scene']} | {s['frames']} | {s['person_frames']} "
            f"| {s['candidate_frames']} | {_num(s['max_score'])} | {span} "
            f"| {_num(s['floor_best_score'])} |"
        )
    lines += ["", CONF_NOTE]
    return "\n".join(lines)


def _people_json(people: Sequence[Detection]) -> list[dict[str, Any]]:
    return [{"score": round(d.score, 3), "box": [round(v, 3) for v in d.box]} for d in people]


def _verdict_json(verdict: dict[str, Any]) -> dict[str, Any]:
    return {**verdict, "score": _round(verdict["score"]), "aspect": _round(verdict["aspect"])}


def _values(text: str) -> list[float]:
    try:
        values = sorted({float(part) for part in text.split(",") if part.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"쉼표로 나눈 수가 아니다: {text!r}") from exc
    if not values or not all(math.isfinite(v) for v in values):
        raise argparse.ArgumentTypeError(f"유한한 수가 하나 이상 있어야 한다: {text!r}")
    return values


def _finite(text: str) -> float:
    value = _values(text)
    if len(value) != 1:
        raise argparse.ArgumentTypeError(f"수 하나여야 한다: {text!r}")
    return value[0]


def main(
    argv: Sequence[str] | None = None,
    *,
    detector_factory: Callable[[Mapping[str, Any]], PersonDetector] = build_detector,
) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("root", type=Path, help="<root>/person_down/<yes|no>/*.jpg")
    parser.add_argument(
        "--conf-floor", type=_finite, default=0.1, help="한 번 추론할 conf 바닥값 (기본 0.1)"
    )
    parser.add_argument(
        "--confs", type=_values, default=[0.2, 0.3, 0.4, 0.5], help="훑을 conf (쉼표)"
    )
    parser.add_argument(
        "--aspects", type=_values, default=[1.2, 1.5, 2.0], help="훑을 종횡비 (쉼표)"
    )
    parser.add_argument("--out", type=Path, help="원자료 JSON 경로 (이미지는 담지 않는다)")
    args = parser.parse_args(argv)

    config = load_base_config()
    operating = (
        float(config["vision"]["coco"]["conf_threshold"]),
        float(config["vision"]["fallen"]["aspect_ratio"]),
    )
    if min(*args.confs, operating[0]) < args.conf_floor:
        parser.error(
            f"conf 는 바닥값 {args.conf_floor} 보다 낮을 수 없다 (훑기 {args.confs}"
            f" · 운용 {operating[0]})"
        )
    if min(args.aspects) <= 1.0:
        parser.error(f"종횡비는 1.0 보다 커야 한다 (가로 > 세로): {args.aspects}")

    try:
        samples = [s for s in collect(args.root) if s.key == KEY]
    except LayoutError as exc:
        print("폴더 구조가 틀렸다 — <root>/<질문키>/<yes|no>/*.jpg", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    if not samples:
        print(f"{args.root}/{KEY}: 사진이 없다", file=sys.stderr)
        return 2

    floored = copy.deepcopy(config)
    floored["vision"]["coco"]["conf_threshold"] = args.conf_floor
    detector = detector_factory(floored)
    try:
        records = infer(samples, detector, str(config["vision"]["coco"]["person_class"]))
        model = detector.model_summary()
    except ModelMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    grid = [(c, a) for c in args.confs for a in args.aspects]
    if operating not in grid:
        grid.append(operating)
    summary, verdicts = summarize(records, grid, operating, config)

    yes = sum(r["label"] == "yes" for r in records)
    scenes = len({(r["label"], r["scene"]) for r in records})
    print(
        f"# 쓰러짐 후보 벤치 — 모델 `{model['name']}` · sha256 {str(model['sha256'])[:12]} "
        f"· {model['provider']}\n\n"
        f"사진 예 {yes}장 · 아니오 {len(records) - yes}장 · 장면 {scenes}개 "
        f"· conf 바닥값 {args.conf_floor}\n"
    )
    print(render(summary, operating))
    if args.out is not None:
        rows = [
            {**r, "people": _people_json(r["people"]), "operating": _verdict_json(v)}
            for r, v in zip(records, verdicts, strict=True)
        ]
        args.out.write_text(
            json.dumps(
                {
                    "meta": {
                        "model": model,
                        "conf_floor": args.conf_floor,
                        "operating": {"conf": operating[0], "aspect": operating[1]},
                        "confs": args.confs,
                        "aspects": args.aspects,
                    },
                    "records": rows,
                    "summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n원자료 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
