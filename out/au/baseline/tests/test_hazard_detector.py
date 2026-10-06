"""화기 위험물 검출 확정 규칙 — `HazardDetector` (ADR-43 대안 ⓐ 개정).

PPE 위반(`PpeDetector`)과 같은 창·횟수 규칙인지, 금지 대상만 세는지, 끄면 잊는지를 본다.
모델은 쓰지 않는다 — 검출기는 주입한 대역이다.
"""

from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest

from host.common.config import ConfigError, load_config, validate_base_config
from host.vision.detector import Detection
from host.vision.hazard_detector import HAZARD_CLASSES, HazardDetector

IMAGE = np.zeros((48, 64, 3), dtype=np.uint8)


@pytest.fixture
def cfg() -> dict:
    return load_config("mechdog-01")


class _Scripted:
    """프레임마다 정해 둔 검출을 돌려준다."""

    def __init__(self) -> None:
        self.labels: tuple[str, ...] = ()
        self.opened = 0

    def open(self) -> None:
        self.opened += 1

    def detect(self, _image) -> list[Detection]:
        return [Detection(label, 0.9, (1.0, 2.0, 30.0, 40.0)) for label in self.labels]


def _detector(cfg: dict, **spec) -> tuple[HazardDetector, _Scripted]:
    config = deepcopy(cfg)
    config["vision"]["hazard"].update(spec)
    scripted = _Scripted()
    return HazardDetector(config, scripted), scripted


def test_the_shipped_settings_match_the_ppe_confirm_rule(cfg: dict) -> None:
    """시작값은 PPE 위반과 같은 창·횟수다 — 둘을 같은 잣대로 설명할 수 있게."""
    hazard, ppe = cfg["vision"]["hazard"], cfg["vision"]["ppe"]
    assert hazard["confirm_window_ms"] == ppe["violation_window_ms"]
    assert hazard["hits_required"] == ppe["violation_hits_required"]
    assert tuple(hazard["classes"]) == HAZARD_CLASSES
    assert hazard["enabled"] is True


def test_one_frame_is_not_a_hazard(cfg: dict) -> None:
    """한 프레임의 검출은 확정이 아니다 — `hits_required` 번이 창 안에 있어야 한다."""
    detector, scripted = _detector(cfg)
    scripted.labels = ("lighter",)
    assert detector.observe(IMAGE, 1000).confirmed == ()
    assert detector.observe(IMAGE, 1100).confirmed == ()
    verdict = detector.observe(IMAGE, 1200)
    assert verdict.confirmed == ("lighter",)
    assert [d.label for d in verdict.detections] == ["lighter"]


def test_hits_outside_the_window_do_not_add_up(cfg: dict) -> None:
    """창(1500ms)보다 넓게 흩어진 검출은 쌓이지 않는다 — PPE 위반 창과 같다."""
    detector, scripted = _detector(cfg)
    scripted.labels = ("powerbank",)
    for now in (0, 1000, 2000, 3000):
        assert detector.observe(IMAGE, now).confirmed == ()


def test_only_forbidden_objects_are_counted(cfg: dict) -> None:
    """금지 대상에서 뺀 물건은 박스도 확정도 없다 — 팀이 고르는 값이다."""
    detector, scripted = _detector(cfg, alarm_classes=["powerbank"])
    scripted.labels = ("lighter", "powerbank")
    for now in (0, 100, 200):
        verdict = detector.observe(IMAGE, now)
    assert verdict.confirmed == ("powerbank",)
    assert [d.label for d in verdict.detections] == ["powerbank"]


def test_reset_forgets_hits_counted_elsewhere(cfg: dict) -> None:
    """끌 때 창을 비운다 — 앞 방문에서 센 것이 다음 방문의 확정을 앞당기지 않게."""
    detector, scripted = _detector(cfg)
    scripted.labels = ("lighter",)
    detector.observe(IMAGE, 0)
    detector.observe(IMAGE, 100)
    detector.reset()
    assert detector.observe(IMAGE, 200).confirmed == ()


def test_confirmed_items_keep_the_model_order(cfg: dict) -> None:
    """기록·방송 문장이 매번 같은 순서로 나온다."""
    detector, scripted = _detector(cfg, alarm_classes=["powerbank", "lighter"])
    scripted.labels = ("powerbank", "lighter")
    for now in (0, 100, 200):
        verdict = detector.observe(IMAGE, now)
    assert verdict.confirmed == HAZARD_CLASSES


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("alarm_classes", ["knife"], "부분집합"),
        ("alarm_classes", [], "비어 있지 않은"),
        ("hits_required", 0, "양의 정수"),
        ("confirm_window_ms", 1.5, "양의 정수"),
        ("enabled", "yes", "true 또는 false"),
        ("conf_threshold", 0, "0 초과 1 이하"),
    ],
)
def test_bad_hazard_settings_are_refused(cfg: dict, key: str, value, message: str) -> None:
    """모델이 모르는 금지 대상을 적으면 그 물건은 조용히 한 번도 경고되지 않는다 — 기동 전에 막는다."""
    broken = deepcopy(cfg)
    broken["vision"]["hazard"][key] = value
    with pytest.raises(ConfigError, match=message):
        validate_base_config(broken)


def test_a_config_without_the_hazard_section_is_valid(cfg: dict) -> None:
    """절이 없으면 기능이 꺼진 것이다 — 예전 설정도 그대로 기동한다."""
    trimmed = deepcopy(cfg)
    del trimmed["vision"]["hazard"]
    validate_base_config(trimmed)
