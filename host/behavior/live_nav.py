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
    relaxed_follow: bool = True
    #: 목표 통로의 가까운 장애물을 비켜 가기 시작하면 알린다 (기본 끔).
    relaxed_detour_notice: bool = False
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
    clear_allowed: bool = True

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
        received = scan.received_ms if scan.received_ms is not None else now_ms
        self.received_ms = received if self.points else None
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

    def open_heading(self, target: float, *, travel_m: float = 0.35) -> float | None:
        """몸 반경 15cm + 여유 5cm로 travel_m 이동 가능한 목표에 가까운 방위."""
        if not self.points:
            return None
        count = round(360 / self.params.gap_bin_deg)
        step = 2 * math.pi / count
        observed = {round(a / step) % count for a, _ in self.points}
        angles, distances = np.array(self.points).T
        guard = math.atan2(0.20, travel_m)
        candidates = [wrap_pi(target)] + [
            wrap_pi(math.radians(a)) for a in np.arange(-180, 180, self.params.gap_bin_deg)
        ]
        candidates.sort(key=lambda a: abs(wrap_pi(a - target)))
        for heading in candidates:
            # 한 바퀴 조립은 약 330도에서 끝난다. 미관측은 해당 방향만 닫는다.
            required = {
                round((heading + offset) / step) % count
                for offset in np.arange(-guard, guard + step, step)
            }
            # 2026-10-06 실기: 라이다 한 바퀴의 빈 30°(미관측)가 정면 근처에 오면 목표 방향을 버리고
            # 옆 방향을 골라 한쪽으로 계속 휘었다. 구간의 절반 이상이 관측됐으면 막힘 판정은 실제 반사로만 한다.
            if len(required & observed) * 2 < len(required):
                continue
            along = distances * np.cos(angles - heading)
            cross = distances * np.sin(angles - heading)
            # 시작 원판과 겹친 옆/뒤 반사도 멀어지는 방향의 출발을 막지는 않는다.
            hits = (along > 1e-6) & (np.abs(cross) <= 0.20)
            contact = along[hits] - np.sqrt(np.maximum(0.0, 0.20**2 - cross[hits] ** 2))
            if not contact.size or contact.min() >= travel_m - 1e-9:
                return heading
        return None

    def corridor(
        self,
        body_radius_m: float,
        heading: float,
        travel_m: float,
        *,
        origin: tuple[float, float] = (0.0, 0.0),
    ) -> tuple[float, float, float] | None:
        """최신 끝점의 관측 자유영역 안에 들어가는 이동 원판(방위, 여유, 폭).

        양옆 여유를 포함한 원판을 이동 선분 전체에 쓸어 검사한다. 인접 빔
        끝점 사이의 선분도 경계로 삼아 점 사이의 얇은 물체/미관측을 열지 않는다.
        기억점·격자 팽창은 이 판정에 더하지 않는다. 목표 반대 방향은 제외한다.
        """
        boundary = self._boundary()
        if boundary is None or not math.isfinite(travel_m) or travel_m <= 0:
            return None
        boundary = boundary - np.array(origin)
        radius = body_radius_m + self.params.corridor_margin_m
        edges = np.roll(boundary, -1, axis=0) - boundary
        # 벽 접선도 후보에 넣어 5도 칸 사이에 놓인 좁은 평행 통로를 놓치지 않는다.
        tangents = [wrap_pi(float(a)) for a in np.arctan2(edges[:, 1], edges[:, 0])]
        candidates = [wrap_pi(heading), *tangents, *(wrap_pi(a + math.pi) for a in tangents)] + [
            wrap_pi(math.radians(a)) for a in np.arange(-180, 180, self.params.gap_bin_deg)
        ]
        candidates = list(dict.fromkeys(round(a, 12) for a in candidates))
        candidates.sort(key=lambda a: abs(wrap_pi(a - heading)))
        for angle in candidates:
            if math.cos(wrap_pi(angle - heading)) <= 1e-6:
                continue
            distance = self.distance(angle)
            if distance is None or distance < self.params.local_stop_m:
                continue
            clearance = self._swept_clearance(boundary, angle, travel_m)
            if clearance >= radius - 1e-9:
                return angle, clearance, 2 * clearance
        return None

    @property
    def complete(self) -> bool:
        """통로가 없음과 관측이 없음은 다르다."""
        return self._boundary() is not None

    def corridor_clear(
        self,
        body_radius_m: float,
        heading: float,
        travel_m: float,
        *,
        origin: tuple[float, float] = (0.0, 0.0),
    ) -> bool:
        """현재 실제 진행 방위의 이동 원판 검사. 새 끝점은 즉시 반영한다."""
        boundary = self._boundary()
        return (
            boundary is not None
            and self._swept_clearance(boundary - np.array(origin), heading, travel_m)
            >= body_radius_m + self.params.corridor_margin_m - 1e-9
        )

    def _boundary(self) -> np.ndarray | None:
        if not self.clear_allowed:
            return None
        # 한 바퀴의 관측만 사용한다. 누락된 각도는 이전 스캔으로 메우지 않는다.
        ranges: dict[float, float] = {}
        for angle, distance in self.points:
            angle %= 2 * math.pi
            ranges[angle] = min(distance, ranges.get(angle, distance))
        if len(ranges) < 3:
            return None
        angles = np.array(sorted(ranges))
        gaps = np.diff(np.append(angles, angles[0] + 2 * math.pi))
        if float(gaps.max()) > math.radians(self.params.gap_bin_deg) + 1e-9:
            return None
        distances = np.array([ranges[a] for a in angles])
        return np.column_stack((np.cos(angles) * distances, np.sin(angles) * distances))

    @staticmethod
    def _swept_clearance(boundary: np.ndarray, heading: float, travel_m: float) -> float:
        # 후보 선분을 x축으로 돌린 뒤 경계 선분과의 최소 거리를 구한다.
        c, s = math.cos(heading), math.sin(heading)
        a = boundary @ np.array([[c, -s], [s, c]])
        b = np.roll(a, -1, axis=0)
        delta = b - a
        # 이동 뒤 중심이 관측 자유 다각형 밖이면 경계와 멀어도 자유가 아니다.
        straddles = (a[:, 1] > 0) != (b[:, 1] > 0)
        intersections = (
            a[straddles, 0] - a[straddles, 1] * delta[straddles, 0] / delta[straddles, 1]
        )
        if int(np.count_nonzero(intersections > 0)) % 2 == 0:
            return 0.0
        denom = np.sum(delta * delta, axis=1)
        denom = np.maximum(denom, 1e-20)
        clearance = math.inf
        for x in (0.0, travel_m):
            point = np.array([x, 0.0])
            t = np.clip(np.sum((point - a) * delta, axis=1) / denom, 0, 1)
            clearance = min(
                clearance, float(np.linalg.norm(a + t[:, None] * delta - point, axis=1).min())
            )
        for points in (a, b):
            dx = points[:, 0] - np.clip(points[:, 0], 0, travel_m)
            clearance = min(clearance, float(np.hypot(dx, points[:, 1]).min()))
        # 끝점 거리만으로는 경계와 진행 선분의 교차를 발견하지 못한다.
        crossing = (a[:, 1] * b[:, 1] <= 0) & (np.abs(delta[:, 1]) > 1e-12)
        if crossing.any():
            x = a[crossing, 0] - a[crossing, 1] * delta[crossing, 0] / delta[crossing, 1]
            if np.any((x >= 0) & (x <= travel_m)):
                return 0.0
        return clearance

    def gap(
        self, body_radius_m: float, minimum_m: float | None = None
    ) -> tuple[float, float, float] | None:
        """관측된 연속 각도 중 몸체가 들어가는 가장 넓은 gap(방위, 거리, 폭)."""
        if not self.clear_allowed:
            return None
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
