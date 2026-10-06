"""ONNX 정밀도 비교 도구(`tools/bench/precision_bench.py`) 검증.

CI 에는 모델 가중치가 없다. 매칭·통계·분할·세션 재구성은 가짜 검출로 보고, 변환·지연은
`onnx` 가 있을 때만 작은 합성 모델로 본다(CI 는 `onnx` 패키지가 없어 건너뛴다).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.vision.detector import Detection, YoloxAdapter  # noqa: E402
from tools.bench import precision_bench as pb  # noqa: E402
from tools.ppe.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
)


def det(label: str, score: float, box: tuple[float, float, float, float]) -> Detection:
    return Detection(label=label, score=score, box=box)


# ── 매칭 ────────────────────────────────────────────────────


def test_iou_overlap_disjoint_and_degenerate() -> None:
    assert pb.iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert pb.iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)
    assert pb.iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert pb.iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_match_counts_missed_extra_and_score_gap() -> None:
    ref = [det("person", 0.9, (0, 0, 10, 10)), det("person", 0.8, (100, 100, 110, 110))]
    cand = [det("person", 0.85, (0, 0, 10, 11)), det("person", 0.7, (50, 50, 60, 60))]
    result = pb.match_detections(ref, cand, iou_threshold=0.5)
    assert result.matched == 1
    assert result.missed == 1
    assert result.extra == 1
    assert result.score_gaps == pytest.approx([0.05])
    assert result.ious == pytest.approx([100 / 110])


def test_match_requires_same_label_and_threshold() -> None:
    ref = [det("helmet", 0.9, (0, 0, 10, 10))]
    assert pb.match_detections(ref, [det("no_helmet", 0.9, (0, 0, 10, 10))]).matched == 0
    # IoU 1/3 < 0.5
    assert pb.match_detections(ref, [det("helmet", 0.9, (5, 0, 15, 10))]).matched == 0


def test_match_is_one_to_one_highest_reference_first() -> None:
    ref = [det("person", 0.6, (0, 0, 10, 10)), det("person", 0.9, (0, 0, 10, 10))]
    cand = [det("person", 0.88, (0, 0, 10, 10))]
    result = pb.match_detections(ref, cand)
    assert (result.matched, result.missed, result.extra) == (1, 1, 0)
    # 점수가 높은 기준(0.9)이 먼저 가져간다.
    assert result.score_gaps == pytest.approx([0.02])


def test_agreement_tally_aggregates_rates() -> None:
    tally = pb.AgreementTally()
    tally.add(pb.match_detections([det("a", 0.9, (0, 0, 10, 10))], [det("a", 0.8, (0, 0, 10, 10))]))
    tally.add(pb.match_detections([det("a", 0.9, (0, 0, 10, 10))], []))
    tally.add(pb.match_detections([], [det("a", 0.9, (0, 0, 10, 10))]))
    tally.add(pb.match_detections([], []))
    out = tally.as_dict()
    assert out["inputs"] == 4
    assert out["reference"] == 2
    assert out["matched"] == 1
    assert out["missed"] == 1
    assert out["extra"] == 1
    assert out["match_rate"] == pytest.approx(0.5)
    # 놓침·추가가 없는 입력 — 점수만 다른 첫째와 둘 다 빈 넷째
    assert out["inputs_identical"] == 2
    assert out["score_gap_mean"] == pytest.approx(0.1)
    assert out["score_gap_max"] == pytest.approx(0.1)
    assert out["iou_mean"] == pytest.approx(1.0)


def test_agreement_tally_empty_has_no_rates() -> None:
    out = pb.AgreementTally().as_dict()
    assert out["match_rate"] is None
    assert out["score_gap_mean"] is None
    assert out["iou_mean"] is None


# ── 통계 ────────────────────────────────────────────────────


def test_latency_stats_reports_n_mean_and_percentiles() -> None:
    stats = pb.latency_stats([float(v) for v in range(1, 101)])
    assert stats["n"] == 100
    assert stats["mean"] == pytest.approx(50.5)
    assert stats["p50"] == pytest.approx(50.5)
    assert stats["p95"] == pytest.approx(95.05)
    assert stats["min"] == 1.0
    assert stats["max"] == 100.0


def test_latency_stats_rejects_empty() -> None:
    with pytest.raises(ValueError):
        pb.latency_stats([])


# ── 보정·평가 분할 ──────────────────────────────────────────


def _events() -> list[dict]:
    events = []
    for i in range(1, 41):
        segment = "standing-all" if 11 <= i <= 30 else None
        people = 0 if i % 5 == 0 else 1
        events.append({"tag": f"{i:05d}", "segment": segment, "people": people})
    return events


def test_calibration_split_uses_only_unlabelled_frames_with_people() -> None:
    events = _events()
    calib = pb.split_calibration(events, 6)
    assert len(calib) == 6
    assert len(set(calib)) == 6
    by_tag = {e["tag"]: e for e in events}
    for tag in calib:
        assert by_tag[tag]["segment"] is None
        assert by_tag[tag]["people"] > 0
    # 결정적이다 — 같은 입력이면 같은 프레임.
    assert calib == pb.split_calibration(events, 6)


def test_calibration_split_rejects_short_pool() -> None:
    with pytest.raises(ValueError):
        pb.split_calibration(_events(), 100)
    with pytest.raises(ValueError):
        pb.split_calibration(_events(), 0)


# ── 세션 재구성·판정 ────────────────────────────────────────


def _base_session() -> dict:
    events = []
    t = 0.0
    for i in range(1, 13):
        segment = "standing-nohelmet" if i <= 6 else ("standing-all" if i <= 10 else None)
        events.append(
            {
                "t": round(t, 1),
                "tag": f"{i:05d}",
                "people": 1,
                "states": [STATE_UNKNOWN],
                "reasons": ["머리 미검출"],
                "labels": [],
                "confirmed": False,
                "hits": 0,
                "segment": segment,
                "expected": None,
                "orientation": "정면" if segment else None,
            }
        )
        t += 0.1
    return {
        "schema_version": 1,
        "device": "mechdog-01",
        "scenario": "xiao",
        "frames": len(events),
        "settings": {
            "providers": ["DmlExecutionProvider"],
            "model_sha256": "live",
            "window_ms": 1500,
            "hits_required": 3,
            "overridden": False,
        },
        "segments": {
            "standing-nohelmet": {"seconds": 0.6},
            "standing-all": {"seconds": 0.4},
        },
        "events": events,
    }


def _states(pattern: dict[str, list[str]]) -> dict[str, dict]:
    return {
        tag: {"states": states, "reasons": [], "labels": ["helmet"] if states else []}
        for tag, states in pattern.items()
    }


def test_replay_recomputes_window_confirmation_and_segment_tallies() -> None:
    base = _base_session()
    states = {e["tag"]: [STATE_VIOLATION] for e in base["events"][:6]}
    states.update({e["tag"]: [STATE_OK] for e in base["events"][6:10]})
    states.update({e["tag"]: [] for e in base["events"][10:]})
    session = pb.replay_session(
        base, _states(states), window_ms=1500, hits_required=3, orientations=["정면"]
    )
    events = session["events"]
    assert [e["confirmed"] for e in events[:6]] == [False, False, True, False, False, False]
    assert [e["hits"] for e in events[:3]] == [1, 2, 3]
    # 구간이 바뀌면 창을 비운다 — 적합 구간 첫 프레임의 히트는 0.
    assert events[6]["hits"] == 0
    assert events[10]["people"] == 0
    seg = session["segments"]["standing-nohelmet"]
    assert seg["frames"] == 6
    assert seg["verdicts"][STATE_VIOLATION] == 6
    assert seg["orientations"]["정면"][STATE_VIOLATION] == 6
    assert seg["seconds"] == 0.6
    assert session["segments"]["standing-all"]["verdicts"][STATE_OK] == 4
    assert session["counts"] == {STATE_VIOLATION: 6, STATE_OK: 4, "사람없음": 2}
    # 원본 세션은 건드리지 않는다.
    assert base["events"][0]["states"] == [STATE_UNKNOWN]


def test_replay_reasons_and_missing_frame_rejected() -> None:
    base = _base_session()
    states = {
        e["tag"]: {"states": [STATE_UNKNOWN], "reasons": ["머리 미검출"], "labels": []}
        for e in base["events"]
    }
    session = pb.replay_session(
        base, states, window_ms=1500, hits_required=3, orientations=["정면"]
    )
    assert session["reasons"] == {"머리 미검출": 12}
    del states["00003"]
    with pytest.raises(KeyError):
        pb.replay_session(base, states, window_ms=1500, hits_required=3, orientations=["정면"])


def test_replay_of_live_states_reproduces_live_confirmations() -> None:
    """같은 판정을 다시 흘리면 원본의 확정 시점이 그대로 나와야 한다(재구성 자체의 검증)."""
    base = _base_session()
    for e in base["events"][:6]:
        e["states"] = [STATE_VIOLATION]
    hits = [1, 2, 3, 4, 5, 6]
    for e, h in zip(base["events"][:6], hits, strict=True):
        e["hits"] = h
        e["confirmed"] = h == 3
    states = {
        e["tag"]: {"states": e["states"], "reasons": e["reasons"], "labels": e["labels"]}
        for e in base["events"]
    }
    session = pb.replay_session(
        base, states, window_ms=1500, hits_required=3, orientations=["정면"]
    )
    assert [e["confirmed"] for e in session["events"]] == [e["confirmed"] for e in base["events"]]


def test_segment_rates_follow_live_check_formula() -> None:
    session = {
        "segments": {
            "standing-all": {
                "frames": 10,
                "verdicts": {STATE_OK: 8, STATE_VIOLATION: 0, STATE_UNKNOWN: 2},
            },
            "standing-none": {
                "frames": 10,
                "verdicts": {STATE_OK: 1, STATE_VIOLATION: 6, STATE_UNKNOWN: 3},
            },
        }
    }
    specs = [
        {"key": "standing-all", "expected": STATE_OK},
        {"key": "standing-none", "expected": STATE_VIOLATION},
        {"key": "sit", "expected": STATE_OK},
    ]
    rates = pb.segment_rates(session, specs)
    assert rates["decidable_rate"] == pytest.approx(15 / 20)
    assert rates["effective_rate"] == pytest.approx(14 / 20)
    assert set(rates["segments"]) == {"standing-all", "standing-none"}


def test_acceptance_runs_judge_on_plan() -> None:
    base = _base_session()
    states = {e["tag"]: {"states": [STATE_OK], "reasons": [], "labels": []} for e in base["events"]}
    session = pb.replay_session(
        base, states, window_ms=1500, hits_required=3, orientations=["정면"]
    )
    result = pb.acceptance(session, DEFAULT_ACCEPTANCE_PLAN, "xiao")
    keys = [c["key"] for c in result["criteria"]]
    assert keys == ["C1", "C2", "C3"]
    assert result["passed"] is False  # 판정 구간을 다 찍지 않았다
    assert result["coverage_gaps"] > 0


def test_state_agreement_counts_identical_frames() -> None:
    a = {
        "1": {"states": [STATE_OK]},
        "2": {"states": [STATE_OK, STATE_UNKNOWN]},
        "3": {"states": []},
    }
    # 사람 순서는 검출 점수 순이라 정밀도마다 바뀔 수 있다 — 순서는 보지 않는다.
    b = {
        "1": {"states": [STATE_OK]},
        "2": {"states": [STATE_UNKNOWN, STATE_OK]},
        "3": {"states": []},
    }
    assert pb.state_agreement(a, b, ["1", "2", "3"]) == {"frames": 3, "identical": 3}
    c = {"1": {"states": [STATE_VIOLATION]}, "2": a["2"], "3": {"states": [STATE_OK]}}
    assert pb.state_agreement(a, c, ["1", "2", "3"]) == {"frames": 3, "identical": 1}


# ── 프레임 처리(가짜 검출기) ────────────────────────────────


class FakeDetector:
    def __init__(self, outputs) -> None:
        self.outputs = outputs
        self.calls = 0
        self.adapter = YoloxAdapter(64)

    def detect(self, image: np.ndarray) -> list[Detection]:
        self.calls += 1
        return self.outputs(image) if callable(self.outputs) else list(self.outputs)


def test_evaluate_frame_reuses_identical_crops_and_judges() -> None:
    image = np.zeros((200, 200, 3), dtype=np.uint8)
    person = det("person", 0.9, (50, 50, 150, 190))
    coco = {
        "fp32": FakeDetector([person, det("chair", 0.9, (0, 0, 10, 10))]),
        "int8": FakeDetector([person]),
    }
    ppe_out = [det("helmet", 0.9, (10, 20, 30, 40)), det("vest", 0.8, (10, 60, 60, 120))]
    ppe = {"fp32": FakeDetector(ppe_out), "int8": FakeDetector(ppe_out[:1])}
    frame = pb.evaluate_frame(
        image, coco, ppe, person_label="person", pad=0.08, head_margin=8, use_clip=True
    )
    assert [d.label for d in frame.persons["fp32"]] == ["person"]
    # 같은 크롭이면 한 번만 돌린다 — 일치율용과 판정용이 같은 입력이다.
    assert ppe["fp32"].calls == 1
    assert ppe["int8"].calls == 1
    assert frame.judgements["fp32"][0].state == STATE_OK
    assert frame.judgements["int8"][0].state == STATE_UNKNOWN
    assert frame.judgements["int8"][0].reason == "몸통 미검출"
    assert frame.reference_crops["int8"][0] == ppe_out[:1]


def test_evaluate_frame_marks_failed_crop() -> None:
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    tiny = det("person", 0.9, (10, 10, 12, 12))
    coco = {"fp32": FakeDetector([tiny])}
    ppe = {"fp32": FakeDetector([])}
    frame = pb.evaluate_frame(
        image, coco, ppe, person_label="person", pad=0.0, head_margin=8, use_clip=True
    )
    assert frame.judgements["fp32"][0].reason == "크롭 실패"
    assert frame.reference_crops["fp32"] == [None]
    assert ppe["fp32"].calls == 0


def _write_frames(folder: Path, count: int) -> None:
    import cv2

    folder.mkdir(parents=True)
    for i in range(1, count + 1):
        image = np.full((120, 160, 3), i * 10 % 255, dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", image)
        assert ok
        buf.tofile(str(folder / f"{i:05d}.jpg"))


def test_run_accuracy_aggregates_agreement_and_states(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    _write_frames(frames, 4)
    person = det("person", 0.9, (20, 10, 120, 110))
    coco = {"fp32": FakeDetector([person]), "fp16": FakeDetector([person])}
    ppe_out = [det("helmet", 0.9, (10, 20, 30, 40)), det("vest", 0.8, (10, 60, 60, 100))]
    ppe = {"fp32": FakeDetector(ppe_out), "fp16": FakeDetector(ppe_out[:1])}
    out = pb.run_accuracy(
        frames,
        ["00001", "00002", "00003", "00004"],
        coco,
        ppe,
        excluded={"00004"},
        person_label="person",
        pad=0.08,
        head_margin=8,
        use_clip=True,
    )
    assert set(out.states) == {"fp32", "fp16"}
    assert out.states["fp32"]["00001"]["states"] == [STATE_OK]
    assert out.states["fp16"]["00002"]["states"] == [STATE_UNKNOWN]
    assert out.states["fp16"]["00002"]["reasons"] == ["몸통 미검출"]
    assert out.states["fp16"]["00004"]["labels"] == ["helmet"]
    coco_fp16 = out.coco["fp16"].as_dict()
    ppe_fp16 = out.ppe["fp16"].as_dict()
    # 보정 프레임(00004)은 일치율에서 빠진다.
    assert coco_fp16["inputs"] == 3
    assert coco_fp16["match_rate"] == 1.0
    assert ppe_fp16["reference"] == 6
    assert ppe_fp16["missed"] == 3


def test_run_accuracy_rejects_missing_frame(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    _write_frames(frames, 1)
    coco = {"fp32": FakeDetector([])}
    ppe = {"fp32": FakeDetector([])}
    with pytest.raises(FileNotFoundError):
        pb.run_accuracy(
            frames,
            ["00001", "00009"],
            coco,
            ppe,
            excluded=set(),
            person_label="person",
            pad=0.08,
            head_margin=8,
            use_clip=True,
        )


# ── 경로·설정 ───────────────────────────────────────────────


def test_variant_path_naming(tmp_path: Path) -> None:
    fp32 = tmp_path / "ppe.onnx"
    assert pb.variant_path(fp32, "fp32", tmp_path / "out") == fp32
    assert pb.variant_path(fp32, "int8-qdq", tmp_path / "out") == tmp_path / "out/ppe.int8-qdq.onnx"
    with pytest.raises(ValueError):
        pb.variant_path(fp32, "int4", tmp_path)


def test_with_model_overrides_only_the_section() -> None:
    config = {"vision": {"ppe": {"model_path": "models/ppe.onnx"}, "providers": ["A"]}}
    out = pb.with_model(config, "ppe", Path("/x/ppe.fp16.onnx"), "CPUExecutionProvider")
    assert out["vision"]["ppe"]["model_path"] == str(Path("/x/ppe.fp16.onnx"))
    assert out["vision"]["providers"] == ["CPUExecutionProvider"]
    assert config["vision"]["ppe"]["model_path"] == "models/ppe.onnx"


def test_provider_alias() -> None:
    assert pb.provider_name("dml") == "DmlExecutionProvider"
    assert pb.provider_name("cpu") == "CPUExecutionProvider"
    with pytest.raises(ValueError):
        pb.provider_name("cuda")


def test_rss_bytes_is_positive_or_none() -> None:
    value = pb.rss_bytes()
    assert value is None or value > 0


def test_load_frame_reads_jpeg(tmp_path: Path) -> None:
    _write_frames(tmp_path / "f", 1)
    image = pb.load_frame(tmp_path / "f", "00001")
    assert image.shape == (120, 160, 3)


def test_write_json_creates_parent(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.json"
    pb.write_json(path, {"값": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"값": 1}


# ── 변환·지연 (onnx 가 있을 때만) ───────────────────────────


def _tiny_yolox(path: Path) -> None:
    """32x32 입력 · 출력 [1, 21, 6] (격자 16+4+1, 클래스 1개) 인 YOLOX 모양 모델."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    rng = np.random.default_rng(0)
    weight = numpy_helper.from_array(rng.normal(0, 0.1, (6, 3, 1, 1)).astype(np.float32), "w")
    bias = numpy_helper.from_array(np.zeros(6, dtype=np.float32), "b")
    shape = numpy_helper.from_array(np.array([1, 1, 6], dtype=np.int64), "shape")
    target = numpy_helper.from_array(np.array([1, 21, 6], dtype=np.int64), "target")
    nodes = [
        helper.make_node("Conv", ["images", "w", "b"], ["conv"], name="conv"),
        helper.make_node("Relu", ["conv"], ["relu"], name="relu"),
        helper.make_node("GlobalAveragePool", ["relu"], ["pool"], name="pool"),
        helper.make_node("Reshape", ["pool", "shape"], ["flat"], name="flat"),
        helper.make_node("Expand", ["flat", "target"], ["output"], name="expand"),
    ]
    graph = helper.make_graph(
        nodes,
        "tiny",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 32, 32])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 21, 6])],
        initializer=[weight, bias, shape, target],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 11)])
    model.ir_version = 7
    onnx.save(model, str(path))


