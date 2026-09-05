"""전역 설정과 MechDog 개체 프로파일을 읽고 시작 전에 검증한다."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"
DEFAULT_DEVICES_DIR = ROOT / "config" / "devices"

REQUIRED_SECTIONS = (
    "network",
    "safety",
    "gait",
    "fsm",
    "vision",
    "localization",
    "escalation",
    "auth",
    "posture",
    "change_detect",
    "zones",
    "logging",
)
REQUIRED_SAFETY_KEYS = (
    "cmd_timeout_ms",
    "link_loss_failsafe_ms",
    "obstacle_stop_cm",
    "battery_warn_v",
    "battery_shutdown_v",
    "tip_angle_deg",
    "tip_duration_ms",
)


class ConfigError(ValueError):
    """설정 누락이나 범위 오류 때문에 안전하게 기동할 수 없음."""


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"설정 파일 없음: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 파싱 실패: {path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"설정 최상위는 매핑이어야 함: {path}")
    return loaded


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _finite_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _require_positive(mapping: dict[str, Any], key: str) -> None:
    value = mapping.get(key)
    if not _finite_number(value) or value <= 0:
        raise ConfigError(f"{key} 는 0보다 큰 유한한 수여야 함")


def validate_base_config(config: dict[str, Any]) -> None:
    if config.get("profile") not in {"dev", "prod"}:
        raise ConfigError("profile 은 dev 또는 prod 여야 함")
    missing = [
        section for section in REQUIRED_SECTIONS if not isinstance(config.get(section), dict)
    ]
    if missing:
        raise ConfigError(f"필수 설정 섹션 누락: {missing}")

    network = config["network"]
    for name in ("cmd_port", "telemetry_port"):
        port = network.get(name)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"network.{name} 는 1~65535 정수여야 함")
    for name in ("cmd_rate_hz", "telemetry_rate_hz"):
        _require_positive(network, name)

    safety = config["safety"]
    missing_safety = [name for name in REQUIRED_SAFETY_KEYS if name not in safety]
    if missing_safety:
        raise ConfigError(f"필수 안전 설정 누락: {missing_safety}")
    for name in REQUIRED_SAFETY_KEYS:
        _require_positive(safety, name)
    if safety["cmd_timeout_ms"] > 300:
        raise ConfigError("safety.cmd_timeout_ms 는 300ms 이하여야 함")
    if safety["cmd_timeout_ms"] >= safety["link_loss_failsafe_ms"]:
        raise ConfigError("명령 정지가 링크 페일세이프보다 먼저 동작해야 함")
    if safety["battery_shutdown_v"] >= safety["battery_warn_v"]:
        raise ConfigError("배터리 셧다운 전압은 경고 전압보다 낮아야 함")

    providers = config["vision"].get("providers")
    if not isinstance(providers, list) or not providers:
        raise ConfigError("vision.providers 가 비어 있음")
    if providers[-1] != "CPUExecutionProvider":
        raise ConfigError("vision.providers 마지막은 CPUExecutionProvider 여야 함")


def validate_device_config(config: dict[str, Any], device_id: str) -> None:
    if config.get("device_id") != device_id:
        raise ConfigError(
            f"개체 프로파일 device_id 불일치: 요청={device_id!r}, 파일={config.get('device_id')!r}"
        )
    if config.get("reference_role") not in {"phase1", "phase2", "none"}:
        raise ConfigError("reference_role 은 phase1, phase2, none 중 하나여야 함")

    offsets = config.get("servo_offset")
    if (
        not isinstance(offsets, list)
        or len(offsets) != 9
        or not all(_finite_number(value) for value in offsets)
    ):
        raise ConfigError("servo_offset 은 유한한 수 9개여야 함")

    network = config["network"]
    for name in ("cmd_port", "telemetry_port"):
        port = network.get(name)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"network.{name} 는 1~65535 정수여야 함")

    if config["profile"] == "prod":
        if not isinstance(config.get("owner_id"), str) or config["owner_id"] in {
            "",
            "unassigned",
        }:
            raise ConfigError("prod 프로파일은 실제 owner_id 가 필요함")
        calibration = config.get("gait_calibration")
        if not isinstance(calibration, dict):
            raise ConfigError("prod 프로파일은 보행 캘리브레이션 실측값이 필요함")
        for name in ("forward_mm_per_sec", "turn_deg_per_sec"):
            value = calibration.get(name)
            if not _finite_number(value) or value <= 0:
                raise ConfigError(f"prod gait_calibration.{name} 실측값이 필요함")
        if not isinstance(calibration.get("measured_on"), str) or not calibration["measured_on"]:
            raise ConfigError("prod gait_calibration.measured_on 기록이 필요함")


def load_base_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """개체 정보가 필요 없는 도구·테스트용 전역 설정 로더."""
    config = _read_mapping(path)
    validate_base_config(config)
    return config


def load_config(
    device_id: str | None,
    *,
    config_path: Path = DEFAULT_CONFIG,
    devices_dir: Path = DEFAULT_DEVICES_DIR,
) -> dict[str, Any]:
    """전역 설정에 ``<device>.yaml``과 선택적 로컬 덮어쓰기를 병합한다."""
    if not isinstance(device_id, str) or not device_id.strip():
        raise ConfigError("--device <unit-id> 를 지정해야 함")
    if Path(device_id).name != device_id:
        raise ConfigError("device_id 에 경로 문자를 사용할 수 없음")

    config = load_base_config(config_path)
    profile_path = devices_dir / f"{device_id}.yaml"
    config = _merge(config, _read_mapping(profile_path))

    local_path = devices_dir / f"{device_id}.local.yaml"
    if local_path.is_file():
        config = _merge(config, _read_mapping(local_path))

    validate_base_config(config)
    validate_device_config(config, device_id)
    return config
