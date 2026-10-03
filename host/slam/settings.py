"""LiDAR 설정 적재와 파라미터 조립 (NFR-3① · Phase 2).

값의 정본은 `config/config.yaml` 의 `lidar:` 절이다. 적재는 `host.common.config.load_config`
를 거쳐 개체 병합과 스키마 검증을 그대로 받는다. `lidar` 절은 Phase 1 에서 없을 수 있어
`REQUIRED_SECTIONS` 에 넣지 않고, 절의 검증은 `_validate` 가 한다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from host.behavior.planner import PlanParams
from host.common.config import ConfigError, load_base_config, load_config
from host.common.units import deg_to_rad
from host.slam.scan_match import MatchParams

ROOT = Path(__file__).resolve().parents[2]

#: 값의 정본. 시험이 파일을 직접 읽어 교차 검증할 때도 이 경로를 쓴다.
CONFIG_PATH = ROOT / "config" / "config.yaml"

#: 지도·구역 산출물이 사는 곳 (커밋하지 않는다).
DEFAULT_MAPS_DIR = ROOT / "maps"

#: `localization.track` 이 이 값일 때만 실기 모드로 기동한다 (ADR-18).
REQUIRED_TRACK = "lidar"

REQUIRED_LIDAR_KEYS = (
    "scan_port",
    "scan_stall_timeout_ms",
    "scan_forward_host",
    "scan_forward_port",
    "scan_forward_enabled",
    "range_min_mm",
    "range_max_mm",
    "scan_batch",
    "resolution_mm",
    "initial_span_cells",
    "expand_pad_cells",
    "hit_logodds",
    "miss_logodds",
    "occupied_logodds",
    "free_logodds",
    "search_span_ratio",
    "search_step_mm",
    "search_angle_deg",
    "search_angle_step_deg",
    "min_known_cells",
    "robot_radius_mm",
    "start_escape_max_mm",
    "tracking_margin_mm",
    "path_simplify_mm",
    "waypoint_radius_mm",
    "heading_tolerance_deg",
    "reverse_threshold_deg",
    "spin_threshold_deg",
    "spin_turn_deg",
    "new_obstacle_check_radius_mm",
    "new_obstacle_margin_mm",
    "new_obstacle_confirmations",
    "obstacle_mark_radius_mm",
    "estop_distance_mm",
    "forward_fan_deg",
    "odom_host",
    "odom_port",
    "odom_rate_hz",
    "odom_imu_stale_ms",
    "map_pose_port",
)

#: 숫자 검사에서 빼는 키 — 문자열·참거짓이거나, 포트처럼 아래에서 범위까지 따로 본다.
_TYPED_SEPARATELY = frozenset(
    {
        "scan_port",
        "scan_forward_host",
        "scan_forward_enabled",
        "scan_forward_port",
        "odom_host",
        "odom_port",
    }
)


def load(device_id: str | None = None) -> dict[str, Any]:
    """전역 설정을 읽고 `lidar` 절을 검증해 돌려준다.

    `device_id` 를 주면 개체 프로파일까지 병합한다(실기). 없으면 기본 설정만 읽는다.
    """
    config = load_base_config() if device_id is None else load_config(device_id)
    section = config.get("lidar")
    if not isinstance(section, dict):
        raise ConfigError("config.yaml 에 lidar 절이 없음 — FR-6·FR-7 은 이 절을 요구한다")
    validate_section(section)
    return config


def read_lidar_section(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """설정 파일에서 `lidar` 절만 읽는다 — 시험의 교차 검증용이다(운용은 `load()`)."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("lidar"), dict):
        raise ConfigError(f"최상위에 lidar 매핑이 있어야 함: {path}")
    return cast(dict[str, Any], loaded["lidar"])


