"""쓰러짐 후보 벤치(`tools/probe/fallen_bench.py`) 검증.

**모델도 카메라도 없이 닫힌다.** 사진은 임시 폴더에 쓴 작은 합성 JPEG(`cv2.imencode`)이고,
검출기는 사진 가로 폭으로 정해 둔 검출을 돌려주는 대역이다 — 실제 사진은 쓰지 않는다.

    ① 판정은 런타임의 `PersonGate`·`FallenGate` 그대로다 — 대표 박스는 점수 최고
    ② conf·종횡비 훑기가 바닥값 한 번의 추론으로 닫힌다
    ③ 장면은 프레임 하나라도 후보면 «후보» 다
    ④ `person_down` 폴더만 읽고, 없거나 인자가 틀리면 종료 코드 2
    ⑤ 운용 설정은 설정 파일 값이고, 원자료에 이미지가 담기지 않는다
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from host.common.config import load_base_config
from host.vision.detector import Detection, ModelMissingError
from tools.probe import fallen_bench as fb

BASE = load_base_config()
OP_CONF = float(BASE["vision"]["coco"]["conf_threshold"])
OP_ASPECT = float(BASE["vision"]["fallen"]["aspect_ratio"])

WIDE = (0.0, 100.0, 300.0, 200.0)  # 3.0
TALL = (0.0, 0.0, 100.0, 300.0)  # 0.33
RATIO_13 = (0.0, 0.0, 130.0, 100.0)  # 1.3


def person(score: float, box: tuple[float, float, float, float]) -> Detection:
    return Detection("person", score, box)


class FakeDetector:
    """검출기 대역. 사진 가로 폭(JPEG 로도 그대로 남는다)으로 검출을 고른다."""

    def __init__(self, by_width: dict[int, list[Detection]], config: Any) -> None:
        self.by_width = by_width
        self.conf = float(config["vision"]["coco"]["conf_threshold"])
        self.seen: list[tuple[int, int]] = []

    def detect(self, image: np.ndarray) -> list[Detection]:
        height, width = int(image.shape[0]), int(image.shape[1])
        self.seen.append((width, height))
        return [d for d in self.by_width.get(width, []) if d.score >= self.conf]

    def model_summary(self) -> dict[str, Any]:
        return {"name": "coco", "sha256": "f" * 64, "provider": "CPUExecutionProvider"}


def _photo(path: Path, width: int, height: int = 24) -> None:
    ok, buf = cv2.imencode(".jpg", np.full((height, width, 3), 128, dtype=np.uint8))
    assert ok
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


def _tree(root: Path, files: dict[str, int]) -> Path:
    for rel, width in files.items():
        _photo(root / rel, width)
    return root


def _factory(
    by_width: dict[int, list[Detection]], made: list[FakeDetector] | None = None
) -> Callable[[Any], FakeDetector]:
    def make(config: Any) -> FakeDetector:
        detector = FakeDetector(by_width, config)
        if made is not None:
            made.append(detector)
        return detector

    return make


def _run(
    root: Path,
    by_width: dict[int, list[Detection]],
    argv: Sequence[str] = (),
    *,
    out: Path | None = None,
) -> tuple[int, list[FakeDetector]]:
    made: list[FakeDetector] = []
    args = [str(root), *argv]
    if out is not None:
        args += ["--out", str(out)]
    return fb.main(args, detector_factory=_factory(by_width, made)), made


# ── ① 판정 ──────────────────────────────────────────────────────────
def test_wide_person_is_candidate() -> None:
    verdict = fb.judge([person(0.9, WIDE)], OP_CONF, 1.5, config=BASE)
    assert verdict["candidate"] is True
    assert verdict["reason"] is None
    assert verdict["aspect"] == pytest.approx(3.0)
    assert verdict["score"] == pytest.approx(0.9)


def test_tall_person_misses_on_aspect() -> None:
    verdict = fb.judge([person(0.9, TALL)], OP_CONF, 1.5, config=BASE)
    assert verdict["candidate"] is False
    assert verdict["reason"] == fb.MISS_ASPECT == "종횡비 미달"


def test_low_score_misses_person_at_op_conf_but_hits_lower_conf() -> None:
    low = OP_CONF - 0.1
    people = [person(low, WIDE)]
    at_op = fb.judge(people, OP_CONF, OP_ASPECT, config=BASE)
    assert at_op["candidate"] is False
    assert at_op["reason"] == fb.MISS_PERSON == "사람 미검출"
    assert fb.judge(people, low, OP_ASPECT, config=BASE)["candidate"] is True


def test_representative_box_is_highest_score_not_widest() -> None:
    people = [person(0.6, WIDE), person(0.9, TALL)]
    verdict = fb.judge(people, OP_CONF, 1.5, config=BASE)
    assert verdict["candidate"] is False
    assert verdict["reason"] == fb.MISS_ASPECT
    assert verdict["score"] == pytest.approx(0.9)


def test_aspect_threshold_sweep() -> None:
    people = [person(0.9, RATIO_13)]
    assert fb.judge(people, OP_CONF, 1.2, config=BASE)["candidate"] is True
    assert fb.judge(people, OP_CONF, 1.5, config=BASE)["candidate"] is False


def test_non_person_wide_detection_is_ignored(tmp_path: Path) -> None:
    root = _tree(tmp_path / "root", {"person_down/yes/침대_01.jpg": 40})
    code, _ = _run(root, {40: [Detection("bed", 0.95, WIDE)]}, out=tmp_path / "raw.json")
    assert code == 0
    raw = json.loads((tmp_path / "raw.json").read_text(encoding="utf-8"))
    assert raw["records"][0]["people"] == []
    op = raw["summary"]["operating"]
    assert op["frames"]["fn"] == 1
    assert op["missed"] == {fb.MISS_PERSON: 1, fb.MISS_ASPECT: 0}


# ── ② 훑기 ──────────────────────────────────────────────────────────
def test_sweep_row_with_lower_conf_recovers_low_score(tmp_path: Path) -> None:
    low = OP_CONF - 0.1
    root = _tree(tmp_path / "root", {"person_down/yes/누움_01.jpg": 40})
    confs = f"{low:.2f},{OP_CONF:.2f}"
    code, made = _run(
        root,
        {40: [person(low, WIDE)]},
        ["--confs", confs, "--aspects", "1.5"],
        out=tmp_path / "raw.json",
    )
    assert code == 0
    assert made[0].conf == pytest.approx(0.1), "바닥값으로 한 번만 추론한다"
    raw = json.loads((tmp_path / "raw.json").read_text(encoding="utf-8"))
    rows = {(r["conf"], r["aspect"]): r for r in raw["summary"]["sweep"]}
    assert rows[round(low, 2), 1.5]["frames"]["recall"] == 1.0
    assert rows[OP_CONF, 1.5]["frames"]["recall"] == 0.0
    assert raw["summary"]["operating"]["missed"][fb.MISS_PERSON] == 1


# ── ③ 장면 채점 ─────────────────────────────────────────────────────
def test_scene_is_candidate_if_any_frame_is(tmp_path: Path, capsys: Any) -> None:
    root = _tree(
        tmp_path / "root",
        {
            "person_down/yes/누움_01.jpg": 40,
            "person_down/yes/누움_02.jpg": 41,
            "person_down/yes/누움_03.jpg": 41,
            "person_down/no/서있음_01.jpg": 41,
            "person_down/no/서있음_02.jpg": 40,
        },
    )
    by_width = {40: [person(0.9, WIDE)], 41: [person(0.9, TALL)]}
    code, _ = _run(root, by_width, out=tmp_path / "raw.json")
    assert code == 0
    raw = json.loads((tmp_path / "raw.json").read_text(encoding="utf-8"))
    op = raw["summary"]["operating"]
    assert (op["frames"]["tp"], op["frames"]["fn"]) == (1, 2)
    assert (op["frames"]["fp"], op["frames"]["tn"]) == (1, 1)
    assert op["missed"] == {fb.MISS_PERSON: 0, fb.MISS_ASPECT: 2}
    scenes = op["scenes"]
    assert (scenes["tp"], scenes["fn"], scenes["fp"], scenes["tn"]) == (1, 0, 1, 0)
    text = capsys.readouterr().out
    assert "누움" in text and "서있음" in text
    assert "PersonGate" in text  # conf 를 내리면 함께 바뀌는 것들 주의


# ── ④ 폴더·인자 ─────────────────────────────────────────────────────
def test_only_person_down_is_counted(tmp_path: Path) -> None:
    root = _tree(
        tmp_path / "root",
        {"person_down/yes/누움_01.jpg": 40, "fallen_object/yes/소화기_01.jpg": 41},
    )
    code, made = _run(root, {40: [person(0.9, WIDE)]}, out=tmp_path / "raw.json")
    assert code == 0
    raw = json.loads((tmp_path / "raw.json").read_text(encoding="utf-8"))
    assert [r["file"] for r in raw["records"]] == ["person_down/yes/누움_01.jpg"]
    assert made[0].seen == [(40, 24)]


def test_no_person_down_photos_exits_2(tmp_path: Path) -> None:
    root = _tree(tmp_path / "root", {"fallen_object/yes/소화기_01.jpg": 41})
    code, made = _run(root, {})
    assert code == 2
    assert made == [], "모델을 만들기 전에 멈춘다"


def test_bad_layout_exits_2(tmp_path: Path) -> None:
    root = _tree(tmp_path / "root", {"person_down/maybe/누움_01.jpg": 40})
    assert _run(root, {})[0] == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--conf-floor", "0.3", "--confs", "0.2,0.4"],
        ["--aspects", "1.0,1.5"],
        ["--aspects", "0.8"],
        ["--confs", "nan"],
    ],
)
def test_bad_sweep_arguments_exit_2(tmp_path: Path, argv: list[str]) -> None:
    root = _tree(tmp_path / "root", {"person_down/yes/누움_01.jpg": 40})
    with pytest.raises(SystemExit) as exc:
        _run(root, {}, argv)
    assert exc.value.code == 2


def test_missing_model_exits_1(tmp_path: Path, capsys: Any) -> None:
    root = _tree(tmp_path / "root", {"person_down/yes/누움_01.jpg": 40})

    class Missing(FakeDetector):
        def detect(self, _image: np.ndarray) -> list[Detection]:
            raise ModelMissingError("모델 파일 없음: models/coco.onnx")

    code = fb.main([str(root)], detector_factory=lambda config: Missing({}, config))
    assert code == 1
    assert "models/coco.onnx" in capsys.readouterr().err


# ── ⑤ 운용 설정·원자료 ──────────────────────────────────────────────
def test_operating_row_uses_config_values_even_if_not_swept(tmp_path: Path, capsys: Any) -> None:
    root = _tree(tmp_path / "root", {"person_down/yes/누움_01.jpg": 40})
    code, _ = _run(
        root,
        {40: [person(0.9, WIDE)]},
        ["--confs", "0.2", "--aspects", "2.5"],
        out=tmp_path / "raw.json",
    )
    assert code == 0
    raw = json.loads((tmp_path / "raw.json").read_text(encoding="utf-8"))
    assert raw["meta"]["operating"] == {"conf": OP_CONF, "aspect": OP_ASPECT}
    rows = raw["summary"]["sweep"]
    assert [(r["conf"], r["aspect"], r["operating"]) for r in rows] == [
        (0.2, 2.5, False),
        (OP_CONF, OP_ASPECT, True),
    ]
    assert f"conf {OP_CONF}" in capsys.readouterr().out


def test_raw_json_keeps_floor_detections_without_image_bytes(tmp_path: Path) -> None:
    root = _tree(tmp_path / "root", {"person_down/yes/누움_01.jpg": 40})
    by_width = {40: [person(0.123456, (1.23456, 2.0, 3.0, 4.0)), person(0.05, WIDE)]}
    code, _ = _run(root, by_width, out=tmp_path / "raw.json")
    assert code == 0
    text = (tmp_path / "raw.json").read_text(encoding="utf-8")
    raw = json.loads(text)
    assert raw["meta"]["conf_floor"] == 0.1
    assert raw["meta"]["model"]["name"] == "coco"
    record = raw["records"][0]
    assert record["width"] == 40 and record["height"] == 24
    assert record["people"] == [{"score": 0.123, "box": [1.235, 2.0, 3.0, 4.0]}]
    assert "jpeg" not in text and "payload" not in text and "image" not in record
    assert record["operating"]["candidate"] is False


def test_end_to_end_markdown(tmp_path: Path, capsys: Any) -> None:
    root = _tree(
        tmp_path / "root",
        {"person_down/yes/누움_01.jpg": 40, "person_down/no/빈방_01.jpg": 42},
    )
    code, made = _run(root, {40: [person(0.9, WIDE)]})
    assert code == 0
    assert sorted(made[0].seen) == [(40, 24), (42, 24)], "decode 경로를 지나 배열로 받는다"
    text = capsys.readouterr().out
    assert "예 1장 · 아니오 1장" in text
    assert "장면 2개" in text
    assert "◀ 운용" in text
