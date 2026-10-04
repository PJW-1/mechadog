"""기존 지도 앵커 반경 안에서만 PPE 요구 항목을 고른다. 측위 불명은 기본 두 항목."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from host.behavior.zones import Zone

DEFAULT_PPE = ("helmet", "vest")


class ZonePpePolicy:
    def __init__(self, config: Mapping[str, Any], anchors: tuple[Zone, ...]) -> None:
        self.anchors = anchors
        self.policies = config["zones"].get("policies", {})
        self.radius = float(config["zones"]["arrival_radius_mm"]) / 1000
        self.timeout = int(config["localization"]["pose_timeout_ms"])
        self.pose: tuple[float, float, float] | None = None
        self.pose_ms: int | None = None

    def note_pose(self, pose: tuple[float, float, float], now_ms: int) -> None:
        self.pose, self.pose_ms = pose, now_ms

    def required(self, now_ms: int) -> tuple[str, ...]:
        if (
            self.pose is None
            or self.pose_ms is None
            or not 0 <= now_ms - self.pose_ms <= self.timeout
        ):
            return DEFAULT_PPE
        matches = [
            zone
            for zone in self.anchors
            if math.hypot(zone.x - self.pose[0], zone.y - self.pose[1]) <= self.radius
        ]
        if not matches:
            return DEFAULT_PPE
        # 반경이 겹치면 둘 중 느슨한 쪽을 임의 선택하지 않고 요구 항목을 합친다.
        return tuple(
            item
            for item in DEFAULT_PPE
            if any(self.policies.get(z.label, {}).get(item, True) for z in matches)
        )