def _run(path: Path, x: np.ndarray) -> np.ndarray:
    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return np.asarray(session.run(None, {"images": x})[0])


def test_convert_all_variants_keep_float_io_and_stay_close(tmp_path: Path) -> None:
    src = tmp_path / "tiny.onnx"
    _tiny_yolox(src)
    rng = np.random.default_rng(1)
    tensors = [rng.integers(0, 255, (1, 3, 32, 32)).astype(np.float32) for _ in range(4)]
    out_dir = tmp_path / "out"
    written = pb.convert_model(src, out_dir, tensors)
    assert sorted(written) == ["fp16", "int8-dyn", "int8-qdq"]
    reference = _run(src, tensors[0])
    for variant, path in written.items():
        assert path == pb.variant_path(src, variant, out_dir)
        assert path.is_file()
        out = _run(path, tensors[0])
        assert out.dtype == np.float32
        assert out.shape == reference.shape
        # FP16 은 거의 같고, INT8 은 보정 4장짜리 합성 모델이라 넉넉히 본다(실제 정확도는 실측으로).
        share = 0.01 if variant == "fp16" else 0.25
        tolerance = share * float(np.max(np.abs(reference))) + 1e-3
        assert float(np.max(np.abs(out - reference))) <= tolerance, variant
    # 중간 파일(옵셋 13 승격·전처리)은 남기지 않는다.
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(p.name for p in written.values())


