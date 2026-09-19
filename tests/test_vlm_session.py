"""VLM 세션 경계 검증 (WBS 4.8.0 · ADR-35).

**여기는 가중치와 GPU 를 만지는 유일한 자리**라 모델 자체는 시험하지 않는다. 대신
그 경계가 지켜지는지를 본다.

    ① 라이브러리가 없어도 `host.runtime` 이 죽지 않는다
    ② 없으면 팩토리가 `None` 이고 예외가 아니다
    ③ 이미지 형식을 제대로 맞춘다
"""

from __future__ import annotations

import io

import pytest

from host.vision import vlm_session
from host.vision.vlm_session import REQUIRED_PACKAGES, build_session_factory, to_image


def test_importing_the_module_does_not_require_transformers() -> None:
    """⚠️ 모듈 수준 import 를 두면 VLM 을 안 쓰는 PC 에서 런타임이 통째로 죽는다."""
    import sys

    source = (
        sys.modules["host.vision.vlm_session"].__loader__.get_source("host.vision.vlm_session")
        or ""
    )
    head = source.split("class QwenVlSession", 1)[0]
    for banned in ("\nimport torch", "\nfrom transformers", "\nimport transformers"):
        assert banned not in head, f"모듈 수준에서 {banned.strip()} 를 하고 있다"


def test_factory_is_none_when_a_dependency_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """없으면 «없음» 으로 간다 — 예외가 아니다 (Tier 3)."""
    monkeypatch.setattr(vlm_session, "missing_packages", lambda: ("transformers",))
    config = {"vision": {"vlm": {"model_id": "x", "budget_ms": 3000}}}
    assert build_session_factory(config) is None


def test_factory_is_none_without_a_vlm_section() -> None:
    assert build_session_factory({"vision": {}}) is None
    assert build_session_factory({}) is None


def test_factory_does_not_load_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 팩토리를 만드는 것과 5.5초짜리 적재는 다른 일이다."""
    monkeypatch.setattr(vlm_session, "missing_packages", lambda: ())
    made: list[str] = []

    class Spy:
        def __init__(self, model_id: str, *, max_new_tokens: int = 32) -> None:  # noqa: ARG002
            made.append(model_id)

    monkeypatch.setattr(vlm_session, "QwenVlSession", Spy)
    config = {"vision": {"vlm": {"model_id": "some/model", "max_new_tokens": 8}}}
    factory = build_session_factory(config)
    assert factory is not None
    assert made == [], "팩토리를 만드는 것만으로 모델이 올라갔다"
    factory()
    assert made == ["some/model"]


def test_required_packages_include_torchvision() -> None:
    """⚠️ 빠지면 모델이 아니라 «프로세서» 를 만들 때 터진다 — 원인을 찾기 어렵다."""
    assert "torchvision" in REQUIRED_PACKAGES


# ── 이미지 맞추기 ───────────────────────────────────────────


def _jpeg_bytes() -> bytes:
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", (8, 6), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def test_jpeg_bytes_become_an_rgb_image() -> None:
    """런타임은 구역 프레임의 JPEG 바이트를 그대로 넘긴다 (`result.jpeg`)."""
    pytest.importorskip("PIL")
    image = to_image(_jpeg_bytes())
    assert image.mode == "RGB"
    assert image.size == (8, 6)


def test_a_bgr_array_is_flipped_to_rgb() -> None:
    """⚠️ 뒤집지 않으면 빨강과 파랑이 바뀐 사진을 모델에게 준다."""
    pytest.importorskip("PIL")
    np = pytest.importorskip("numpy")
    bgr = np.zeros((2, 2, 3), dtype=np.uint8)
    bgr[:, :, 0] = 255  # BGR 의 B
    image = to_image(bgr)
    assert image.getpixel((0, 0)) == (0, 0, 255), "BGR→RGB 변환이 빠졌다"


def test_unknown_payload_is_refused() -> None:
    pytest.importorskip("PIL")
    with pytest.raises(TypeError, match="판독에 넣을 수 없는"):
        to_image(object())
