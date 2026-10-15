"""모델 메타데이터 — sha256 한 번 계산 · 구조화 메타 파일(`models/<name>.meta.json`) 읽기.

CI 에는 가중치가 없으므로 작은 가짜 파일로 시험한다. 저장소의 메타 파일은 정본
(`tools/fetch_models.py` 의 `WEIGHTS` · 클래스 목록 · `config.yaml`)과 대조한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from host.common.config import repo_path
from host.vision import model_info
from host.vision.coco_labels import COCO_CLASSES
from host.vision.detector import Detector
from host.vision.model_info import inspect_model, meta_path_for
from host.vision.ppe_detector import PPE_CLASSES
from tools.fetch_models import WEIGHTS


def _fake_model(tmp_path: Path, payload: bytes = b"fake-onnx") -> Path:
    path = tmp_path / "toy.onnx"
    path.write_bytes(payload)
    return path


def test_sha256_is_the_file_digest_and_meta_is_optional(tmp_path: Path) -> None:
    path = _fake_model(tmp_path)
    info = inspect_model("toy", path)
    assert info.sha256 == hashlib.sha256(b"fake-onnx").hexdigest()
    assert info.file == "toy.onnx"
    assert info.meta == {}, "메타 파일이 없으면 넘어간다"


def test_missing_weight_has_no_digest(tmp_path: Path) -> None:
    """잴 수 없는 값은 지어내지 않는다 — 파일이 없으면 sha256 은 `None`."""
    info = inspect_model("toy", tmp_path / "absent.onnx")
    assert info.sha256 is None


def test_meta_file_is_read_when_present(tmp_path: Path) -> None:
    path = _fake_model(tmp_path)
    meta = {
        "schema": 1,
        "input_size": 640,
        "classes": ["a", "b"],
        "source": {"release": "toy-v1"},
        "training_data": "toy-data-v1",
    }
    meta_path_for(path).write_text(json.dumps(meta), encoding="utf-8")
    assert meta_path_for(path).name == "toy.meta.json"
    assert inspect_model("toy", path, labels=("a", "b")).meta == meta


@pytest.mark.parametrize("text", ["{깨진", "[1, 2]"])
def test_broken_meta_file_is_skipped(tmp_path: Path, text: str) -> None:
    path = _fake_model(tmp_path)
    meta_path_for(path).write_text(text, encoding="utf-8")
    assert inspect_model("toy", path).meta == {}


def test_meta_mismatch_is_logged_not_fatal(tmp_path: Path, monkeypatch) -> None:
    """메타 파일의 해시·클래스가 실제와 다르면 경고만 남긴다 — 기동은 막지 않는다."""
    path = _fake_model(tmp_path)
    meta_path_for(path).write_text(
        json.dumps({"sha256": "0" * 64, "classes": ["b", "a"]}), encoding="utf-8"
    )
    warned: list[tuple[str, dict]] = []
    monkeypatch.setattr(model_info.LOG, "warning", lambda event, **kw: warned.append((event, kw)))
    info = inspect_model("toy", path, labels=("a", "b"))
    assert info.sha256 == hashlib.sha256(b"fake-onnx").hexdigest()
    assert [event for event, _ in warned] == [
        "model_meta_sha256_mismatch",
        "model_meta_classes_mismatch",
    ]


class _Session:
    def get_inputs(self) -> list:
        class _Input:
            name = "images"

        return [_Input()]

    def get_providers(self) -> list[str]:
        return ["DmlExecutionProvider", "CPUExecutionProvider"]

    def run(self, _outputs, _feed) -> list[np.ndarray]:
        return [np.zeros((1, 8400, 85), dtype=np.float32)]


def test_detector_hashes_once_at_load_and_reports_provider(
    cfg: dict, tmp_path: Path, monkeypatch
) -> None:
    path = _fake_model(tmp_path)
    local = dict(cfg)
    local["vision"] = dict(cfg["vision"], coco=dict(cfg["vision"]["coco"], model_path=str(path)))
    calls: list[Path] = []
    real = model_info.sha256_of
    monkeypatch.setattr(model_info, "sha256_of", lambda p: calls.append(p) or real(p))

    det = Detector(local, labels=COCO_CLASSES, session_factory=lambda *_: _Session())
    assert det.loaded is False
    det.open()
    det.open()
    det.detect(np.zeros((48, 64, 3), dtype=np.uint8))
    assert det.model_file().sha256 == hashlib.sha256(b"fake-onnx").hexdigest()

    assert len(calls) == 1, "35MB 파일을 프레임마다 해시하지 않는다"
    assert det.loaded is True
    assert det.model_summary() == {
        "name": "coco",
        "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
        "provider": "DmlExecutionProvider",
    }


def test_provider_is_omitted_when_the_session_cannot_say(cfg: dict, tmp_path: Path) -> None:
    class _Bare(_Session):
        get_providers = None  # type: ignore[assignment]

    local = dict(cfg)
    local["vision"] = dict(
        cfg["vision"], coco=dict(cfg["vision"]["coco"], model_path=str(tmp_path / "x.onnx"))
    )
    det = Detector(local, labels=COCO_CLASSES, session_factory=lambda *_: _Bare())
    det.open()
    assert det.model_summary() == {"name": "coco", "sha256": None, "provider": None}


# ── 저장소의 메타 파일은 정본과 어긋나지 않는다 ─────────────────────────
_EXPECTED = {
    "coco": COCO_CLASSES,
    "ppe": PPE_CLASSES,
}


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_repo_meta_files_agree_with_the_canonical_values(cfg: dict, name: str) -> None:
    model = repo_path(cfg["vision"][name]["model_path"])
    meta_path = meta_path_for(model)
    assert meta_path.is_file(), f"{meta_path.name} 가 저장소에 있어야 한다"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    weight = next(w for w in WEIGHTS if Path(w.dest).name == model.name)
    assert meta["schema"] == 1
    assert meta["name"] == name
    assert meta["sha256"] == weight.sha256
    assert meta["size"] == weight.size
    assert meta["source"]["url"] == weight.url
    assert meta["input_size"] == cfg["vision"][name]["input_size"]
    assert meta["model_family"] == cfg["vision"][name]["model_family"]
    assert tuple(meta["classes"]) == tuple(_EXPECTED[name])
    assert meta["training_data"]
