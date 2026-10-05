"""임시 지도 비움과 최신 360도 스캔 판단. 원본/측위 지도는 수정하지 않는다."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from host.common.config import ConfigError
from host.common.lidar_link import Scan
from host.common.units import wrap_pi
from host.slam.occupancy import OccupancyGrid, bresenham
from host.slam.scan_match import Pose


@dataclass(frozen=True)
class NavParams:
    live_clear_scans: int = 3
    live_clear_ttl_ms: int = 2000
    local_slow_m: float = 0.40
    local_stop_m: float = 0.25
    local_fan_deg: float = 30.0
    scan_max_age_ms: int = 500
    dynamic_ttl_ms: int = 2000
    gap_bin_deg: float = 5.0
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
        if not 0.25 <= result.local_stop_m < result.local_slow_m:
            raise ConfigError("0.25 <= nav.local_stop_m < nav.local_slow_m 이어야 함")
        if not 5 <= result.local_fan_deg <= 90 or not 1 <= result.gap_bin_deg <= 10:
            raise ConfigError("nav 부채꼴/각도 구간 범위 오류")
        if result.avoidance_turn_deg > 30 or result.avoidance_step_scale > 1:
            raise ConfigError("nav 회피 조향은 30도 이하, 보폭 비율은 1 이하여야 함")
        if (
            result.blockage_long_ms < result.blockage_short_ms
            or result.blockage_confirm_ms >= result.blockage_short_ms
        ):
            raise ConfigError("nav 막힘 확인/단기/장기 시간 순서 오류")
        if result.raytrace_max_m < result.obstacle_max_m:
            raise ConfigError("nav 비움 범위는 표시 범위 이상이어야 함")
        return result


@dataclass
class LiveClear:
    params: NavParams
    counts: np.ndarray | None = None
    seen_ms: np.ndarray | None = None
    frame: tuple[float, float, float, tuple[int, ...]] | None = None
    last_id: tuple[str, str, int] | None = None
    last_observed_ms: int | None = None

    def reset(self, grid: OccupancyGrid) -> None:
        self.frame = (
            grid.meta.resolution,
            grid.meta.origin_x,
            grid.meta.origin_y,
            grid.cells.shape,
        )
        self.counts = np.zeros_like(grid.cells, dtype=np.int32)
        self.seen_ms = np.full_like(grid.cells, -1, dtype=np.int64)
        self.last_id = None
        self.last_observed_ms = None

    def observe(
        self,
        grid: OccupancyGrid,
        pose: Pose,
        scan: Scan,
        now_ms: int,
        *,
        verified: bool,
        range_m: tuple[float, float],
    ) -> None:
        frame = (grid.meta.resolution, grid.meta.origin_x, grid.meta.origin_y, grid.cells.shape)
        if self.frame != frame:
            self.reset(grid)
        assert self.counts is not None and self.seen_ms is not None
        if not verified:
            self.counts.fill(0)
            self.seen_ms.fill(-1)
            return
        scan_id = (scan.device_id, scan.boot_id, scan.seq)
        if (
            self.last_id is not None
            and scan_id[:2] == self.last_id[:2]
            and scan.seq <= self.last_id[2]
        ):
            return
        if self.last_id is not None and scan_id[:2] != self.last_id[:2]:
            self.counts.fill(0)
            self.seen_ms.fill(-1)
        if (
            self.last_observed_ms is not None
            and not 0 <= now_ms - self.last_observed_ms <= self.params.scan_max_age_ms
        ):
            self.counts.fill(0)
        self.last_id = scan_id
        self.last_observed_ms = now_ms
        free = np.zeros_like(grid.cells, dtype=bool)
        hits = np.zeros_like(free)
        start = grid.to_cell(*pose[:2])
        for angle, distance in scan.points:
            if (
                not math.isfinite(angle)
                or not math.isfinite(distance)
                or not range_m[0] <= distance <= range_m[1]
            ):
                continue
            heading = pose[2] + angle
            ray_distance = min(distance, self.params.raytrace_max_m)
            end = grid.to_cell(
                pose[0] + math.cos(heading) * ray_distance,
                pose[1] + math.sin(heading) * ray_distance,
            )
            # 끝점과 그 직전 셀은 비움 증거에서 제외(셀 경계/얇은 물체).
            for cell in bresenham(*start, *end)[:-2]:
                if grid.inside(*cell):
                    free[cell] = True
            if grid.inside(*end):
                hits[end] = True
        free[hits] = False
        self.counts[~free] = 0
        self.counts[free] = np.minimum(self.counts[free] + 1, self.params.live_clear_scans)
        self.seen_ms[free & (self.counts >= self.params.live_clear_scans)] = now_ms
        self.seen_ms[hits] = -1

    def mask(self, grid: OccupancyGrid, now_ms: int) -> np.ndarray:
        frame = (grid.meta.resolution, grid.meta.origin_x, grid.meta.origin_y, grid.cells.shape)
        if self.frame != frame:
            self.reset(grid)
        assert self.seen_ms is not None
        return (
            (self.seen_ms >= 0)
            & (now_ms >= self.seen_ms)
            & (now_ms - self.seen_ms < self.params.live_clear_ttl_ms)
        )

    def grid(self, source: OccupancyGrid, now_ms: int, free_thresh: float) -> OccupancyGrid:
        cells = source.cells.copy()
        cells[self.mask(source, now_ms)] = free_thresh - 1.0
        return OccupancyGrid(source.meta, cells)


@dataclass
class LocalScan:
    params: NavParams
    points: tuple[tuple[float, float], ...] = ()
    received_ms: int | None = None
    last_id: tuple[str, str, int] | None = None

    def observe(self, scan: Scan, now_ms: int, range_m: tuple[float, float]) -> bool:
        identity = (scan.device_id, scan.boot_id, scan.seq)
        if (
            self.last_id is not None
            and identity[:2] == self.last_id[:2]
            and scan.seq <= self.last_id[2]
        ):
            return False
        self.last_id = identity
        # 최소 사거리보다 가까운 양수도 국소 정지 근거다. 측위 필터로 지우지 않는다.
        self.points = tuple(
            (wrap_pi(a), d)
            for a, d in scan.points
            if math.isfinite(a) and math.isfinite(d) and 0 < d <= range_m[1]
        )
        self.received_ms = now_ms if self.points else None
        return True

    def fresh(self, now_ms: int) -> bool:
        return (
            self.received_ms is not None
            and 0 <= now_ms - self.received_ms <= self.params.scan_max_age_ms
        )

    def distance(self, heading: float = 0.0) -> float | None:
        distances = [
            d
            for a, d in self.points
            if abs(wrap_pi(a - heading)) <= math.radians(self.params.local_fan_deg)
        ]
        return min(distances) if distances else None

    def gap(
        self, body_radius_m: float, minimum_m: float | None = None
    ) -> tuple[float, float, float] | None:
        """관측된 연속 각도 중 몸체가 들어가는 가장 넓은 gap(방위, 거리, 폭)."""
        count = round(360 / self.params.gap_bin_deg)
        step = 2 * math.pi / count
        bins = [0.0] * count
        for angle, distance in self.points:
            index = round((angle % (2 * math.pi)) / step) % count
            bins[index] = distance if bins[index] == 0 else min(bins[index], distance)
        limit = max(self.params.local_stop_m, minimum_m or self.params.local_slow_m)
        safe = [d > limit for d in bins]
        if all(safe):
            return 0.0, min(bins), 2 * math.pi
        candidates: list[tuple[float, float, float]] = []
        for start in range(count):
            if not safe[start] or safe[(start - 1) % count]:
                continue
            length = 0
            while length < count and safe[(start + length) % count]:
                length += 1
            distance = min(bins[(start + i) % count] for i in range(length))
            width = length * step
            if width < 2 * math.asin(min(1.0, body_radius_m / distance)) + step:
                continue
            guard = math.ceil(math.asin(min(1.0, body_radius_m / distance)) / step)
            choices = [wrap_pi((start + i) * step) for i in range(guard, length - guard)]
            if not choices:
                continue
            angle = min(choices, key=abs)
            candidates.append((angle, distance, width))
        return max(candidates, key=lambda item: (item[2], -abs(item[0]))) if candidates else None
