"""길 찾기 파라미터 — `lidar:` 절에서 오는 경로 계획(`PlanParams`)과 `nav:` 절(`NavParams`).

행동 계층(`host.behavior.planner`·`live_nav`)이 쓰고, 설정 적재·검증(`host.slam.settings`·
`host.common.config`)도 만들어 보므로 아래 계층에 둔다. 아래 계층은 `host.behavior` 를
가져오지 않는다 (`tests/test_layering.py`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from host.common.config import ConfigError


@dataclass(frozen=True, slots=True)
class PlanParams:
    """계획 파라미터. 로봇 반경만 빼면 전부 `config.yaml` 의 `lidar:` 절에서 온다."""

    occ_thresh: float
    free_thresh: float
    #: 경로 중심선이 장애물에서 유지할 거리 = 로봇 반경 + 추종 여유. 호스트 LiDAR E-STOP
    #: 거리보다 커야 정상 추종이 비상정지로 끝나지 않는다 (`settings._validate` 가 확인한다).
    clearance_m: float
    simplify_eps_m: float
    #: 이 값 이상의 점유 셀(원본 벽·충분히 확인된 장애물)은 `clearance_m` 전부를 부풀린다.
    #: 병합으로 들어온 가구 다리급 셀(occ_thresh 이상 ~ 이 값 미만)은
    #: `soft_clearance_m` 만 부풀린다 — 로봇이 실제로 섰던 자리 옆의 다리까지
    #: 도달 불가로 만들지 않기 위해서다. 라이브 적분으로 다시 확인되면 값이
    #: 올라 저절로 단단한 층이 된다.
    hard_thresh: float = 4.0
    soft_clearance_m: float = 0.15
    #: 추종 여유 안에서 탈출할 때도 지켜야 하는 몸체 반경. config의 실측 반경을 사용한다.
    body_radius_m: float = 0.15
    start_escape_max_m: float = 0.6
    inflation_radius_m: float = 0.55
    cost_scaling_factor: float = 3.0
    cost_weight: float = 0.0


@dataclass(frozen=True)
class NavParams:
    relaxed_follow: bool = True
    #: 목표 통로의 가까운 장애물을 비켜 가기 시작하면 알린다 (기본 끔).
    relaxed_detour_notice: bool = False
    #: 따라가기에서 목표까지 거리가 `relaxed_stuck_ms` 동안 줄지 않으면 «길 막힘» 1회 알리고 앞이 열릴 때까지 선다.
    relaxed_stuck_hold: bool = False
    relaxed_stuck_ms: int = 12000
    relaxed_walk_detour: bool = False
    relaxed_detour_distance_m: float = 0.9
    relaxed_detour_angle_deg: float = 20.0
    route_person_search: bool = False
    route_search_pause_ms: int = 1000
    route_search_turn_deg: float = 10.0
    route_direct: bool = True
    route_direct_stop_ms: int = 3000
    live_clear_scans: int = 3
    live_clear_ttl_ms: int = 2000
    local_slow_m: float = 0.40
    local_stop_m: float = 0.25
    local_fan_deg: float = 30.0
    scan_max_age_ms: int = 500
    dynamic_ttl_ms: int = 2000
    gap_bin_deg: float = 5.0
    corridor_margin_m: float = 0.02
    avoidance_m: float = 0.20
    avoidance_timeout_ms: int = 3000
    avoidance_turn_deg: float = 15.0
    avoidance_step_scale: float = 0.5
    obstacle_event_interval_ms: int = 1000
    blockage_short_ms: int = 4000
    blockage_long_ms: int = 180000
    blockage_confirm_ms: int = 2000
    recovery_scan_timeout_ms: int = 120000
    recovery_motion_timeout_ms: int = 5000
    raytrace_max_m: float = 3.0
    obstacle_max_m: float = 2.5
    local_window_m: float = 3.0
    inflation_radius_m: float = 0.55
    cost_scaling_factor: float = 3.0
    cost_weight: float = 2.0

    @classmethod
    def of(cls, config: Mapping[str, Any]) -> NavParams:
        section = config.get("nav", {})
        if not isinstance(section, Mapping):
            raise ConfigError("nav 는 설정 객체여야 함")
        defaults = cls()
        values: dict[str, Any] = {}
        for key in cls.__dataclass_fields__:
            value = section.get(key, getattr(defaults, key))
            if isinstance(getattr(defaults, key), bool):
                if not isinstance(value, bool):
                    raise ConfigError(f"nav.{key} 는 bool이어야 함")
                values[key] = value
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ConfigError(f"nav.{key} 는 유한한 양수여야 함")
            if isinstance(getattr(defaults, key), int) and not isinstance(value, int):
                raise ConfigError(f"nav.{key} 는 정수여야 함")
            values[key] = value
        result = cls(**values)
        if result.route_search_turn_deg > 30 or result.relaxed_detour_angle_deg > 60:
            raise ConfigError("nav 탐색 회전은 30도 이하, 우회 알림 편향은 60도 이하여야 함")
        if not 0.25 <= result.local_stop_m < result.local_slow_m:
            raise ConfigError("0.25 <= nav.local_stop_m < nav.local_slow_m 이어야 함")
        if not 5 <= result.local_fan_deg <= 90 or not 1 <= result.gap_bin_deg <= 10:
            raise ConfigError("nav 부채꼴/각도 구간 범위 오류")
        if result.avoidance_turn_deg > 30 or result.avoidance_step_scale > 1:
            raise ConfigError("nav 회피 조향은 30도 이하, 보폭 비율은 1 이하여야 함")
        if result.corridor_margin_m < 0.02:
            raise ConfigError("nav.corridor_margin_m 은 0.02m 이상이어야 함")
        if (
            result.blockage_long_ms < result.blockage_short_ms
            or result.blockage_confirm_ms >= result.blockage_short_ms
        ):
            raise ConfigError("nav 막힘 확인/단기/장기 시간 순서 오류")
        if result.raytrace_max_m < result.obstacle_max_m:
            raise ConfigError("nav 비움 범위는 표시 범위 이상이어야 함")
        return result
