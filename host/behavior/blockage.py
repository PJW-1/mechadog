"""진행을 막은 반사점의 수명과 사건 묶음. 원본 지도에는 쓰지 않는다."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from host.behavior.live_nav import NavParams
from host.behavior.planner import mark_obstacle
from host.common.lidar_link import Scan
from host.slam.occupancy import OccupancyGrid, bresenham
from host.slam.scan_match import Pose


@dataclass
class Blockage:
    id: int
    points: dict[tuple[int, int], tuple[float, float]]
    first_ms: int
    expires_ms: int
    confirmed: bool = False
    reported: set[str] = field(default_factory=set)

    @property
    def centre(self) -> tuple[float, float]:
        values = list(self.points.values())
        return (sum(p[0] for p in values) / len(values), sum(p[1] for p in values) / len(values))


@dataclass
class BlockageMemory:
    params: NavParams
    items: list[Blockage] = field(default_factory=list)
    serial: int = 0

    @staticmethod
    def _novel_hit(grid: OccupancyGrid, cell: tuple[int, int]) -> bool:
        """이미 지도에 있는 벽을 새 장애물 덩어리에 연결하지 않는다."""
        if not grid.inside(*cell):
            return False
        row, col = cell
        nearby = grid.cells[max(0, row - 1) : row + 2, max(0, col - 1) : col + 2]
        return not bool(np.any(nearby >= 1.0))

    def remember(
        self, grid: OccupancyGrid, points: list[tuple[float, float]], now_ms: int
    ) -> Blockage | None:
        if not points:
            return None
        points = [p for p in points if self._novel_hit(grid, grid.to_cell(*p))]
        if not points:
            return None
        item = next(
            (
                b
                for b in self.items
                if any(math.dist(p, q) <= 0.4 for p in points for q in b.points.values())
            ),
            None,
        )
        if item is None:
            self.serial += 1
            item = Blockage(self.serial, {}, now_ms, now_ms + self.params.blockage_short_ms)
            self.items.append(item)
        item.points.update({grid.to_cell(*p): p for p in points})
        return item

    def expire(self, now_ms: int) -> None:
        self.items = [b for b in self.items if now_ms < b.expires_ms]

    def observe(
        self, grid: OccupancyGrid, pose: Pose, scan: Scan, now_ms: int
    ) -> set[tuple[int, int]]:
        """실제로 뒤까지 통과한 빔만 지운다. 끝점/바로 앞 셀·가림은 비움이 아니다."""
        free: set[tuple[int, int]] = set()
        hits: set[tuple[int, int]] = set()
        hit_points: dict[tuple[int, int], tuple[float, float]] = {}
        for item in self.items:
            item.points = {
                grid.to_cell(*p): p for p in item.points.values() if grid.inside(*grid.to_cell(*p))
            }
        for angle, distance in scan.points:
            if not math.isfinite(angle) or not math.isfinite(distance) or distance <= 0:
                continue
            heading = pose[2] + angle
            length = min(distance, self.params.raytrace_max_m)
            end = grid.to_cell(
                pose[0] + math.cos(heading) * length, pose[1] + math.sin(heading) * length
            )
            free.update(bresenham(*grid.to_cell(*pose[:2]), *end)[:-2])
            if distance <= self.params.obstacle_max_m:
                hit = grid.to_cell(
                    pose[0] + math.cos(heading) * distance, pose[1] + math.sin(heading) * distance
                )
                hits.add(hit)
                if self._novel_hit(grid, hit):
                    hit_points[hit] = (
                        pose[0] + math.cos(heading) * distance,
                        pose[1] + math.sin(heading) * distance,
                    )
        free -= hits
        for item in self.items:
            # 돌아보며 같은 덩어리의 드러난 가장자리를 이어 기억한다.
            # 긴 벽의 일부만 막아 놓고 매번 새 우회 사건을 만드는 일을 줄인다.
            pending = dict(hit_points)
            frontier = list(item.points.values())
            while frontier:
                point = frontier.pop()
                connected = [cell for cell, p in pending.items() if math.dist(point, p) <= 0.25]
                for cell in connected:
                    p = pending.pop(cell)
                    item.points[cell] = p
                    frontier.append(p)
            # 인접 반사는 확인 증거지만, 새 빔이 실제 비운 셀에 옛 끝점을 남기지 않는다.
            for cell in list(item.points):
                visible = any(max(abs(cell[0] - h[0]), abs(cell[1] - h[1])) <= 1 for h in hits)
                if cell in free and cell not in hits:
                    del item.points[cell]
                elif visible and now_ms - item.first_ms >= self.params.blockage_confirm_ms:
                    item.confirmed = True
                    item.expires_ms = now_ms + self.params.blockage_long_ms
        self.items = [b for b in self.items if b.points]
        self.expire(now_ms)
        return free

    def mask(self, grid: OccupancyGrid, radius_m: float) -> np.ndarray:
        result = np.zeros_like(grid.cells, dtype=bool)
        for item in self.items:
            for point in item.points.values():
                mark_obstacle(result, grid, point, radius_m)
        return result

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": b.id,
                "x": b.centre[0],
                "y": b.centre[1],
                "confirmed": b.confirmed,
                "expires_ms": b.expires_ms,
                "points": list(b.points.values()),
            }
            for b in self.items
        ]


@dataclass
class Recovery:
    target: str
    started_ms: int
    yaw: float
    blockage_id: int | None
    swept_rad: float = 0.0
    scanning: bool = False
    settling_ms: int | None = None
    reason: str = "path_obstacle"
    last_motion_ms: int | None = None
