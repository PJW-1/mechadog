"""경로 계획 — 팽창 · A* · 웨이포인트 · 다음 구역 선정 (FR-7.2/7.3 · Phase 2).

**직선거리로 다음 구역을 고르지 않는다.** 벽 하나 건너 3m 인 구역이 돌아가면
9m 이므로, 가까운 미방문 구역은 **실제 A* 경로 길이**로 정한다 (FR-7.3). 그래서
`select_next` 가 후보마다 경로를 한 번씩 푼다 — 구역이 3개(`zones.ids`)이므로
사이클당 최대 3번이고, 이 규모에서는 비용이 문제되지 않는다.

팽창은 원판 커널로 numpy 에서 직접 한다 — 반경이 몇 셀뿐이라 scipy 의존성을 들이지 않는다.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import cast

import numpy as np

from host.slam.occupancy import OccupancyGrid

Cell = tuple[int, int]
Point = tuple[float, float]

#: 8방향 이동과 비용. 대각을 `sqrt(2)` 로 두지 않으면 A* 가 계단 경로를 직선과
#: 같은 값으로 보고, 웨이포인트 단순화가 지그재그를 그대로 남긴다.
MOVES: tuple[tuple[int, int, float], ...] = (
    (-1, 0, 1.0),
    (1, 0, 1.0),
    (0, -1, 1.0),
    (0, 1, 1.0),
    (-1, -1, math.sqrt(2)),
    (-1, 1, math.sqrt(2)),
    (1, -1, math.sqrt(2)),
    (1, 1, math.sqrt(2)),
)


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


def inflate(grid: OccupancyGrid, params: PlanParams) -> np.ndarray:
    """이동 불가 마스크. **막힌 셀과 미관측 셀 둘 다 막는다.**

    미관측(값 0)을 통과 가능으로 두면 A* 가 지도 밖 여백을 최단 경로로 골라
    나가고, 실제로는 그곳에 무엇이 있는지 모른다. 순찰 로봇에서 *모르는 곳*은
    *빈 곳*이 아니다 — `free_thresh` 이하로 **확실히 비었다고 관측된** 셀만
    통과를 허용한다.

    장애물은 두 층으로 부풀린다: `hard_thresh` 이상(원본 벽·반복 확인된 장애물)은
    `clearance_m` 전부, 그 미만(세션 지도 병합으로 들어온 가구 다리급)은
    `soft_clearance_m` 만 — 다리가 있는 곳까지 아예 못 서는 건 지도가 아니라 팽창 탓이다.
    """
    occupied = grid.cells >= params.occ_thresh
    unknown = grid.cells > params.free_thresh
    radius_cells = int(math.ceil(params.clearance_m / grid.meta.resolution))
    blocked = unknown | dilate(occupied, radius_cells)
    if params.soft_clearance_m < params.clearance_m and (grid.cells >= params.occ_thresh).any():
        hard = grid.cells >= params.hard_thresh
        soft = occupied & ~hard
        soft_cells = int(math.ceil(params.soft_clearance_m / grid.meta.resolution))
        # 단단한 층 주변은 clearance_m 전부가 이미 막는다 — soft 부풀림을 OR 하면
        # hard 셀 옆 soft 셀이 soft 반경만큼 더 막아 버리지 않게, 결과를 재구성한다.
        blocked = unknown | dilate(hard, radius_cells) | dilate(soft, max(soft_cells, 0))
    return cast(np.ndarray, blocked)


def dilate(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """원판 커널 팽창. 로봇 반경만큼 장애물을 부풀린다.

    로봇을 점으로 계획하고 장애물을 부풀리는 것이 그 반대보다 싸다 — 계획
    공간이 격자 하나로 남는다.
    """
    if radius_cells <= 0:
        return mask.copy()
    out = mask.copy()
    height, width = mask.shape
    for d_row in range(-radius_cells, radius_cells + 1):
        for d_col in range(-radius_cells, radius_cells + 1):
            if d_row * d_row + d_col * d_col > radius_cells * radius_cells:
                continue
            # 배열을 굴리지 않고 잘라 붙인다 — `np.roll` 은 반대편 경계에서
            # 감겨 들어와 지도의 왼쪽 벽이 오른쪽 벽을 부풀린다.
            src_rows = slice(max(0, -d_row), height - max(0, d_row))
            src_cols = slice(max(0, -d_col), width - max(0, d_col))
            dst_rows = slice(max(0, d_row), height - max(0, -d_row))
            dst_cols = slice(max(0, d_col), width - max(0, -d_col))
            out[dst_rows, dst_cols] |= mask[src_rows, src_cols]
    return out


def mark_obstacle(blocked: np.ndarray, grid: OccupancyGrid, hit: Point, radius_m: float) -> None:
    """지도에 없던 장애물을 **동적 마스크에만** 찍는다.

    지도(`grid.cells`)는 고치지 않는다 — 동적 장애물은 이번 순찰의 사실이지 공간의 사실이 아니다.
    """
    row0, col0 = grid.to_cell(*hit)
    radius_cells = int(math.ceil(radius_m / grid.meta.resolution))
    height, width = blocked.shape
    for d_row in range(-radius_cells, radius_cells + 1):
        for d_col in range(-radius_cells, radius_cells + 1):
            if d_row * d_row + d_col * d_col > radius_cells * radius_cells:
                continue
            row, col = row0 + d_row, col0 + d_col
            if 0 <= row < height and 0 <= col < width:
                blocked[row, col] = True


def astar(start: Cell, goal: Cell, blocked: np.ndarray) -> list[Cell] | None:
    """8방향 A*. 경로가 없으면 `None`.

    출발 셀이 막혀 있어도 탐색한다(측위 오차·팽창으로 자기 자리가 막혀 보일 수 있다).
    목표 셀이 막혔으면 즉시 거절한다.
    """
    height, width = blocked.shape
    if not (0 <= goal[0] < height and 0 <= goal[1] < width) or blocked[goal]:
        return None
    if not (0 <= start[0] < height and 0 <= start[1] < width):
        return None

    open_set: list[tuple[float, Cell]] = [(0.0, start)]
    came_from: dict[Cell, Cell] = {}
    g_score: dict[Cell, float] = {start: 0.0}
    closed: set[Cell] = set()

    while open_set:
        _, current = heapq.heappop(open_set)
        if current == goal:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path
        if current in closed:
            continue
        closed.add(current)
        for d_row, d_col, cost in MOVES:
            neighbour = (current[0] + d_row, current[1] + d_col)
            if not (0 <= neighbour[0] < height and 0 <= neighbour[1] < width):
                continue
            if blocked[neighbour] and neighbour != goal:
                continue
            if d_row != 0 and d_col != 0:
                # 대각선 양옆의 직교 셀 중 하나라도 막혔으면 그 모서리를
                # 가로지르지 않는다. 목적 셀만 비어 있어도 실제 로봇 몸체는
                # 두 장애물 사이를 통과해야 하므로 충돌 경로다.
                side_a = (current[0] + d_row, current[1])
                side_b = (current[0], current[1] + d_col)
                if blocked[side_a] or blocked[side_b]:
                    continue
            tentative = g_score[current] + cost
            if tentative < g_score.get(neighbour, math.inf):
                g_score[neighbour] = tentative
                came_from[neighbour] = current
                heuristic = math.hypot(goal[0] - neighbour[0], goal[1] - neighbour[1])
                heapq.heappush(open_set, (tentative + heuristic, neighbour))
    return None


def path_length_m(path: list[Cell], resolution: float) -> float:
    """격자 경로의 실거리. 다음 구역 선정의 비용 함수다."""
    return (
        sum(
            math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
            for i in range(1, len(path))
        )
        * resolution
    )


def to_waypoints(path: list[Cell], grid: OccupancyGrid, eps_m: float) -> list[Point]:
    """격자 경로를 실좌표 웨이포인트로 줄인다 (Douglas-Peucker).

    셀 단위 경로를 그대로 추종하면 5cm 마다 방위를 다시 맞추게 되고, 호(arc)
    조향밖에 없는 로봇(ADR-11)은 그 자리에서 진동한다.
    """
    points = [grid.to_world(row, col) for row, col in path]
    return simplify(points, eps_m)


def simplify(points: list[Point], eps_m: float) -> list[Point]:
    """Douglas-Peucker. 재귀 깊이는 경로 길이의 로그이므로 문제되지 않는다."""
    if len(points) < 3:
        return list(points)

    first, last = points[0], points[-1]
    worst_index, worst_dist = 0, 0.0
    for index in range(1, len(points) - 1):
        dist = _perpendicular(points[index], first, last)
        if dist > worst_dist:
            worst_dist, worst_index = dist, index
    if worst_dist <= eps_m:
        return [first, last]
    left = simplify(points[: worst_index + 1], eps_m)
    right = simplify(points[worst_index:], eps_m)
    return left[:-1] + right


def _perpendicular(point: Point, start: Point, end: Point) -> float:
    if start == end:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    (x1, y1), (x2, y2) = start, end
    numerator = abs((y2 - y1) * point[0] - (x2 - x1) * point[1] + x2 * y1 - y2 * x1)
    return numerator / math.hypot(x2 - x1, y2 - y1)


@dataclass(frozen=True, slots=True)
class Plan:
    """한 구역으로 가는 계획. 경로가 없으면 `label` 이 `None` 이다.

    `requested` 는 호출자가 찍은 목표, `effective` 는 스냅 뒤 실제로 향하는 목표다.
    둘이 다르면 `goal_moved_m` 이 그 거리다 — 대시보드가 «찍은 곳» 과 «간 곳» 을
    구분해 표시하고, 로그는 목표가 조용히 바뀌지 않았음을 보증한다.
    """

    label: str | None
    waypoints: tuple[Point, ...] = ()
    length_m: float = 0.0
    #: 목표가 막힌 셀에 찍혀 가장 가까운 주행 가능 셀로 옮겨졌으면 그 거리(m).
    goal_moved_m: float = 0.0
    #: 출발 자세가 팽창·미관측 셀에 걸려 옮겨졌으면 그 거리(m). 0 이면 출발 그대로.
    start_moved_m: float = 0.0
    #: 호출자가 요청한 목표 좌표 (스냅 전).
    requested: Point | None = None
    #: 실제로 향하는 목표 좌표 (스냅 후). 실패한 계획에도 남겨 «어디를 못 갔나» 를 보인다.
    effective: Point | None = None
    #: 실패 이유 — «»(성공) | goal_unreachable | start_blocked | no_path
    fail_reason: str = ""

    @property
    def reachable(self) -> bool:
        return self.label is not None and bool(self.waypoints)


def reachable_mask(grid: OccupancyGrid, blocked: np.ndarray, start: Point) -> np.ndarray:
    """`start` 셀에서 자유 셀만으로 닿는 영역 (BFS). 시작 셀이 막혀 있으면 전부 False."""
    from collections import deque

    rows, cols = blocked.shape
    seen = np.zeros((rows, cols), dtype=bool)
    row, col = grid.to_cell(*start)
    if not (0 <= row < rows and 0 <= col < cols) or blocked[row, col]:
        return seen
    seen[row, col] = True
    queue = deque([(row, col)])
    while queue:
        r, c = queue.popleft()
        for d_row, d_col, _cost in MOVES:
            r2, c2 = r + d_row, c + d_col
            if not (0 <= r2 < rows and 0 <= c2 < cols) or seen[r2, c2] or blocked[r2, c2]:
                continue
            # A* 와 같은 코너 컷 방지 — 대각선 양옆이 막힌 틈은 «도달 가능» 이 아니다.
            if d_row != 0 and d_col != 0 and (blocked[r + d_row, c] or blocked[r, c + d_col]):
                continue
            seen[r2, c2] = True
            queue.append((r2, c2))
    return seen


def snap_to_free(
    grid: OccupancyGrid,
    blocked: np.ndarray,
    point: Point,
    max_m: float,
    allowed: np.ndarray | None = None,
) -> tuple[Point, float]:
    """`point` 가 막힌 셀에 있으면 가장 가까운 자유 셀을 찾아 돌려준다.

    가구 다리 병합으로 앵커·찍은 좌표가 팽창 안에 들어가는 일이 생긴다 —
    로봇이 실제로 설 수 있는 옆 자리까지는 가는 게 «못 간다» 보다 낫다.
    `max_m` 안에 자유 셀이 없으면 `(point, inf)` — 호출자가 목표를 버린다.

    `allowed`(출발점 도달 영역)가 주어지면 자유 셀이어도 그 안에 있는 것만 고른다 —
    지오메트리상 가까워도 벽 건너 고립된 섬이면 스냅해봤자 경로는 없다.
    """
    row, col = grid.to_cell(*point)
    rows, cols = blocked.shape
    if (
        0 <= row < rows
        and 0 <= col < cols
        and not blocked[row, col]
        and (allowed is None or allowed[row, col])
    ):
        return point, 0.0
    res = grid.meta.resolution
    ring = int(math.ceil(max_m / res))
    best: Point | None = None
    best_d = math.inf
    for d_row in range(-ring, ring + 1):
        for d_col in range(-ring, ring + 1):
            if d_row * d_row + d_col * d_col > ring * ring:
                continue
            r2, c2 = row + d_row, col + d_col
            if not (0 <= r2 < rows and 0 <= c2 < cols) or blocked[r2, c2]:
                continue
            if allowed is not None and not allowed[r2, c2]:
                continue
            dist = math.hypot(d_row, d_col) * res
            if dist < best_d:
                best_d = dist
                best = (
                    float(grid.meta.origin_x + (c2 + 0.5) * res),
                    float(grid.meta.origin_y + (r2 + 0.5) * res),
                )
    if best is None:
        return point, math.inf
    return best, float(best_d)


def plan_to(
    label: str,
    goal: Point,
    start: Point,
    grid: OccupancyGrid,
    blocked: np.ndarray,
    params: PlanParams,
    snap_m: float = 0.6,
) -> Plan:
    """`start → goal` A* 계획. 막힌 목표·출발은 `snap_m` 안의 가장 가까운 **도달 가능한**
    자유 셀로 옮긴다 — 옮겨진 목표는 `effective`·`goal_moved_m` 에 남는다."""
    start_free, start_moved = snap_to_free(grid, blocked, start, snap_m)
    if not math.isfinite(start_moved):
        return Plan(None, requested=(float(goal[0]), float(goal[1])), fail_reason="start_blocked")
    allowed = reachable_mask(grid, blocked, start_free)
    requested_xy = (float(goal[0]), float(goal[1]))
    goal_free, goal_moved = snap_to_free(grid, blocked, goal, snap_m, allowed=allowed)
    if not math.isfinite(goal_moved):
        return Plan(
            None, requested=requested_xy, start_moved_m=start_moved, fail_reason="goal_unreachable"
        )
    path = astar(grid.to_cell(*start_free), grid.to_cell(*goal_free), blocked)
    if path is None:
        return Plan(
            None,
            requested=requested_xy,
            effective=goal_free,
            start_moved_m=start_moved,
            goal_moved_m=goal_moved,
            fail_reason="no_path",
        )
    return Plan(
        label,
        tuple(to_waypoints(path, grid, params.simplify_eps_m)),
        path_length_m(path, grid.meta.resolution),
        goal_moved_m=goal_moved,
        start_moved_m=start_moved,
        requested=requested_xy,
        effective=goal_free,
    )


def detect_new_obstacle(
    pose: tuple[float, float, float],
    scan_points: tuple[tuple[float, float], ...],
    grid: OccupancyGrid,
    blocked: np.ndarray,
    *,
    check_radius_m: float,
    margin_m: float,
    occ_thresh: float,
    known_grid: OccupancyGrid | None = None,
) -> Point | None:
    """지도에 없던 장애물을 찾는다. 빔마다 **예상 거리와 실측을 비교한다.**

    `margin_m` 은 측위 오차를 감안한 여유다. 없으면 벽에서 몇 cm 어긋난 측위가
    벽 자체를 "새 장애물"로 보고 순찰 내내 재계획한다.

    `known_grid`(측위 지도)가 있으면 그쪽 점유 셀도 «아는 것»으로 친다 — 항법
    지도엔 없지만 측위 지도엔 있는 가구 다리가 주행 때마다 «새 장애물»로
    재확정되어 좁은 통로를 봉쇄하는 일을 막는다 (2026-10-03 실측 재현).
    """
    x0, y0, yaw = pose
    res = grid.meta.resolution
    steps = max(1, int(check_radius_m / res))
    for angle, dist in scan_points:
        if dist > check_radius_m:
            continue
        d_x, d_y = math.cos(angle + yaw), math.sin(angle + yaw)
        expected = check_radius_m
        for step in range(1, steps + 1):
            wx, wy = x0 + d_x * step * res, y0 + d_y * step * res
            row, col = grid.to_cell(wx, wy)
            if not grid.inside(row, col):
                break
            known_hit = False
            if known_grid is not None:
                k_row, k_col = known_grid.to_cell(wx, wy)
                known_hit = known_grid.inside(k_row, k_col) and (
                    known_grid.cells[k_row, k_col] >= occ_thresh
                )
            if grid.cells[row, col] >= occ_thresh or blocked[row, col] or known_hit:
                expected = step * res
                break
        if dist < expected - margin_m:
            return x0 + d_x * dist, y0 + d_y * dist
    return None


def min_forward_distance(
    scan_points: tuple[tuple[float, float], ...],
    half_angle_rad: float,
) -> float | None:
    """전방 부채꼴 안의 최단 거리. 호스트측 위험 판단에 쓴다."""
    forward = [
        dist
        for angle, dist in scan_points
        if abs((angle + math.pi) % (2 * math.pi) - math.pi) <= half_angle_rad
    ]
    return min(forward) if forward else None