def test_head_tail_stops_at_conv(tmp_path: Path) -> None:
    """출력에서 거꾸로 Conv 를 만날 때까지의 디코드 꼬리만 FP32 로 남긴다."""
    onnx = pytest.importorskip("onnx")
    src = tmp_path / "tiny.onnx"
    _tiny_yolox(src)
    assert pb.head_tail_nodes(onnx.load(str(src))) == ["expand", "flat", "pool", "relu"]


def test_static_qdq_keeps_head_tail_float(tmp_path: Path) -> None:
    onnx = pytest.importorskip("onnx")
    src = tmp_path / "tiny.onnx"
    _tiny_yolox(src)
    tensors = [np.full((1, 3, 32, 32), 100.0, dtype=np.float32)]
    written = pb.convert_model(src, tmp_path / "out", tensors)
    model = onnx.load(str(written["int8-qdq"]))
    quantized = {n.input[0] for n in model.graph.node if n.op_type == "QuantizeLinear"}
    # 디코드 꼬리(Relu → 풀링 → Reshape → Expand)의 텐서는 FP32 로 흐른다.
    assert not quantized & {"relu", "pool", "flat", "output"}
    assert any(n.op_type == "DequantizeLinear" for n in model.graph.node)  # Conv 가중치는 INT8


def test_latency_one_reports_stats_memory_and_providers(tmp_path: Path) -> None:
    src = tmp_path / "tiny.onnx"
    _tiny_yolox(src)
    images = [np.zeros((40, 30, 3), dtype=np.uint8), np.full((30, 40, 3), 200, dtype=np.uint8)]
    out = pb.latency_one(
        src,
        "CPUExecutionProvider",
        images,
        input_size=32,
        labels=["thing"],
        warmup=2,
        runs=5,
    )
    assert out["providers"] == ["CPUExecutionProvider"]
    assert out["pipeline_ms"]["n"] == 5
    assert out["run_ms"]["n"] == 5
    assert out["file_mb"] > 0
    assert "rss_session_mb" in out


