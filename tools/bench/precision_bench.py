"""ONNX 정밀도(FP32·FP16·INT8)별 정확도·지연·크기·메모리를 비교한다 (포트폴리오 개선 5단계).

대상은 운용 2단 검출기 두 개다 — `models/coco.onnx`(YOLOX-S 사람 검출)와 `models/ppe.onnx`
(PPE 4클래스). 전·후처리와 판정은 운용 코드(`host/vision/detector.py`,
`tools/ppe/ppe_live_check.py`)를 그대로 쓰고, 합격 판정은 `tools/ppe/acceptance_judge.py` 를 그대로
부른다. 로봇·카메라에는 붙지 않는 오프라인 도구다.

    # ① 변환 — FP16 · INT8 동적 · INT8 정적 QDQ (보정은 평가와 겹치지 않는 프레임)
    python tools/bench/precision_bench.py convert --device mechdog-01 \\
        --frames <세션>/frames --session <세션>/session.json --out-dir models/precision
    # ② 정확도 — FP32 대비 검출 일치율 + 정밀도별 3.7.3 판정
    python tools/bench/precision_bench.py accuracy --device mechdog-01 \\
        --frames <세션>/frames --session <세션>/session.json --variants-dir models/precision \\
        --out models/precision/accuracy.json
    # ③ 지연·메모리 — (모델, 정밀도, 프로바이더)마다 새 프로세스
    python tools/bench/precision_bench.py latency --device mechdog-01 \\
        --frames <세션>/frames --session <세션>/session.json --variants-dir models/precision \\
        --providers dml,cpu --out models/precision/latency.json

⚠️ **변환 모델은 커밋하지 않는다.** `models/*` 는 깃이 무시한다. 기본 출력 폴더도 그 아래다.
⚠️ **프레임에는 얼굴이 있다.** 이 도구는 프레임을 읽기만 하고 결과에는 집계 수치와 프레임
번호만 남긴다.

보정·평가 분할 (누수 방지)
    보정 프레임은 **사람이 구간 버튼을 누르지 않은 동안**(`segment` 가 없는 이벤트) 중 실측에서
    사람이 잡힌 프레임에서만 고른다. 3.7.3 판정은 구간이 붙은 프레임만 세므로 판정과는 겹치지
    않고, 검출 일치율 집계에서는 보정 프레임을 뺀다. 고르는 법은 그 후보를 프레임 번호순으로
    세워 등간격으로 N 장 — 결정적이라 같은 세션이면 같은 프레임이 나온다.

판정 재구성
    실측 세션의 이벤트(시각 `t`·구간·방향)는 그대로 두고 프레임별 판정만 바꿔 끼운다. 위반 확정
    창(`ViolationWindow`)은 `t`(0.1초 단위로 반올림된 값)로 다시 돌리고 구간이 바뀔 때 비운다 —
    실측 때는 단조 시계였으므로 창 경계에서 ±50ms 차이가 날 수 있다.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host.common.config import load_config, repo_path  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.vision.coco_labels import COCO_CLASSES  # noqa: E402
from host.vision.detector import Detection, Detector  # noqa: E402
from tools.ppe.acceptance_judge import (  # noqa: E402
    coverage_gaps,
    judge_session,
    judged_specs,
    load_criteria,
    operating_window_problems,
)
from tools.ppe.episode_eval import percentile  # noqa: E402
from tools.ppe.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
    Judgement,
    ViolationWindow,
    crop_person,
    judge,
    load_acceptance_plan,
    verdict_tally,
)

#: 비교할 정밀도. 첫째가 기준이다.
VARIANTS = ("fp32", "fp16", "int8-dyn", "int8-qdq")
SECTIONS = ("coco", "ppe")
PROVIDERS = {"dml": "DmlExecutionProvider", "cpu": "CPUExecutionProvider"}
DEFAULT_OUT_DIR = ROOT / "models" / "precision"
#: 정적 양자화의 퍼채널 QDQ 는 옵셋 13 이상이 필요하다 — 운용 모델은 옵셋 11 이다.
QDQ_MIN_OPSET = 13
MB = 1024 * 1024


class DetectorLike(Protocol):
    def detect(self, image: np.ndarray) -> list[Detection]: ...


# ── 매칭 ────────────────────────────────────────────────────


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """두 xyxy 박스의 IoU. 면적이 0 이면 0."""
    inter_w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    inter_h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = inter_w * inter_h
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


@dataclass(slots=True)
class MatchResult:
    matched: int
    missed: int
    extra: int
    score_gaps: list[float]
    ious: list[float]


def match_detections(
    reference: Sequence[Detection], candidate: Sequence[Detection], iou_threshold: float = 0.5
) -> MatchResult:
    """기준(FP32) 검출과 후보 검출을 같은 라벨·IoU 기준 이상으로 1:1 매칭한다.

    기준을 점수 내림차순으로 돌며 아직 안 쓴 후보 중 IoU 가 가장 큰 것을 가져간다.
    놓친 검출 = 매칭 안 된 기준, 추가 검출 = 매칭 안 된 후보.
    """
    used: set[int] = set()
    gaps: list[float] = []
    ious: list[float] = []
    for ref in sorted(reference, key=lambda d: d.score, reverse=True):
        best, best_iou = -1, iou_threshold
        for index, cand in enumerate(candidate):
            if index in used or cand.label != ref.label:
                continue
            overlap = iou(ref.box, cand.box)
            if overlap >= best_iou:
                best, best_iou = index, overlap
        if best >= 0:
            used.add(best)
            gaps.append(abs(candidate[best].score - ref.score))
            ious.append(best_iou)
    matched = len(gaps)
    return MatchResult(matched, len(reference) - matched, len(candidate) - matched, gaps, ious)


@dataclass(slots=True)
class AgreementTally:
    """입력(프레임 또는 크롭) 여러 개의 매칭 결과를 모은다."""

    inputs: int = 0
    identical: int = 0
    reference: int = 0
    matched: int = 0
    missed: int = 0
    extra: int = 0
    gap_sum: float = 0.0
    gap_max: float = 0.0
    iou_sum: float = 0.0

    def add(self, result: MatchResult) -> None:
        self.inputs += 1
        self.identical += int(result.missed == 0 and result.extra == 0)
        self.reference += result.matched + result.missed
        self.matched += result.matched
        self.missed += result.missed
        self.extra += result.extra
        self.gap_sum += sum(result.score_gaps)
        self.gap_max = max([self.gap_max, *result.score_gaps])
        self.iou_sum += sum(result.ious)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inputs": self.inputs,
            "inputs_identical": self.identical,
            "reference": self.reference,
            "matched": self.matched,
            "missed": self.missed,
            "extra": self.extra,
            "match_rate": self.matched / self.reference if self.reference else None,
            "score_gap_mean": self.gap_sum / self.matched if self.matched else None,
            "score_gap_max": self.gap_max if self.matched else None,
            "iou_mean": self.iou_sum / self.matched if self.matched else None,
        }


# ── 통계 ────────────────────────────────────────────────────


def latency_stats(samples_ms: Sequence[float]) -> dict[str, float | int]:
    """워밍업을 뺀 표본 → n·평균·p50·p95·최소·최대 (선형 보간 백분위)."""
    if not samples_ms:
        raise ValueError("지연 표본이 없다")
    values = [float(v) for v in samples_ms]
    return {
        "n": len(values),
        "mean": sum(values) / len(values),
        "p50": float(percentile(values, 0.50) or 0.0),
        "p95": float(percentile(values, 0.95) or 0.0),
        "min": min(values),
        "max": max(values),
    }


def rss_bytes() -> int | None:
    """이 프로세스의 RSS. `psutil` 이 없으면 None (CI 등)."""
    try:
        import psutil
    except ImportError:
        return None
    return int(psutil.Process().memory_info().rss)


# ── 분할 ────────────────────────────────────────────────────


def split_calibration(events: Sequence[Mapping[str, Any]], count: int) -> list[str]:
    """보정 프레임 번호를 고른다 — 구간 밖 · 사람이 잡힌 프레임에서 등간격으로 `count` 장."""
    if count <= 0:
        raise ValueError(f"보정 프레임 수는 양수여야 함: {count}")
    pool = [str(e["tag"]) for e in events if not e.get("segment") and e.get("people")]
    if len(pool) < count:
        raise ValueError(f"보정 후보 {len(pool)}장 < 요청 {count}장")
    step = len(pool) / count
    return [pool[int(i * step)] for i in range(count)]


# ── 프레임 처리 ─────────────────────────────────────────────


@dataclass(slots=True)
class FrameEval:
    """한 프레임의 정밀도별 결과. 첫 정밀도가 기준이다."""

    persons: dict[str, list[Detection]]
    #: 기준 정밀도가 찾은 사람의 크롭에 정밀도별 PPE 를 돌린 결과 (크롭 실패면 None)
    reference_crops: dict[str, list[list[Detection] | None]]
    judgements: dict[str, list[Judgement]]


def evaluate_frame(
    image: np.ndarray,
    coco: Mapping[str, DetectorLike],
    ppe: Mapping[str, DetectorLike],
    *,
    person_label: str,
    pad: float,
    head_margin: int,
    use_clip: bool,
) -> FrameEval:
    """운용 `process()` 와 같은 규칙(사람 → 크롭 → PPE → 판정)을 정밀도마다 돈다.

    같은 정밀도·같은 크롭 좌표의 PPE 결과는 한 번만 계산한다 — 일치율용(기준 사람의 크롭)과
    판정용(자기 사람의 크롭)이 겹치는 경우가 대부분이다.
    """
    variants = list(coco)
    reference = variants[0]
    cache: dict[tuple[str, int, int, int, int], list[Detection]] = {}

    def ppe_on(variant: str, box: tuple[float, float, float, float]) -> list[Detection] | None:
        crop, (x, y) = crop_person(image, box, pad)
        if crop is None:
            return None
        key = (variant, x, y, crop.shape[1], crop.shape[0])
        if key not in cache:
            cache[key] = ppe[variant].detect(crop)
        return cache[key]

    persons = {v: [d for d in coco[v].detect(image) if d.label == person_label] for v in variants}
    reference_crops = {v: [ppe_on(v, p.box) for p in persons[reference]] for v in variants}
    judgements: dict[str, list[Judgement]] = {}
    for variant in variants:
        judged = []
        for person in persons[variant]:
            found = ppe_on(variant, person.box)
            judged.append(
                Judgement(STATE_UNKNOWN, "크롭 실패", ())
                if found is None
                else judge(found, head_margin, use_clip)
            )
        judgements[variant] = judged
    return FrameEval(persons, reference_crops, judgements)


def load_frame(folder: Path, tag: str) -> np.ndarray:
    import cv2

    path = folder / f"{tag}.jpg"
    if not path.is_file():
        raise FileNotFoundError(f"프레임 없음: {path}")
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"디코드 실패: {path}")
    return np.asarray(image)


@dataclass(slots=True)
class AccuracyRun:
    states: dict[str, dict[str, dict[str, list[str]]]] = field(default_factory=dict)
    coco: dict[str, AgreementTally] = field(default_factory=dict)
    ppe: dict[str, AgreementTally] = field(default_factory=dict)


def run_accuracy(
    frames: Path,
    tags: Sequence[str],
    coco: Mapping[str, DetectorLike],
    ppe: Mapping[str, DetectorLike],
    *,
    excluded: set[str],
    person_label: str,
    pad: float,
    head_margin: int,
    use_clip: bool,
    progress: Callable[[int, int], None] | None = None,
) -> AccuracyRun:
    """프레임마다 정밀도별 판정을 내고, 보정 프레임을 뺀 나머지로 FP32 대비 일치율을 센다."""
    variants = list(coco)
    reference = variants[0]
    run = AccuracyRun(
        states={v: {} for v in variants},
        coco={v: AgreementTally() for v in variants},
        ppe={v: AgreementTally() for v in variants},
    )
    for index, tag in enumerate(tags):
        image = load_frame(frames, tag)
        result = evaluate_frame(
            image,
            coco,
            ppe,
            person_label=person_label,
            pad=pad,
            head_margin=head_margin,
            use_clip=use_clip,
        )
        for variant in variants:
            judged = result.judgements[variant]
            run.states[variant][tag] = {
                "states": [j.state for j in judged],
                "reasons": [j.reason for j in judged if j.reason],
                "labels": sorted({d.label for j in judged for d in j.detections}),
            }
            if tag in excluded:
                continue
            run.coco[variant].add(
                match_detections(result.persons[reference], result.persons[variant])
            )
            for ref_crop, cand_crop in zip(
                result.reference_crops[reference], result.reference_crops[variant], strict=True
            ):
                if ref_crop is not None and cand_crop is not None:
                    run.ppe[variant].add(match_detections(ref_crop, cand_crop))
        if progress is not None:
            progress(index + 1, len(tags))
    return run


def state_agreement(
    a: Mapping[str, Mapping[str, Any]], b: Mapping[str, Mapping[str, Any]], tags: Iterable[str]
) -> dict[str, int]:
    """프레임별 판정 목록(사람 순서 무시)이 같은 프레임 수."""
    frames = identical = 0
    for tag in tags:
        frames += 1
        identical += int(sorted(a[tag]["states"]) == sorted(b[tag]["states"]))
    return {"frames": frames, "identical": identical}


# ── 세션 재구성·판정 ────────────────────────────────────────


def replay_session(
    base: Mapping[str, Any],
    frame_states: Mapping[str, Mapping[str, Any]],
    *,
    window_ms: int,
    hits_required: int,
    orientations: Sequence[str],
) -> dict[str, Any]:
    """실측 세션의 시각·구간·방향에 새 판정을 끼워 `ppe_live_check` 형식 세션을 다시 만든다."""
    window = ViolationWindow(window_ms, hits_required)
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    segments: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    previous: object = object()
    base_segments = base.get("segments", {})
    for base_event in base["events"]:
        tag = str(base_event["tag"])
        result = frame_states[tag]
        segment = base_event.get("segment")
        if segment != previous:
            window.reset()  # 실측 도구는 구간 버튼을 누를 때 창을 비운다
            previous = segment
        states = list(result["states"])
        newly = window.observe(STATE_VIOLATION in states, round(float(base_event["t"]) * 1000))
        if not states:
            counts["사람없음"] += 1
        counts.update(states)
        reasons.update(result.get("reasons", []))
        if segment:
            slot = segments.setdefault(
                segment,
                {
                    "verdicts": {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0},
                    "orientations": {
                        name: {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0}
                        for name in orientations
                    },
                    "frames": 0,
                    "seconds": base_segments.get(segment, {}).get("seconds", 0.0),
                },
            )
            slot["frames"] += 1
            facing = base_event.get("orientation")
            for state in states:
                slot["verdicts"][state] += 1
                if facing in slot["orientations"]:
                    slot["orientations"][facing][state] += 1
        events.append(
            {
                **base_event,
                "people": len(states),
                "states": states,
                "reasons": list(result.get("reasons", [])),
                "labels": list(result.get("labels", [])),
                "confirmed": bool(newly),
                "hits": window.hits,
            }
        )
    session = {key: copy.deepcopy(value) for key, value in base.items() if key != "events"}
    session.update(
        {
            "counts": dict(counts),
            "reasons": dict(reasons),
            "segments": segments,
            "events": events,
        }
    )
    return session


def segment_rates(session: Mapping[str, Any], specs: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """`ppe_live_check.segment_section` 과 같은 식의 판정 가능률·실효 성공률."""
    total_right = total = covered = covered_determinate = 0
    per_segment: dict[str, dict[str, int]] = {}
    for spec in specs:
        stats = session.get("segments", {}).get(spec["key"])
        if not stats or not stats.get("frames"):
            continue
        tally = verdict_tally(spec["expected"], stats["verdicts"])
        per_segment[spec["key"]] = tally
        total_right += tally["right"]
        total += tally["count"]
        covered += tally["coverage_count"]
        covered_determinate += tally["coverage_determinate"]
    return {
        "decidable_rate": covered_determinate / covered if covered else None,
        "effective_rate": total_right / total if total else None,
        "segments": per_segment,
    }


def acceptance(session: Mapping[str, Any], plan: Path, scenario: str) -> dict[str, Any]:
    """`acceptance_judge` 의 C1~C3·커버리지·운용 창 검사를 그대로 부른다."""
    specs, step_s, orientations = load_acceptance_plan(plan, scenario)
    criteria = load_criteria(plan, scenario)
    data = dict(session)
    results = judge_session(data, specs, criteria, step_s)
    gaps = coverage_gaps(data, judged_specs(specs, criteria), orientations, step_s)
    problems = operating_window_problems(data, criteria)
    return {
        "criteria": [
            {"key": c.key, "name": c.name, "passed": c.passed, "detail": c.detail} for c in results
        ],
        "coverage_gaps": len(gaps),
        "window_problems": problems,
        "passed": all(c.passed for c in results) and not gaps and not problems,
    }


# ── 경로·설정 ───────────────────────────────────────────────


def variant_path(fp32_path: Path, variant: str, out_dir: Path) -> Path:
    """정밀도별 모델 경로. FP32 는 원본 그대로, 나머지는 `<out>/<이름>.<정밀도>.onnx`."""
    if variant not in VARIANTS:
        raise ValueError(f"모르는 정밀도: {variant!r}. 가능한 값={VARIANTS}")
    if variant == "fp32":
        return fp32_path
    return out_dir / f"{fp32_path.stem}.{variant}.onnx"


def with_model(
    config: Mapping[str, Any], section: str, model: Path, provider: str
) -> dict[str, Any]:
    """설정 사본에서 한 섹션의 모델 경로와 프로바이더만 바꾼다."""
    out = copy.deepcopy(dict(config))
    out["vision"][section]["model_path"] = str(model)
    out["vision"]["providers"] = [provider]
    return out


def provider_name(alias: str) -> str:
    try:
        return PROVIDERS[alias.strip().lower()]
    except KeyError:
        raise ValueError(f"모르는 프로바이더: {alias!r}. 가능한 값={sorted(PROVIDERS)}") from None


def labels_for(config: Mapping[str, Any], section: str) -> list[str]:
    return list(COCO_CLASSES) if section == "coco" else list(config["vision"]["ppe"]["classes"])


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_session(path: Path, provider: str) -> Any:
    """지정한 프로바이더 하나로 세션을 만든다 (DirectML 은 지원 안 되는 노드만 CPU 로 간다)."""
    import onnxruntime as ort

    return ort.InferenceSession(str(path), providers=[provider])


def build_detector(
    config: Mapping[str, Any], section: str, model: Path, provider: str
) -> tuple[Detector, Any]:
    session = make_session(model, provider)
    detector = Detector(
        with_model(config, section, model, provider),
        section=section,
        labels=labels_for(config, section),
        session_factory=lambda _path, _preferred: session,
    )
    detector.open()
    return detector, session


# ── 변환 ────────────────────────────────────────────────────


def head_tail_nodes(model: Any) -> list[str]:
    """그래프 출력에서 거꾸로 Conv 를 만날 때까지의 노드 이름 (정렬).

    YOLOX 출력은 박스 회귀(수 단위)와 sigmoid 확률(0~1)을 한 텐서로 이어 붙인다. 이 꼬리까지
    UINT8 하나의 눈금으로 양자화하면 objectness 가 몇 단계(2026-10-06 PPE 5개 값)로 뭉개진다.
    그래서 Conv 는 INT8 로 두고 꼬리만 FP32 로 남긴다. 해상도는 돌아오지만(393개 값) PPE 의
    objectness 최댓값이 낮아지는 문제는 앞단 양자화 오차라 이것으로 풀리지 않는다.
    """
    producers = {out: node for node in model.graph.node for out in node.output}
    names: set[str] = set()
    stack = [output.name for output in model.graph.output]
    seen: set[str] = set()
    while stack:
        tensor = stack.pop()
        node = producers.get(tensor)
        if node is None or tensor in seen or node.op_type == "Conv":
            continue
        seen.add(tensor)
        names.add(node.name)
        stack.extend(node.input)
    return sorted(names)


def convert_model(src: Path, out_dir: Path, calibration: Sequence[np.ndarray]) -> dict[str, Path]:
    """FP32 모델 → FP16 · INT8 동적 · INT8 정적 QDQ. 입출력은 FP32 로 둔다(어댑터 그대로)."""
    import onnx
    from onnx import version_converter
    from onnxruntime.quantization import (
        CalibrationDataReader,
        QuantFormat,
        QuantType,
        quantize_dynamic,
        quantize_static,
    )
    from onnxruntime.quantization.shape_inference import quant_pre_process
    from onnxruntime.transformers.float16 import convert_float_to_float16

    out_dir.mkdir(parents=True, exist_ok=True)
    model = onnx.load(str(src))
    input_name = model.graph.input[0].name
    written: dict[str, Path] = {}

    fp16 = variant_path(src, "fp16", out_dir)
    onnx.save(convert_float_to_float16(model, keep_io_types=True), str(fp16))
    written["fp16"] = fp16

    dynamic = variant_path(src, "int8-dyn", out_dir)
    quantize_dynamic(str(src), str(dynamic), weight_type=QuantType.QUInt8)
    written["int8-dyn"] = dynamic

    class Reader(CalibrationDataReader):  # type: ignore[misc]
        def __init__(self) -> None:
            self._items = iter(calibration)

        def get_next(self) -> dict[str, np.ndarray] | None:
            item = next(self._items, None)
            return None if item is None else {input_name: item}

    static = variant_path(src, "int8-qdq", out_dir)
    with tempfile.TemporaryDirectory() as work:
        opset = max(
            (o.version for o in model.opset_import if o.domain in ("", "ai.onnx")), default=0
        )
        upgraded = Path(work) / "opset.onnx"
        onnx.save(
            version_converter.convert_version(model, QDQ_MIN_OPSET)
            if opset < QDQ_MIN_OPSET
            else model,
            str(upgraded),
        )
        prepared = Path(work) / "prepared.onnx"
        quant_pre_process(str(upgraded), str(prepared))
        quantize_static(
            str(prepared),
            str(static),
            Reader(),
            quant_format=QuantFormat.QDQ,
            activation_type=QuantType.QUInt8,
            weight_type=QuantType.QInt8,
            per_channel=True,
            nodes_to_exclude=head_tail_nodes(onnx.load(str(prepared))),
            extra_options={"CalibMaxIntermediateOutputs": 16},
        )
    written["int8-qdq"] = static
    return written


# ── 지연·메모리 ─────────────────────────────────────────────


def latency_one(
    model: Path,
    provider: str,
    images: Sequence[np.ndarray],
    *,
    input_size: int,
    labels: Sequence[str],
    warmup: int,
    runs: int,
    conf: float = 0.5,
    iou_threshold: float = 0.45,
) -> dict[str, Any]:
    """한 모델·한 프로바이더의 지연과 RSS 증가분. 새 프로세스에서 부르는 것을 전제로 한다.

    `pipeline_ms` 는 운용 `Detector.detect` 그대로(전처리 → 추론 → 디코드 → 억제), `run_ms` 는
    `session.run` 만이다. 둘 다 워밍업 `warmup` 회를 뺀다. RSS 는 CPU 메모리만 보며 DirectML 의
    GPU 메모리는 들어가지 않는다.
    """
    config = {
        "vision": {
            "providers": [provider],
            "m": {
                "model_family": "yolox",
                "input_size": input_size,
                "conf_threshold": conf,
                "iou_threshold": iou_threshold,
                "model_path": str(model),
            },
        }
    }
    before = rss_bytes()
    session = make_session(model, provider)
    detector = Detector(
        config, section="m", labels=labels, session_factory=lambda _p, _pref: session
    )
    detector.open()  # 운용과 같은 1회 데우기
    loaded = rss_bytes()
    input_name = session.get_inputs()[0].name
    tensors = [detector.adapter.preprocess(image).tensor for image in images]

    pipeline: list[float] = []
    for i in range(warmup + runs):
        image = images[i % len(images)]
        started = time.perf_counter()
        detector.detect(image)
        if i >= warmup:
            pipeline.append((time.perf_counter() - started) * 1000)
    run_only: list[float] = []
    for i in range(warmup + runs):
        tensor = tensors[i % len(tensors)]
        started = time.perf_counter()
        session.run(None, {input_name: tensor})
        if i >= warmup:
            run_only.append((time.perf_counter() - started) * 1000)
    after = rss_bytes()

    def delta(a: int | None, b: int | None) -> float | None:
        return (b - a) / MB if a is not None and b is not None else None

    return {
        "providers": list(session.get_providers()),
        "file_mb": model.stat().st_size / MB,
        "rss_session_mb": delta(before, loaded),
        "rss_after_runs_mb": delta(before, after),
        "pipeline_ms": latency_stats(pipeline),
        "run_ms": latency_stats(run_only),
    }


# ── CLI ─────────────────────────────────────────────────────


def _model_paths(config: Mapping[str, Any], variants_dir: Path) -> dict[str, dict[str, Path]]:
    out: dict[str, dict[str, Path]] = {}
    for section in SECTIONS:
        fp32 = repo_path(config["vision"][section]["model_path"])
        out[section] = {v: variant_path(fp32, v, variants_dir) for v in VARIANTS}
    return out


def _load_session(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError(f"ppe_live_check 세션이 아니다: {path}")
    return data


def cmd_convert(args: argparse.Namespace) -> int:
    config = load_config(args.device)
    session = _load_session(args.session)
    calib = split_calibration(session["events"], args.calib_frames)
    paths = _model_paths(config, args.out_dir)
    provider = provider_name(args.provider)
    coco, _ = build_detector(config, "coco", paths["coco"]["fp32"], provider)
    ppe_adapter = Detector(
        config, section="ppe", labels=labels_for(config, "ppe"), session_factory=lambda *_: None
    ).adapter
    person = config["vision"]["coco"]["person_class"]
    coco_tensors: list[np.ndarray] = []
    ppe_tensors: list[np.ndarray] = []
    for tag in calib:
        image = load_frame(args.frames, tag)
        coco_tensors.append(coco.adapter.preprocess(image).tensor)
        for found in coco.detect(image):
            if found.label != person:
                continue
            crop, _ = crop_person(image, found.box, args.crop_pad)
            if crop is not None:
                ppe_tensors.append(ppe_adapter.preprocess(crop).tensor)
    print(
        f"보정 프레임 {len(calib)}장 · COCO 입력 {len(coco_tensors)} · PPE 크롭 {len(ppe_tensors)}"
    )
    if not ppe_tensors:
        print("보정 프레임에서 사람 크롭이 하나도 안 나왔다", file=sys.stderr)
        return 2
    manifest: dict[str, Any] = {
        "rule": "segment 없는(구간 버튼 밖) · 실측 people>0 이벤트를 프레임 번호순으로 세워 등간격 선택",
        "session": str(args.session),
        "calibration_tags": calib,
        "coco_inputs": len(coco_tensors),
        "ppe_crops": len(ppe_tensors),
        "crop_pad": args.crop_pad,
        "models": {},
    }
    for section, tensors in (("coco", coco_tensors), ("ppe", ppe_tensors)):
        started = time.perf_counter()
        written = convert_model(paths[section]["fp32"], args.out_dir, tensors)
        print(f"{section} 변환 {time.perf_counter() - started:.1f}초")
        manifest["models"][section] = {
            v: {"path": str(p), "mb": p.stat().st_size / MB, "sha256": sha256_file(p)}
            for v, p in {"fp32": paths[section]["fp32"], **written}.items()
        }
    write_json(args.out_dir / "calibration.json", manifest)
    print(f"기록 {args.out_dir / 'calibration.json'}")
    return 0


def cmd_accuracy(args: argparse.Namespace) -> int:
    config = load_config(args.device)
    base = _load_session(args.session)
    manifest = json.loads((args.variants_dir / "calibration.json").read_text(encoding="utf-8"))
    excluded = set(manifest["calibration_tags"])
    paths = _model_paths(config, args.variants_dir)
    variants = [v for v in VARIANTS if v in args.variants.split(",")]
    if not variants or variants[0] != "fp32":
        raise SystemExit("--variants 는 fp32 로 시작해야 한다 (일치율 기준)")
    provider = provider_name(args.provider)
    coco: dict[str, DetectorLike] = {}
    ppe: dict[str, DetectorLike] = {}
    used: dict[str, dict[str, list[str]]] = {}
    for variant in variants:
        coco[variant], cs = build_detector(config, "coco", paths["coco"][variant], provider)
        ppe[variant], ps = build_detector(config, "ppe", paths["ppe"][variant], provider)
        used[variant] = {"coco": cs.get_providers(), "ppe": ps.get_providers()}
    tags = [str(e["tag"]) for e in base["events"]][:: args.stride]
    ppe_cfg = config["vision"]["ppe"]
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        if done % 500 == 0 or done == total:
            print(f"  {done}/{total} · {time.monotonic() - started:.0f}초", flush=True)

    run = run_accuracy(
        args.frames,
        tags,
        coco,
        ppe,
        excluded=excluded,
        person_label=config["vision"]["coco"]["person_class"],
        pad=args.crop_pad,
        head_margin=int(ppe_cfg["head_margin_px"]),
        use_clip=bool(ppe_cfg["require_head_visible"]),
        progress=progress,
    )
    window_ms = int(ppe_cfg.get("violation_window_ms", 1500))
    hits = int(ppe_cfg.get("violation_hits_required", 3))
    specs, _, orientations = load_acceptance_plan(args.plan, base.get("scenario") or "xiao")
    eval_tags = [t for t in tags if t not in excluded]
    live_states = {str(e["tag"]): e for e in base["events"]}
    report: dict[str, Any] = {
        "provider": provider,
        "stride": args.stride,
        "frames": len(tags),
        "agreement_frames": len(eval_tags),
        "calibration_excluded": len(excluded & set(tags)),
        "live": {
            "rates": segment_rates(base, specs),
            "acceptance": acceptance(base, args.plan, base.get("scenario") or "xiao"),
            "alarms": sum(1 for e in base["events"] if e.get("confirmed")),
        },
        "variants": {},
    }
    for variant in variants:
        entry: dict[str, Any] = {
            "providers": used[variant],
            "coco_person_agreement": run.coco[variant].as_dict(),
            "ppe_agreement": run.ppe[variant].as_dict(),
            "states_vs_fp32": state_agreement(run.states["fp32"], run.states[variant], eval_tags),
            "states_vs_live": state_agreement(live_states, run.states[variant], tags),
        }
        if args.stride == 1:
            session = replay_session(
                {
                    **base,
                    "settings": {
                        **base.get("settings", {}),
                        "providers": [provider],
                        "model_sha256": manifest["models"]["ppe"][variant]["sha256"],
                        "window_ms": window_ms,
                        "hits_required": hits,
                        "overridden": False,
                    },
                },
                run.states[variant],
                window_ms=window_ms,
                hits_required=hits,
                orientations=orientations,
            )
            entry["counts"] = session["counts"]
            entry["reasons"] = session["reasons"]
            entry["alarms"] = sum(1 for e in session["events"] if e["confirmed"])
            entry["rates"] = segment_rates(session, specs)
            entry["acceptance"] = acceptance(session, args.plan, session.get("scenario") or "xiao")
            if args.sessions_dir:
                write_json(args.sessions_dir / f"session.{variant}.json", session)
        report["variants"][variant] = entry
    write_json(args.out, report)
    print(f"기록 {args.out} · {time.monotonic() - started:.0f}초")
    return 0


def cmd_latency(args: argparse.Namespace) -> int:
    config = load_config(args.device)
    base = _load_session(args.session)
    manifest = json.loads((args.variants_dir / "calibration.json").read_text(encoding="utf-8"))
    excluded = set(manifest["calibration_tags"])
    pool = [str(e["tag"]) for e in base["events"] if e.get("people") and e["tag"] not in excluded]
    step = max(1, len(pool) // args.images)
    tags = pool[::step][: args.images]
    paths = _model_paths(config, args.variants_dir)
    results: list[dict[str, Any]] = []
    for alias in args.providers.split(","):
        provider = provider_name(alias)
        for section in SECTIONS:
            for variant in [v for v in VARIANTS if v in args.variants.split(",")]:
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "latency-one",
                    "--model",
                    str(paths[section][variant]),
                    "--provider",
                    alias,
                    "--frames",
                    str(args.frames),
                    "--tags",
                    ",".join(tags),
                    "--input-size",
                    str(config["vision"][section]["input_size"]),
                    "--labels",
                    ",".join(labels_for(config, section)),
                    "--conf",
                    str(config["vision"][section]["conf_threshold"]),
                    "--warmup",
                    str(args.warmup),
                    "--runs",
                    str(args.runs),
                ]
                done = subprocess.run(command, capture_output=True, text=True, check=False)
                row: dict[str, Any] = {"model": section, "variant": variant, "provider": provider}
                lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
                if done.returncode != 0 or not lines:
                    row["error"] = (done.stderr.strip().splitlines() or ["(출력 없음)"])[-1]
                else:
                    row.update(json.loads(lines[-1]))
                print(json.dumps(row, ensure_ascii=False), flush=True)
                results.append(row)
    write_json(
        args.out,
        {
            "images": len(tags),
            "warmup": args.warmup,
            "runs": args.runs,
            "preliminary": "다른 작업과 GPU 를 함께 쓸 수 있는 상태에서 잰 예비 수치",
            "results": results,
        },
    )
    print(f"기록 {args.out}")
    return 0


def cmd_latency_one(args: argparse.Namespace) -> int:
    images = [load_frame(args.frames, tag) for tag in args.tags.split(",")]
    out = latency_one(
        args.model,
        provider_name(args.provider),
        images,
        input_size=args.input_size,
        labels=args.labels.split(","),
        warmup=args.warmup,
        runs=args.runs,
        conf=args.conf,
    )
    print(json.dumps(out, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ONNX FP32·FP16·INT8 정확도·지연 비교")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--device", required=True, help="개체 프로파일 (전·후처리 설정)")
        p.add_argument("--frames", type=Path, required=True, help="세션 프레임 폴더 (jpg)")
        p.add_argument("--session", type=Path, required=True, help="ppe_live_check session.json")

    convert = sub.add_parser("convert", help="FP16·INT8 동적·INT8 정적 QDQ 모델을 만든다")
    common(convert)
    convert.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    convert.add_argument("--calib-frames", type=int, default=100)
    convert.add_argument("--crop-pad", type=float, default=0.08)
    convert.add_argument("--provider", default="dml", help="보정용 사람 검출 프로바이더")
    convert.set_defaults(func=cmd_convert)

    accuracy = sub.add_parser("accuracy", help="FP32 대비 일치율과 정밀도별 3.7.3 판정")
    common(accuracy)
    accuracy.add_argument("--variants-dir", type=Path, default=DEFAULT_OUT_DIR)
    accuracy.add_argument("--variants", default=",".join(VARIANTS))
    accuracy.add_argument("--provider", default="dml")
    accuracy.add_argument("--crop-pad", type=float, default=0.08)
    accuracy.add_argument(
        "--stride", type=int, default=1, help="N 장마다 1장. 1 이 아니면 3.7.3 판정은 건너뛴다"
    )
    accuracy.add_argument("--plan", type=Path, default=DEFAULT_ACCEPTANCE_PLAN)
    accuracy.add_argument("--out", type=Path, required=True)
    accuracy.add_argument(
        "--sessions-dir", type=Path, help="정밀도별 재구성 세션을 쓸 폴더 (깃 무시 경로)"
    )
    accuracy.set_defaults(func=cmd_accuracy)

    latency = sub.add_parser("latency", help="정밀도·프로바이더별 지연과 RSS")
    common(latency)
    latency.add_argument("--variants-dir", type=Path, default=DEFAULT_OUT_DIR)
    latency.add_argument("--variants", default=",".join(VARIANTS))
    latency.add_argument("--providers", default="dml,cpu")
    latency.add_argument("--images", type=int, default=50, help="돌려 쓸 실제 프레임 수")
    latency.add_argument("--warmup", type=int, default=20)
    latency.add_argument("--runs", type=int, default=200)
    latency.add_argument("--out", type=Path, required=True)
    latency.set_defaults(func=cmd_latency)

    one = sub.add_parser("latency-one", help="(내부) 한 모델·한 프로바이더를 새 프로세스에서 잰다")
    one.add_argument("--model", type=Path, required=True)
    one.add_argument("--provider", required=True)
    one.add_argument("--frames", type=Path, required=True)
    one.add_argument("--tags", required=True)
    one.add_argument("--input-size", type=int, required=True)
    one.add_argument("--labels", required=True)
    one.add_argument("--conf", type=float, default=0.5)
    one.add_argument("--warmup", type=int, default=20)
    one.add_argument("--runs", type=int, default=200)
    one.set_defaults(func=cmd_latency_one)
    return parser


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
