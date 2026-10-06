"""신선한 위치의 영역 라벨로 PPE 요구 항목을 고른다. 구역 불명은 기본 두 항목."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from host.behavior.zone_map import ZoneMap

DEFAULT_PPE = ("helmet", "vest")


class ZonePpePolicy:
    def __init__(self, config: Mapping[str, Any], zone_map: ZoneMap | None = None) -> None:
        self.zone_map = zone_map
        self.labels = frozenset(str(label) for label in config["zones"]["ids"])
        self.policies = config["zones"].get("policies", {})
        self.timeout = int(config["localization"]["pose_timeout_ms"])
        self.pose: tuple[float, float, float] | None = None
        self.pose_ms: int | None = None

    def note_pose(self, pose: tuple[float, float, float], now_ms: int) -> None:
        self.pose, self.pose_ms = pose, now_ms

    def current_zone(self, now_ms: int) -> str | None:
        if (
            self.zone_map is None
            or self.pose is None
            or self.pose_ms is None
            or not 0 <= now_ms - self.pose_ms <= self.timeout
            or not all(math.isfinite(value) for value in self.pose)
        ):
            return None
        label = self.zone_map.zone_at(self.pose[0], self.pose[1])
        return label if label in self.labels else None

    def requirements_for(self, zone: str | None) -> tuple[str, ...]:
        policy = self.policies.get(zone, {}) if zone in self.labels else {}
        return tuple(item for item in DEFAULT_PPE if policy.get(item, True))

    def required(self, now_ms: int) -> tuple[str, ...]:
        return self.requirements_for(self.current_zone(now_ms))
