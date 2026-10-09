"""항법 지도 — 계획·추종이 보는 막힘 마스크와 실시간 장애물을 지도 위에 쌓는다.

`PatrolController` 의 지도 쪽 상태를 여기에 모은다.

- **마스크** — 저장 지도 팽창(`static`)·몸체 여유(`body`)·실시간 장애물(`dynamic`).
  격자 모양이나 내용이 바뀌면 새로 만들고, 진행 선분이 새로 막혔으면 목표만 남겨
  재계획을 건다 (`rebuild_masks`).
- **실시간 장애물** — 검증된 빔 비움(`live_clear`)과 실제 끝점을 투영하고, 신규 끝점을
  꺼내 가게 쌓는다 (`project_scan` → `new_obstacles`). 오래된 것은 지운다 (`refresh`).
- **지도 성장** — 확실한 정합만 지도에 적분하고 주기적으로 팽창을 새로 만든다 (`grow`).
- **비용·국소 창** — 거리 비용(`costs`), 대시보드용 3m 창(`local_window`), 국소 진행
  선분 확인(`path_clear`).

자세·격자·계획은 순찰기의 것을 그대로 쓰고 바꾼다. 상태 변경은 순찰 루프 스레드에서만
일어난다.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

from host.behavior.live_nav import LiveClear
from host.behavior.patrol_phase import Phase
from host.behavior.planner import (
    Plan,
    body_collision_mask,
    distance_costs,
    inflate,
    mark_obstacle,
    segment_clear,
)
from host.common.logging_setup import event_logger
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.scan_match import integrate_scan

if TYPE_CHECKING:
    from host.behavior.patrol import PatrolController
    from host.common.lidar_link import Scan

#: 순찰 컨트롤러와 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.behavior.patrol")

#: 정합 점수가 전체 점수의 이 비율 이상일 때만 그 바퀴를 지도에 적분한다. 낮은 점수의
#: 자세로 적분하면 틀린 위치에 벽을 찍어 지도가 스스로 오염된다 — 아래에서는
#: «이 위치가 맞다» 는 정합만 공간 사실로 승격한다.
MAP_WRITE_MIN_SCORE_FRAC: float = 0.6

#: 지도가 자랐을 때 팽창 마스크를 다시 만드는 간격(적분한 바퀴 수). A* 는
#: `static` 을 보고 푸는데, 적분으로 새로 «확실히 빈» 셀이 생겨도 팽창이
#: 옛 지도 그대로면 로봇이 선 자리가 계속 막혀 보인다 (2026-10-02 실기:
#: 측위는 잠겼는데 시작 셀이 미관측이라 `no_reachable_zone`).
MAP_INFLATE_EVERY: int = 4


class NavigationMap:
    """순찰기의 막힘 마스크·실시간 장애물·거리 비용을 쥔다."""

    def __init__(self, patrol: PatrolController) -> None:
        self._patrol = patrol
        #: 검증된 빔이 지나간 셀을 잠시 비워 보이게 하는 덧칠.
        self.live_clear = LiveClear(patrol.nav_params)
        static = inflate(patrol.grid, patrol.plan_params)
        #: 저장 지도 팽창 마스크 (A* 가 푸는 정적 막힘).
        self.static: np.ndarray | None = static
        #: 몸체 최소 여유 마스크.
        self.body: np.ndarray | None = body_collision_mask(patrol.grid, patrol.plan_params)
        #: 실시간 장애물·막힘 기억 마스크.
        self.dynamic: np.ndarray | None = np.zeros_like(static, dtype=bool)
        #: 마스크를 만든 격자의 (해상도, 원점 x, 원점 y) — 바뀌면 마스크가 어긋난다.
        self.frame: tuple[float, float, float] | None = (
            patrol.grid.meta.resolution,
            patrol.grid.meta.origin_x,
            patrol.grid.meta.origin_y,
        )
        #: 지도에 적분한 뒤 팽창을 아직 안 다시 한 바퀴 수.
        self.updates_since_inflate: int = 0
        #: 새 장애물 연속 확인 후보 — 실시간 회피 정책은 후보 단계 없이 바로 끝점을 낸다(항상 None).
        #: 프레임 수집기(4.8.7)의 `obstacle_pending` 계약을 위해 남긴다.
        self.pending_hit: tuple[float, float] | None = None
        #: 지금 살아 있는 실시간 장애물 끝점 (순찰 좌표 m).
        self.obstacles: list[tuple[float, float]] = []
        #: 아직 아무도 꺼내 가지 않은 신규 장애물 확정 (`take_new_obstacles`).
        self.new_obstacles: list[tuple[float, float]] = []
        #: 셀 → (x, y, 마지막으로 본 시각) — `dynamic_ttl_ms` 가 지나면 잊는다.
        self.dynamic_seen: dict[tuple[int, int], tuple[float, float, int]] = {}
        #: 이미 투영한 스캔 id — 같은 바퀴를 두 번 투영하지 않는다.
        self.projected_scan_id: tuple[str, str, int] | None = None
        #: 마지막 신규 장애물 사건 시각 — `obstacle_event_interval_ms` 로 간격을 둔다.
        self.last_obstacle_event_ms: int | None = None
        #: 거리 비용과 그것을 계산한 막힘 마스크 — 마스크가 같으면 다시 쓰지 않는다.
        self.cost_cache: np.ndarray | None = None
        self.cost_blocked: np.ndarray | None = None

    def rebuild_masks(self) -> None:
        """격자 모양·내용이 바뀐 직후 팽창·동적 마스크를 새로 만든다."""
        patrol = self._patrol
        static = inflate(patrol.navigation_grid, patrol.plan_params)
        body = body_collision_mask(patrol.navigation_grid, patrol.plan_params)
        frame = (patrol.grid.meta.resolution, patrol.grid.meta.origin_x, patrol.grid.meta.origin_y)
        changed = (
            self.frame != frame
            or self.static is None
            or self.body is None
            or not np.array_equal(self.static, static)
            or not np.array_equal(self.body, body)
        )
        self.static, self.frame = static, frame
        self.body = body
        dynamic = np.zeros_like(self.static, dtype=bool)
        for hit in self.obstacles:
            mark_obstacle(dynamic, patrol.grid, hit, patrol.plan_params.body_radius_m)
        dynamic |= patrol.recovery.memory.mask(patrol.grid, patrol.plan_params.body_radius_m)
        self.dynamic = dynamic
        self.updates_since_inflate = 0
        if (
            changed
            and patrol.plan.reachable
            # 직접 동선은 최신 거리로 추종한다. 저장 격자상의 선분이 막혔다고
            # 회전마다 재계획하면 STOP/회전이 번갈아 안정 스캔을 계속 무효화한다.
            and not (patrol.route.direct_moving and patrol.route.direct_detour_start is None)
            and not segment_clear(
                patrol.navigation_grid,
                static,
                patrol.pose[:2],
                patrol.plan.waypoints[min(patrol.waypoint_index, len(patrol.plan.waypoints) - 1)],
            )
        ):
            # 새 정적 장애물은 신규 탐지에서 이미 아는 것으로 빠진다. 옛 경로까지
            # 유지하면 그 장애물을 그대로 통과하므로 목표만 보존해 다시 계획한다.
            patrol.plan = Plan(patrol.plan.label)
            patrol.waypoint_index = 0
            if patrol.phase in (Phase.MOVING, Phase.AIMING) or (
                patrol._spinning and patrol.phase is Phase.PLANNING
            ):
                patrol.phase = Phase.PLANNING
                patrol._require_replan_stop()
                patrol.commander.halt()
            LOG.info("navigation_mask_changed_replan", target=patrol.plan.label)

    def grow(self, points: np.ndarray, score: int) -> None:
        """정합이 확실한 바퀴를 지도에 적분하고 주기적으로 팽창을 새로 만든다.

        보행 속도에서 한 바퀴(~0.1초) 사이의 이동은 수 cm 이하라 이동 중 적분 오차는
        셀 하나 이하다 — 지도는 로봇이 지나간 곳을 «확실히 빈» 셀로 채워 나간다.
        """
        patrol = self._patrol
        if (
            not patrol.live_map_write
            or patrol.map_hit_logodds <= 0
            or not patrol.localization.verified
            or score < MAP_WRITE_MIN_SCORE_FRAC * len(points)
        ):
            return
        integrate_scan(
            patrol.match_grid,
            patrol.pose,
            points,
            hit=patrol.map_hit_logodds,
            miss=patrol.map_miss_logodds,
            pad_cells=patrol.map_pad_cells,
        )
        self.updates_since_inflate += 1
        # `integrate` 가 격자를 늘리면 원점이 옮겨져 두 마스크의 셀 좌표가 전부 어긋난다 —
        # 주기와 무관하게 모양이 달라진 즉시 둘 다 새로 만든다. 장애물 표시는 월드 좌표라
        # (`self.obstacles`) 새 격자에 다시 찍을 수 있다.
        if self.updates_since_inflate >= MAP_INFLATE_EVERY or (
            self.static is not None and self.static.shape != patrol.grid.cells.shape
        ):
            self.rebuild_masks()

    def project_scan(self, scan: Scan) -> None:
        """verified 빔 비움과 실제 끝점+몸 반경만 임시로 투영한다."""
        patrol = self._patrol
        if not patrol._local_scan.clear_allowed:
            return
        if (scan.device_id, scan.boot_id, scan.seq) != patrol._local_scan.last_id:
            return
        if self.projected_scan_id == patrol._local_scan.last_id:
            return
        patrol._local_scan_pose = (*patrol.pose[:2], patrol.heading.steering_yaw())
        self.projected_scan_id = patrol._local_scan.last_id
        if patrol.relaxed.scan.last_id == patrol._local_scan.last_id:
            patrol.relaxed.scan_yaw = patrol._local_scan_pose[2]
        now_ms = patrol._scan_now_ms
        if patrol.pose_verified and not patrol.pose_stale(now_ms):
            free = patrol.recovery.memory.observe(patrol.grid, patrol.pose, scan, now_ms)
            self.dynamic_seen = {
                cell: value for cell, value in self.dynamic_seen.items() if cell not in free
            }
        self.live_clear.observe(
            patrol.grid,
            patrol.pose,
            scan,
            now_ms,
            verified=patrol.pose_verified and not patrol.pose_stale(now_ms),
            range_m=patrol.range_m,
        )
        new_hit: tuple[float, float, float] | None = None
        if not patrol.pose_stale(now_ms) and (not patrol.localization.untrusted):
            for angle, distance in scan.points:
                if (
                    not math.isfinite(angle)
                    or not math.isfinite(distance)
                    or not patrol.range_m[0]
                    <= distance
                    <= min(patrol.new_obstacle_check_radius_m, patrol.nav_params.obstacle_max_m)
                ):
                    continue
                heading = patrol.pose[2] + angle
                x = patrol.pose[0] + math.cos(heading) * distance
                y = patrol.pose[1] + math.sin(heading) * distance
                cell = patrol.grid.to_cell(x, y)
                if not patrol.grid.inside(*cell):
                    continue
                if (
                    cell not in self.dynamic_seen
                    and patrol.grid.cells[cell] < patrol.plan_params.occ_thresh
                    and (new_hit is None or distance < new_hit[2])
                ):
                    new_hit = (x, y, distance)
                self.dynamic_seen[cell] = (x, y, now_ms)
        if new_hit is not None and (
            self.last_obstacle_event_ms is None
            or now_ms - self.last_obstacle_event_ms >= patrol.nav_params.obstacle_event_interval_ms
        ):
            self.new_obstacles.append(new_hit[:2])
            self.last_obstacle_event_ms = now_ms
        self.refresh(now_ms)

    def refresh(self, now_ms: int) -> None:
        patrol = self._patrol
        patrol._now_ms = now_ms
        if patrol.pose_stale(now_ms) or (
            patrol.localization.own_localization and not patrol.localization.verified
        ):
            self.live_clear.reset(patrol.grid)
        self.dynamic_seen = {
            cell: item
            for cell, item in self.dynamic_seen.items()
            if 0 <= now_ms - item[2] < patrol.nav_params.dynamic_ttl_ms
        }
        self.obstacles = [(x, y) for x, y, _ in self.dynamic_seen.values()]
        patrol.recovery.memory.expire(now_ms)
        self.rebuild_masks()

    @property
    def costs(self) -> np.ndarray:
        patrol = self._patrol
        blocked = patrol.blocked
        if self.cost_blocked is None or not np.array_equal(self.cost_blocked, blocked):
            self.cost_cache = (
                distance_costs(
                    blocked,
                    patrol.grid.meta.resolution,
                    patrol.nav_params.inflation_radius_m,
                    patrol.nav_params.cost_scaling_factor,
                )
                * patrol.nav_params.cost_weight
            )
            self.cost_blocked = blocked.copy()
        assert self.cost_cache is not None
        return self.cost_cache

    @property
    def local_window(self) -> dict[str, Any]:
        """로봇 중심 3m 창. 미관측/창 밖을 자유로 추정하지 않는다."""
        patrol = self._patrol
        row, col = patrol.grid.to_cell(*patrol.pose[:2])
        half = math.ceil(patrol.nav_params.local_window_m / patrol.grid.meta.resolution / 2)
        rows, cols = patrol.grid.cells.shape
        r0, r1, c0, c1 = (
            max(0, row - half),
            min(rows, row + half + 1),
            max(0, col - half),
            min(cols, col + half + 1),
        )
        if r0 >= r1 or c0 >= c1:
            return {"width": 0, "height": 0, "blocked": [], "costs": []}
        return {
            "resolution_m": patrol.grid.meta.resolution,
            "origin": [
                patrol.grid.meta.origin_x + c0 * patrol.grid.meta.resolution,
                patrol.grid.meta.origin_y + r0 * patrol.grid.meta.resolution,
            ],
            "width": c1 - c0,
            "height": r1 - r0,
            "blocked": patrol.blocked[r0:r1, c0:c1].tolist(),
            "costs": self.costs[r0:r1, c0:c1].round(3).tolist(),
        }

    def path_clear(self, waypoint: tuple[float, float]) -> bool:
        """로봇 중심 3m 창의 몸체 마스크로 국소 진행 선분을 확인한다."""
        patrol = self._patrol
        row, col = patrol.grid.to_cell(*patrol.pose[:2])
        half = math.ceil(patrol.nav_params.local_window_m / patrol.grid.meta.resolution / 2)
        rows, cols = patrol.grid.cells.shape
        r0, r1, c0, c1 = (
            max(0, row - half),
            min(rows, row + half + 1),
            max(0, col - half),
            min(cols, col + half + 1),
        )
        if r0 >= r1 or c0 >= c1:
            return False
        local = OccupancyGrid(
            MapMeta(
                patrol.grid.meta.resolution,
                patrol.grid.meta.origin_x + c0 * patrol.grid.meta.resolution,
                patrol.grid.meta.origin_y + r0 * patrol.grid.meta.resolution,
                c1 - c0,
                r1 - r0,
            ),
            patrol.grid.cells[r0:r1, c0:c1],
        )
        dx, dy = waypoint[0] - patrol.pose[0], waypoint[1] - patrol.pose[1]
        extent = patrol.nav_params.local_window_m / 2 - patrol.grid.meta.resolution
        scale = min(1.0, extent / max(abs(dx), abs(dy), 1e-9))
        end = (patrol.pose[0] + dx * scale, patrol.pose[1] + dy * scale)
        return segment_clear(local, patrol.body_blocked[r0:r1, c0:c1], patrol.pose[:2], end)