def validate_section(section: dict[str, Any]) -> None:
    if not isinstance(section.get("global_full_scan_ambiguity", False), bool):
        raise ConfigError("lidar.global_full_scan_ambiguity must be true or false")
    missing = [key for key in REQUIRED_LIDAR_KEYS if key not in section]
    if missing:
        raise ConfigError(f"lidar 설정 누락: {missing}")
    # 아래 비교가 문자열·빈 값에서 `TypeError` 로 새지 않게 숫자부터 확인한다. NaN 은 모든
    # 대소 비교가 거짓이라 범위 검사를 조용히 통과하므로 유한한 수만 받는다.
    for key in REQUIRED_LIDAR_KEYS:
        if key in _TYPED_SEPARATELY:
            continue
        value = section[key]
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ConfigError(f"lidar.{key} 는 유한한 숫자여야 함: {value!r}")
    scan_port = section["scan_port"]
    if not isinstance(scan_port, int) or isinstance(scan_port, bool) or not 1 <= scan_port <= 65535:
        raise ConfigError("lidar.scan_port 는 1~65535 정수여야 함")
    if section["range_min_mm"] >= section["range_max_mm"]:
        raise ConfigError("range_min_mm 이 range_max_mm 보다 작아야 함")
    if section["free_logodds"] >= section["occupied_logodds"]:
        raise ConfigError("free_logodds 가 occupied_logodds 보다 작아야 함")
    if section["hit_logodds"] <= 0 or section["miss_logodds"] >= 0:
        raise ConfigError("hit_logodds 는 양수, miss_logodds 는 음수여야 함")
    if section["resolution_mm"] <= 0 or section["initial_span_cells"] <= 0:
        raise ConfigError("resolution_mm · initial_span_cells 는 0보다 커야 함")
    # 전달 목적지가 수신 포트와 같으면 두 스키마가 한 소켓에 섞인다 (WBS 5.4.4).
    port = section["scan_forward_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("lidar.scan_forward_port 는 1~65535 정수여야 함")
    if port == section["scan_port"]:
        raise ConfigError(f"lidar.scan_forward_port 가 scan_port({section['scan_port']}) 와 같음")
    host = section["scan_forward_host"]
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("lidar.scan_forward_host 는 비어 있지 않은 문자열이어야 함")
    if not isinstance(section["scan_forward_enabled"], bool):
        raise ConfigError("lidar.scan_forward_enabled 는 true 또는 false 여야 함")

    # ODOM 은 `scan_forward_port` 형식을 본 뒤에 검사한다 — 오류가 제 이름으로 나오게.
    odom_port = section["odom_port"]
    if not isinstance(odom_port, int) or isinstance(odom_port, bool) or not 1 <= odom_port <= 65535:
        raise ConfigError("lidar.odom_port 는 1~65535 정수여야 함")
    odom_host = section["odom_host"]
    if not isinstance(odom_host, str) or not odom_host.strip():
        raise ConfigError("lidar.odom_host 는 비어 있지 않은 문자열이어야 함")
    # 같은 포트면 컨테이너가 스캔과 ODOM 을 한 소켓에서 받아 서로를 «모르는 타입» 으로 버린다.
    if section["odom_port"] in (section["scan_port"], section["scan_forward_port"]):
        raise ConfigError("odom_port 는 scan_port · scan_forward_port 와 달라야 함")
    if section["odom_rate_hz"] <= 0 or section["odom_imu_stale_ms"] <= 0:
        raise ConfigError("odom_rate_hz · odom_imu_stale_ms 는 0보다 커야 함")

    # 제자리 회전 임계는 직진 허용 오차보다 커야 한다 — 아니면 허용 오차 밖이 전부 회전이라
    # 호 조향 구간이 사라지고, 같거나 작으면 정렬을 마친 직후 다시 돈다 (ADR-11 개정).
    if not section["heading_tolerance_deg"] < section["spin_threshold_deg"] <= 180:
        raise ConfigError("heading_tolerance_deg < spin_threshold_deg <= 180 이어야 함")
    if not 0 < section["spin_turn_deg"] <= 30:
        raise ConfigError("spin_turn_deg 는 0 초과 30 이하여야 함 (MOVE angle 규약 상한)")
    map_pose_port = section["map_pose_port"]
    if (
        not isinstance(map_pose_port, int)
        or isinstance(map_pose_port, bool)
        or not 1 <= map_pose_port <= 65535
    ):
        raise ConfigError("lidar.map_pose_port 는 1~65535 정수여야 함")
    if map_pose_port in (section["scan_port"], section["scan_forward_port"], odom_port, 5202):
        raise ConfigError("map_pose_port 는 scan · forward · odom · live-map 포트와 달라야 함")

    # 팽창(반경 + 추종 여유)이 E-STOP 거리보다 커야 한다 — 아니면 정상 추종이 비상정지로 끝난다.
    clearance = section["robot_radius_mm"] + section["tracking_margin_mm"]
    if section["robot_radius_mm"] <= 0 or section["start_escape_max_mm"] <= 0:
        raise ConfigError("robot_radius_mm · start_escape_max_mm 는 양수여야 함")
    if clearance <= section["estop_distance_mm"]:
        raise ConfigError(
            f"robot_radius_mm + tracking_margin_mm ({clearance}) 이 "
            f"estop_distance_mm ({section['estop_distance_mm']}) 보다 커야 함 — "
            "그렇지 않으면 계획된 경로가 E-STOP 거리를 지나간다"
        )


def require_lidar_track(config: dict[str, Any], *, simulation: bool) -> None:
    """실기 모드에서 `localization.track` 이 `lidar` 인지 확인한다 (ADR-18).

    아니면 기동 시점에 막는다(Phase 1 표준 구성에는 LiDAR 가 없다). 시뮬레이션은 막지 않는다.
    """
    track = config.get("localization", {}).get("track")
    if simulation or track == REQUIRED_TRACK:
        return
    raise ConfigError(
        f"localization.track 이 {track!r} 이다 — 실기 LiDAR 운용에는 "
        f"{REQUIRED_TRACK!r} 여야 한다 (Phase 2 · ADR-18). "
        "알고리즘만 확인하려면 --simulate 를 쓴다"
    )


def maps_dir(config: Mapping[str, Any]) -> Path:
    """지도 산출물 경로. 설정에 없으면 저장소의 `maps/` 다."""
    configured = config.get("lidar", {}).get("maps_dir")
    return Path(configured) if configured else DEFAULT_MAPS_DIR


# ══════════════════════════════════════════════════════════════
#  파라미터 조립 — 숫자는 코드에 박지 않는다 (NFR-3①)
#
#  없는 키는 기본값으로 때우지 않고 `KeyError` 로 올린다.
# ══════════════════════════════════════════════════════════════


def plan_params_from_config(config: Mapping[str, Any]) -> PlanParams:
    lidar = config["lidar"]
    return PlanParams(
        occ_thresh=float(lidar["occupied_logodds"]),
        free_thresh=float(lidar["free_logodds"]),
        # 반경 + 추종 여유. 둘을 더해 두는 이유는 `PlanParams.clearance_m` 주석에.
        clearance_m=(float(lidar["robot_radius_mm"]) + float(lidar["tracking_margin_mm"])) / 1000.0,
        simplify_eps_m=float(lidar["path_simplify_mm"]) / 1000.0,
        # 벽·충분히 확인된 장애물(logodds ≥ 이 값)만 전체 여유를 부풀린다.
        hard_thresh=float(lidar["hard_occ_thresh"]),
        # 그 미만의 셀(세션 지도 병합으로 들어온 가구 다리급)은 이 여유만 —
        # 기본은 로봇 반경: 다리를 피해 갈 수는 있되 몸이 닿지는 않는다.
        soft_clearance_m=float(lidar["furniture_clearance_mm"]) / 1000.0,
        body_radius_m=float(lidar["robot_radius_mm"]) / 1000.0,
        start_escape_max_m=float(lidar["start_escape_max_mm"]) / 1000.0,
    )


def match_params_from_config(config: Mapping[str, Any]) -> MatchParams:
    lidar = config["lidar"]
    localization = config["localization"]
    # 탐색 범위는 한 사이클의 이동량(`move_increment_mm`)에 비례한다.
    span_m = float(localization["move_increment_mm"]) / 1000.0 * float(lidar["search_span_ratio"])
    return MatchParams(
        search_lin_m=span_m,
        search_lin_step_m=float(lidar["search_step_mm"]) / 1000.0,
        search_ang_rad=deg_to_rad(float(lidar["search_angle_deg"])),
        search_ang_step_rad=deg_to_rad(float(lidar["search_angle_step_deg"])),
        occ_thresh=float(lidar["occupied_logodds"]),
        min_known_cells=int(lidar["min_known_cells"]),
        sigma_m=float(lidar.get("match_sigma_mm", 0)) / 1000.0,
    )


def range_from_config(config: Mapping[str, Any]) -> tuple[float, float]:
    lidar = config["lidar"]
    return (
        float(lidar["range_min_mm"]) / 1000.0,
        float(lidar["range_max_mm"]) / 1000.0,
    )
