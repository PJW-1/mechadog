"""펌웨어의 안전 상수와 `config.yaml` 의 `safety` 절이 어긋나지 않게 잠근다 (FR-1.3 · FR-1.5 · FR-2.2 · NFR-2.3).

안전 판정은 ESP32 가 혼자 한다(아키텍처 1.2). 그래서 같은 값이 두 곳에 있다 —
스케치의 `constexpr` 상수(실제로 쓰이는 값)와 `config.yaml`(호스트 검증·문서·대시보드가 읽는 값).
한쪽만 고치면 호스트가 «7cm 에서 멈춘다» 고 믿는 동안 로봇은 다른 거리에서 멈춘다.
이 시험은 스케치 소스를 읽어 두 값이 같은지 본다.

`src/safety_monitor.h` 의 `SafetyThresholds` 기본값(25cm 등)은 PC 단위 시험용이고
스케치가 덮어쓰므로 대조 대상이 아니다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.yaml"
SKETCH = ROOT / "firmware" / "mechdog_motion" / "mechdog_motion.ino"

# 스케치 상수 이름 → `config.yaml` `safety` 키
PAIRS = {
    "kCommandTimeoutMs": "cmd_timeout_ms",
    "kLinkHealthyAgeMs": "link_loss_failsafe_ms",
    "kBatteryWarningV": "battery_warn_v",
    "kBatteryShutdownV": "battery_shutdown_v",
    "kObstacleStopCm": "obstacle_stop_cm",
}


def sketch_constant(name: str) -> float:
    pattern = rf"constexpr\s+\w+\s+{name}\s*=\s*([0-9.]+)f?\s*;"
    match = re.search(pattern, SKETCH.read_text(encoding="utf-8"))
    assert match is not None, f"스케치에서 {name} 를 찾지 못했다"
    return float(match.group(1))


def config_safety() -> dict[str, float]:
    section = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["safety"]
    return {key: float(section[key]) for key in PAIRS.values()}


@pytest.mark.parametrize(("constant", "key"), PAIRS.items())
def test_sketch_safety_constant_matches_config(constant: str, key: str) -> None:
    assert sketch_constant(constant) == pytest.approx(config_safety()[key]), (
        f"펌웨어 {constant} 와 config.yaml safety.{key} 가 다르다 — 둘을 함께 고친다"
    )


def test_obstacle_clear_distance_is_beyond_stop_distance() -> None:
    """해제 거리가 정지 거리보다 멀어야 임계 부근에서 상태가 떨리지 않는다."""
    assert sketch_constant("kObstacleClearCm") > sketch_constant("kObstacleStopCm")