# ── CLI (가짜 검출기·가짜 하위 프로세스) ────────────────────


class FakeSession:
    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]


def _cli_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    frames = tmp_path / "frames"
    _write_frames(frames, 12)
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps(_base_session(), ensure_ascii=False), encoding="utf-8")
    return frames, session_path, tmp_path / "variants"


def _fake_build(config, section, model, provider):  # noqa: ARG001
    if section == "coco":
        return FakeDetector([det("person", 0.9, (20, 10, 120, 110))]), FakeSession()
    found = [det("helmet", 0.9, (10, 20, 30, 40)), det("no_vest", 0.8, (10, 60, 60, 100))]
    return FakeDetector(found), FakeSession()


def _cli_base(frames: Path, session_path: Path) -> list[str]:
    return ["--device", "mechdog-01", "--frames", str(frames), "--session", str(session_path)]


def test_cli_convert_writes_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frames, session_path, variants = _cli_fixture(tmp_path)
    seen: dict[str, int] = {}

    def fake_convert(src: Path, out_dir: Path, tensors) -> dict[str, Path]:
        seen[src.stem] = len(tensors)
        out_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        for variant in pb.VARIANTS[1:]:
            path = pb.variant_path(src, variant, out_dir)
            path.write_bytes(b"x")
            written[variant] = path
        return written

    fp32 = tmp_path / "fp32"
    fp32.mkdir()
    for name in pb.SECTIONS:
        (fp32 / f"{name}.onnx").write_bytes(b"model")
    monkeypatch.setattr(pb, "build_detector", _fake_build)
    monkeypatch.setattr(pb, "convert_model", fake_convert)
    monkeypatch.setattr(
        pb,
        "_model_paths",
        lambda _config, out: {
            s: {v: pb.variant_path(fp32 / f"{s}.onnx", v, out) for v in pb.VARIANTS}
            for s in pb.SECTIONS
        },
    )
    args = ["convert", *_cli_base(frames, session_path), "--out-dir", str(variants)]
    assert pb.main([*args, "--calib-frames", "1"]) == 0
    manifest = json.loads((variants / "calibration.json").read_text(encoding="utf-8"))
    assert manifest["calibration_tags"] == ["00011"]
    assert seen == {"coco": 1, "ppe": 1}
    assert set(manifest["models"]["ppe"]) == set(pb.VARIANTS)


