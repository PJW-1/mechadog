"""로드한 모델이 무엇이었는지 — 가중치 sha256 과 구조화 메타 파일.

가중치(.onnx)는 저장소에 없다(`models/README.md`). 그래서 사건이 «어느 파일로» 판단했는지는
파일 해시로만 증명된다. 35MB 파일이라 **기동 때 한 번만** 계산해 들고 있는다.

구조화 메타 파일 `models/<이름>.meta.json` (형식 `schema: 1`) — 있으면 읽고 없으면 넘어간다:

    name           설정 절 이름 (coco · ppe · hazard)
    model_family   `vision.<절>.model_family` 와 같은 값
    input_size     정사각 입력 한 변 (픽셀)
    classes        출력 클래스 순서 — 모델과의 계약
    source         {url, release, license}
    training_data  학습 데이터 버전·출처 한 줄
    sha256 · size  기대 가중치 (정본은 `tools/fetch_models.py` 의 `WEIGHTS`)

⚠️ 메타 파일이 실제 가중치·클래스와 다르면 **경고만 남긴다.** 기동을 막는 검증은
`tools/fetch_models.py --check` 의 몫이다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.vision")

#: 한 번에 읽는 크기 (`tools/fetch_models.py` 와 같다).
CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ModelFile:
    """가중치 파일 하나의 정체 — 세션을 열지 않아도 알 수 있는 값만."""

    name: str
    file: str
    #: 파일이 없으면 `None` — 잴 수 없는 값을 지어내지 않는다.
    sha256: str | None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "file": self.file, "sha256": self.sha256, "meta": self.meta}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def meta_path_for(model_path: Path) -> Path:
    """`models/ppe.onnx` → `models/ppe.meta.json`."""
    return model_path.with_name(f"{model_path.stem}.meta.json")


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        LOG.warning("model_meta_unreadable", path=path.name, error=f"{type(exc).__name__}: {exc}")
        return {}
    if not isinstance(meta, dict):
        LOG.warning("model_meta_unreadable", path=path.name, error="객체가 아님")
        return {}
    return meta


def inspect_model(name: str, path: Path, *, labels: Sequence[str] | None = None) -> ModelFile:
    """가중치 해시를 계산하고 메타 파일을 읽는다. 기동 때 모델마다 한 번 부른다."""
    digest = sha256_of(path) if path.is_file() else None
    meta = _read_meta(meta_path_for(path))
    expected = meta.get("sha256")
    if digest is not None and isinstance(expected, str) and expected != digest:
        LOG.warning("model_meta_sha256_mismatch", model=name, actual=digest, expected=expected)
    classes = meta.get("classes")
    if labels is not None and isinstance(classes, list) and tuple(classes) != tuple(labels):
        LOG.warning("model_meta_classes_mismatch", model=name)
    return ModelFile(name=name, file=path.name, sha256=digest, meta=meta)
