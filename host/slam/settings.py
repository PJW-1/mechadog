"""LiDAR 설정 적재와 파라미터 조립 (NFR-3① · Phase 2).

**값의 정본은 `config/config.yaml` 의 `lidar:` 절이다.** 별 파일을 두지 않는다 —
*"전 상수를 `config.yaml` 로 분리"* 가 이 저장소의 원칙이고(README 엔지니어링
원칙), 설정이 두 파일로 갈리면 **어느 쪽이 실제로 쓰이는지 코드를 읽어야
알게 된다.**

⚠️ **기존 로더를 우회하지 않는다.** `host.common.config.load_config` 를 그대로
부르므로 개체 프로파일 병합과 스키마 검증이 전부 그대로 걸린다. 여기서 직접
YAML 을 읽어 쓰면 검증을 지나치는 두 번째 경로가 생긴다.

⚠️ **`lidar` 절은 `common/config.py` 의 `REQUIRED_SECTIONS` 에 없다.** 그래서
그 검증은 이 절의 누락을 잡지 못하고, 아래 `_validate` 가 대신 본다. 절을
`REQUIRED_SECTIONS` 에 넣으면 LiDAR 를 쓰지 않는 Phase 1 운용까지 기동을
막으므로 그렇게 하지 않았다 (`localization.track: none` 이 정상 상태다).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from host.behavior.planner import PlanParams
from host.common.config import ConfigError, load_base_config, load_config
from host.common.units import deg_to_rad
from host.slam.scan_match import MatchParams

ROOT = Path(__file__).resolve().parents[2]

#: 값의 정본. 시험이 파일을 직접 읽어 교차 검증할 때도 이 경로를 쓴다.
CONFIG_PATH = ROOT / "config" / "config.yaml"

#: 지도·구역 산출물이 사는 곳. **커밋하지 않는다** (`.gitignore` 49행).
DEFAULT_MAPS_DIR = ROOT / "maps"

#: `localization.track` 이 이 값일 때만 실기 모드로 기동한다 (ADR-18).
REQUIRED_TRACK = "lidar"

REQUIRED_LIDAR_KEYS = (
    "scan_port",
    "scan_stall_timeout_ms",
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
    "tracking_margin_mm",
    "path_simplify_mm",
    "waypoint_radius_mm",
    "heading_tolerance_deg",
    "reverse_threshold_deg",
    "new_obstacle_check_radius_mm",
    "new_obstacle_margin_mm",
    "new_obstacle_confirmations",
    "obstacle_mark_radius_mm",
    "estop_distance_mm",
    "forward_fan_deg",
)


def load(device_id: str | None = None) -> dict[str, Any]:
    """전역 설정을 읽고 `lidar` 절을 검증해 돌려준다.

    `device_id` 를 주면 개체 프로파일까지 병합한다 (실기 운용). 없으면
    시뮬레이션·시험용 기본 설정만 읽는다 — `load_base_config` 가 그 용도로
    이미 있다.
    """
    config = load_base_config() if device_id is None else load_config(device_id)
    section = config.get("lidar")
    if not isinstance(section, dict):
        raise ConfigError("config.yaml 에 lidar 절이 없음 — FR-6·FR-7 은 이 절을 요구한다")
    validate_section(section)
    return config


def read_lidar_section(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """설정 파일에서 `lidar` 절만 읽는다. **시험의 교차 검증용이다.**

    운용 경로는 `load()` 를 쓴다 — 그쪽은 개체 프로파일 병합과 기존 스키마
    검증을 함께 거친다.
    """
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("lidar"), dict):
        raise ConfigError(f"최상위에 lidar 매핑이 있어야 함: {path}")
    return loaded["lidar"]


def validate_section(section: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_LIDAR_KEYS if key not in section]
    if missing:
        raise ConfigError(f"lidar 설정 누락: {missing}")
    if section["range_min_mm"] >= section["range_max_mm"]:
        raise ConfigError("range_min_mm 이 range_max_mm 보다 작아야 함")
    if section["free_logodds"] >= section["occupied_logodds"]:
        raise ConfigError("free_logodds 가 occupied_logodds 보다 작아야 함")
    if section["hit_logodds"] <= 0 or section["miss_logodds"] >= 0:
        raise ConfigError("hit_logodds 는 양수, miss_logodds 는 음수여야 함")
    if section["resolution_mm"] <= 0 or section["initial_span_cells"] <= 0:
        raise ConfigError("resolution_mm · initial_span_cells 는 0보다 커야 함")

    # ⚠️ **경로가 지나갈 자리가 E-STOP 거리 안이면 안 된다.**
    #
    # 팽창(반경 + 추종 여유)이 E-STOP 거리보다 크지 않으면, 계획된 경로를
    # 완벽히 따라 걷는 것만으로 LiDAR 가 E-STOP 거리를 읽어 **정상 순찰이
    # 비상정지로 끝난다.** 값 하나하나는 그럴듯해 보이므로 사람이 검토로
    # 잡기 어렵다 — 그래서 기동 시에 관계를 확인한다.
    clearance = section["robot_radius_mm"] + section["tracking_margin_mm"]
    if clearance <= section["estop_distance_mm"]:
        raise ConfigError(
            f"robot_radius_mm + tracking_margin_mm ({clearance}) 이 "
            f"estop_distance_mm ({section['estop_distance_mm']}) 보다 커야 함 — "
            "그렇지 않으면 계획된 경로가 E-STOP 거리를 지나간다"
        )


def require_lidar_track(config: dict[str, Any], *, simulation: bool) -> None:
    """실기 모드에서 `localization.track` 이 `lidar` 인지 확인한다 (ADR-18).

    **Phase 1 표준 구성에는 LiDAR 가 달려 있지 않다** (CONTRIBUTING 1절). 설정이
    `none` 인 채로 실기 순찰을 돌리면 LiDAR 없는 기체에서 스캔을 기다리다
    측위 상실로 정지한다 — 그때 원인을 찾기 어려우므로 기동 시점에 막는다.

    시뮬레이션은 막지 않는다. 알고리즘 검증은 하드웨어 구성과 무관하고,
    막으면 Phase 1 기간 내내 개발을 못 한다.
    """
    track = config.get("localization", {}).get("track")
    if simulation or track == REQUIRED_TRACK:
        return
    raise ConfigError(
        f"localization.track 이 {track!r} 이다 — 실기 LiDAR 운용에는 "
        f"{REQUIRED_TRACK!r} 여야 한다 (Phase 2 · ADR-18). "
        "알고리즘만 확인하려면 --simulate 를 쓴다"
    )


def maps_dir(config: dict[str, Any]) -> Path:
    """지도 산출물 경로. 설정에 없으면 저장소의 `maps/` 다."""
    configured = config.get("lidar", {}).get("maps_dir")
    return Path(configured) if configured else DEFAULT_MAPS_DIR


# ══════════════════════════════════════════════════════════════
#  파라미터 조립 — 숫자는 코드에 박지 않는다 (NFR-3①)
#
#  ⚠️ **없는 키를 기본값으로 때우지 않는다.** `fsm.py._lookup` 이 같은 이유로
#     `KeyError` 를 그대로 올린다 — 기본값을 두면 설정에서 항목을 지워도 동작이
#     그대로라 설정이 정본이 아니게 된다.
# ══════════════════════════════════════════════════════════════


def plan_params_from_config(config: Mapping[str, Any]) -> PlanParams:
    lidar = config["lidar"]
    return PlanParams(
        occ_thresh=float(lidar["occupied_logodds"]),
        free_thresh=float(lidar["free_logodds"]),
        # 반경 + 추종 여유. 둘을 더해 두는 이유는 `PlanParams.clearance_m` 주석에.
        clearance_m=(float(lidar["robot_radius_mm"]) + float(lidar["tracking_margin_mm"])) / 1000.0,
        simplify_eps_m=float(lidar["path_simplify_mm"]) / 1000.0,
    )


def match_params_from_config(config: Mapping[str, Any]) -> MatchParams:
    lidar = config["lidar"]
    localization = config["localization"]
    # 탐색 범위의 근거는 **한 사이클의 이동량**이다 (`move_increment_mm`).
    # 그보다 좁으면 정상 이동을 따라잡지 못하고, 넓으면 탐색이 제곱으로 커진다.
    span_m = float(localization["move_increment_mm"]) / 1000.0 * float(lidar["search_span_ratio"])
    return MatchParams(
        search_lin_m=span_m,
        search_lin_step_m=float(lidar["search_step_mm"]) / 1000.0,
        search_ang_rad=deg_to_rad(float(lidar["search_angle_deg"])),
        search_ang_step_rad=deg_to_rad(float(lidar["search_angle_step_deg"])),
        occ_thresh=float(lidar["occupied_logodds"]),
        min_known_cells=int(lidar["min_known_cells"]),
    )


def range_from_config(config: Mapping[str, Any]) -> tuple[float, float]:
    lidar = config["lidar"]
    return (
        float(lidar["range_min_mm"]) / 1000.0,
        float(lidar["range_max_mm"]) / 1000.0,
    )