def test_cli_accuracy_reports_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frames, session_path, variants = _cli_fixture(tmp_path)
    variants.mkdir()
    sha = {v: {"sha256": v} for v in pb.VARIANTS}
    (variants / "calibration.json").write_text(
        json.dumps({"calibration_tags": ["00012"], "models": {"ppe": sha}}), encoding="utf-8"
    )
    monkeypatch.setattr(pb, "build_detector", _fake_build)
    out = tmp_path / "accuracy.json"
    args = ["accuracy", *_cli_base(frames, session_path), "--variants-dir", str(variants)]
    args += ["--variants", "fp32,int8-qdq", "--out", str(out)]
    assert pb.main([*args, "--sessions-dir", str(tmp_path / "sessions")]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["calibration_excluded"] == 1
    assert report["agreement_frames"] == 11
    qdq = report["variants"]["int8-qdq"]
    assert qdq["coco_person_agreement"]["match_rate"] == 1.0
    assert qdq["states_vs_fp32"] == {"frames": 11, "identical": 11}
    assert qdq["counts"][STATE_VIOLATION] == 12
    assert qdq["acceptance"]["passed"] is False
    assert (tmp_path / "sessions" / "session.int8-qdq.json").is_file()


def test_cli_accuracy_requires_fp32_first(tmp_path: Path) -> None:
    frames, session_path, variants = _cli_fixture(tmp_path)
    variants.mkdir()
    (variants / "calibration.json").write_text('{"calibration_tags": []}', encoding="utf-8")
    args = ["accuracy", *_cli_base(frames, session_path), "--variants-dir", str(variants)]
    with pytest.raises(SystemExit):
        pb.main([*args, "--variants", "fp16", "--out", str(tmp_path / "a.json")])


def test_cli_latency_collects_subprocess_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames, session_path, variants = _cli_fixture(tmp_path)
    variants.mkdir()
    (variants / "calibration.json").write_text('{"calibration_tags": ["00001"]}', encoding="utf-8")
    calls: list[list[str]] = []

    class Done:
        def __init__(self, ok: bool) -> None:
            self.returncode = 0 if ok else 1
            self.stdout = '{"providers": ["CPUExecutionProvider"]}\n' if ok else ""
            self.stderr = "" if ok else "boom\nInvalidGraph"

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Done("int8" not in command[command.index("--model") + 1])

    monkeypatch.setattr(pb.subprocess, "run", fake_run)
    out = tmp_path / "latency.json"
    args = ["latency", *_cli_base(frames, session_path), "--variants-dir", str(variants)]
    assert pb.main([*args, "--providers", "cpu", "--out", str(out)]) == 0
    rows = json.loads(out.read_text(encoding="utf-8"))["results"]
    assert len(rows) == len(pb.SECTIONS) * len(pb.VARIANTS) == len(calls)
    assert rows[0]["providers"] == ["CPUExecutionProvider"]
    assert any(row.get("error") == "InvalidGraph" for row in rows)
    # 보정 프레임은 지연 입력에서도 뺀다.
    assert "00001" not in calls[0][calls[0].index("--tags") + 1].split(",")


def test_cli_latency_one_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_frames(tmp_path / "f", 2)
    monkeypatch.setattr(pb, "latency_one", lambda *a, **k: {"images": len(a[2]), "k": k["runs"]})
    args = ["latency-one", "--model", "m.onnx", "--provider", "cpu", "--frames"]
    args += [str(tmp_path / "f"), "--tags", "00001,00002", "--input-size", "640"]
    args += ["--labels", "a,b", "--runs", "3"]
    assert pb.main(args) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"images": 2, "k": 3}
