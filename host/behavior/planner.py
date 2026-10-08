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
    #: 추종 여유 안에서 탈출할 때도 지켜야 하는 몸체 반경. config의 실측 반경을 사용한다.
    body_radius_m: float = 0.15
    start_escape_max_m: float = 0.6
    inflation_radius_m: float = 0.55
    cost_scaling_factor: float = 3.0
    cost_weight: float = 0.0


def _collision_cells(
    grid: OccupancyGrid, obstacle_grid: OccupancyGrid, params: PlanParams, pad: int
) -> np.ndarray:
    """같은 격자 좌표계의 점유 증거만 합친 사본. 원본 자유/미관측 값은 덮지 않는다."""
    res = grid.meta.resolution
    offset_x = (obstacle_grid.meta.origin_x - grid.meta.origin_x) / res
    offset_y = (obstacle_grid.meta.origin_y - grid.meta.origin_y) / res
    # 확장된 loc 지도는 원점·크기가 달라도 같은 격자다. 좌표계를 모르는 두 지도를
    # 최근접 셀로 억지로 맞추면 다리가 사라지거나 옮겨지므로 시작 단계에서 거절한다.
    if not math.isclose(res, obstacle_grid.meta.resolution, rel_tol=0.0, abs_tol=1e-9) or any(
        not math.isclose(offset, round(offset), rel_tol=0.0, abs_tol=1e-6)
        for offset in (offset_x, offset_y)
    ):
        raise ValueError("navigation and obstacle grids must use the same aligned cell lattice")
    # 지도 바깥에 있는 점유 셀의 팽창도 안쪽에 닿을 수 있어 여유 폭까지 합친다.
    cells = np.pad(grid.cells, pad, constant_values=-np.inf)
    row, col = round(offset_y) + pad, round(offset_x) + pad
    height, width = cells.shape
    extra_height, extra_width = obstacle_grid.cells.shape
    r0, c0 = max(0, row), max(0, col)
    r1, c1 = min(height, row + extra_height), min(width, col + extra_width)
    if r0 < r1 and c0 < c1:
        target = cells[r0:r1, c0:c1]
        source = obstacle_grid.cells[r0 - row : r1 - row, c0 - col : c1 - col]
        # 사용자 선택(2026-10-03): nav에서 확인된 free 위 loc-only 점유는
        # 계획에서 제외한다. 원본 증거는 보존하고 실제 물체는 실시간 LiDAR로 잡는다.
        # nav unknown/occupied와 지도 밖의 점유는 이 예외로 열지 않는다.
        include = (source >= params.occ_thresh) & (
            (target > params.free_thresh) | np.isneginf(target)
        )
        np.maximum(target, source, out=target, where=include)
    return cells


def inflate(
    grid: OccupancyGrid, params: PlanParams, *, obstacle_grid: OccupancyGrid | None = None
) -> np.ndarray:
    """이동 불가 마스크. **막힌 셀과 미관측 셀 둘 다 막는다.**

    미관측(값 0)을 통과 가능으로 두면 A* 가 지도 밖 여백을 최단 경로로 골라
    나가고, 실제로는 그곳에 무엇이 있는지 모른다. 순찰 로봇에서 *모르는 곳*은
    *빈 곳*이 아니다 — `free_thresh` 이하로 **확실히 비었다고 관측된** 셀만
    통과를 허용한다.

    장애물은 두 층으로 부풀린다: `hard_thresh` 이상(원본 벽·반복 확인된 장애물)은
    `clearance_m` 전부, 그 미만(세션 지도 병합으로 들어온 가구 다리급)은
    `soft_clearance_m` 만 — 다리가 있는 곳까지 아예 못 서는 건 지도가 아니라 팽창 탓이다.
    """
    # 원본 지도는 수정하지 않는다. nav free 위 loc-only 점유만 계획에서 제외하며,
    # loc free로 nav 미관측을 열거나 nav 벽을 지우지 않는다.
    pad = 0
    cells = grid.cells
    if obstacle_grid is not None:
        pad = max(0, math.ceil(params.clearance_m / grid.meta.resolution))
        cells = _collision_cells(grid, obstacle_grid, params, pad)
    occupied = cells >= params.occ_thresh
    unknown = grid.cells > params.free_thresh
    radius_cells = int(math.ceil(params.clearance_m / grid.meta.resolution))
    inflated = dilate(occupied, radius_cells)
    if params.soft_clearance_m < params.clearance_m and occupied.any():
        hard = cells >= params.hard_thresh
        soft = occupied & ~hard
        soft_cells = int(math.ceil(params.soft_clearance_m / grid.meta.resolution))
        # 단단한 층 주변은 clearance_m 전부가 이미 막는다 — soft 부풀림을 OR 하면
        # hard 셀 옆 soft 셀이 soft 반경만큼 더 막아 버리지 않게, 결과를 재구성한다.
        inflated = dilate(hard, radius_cells) | dilate(soft, max(soft_cells, 0))
    height, width = grid.cells.shape
    blocked = unknown | inflated[pad : pad + height, pad : pad + width]
    return cast(np.ndarray, blocked)


def body_collision_mask(
    grid: OccupancyGrid, params: PlanParams, *, obstacle_grid: OccupancyGrid | None = None
) -> np.ndarray:
    """탈출 구간의 몸체 충돌 마스크. 셀 사각형 사이 거리로 전체 몸체를 검사한다.

    셀 중심만 15cm 떨어져 있어도 셀 가장자리에서는 몸이 닿을 수 있다.
    후보 셀의 어느 위치에서도 장애물/미관측과 반경만큼 떨어지는 셀만 허용한다.
    """
    radius = params.body_radius_m
    if not math.isfinite(radius) or radius <= 0 or radius > params.clearance_m:
        raise ValueError("body radius must be positive and no larger than planning clearance")
    res = grid.meta.resolution
    pad = math.ceil(radius / res) + 1
    cells = (
        _collision_cells(grid, obstacle_grid, params, pad)
        if obstacle_grid is not None
        else np.pad(grid.cells, pad, constant_values=-np.inf)
    )
    unsafe = cells > params.free_thresh
    unsafe[:pad, :] = unsafe[-pad:, :] = True
    unsafe[:, :pad] = unsafe[:, -pad:] = True
    out = unsafe.copy()
    height, width = unsafe.shape
    for dr in range(-pad, pad + 1):
        for dc in range(-pad, pad + 1):
            gap = math.hypot(max(abs(dr) - 1, 0), max(abs(dc) - 1, 0)) * res
            if gap >= radius - 1e-9 and (dr or dc):
                continue
            src = (slice(max(0, -dr), height - max(0, dr)), slice(max(0, -dc), width - max(0, dc)))
            dst = (slice(max(0, dr), height - max(0, -dr)), slice(max(0, dc), width - max(0, -dc)))
            out[dst] |= unsafe[src]
    h, w = grid.cells.shape
    return cast(np.ndarray, out[pad : pad + h, pad : pad + w])


def segment_clear(grid: OccupancyGrid, blocked: np.ndarray, start: Point, end: Point) -> bool:
    """직선이 닿는 모든 셀 검사. 모서리/격자선 양쪽 셀을 포함해 코너 절단을 막는다."""
    res = grid.meta.resolution
    a = ((start[0] - grid.meta.origin_x) / res, (start[1] - grid.meta.origin_y) / res)
    b = ((end[0] - grid.meta.origin_x) / res, (end[1] - grid.meta.origin_y) / res)
    if not all(math.isfinite(v) for v in (*a, *b)):
        return False
    times = {0.0, 1.0}
    for v0, v1 in zip(a, b, strict=True):
        if v0 != v1:
            for boundary in range(math.ceil(min(v0, v1)), math.floor(max(v0, v1)) + 1):
                t = (boundary - v0) / (v1 - v0)
                if 0 <= t <= 1:
                    times.add(t)
    ordered = sorted(times)
    times.update((left + right) / 2 for left, right in zip(ordered, ordered[1:], strict=False))
    for t in times:
        x, y = (a[i] + t * (b[i] - a[i]) for i in range(2))
        for dx in (-1e-9, 1e-9):
            for dy in (-1e-9, 1e-9):
                row, col = math.floor(y + dy), math.floor(x + dx)
                if not grid.inside(row, col) or blocked[row, col]:
                    return False
    return True


def escape_start(
    grid: OccupancyGrid, blocked: np.ndarray, body_blocked: np.ndarray, start: Point, max_m: float
) -> tuple[Point, ...] | None:
    """몸체 여유를 유지하며 가까운 정상 자유 셀로 연결한다. 위치를 옮겨 가정하지 않는다."""
    if not math.isfinite(max_m) or max_m <= 0:
        return None
    cell = grid.to_cell(*start)
    if not grid.inside(*cell) or not segment_clear(grid, body_blocked, start, start):
        return None
    if segment_clear(grid, blocked, start, start):
        return (start,)
    center = grid.to_world(*cell)
    if not segment_clear(grid, body_blocked, start, center):
        return None
    initial = math.dist(start, center)
    queue = [(initial, cell)]
    costs = {cell: initial}
    parent: dict[Cell, Cell] = {}
    while queue:
        cost, current = heapq.heappop(queue)
        if cost > costs[current] or cost > max_m:
            continue
        if not blocked[current]:
            path = [current]
            while current in parent:
                current = parent[current]
                path.append(current)
            points = [start, *(grid.to_world(*p) for p in reversed(path))]
            return tuple(p for i, p in enumerate(points) if i == 0 or p != points[i - 1])
        for dr, dc, step in MOVES:
            nxt = (current[0] + dr, current[1] + dc)
            if not grid.inside(*nxt) or body_blocked[nxt]:
                continue
            if (
                dr
                and dc
                and (
                    body_blocked[current[0] + dr, current[1]]
                    or body_blocked[current[0], current[1] + dc]
                )
            ):
                continue
            new_cost = cost + step * grid.meta.resolution
            if new_cost <= max_m and new_cost < costs.get(nxt, math.inf):
                costs[nxt], parent[nxt] = new_cost, current
                heapq.heappush(queue, (new_cost, nxt))
    return None


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
    res = grid.meta.resolution
    radius_cells = int(math.ceil(radius_m / res)) + 1
    height, width = blocked.shape
    r0, r1 = max(0, row0 - radius_cells), min(height, row0 + radius_cells + 1)
    c0, c1 = max(0, col0 - radius_cells), min(width, col0 + radius_cells + 1)
    if r0 >= r1 or c0 >= c1:
        return
    # 실제 끝점 원판과 셀 사각형의 교차. 끝점/반경은 격자로 반올림하지 않는다.
    x = grid.meta.origin_x + np.arange(c0, c1) * res
    y = grid.meta.origin_y + np.arange(r0, r1) * res
    dx = np.maximum(np.maximum(x - hit[0], hit[0] - (x + res)), 0)
    dy = np.maximum(np.maximum(y - hit[1], hit[1] - (y + res)), 0)
    blocked[r0:r1, c0:c1] |= dy[:, None] ** 2 + dx[None, :] ** 2 <= radius_m**2


def distance_costs(
    blocked: np.ndarray, resolution: float, radius_m: float, scaling: float
) -> np.ndarray:
    """충돌 경계 밖에서 지수 감소하는 통행 비용. 이진 안전 마스크는 유지한다."""
    out = np.zeros(blocked.shape, dtype=np.float32)
    height, width = blocked.shape
    ring = math.ceil(radius_m / resolution)
    for dr in range(-ring, ring + 1):
        for dc in range(-ring, ring + 1):
            distance = math.hypot(dr, dc) * resolution
            if distance > radius_m:
                continue
            src = (slice(max(0, -dr), height - max(0, dr)), slice(max(0, -dc), width - max(0, dc)))
            dst = (slice(max(0, dr), height - max(0, -dr)), slice(max(0, dc), width - max(0, -dc)))
            np.maximum(out[dst], blocked[src] * math.exp(-scaling * distance), out=out[dst])
    return out


def astar(
    start: Cell, goal: Cell, blocked: np.ndarray, costs: np.ndarray | None = None
) -> list[Cell] | None:
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
            tentative = g_score[current] + cost * (
                1.0 + (float(costs[neighbour]) if costs is not None else 0.0)
            )
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


def _weighted_length(grid: OccupancyGrid, costs: np.ndarray, start: Point, end: Point) -> float:
    length = math.dist(start, end)
    count = max(2, math.ceil(length / grid.meta.resolution * 2) + 1)
    xs, ys = np.linspace(start[0], end[0], count), np.linspace(start[1], end[1], count)
    cols = np.floor((xs - grid.meta.origin_x) / grid.meta.resolution).astype(int)
    rows = np.floor((ys - grid.meta.origin_y) / grid.meta.resolution).astype(int)
    if (
        np.any(rows < 0)
        or np.any(cols < 0)
        or np.any(rows >= costs.shape[0])
        or np.any(cols >= costs.shape[1])
    ):
        return math.inf
    return length * (1.0 + float(costs[rows, cols].mean()))


def cost_waypoints(
    points: list[Point], grid: OccupancyGrid, blocked: np.ndarray, costs: np.ndarray
) -> list[Point]:
    """충돌 없고 비용 증가 2% 이내인 선분만 합친다. 비용 경로의 잔떨림을 줄인다."""
    if len(points) < 3:
        return points
    accumulated = [0.0]
    for a, b in zip(points, points[1:], strict=False):
        accumulated.append(accumulated[-1] + _weighted_length(grid, costs, a, b))
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        chosen = index + 1
        for end in range(min(len(points) - 1, index + 30), index + 1, -1):
            if (
                segment_clear(grid, blocked, points[index], points[end])
                and _weighted_length(grid, costs, points[index], points[end])
                <= (accumulated[end] - accumulated[index]) * 1.02
            ):
                chosen = end
                break
        result.append(points[chosen])
        index = chosen
    return result


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
    #: 실제로 주행할 탈출 연결의 시작→끝 거리(m). 위치를 옮긴 것으로 가정하지 않는다.
    start_moved_m: float = 0.0
    #: 호출자가 요청한 목표 좌표 (스냅 전).
    requested: Point | None = None
    #: 실제로 향하는 목표 좌표 (스냅 후). 실패한 계획에도 남겨 «어디를 못 갔나» 를 보인다.
    effective: Point | None = None
    #: 실패 이유 — «»(성공) | goal_unreachable | start_occupied | start_unobserved
    #: | start_outside_map
    #: | start_clearance_blocked | no_path
    fail_reason: str = ""
    #: 이 인덱스까지는 몸체 반경으로 검사한 탈출 구간. 점을 생략할 때도 연결을 재검사한다.
    escape_end_index: int = -1

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
        and segment_clear(grid, blocked, point, point)
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
            candidate = grid.to_world(r2, c2)
            dist = math.dist(point, candidate)
            if dist > max_m:
                continue
            if dist < best_d:
                best_d = dist
                best = candidate
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
    *,
    body_blocked: np.ndarray | None = None,
    costs: np.ndarray | None = None,
) -> Plan:
    """실제 출발점부터 계획한다. 여유 영역에서는 몸체 검사한 탈출 연결을 포함한다."""
    requested_xy = (float(goal[0]), float(goal[1]))
    start_cell = grid.to_cell(*start)
    if not grid.inside(*start_cell):
        return Plan(None, requested=requested_xy, fail_reason="start_outside_map")
    # 호출자가 잘못 만든 마스크도 미관측·점유 출발을 자유 공간으로 바꿀 수 없다.
    if grid.cells[start_cell] >= params.occ_thresh:
        return Plan(None, requested=requested_xy, fail_reason="start_occupied")
    if not grid.cells[start_cell] <= params.free_thresh:
        return Plan(None, requested=requested_xy, fail_reason="start_unobserved")
    connector: tuple[Point, ...] = ()
    start_free, start_moved = start, 0.0
    # 격자선 위 자세는 to_cell 한 칸뿐 아니라 접촉한 양쪽 칸을 검사한다.
    # 첫 선분과 같은 기준을 써야 자유 셀에서 탈출 연결을 건너뛰는 no_path가 없다.
    if not segment_clear(grid, blocked, start, start):
        if body_blocked is None:
            # 출처가 불명확한 추가 차단(동적 표시 등)은 여유 완화로 버리지 않는다.
            body_blocked = body_collision_mask(grid, params) | (blocked & ~inflate(grid, params))
        connector = (
            escape_start(grid, blocked, body_blocked, start, params.start_escape_max_m) or ()
        )
        if not connector:
            return Plan(None, requested=requested_xy, fail_reason="start_clearance_blocked")
        start_free = connector[-1]
        start_moved = math.dist(start, start_free)
    allowed = reachable_mask(grid, blocked, start_free)
    goal_free, goal_moved = snap_to_free(grid, blocked, goal, snap_m, allowed=allowed)
    if not math.isfinite(goal_moved):
        return Plan(
            None, requested=requested_xy, start_moved_m=start_moved, fail_reason="goal_unreachable"
        )
    if costs is None and params.cost_weight > 0:
        costs = (
            distance_costs(
                blocked, grid.meta.resolution, params.inflation_radius_m, params.cost_scaling_factor
            )
            * params.cost_weight
        )
    path = astar(grid.to_cell(*start_free), grid.to_cell(*goal_free), blocked, costs)
    if path is None:
        return Plan(
            None,
            requested=requested_xy,
            effective=goal_free,
            start_moved_m=start_moved,
            goal_moved_m=goal_moved,
            fail_reason="no_path",
        )
    # 비용으로 벽에서 떨어진 경로를 직선 단순화가 다시 벽 옆으로 자르지 않는다.
    points = to_waypoints(path, grid, 0.0 if costs is not None else params.simplify_eps_m)
    points = [start_free, *points[1:-1], goal_free] if len(points) > 1 else [start_free, goal_free]
    if any(
        not segment_clear(grid, blocked, a, b) for a, b in zip(points, points[1:], strict=False)
    ):
        points = [start_free, *(grid.to_world(*p) for p in path[1:-1]), goal_free]
    if any(
        not segment_clear(grid, blocked, a, b) for a, b in zip(points, points[1:], strict=False)
    ):
        return Plan(None, requested=requested_xy, fail_reason="no_path")
    if costs is not None:
        points = cost_waypoints(points, grid, blocked, costs)
    escape_end = len(connector) - 1 if connector else -1
    if connector:
        points = [*connector, *points[1:]]
    return Plan(
        label,
        tuple(points),
        sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False)),
        goal_moved_m=goal_moved,
        start_moved_m=start_moved,
        requested=requested_xy,
        effective=goal_free,
        escape_end_index=escape_end,
    )


def _expected_obstacle_distance(
    grid: OccupancyGrid,
    blocked: np.ndarray,
    start: Point,
    direction: Point,
    limit: float,
    occ_thresh: float,
) -> float:
    """격자 경계로 빔을 나눠 얇게 스치는 셀도 검사한다 (거리 고정 샘플 금지)."""
    res = grid.meta.resolution
    origin = (grid.meta.origin_x, grid.meta.origin_y)
    distances = {0.0, limit}
    for value, delta, offset in zip(start, direction, origin, strict=True):
        if abs(delta) < 1e-12:
            continue
        a, b = (value - offset) / res, (value + delta * limit - offset) / res
        for boundary in range(math.ceil(min(a, b)), math.floor(max(a, b)) + 1):
            distance = (boundary * res + offset - value) / delta
            if 0 <= distance <= limit:
                distances.add(distance)
    ordered = sorted(distances)
    for entry, end in zip(ordered, ordered[1:], strict=False):
        # 경계 양쪽·코너 접촉 셀과 구간 내부 모두 확인한다.
        for distance in (entry, (entry + end) / 2):
            x = start[0] + direction[0] * distance
            y = start[1] + direction[1] * distance
            for dx, dy in ((-1e-9, -1e-9), (-1e-9, 1e-9), (1e-9, -1e-9), (1e-9, 1e-9)):
                row, col = grid.to_cell(x + dx, y + dy)
                if grid.inside(row, col) and (
                    grid.cells[row, col] >= occ_thresh or blocked[row, col]
                ):
                    return entry
    return limit


def detect_new_obstacle(
    pose: tuple[float, float, float],
    scan_points: tuple[tuple[float, float], ...],
    grid: OccupancyGrid,
    blocked: np.ndarray,
    *,
    check_radius_m: float,
    margin_m: float,
    occ_thresh: float,
    known_tolerance_m: float = 0.0,
) -> Point | None:
    """지도에 없던 장애물을 찾는다. 빔마다 **예상 거리와 실측을 비교한다.**

    `margin_m` 은 측위 오차를 감안한 여유다. 없으면 벽에서 몇 cm 어긋난 측위가
    벽 자체를 "새 장애물"로 보고 순찰 내내 재계획한다.

    예상 반사는 nav 점유와 확인된 동적 표시(`blocked`)만 사용한다. 계획에서 제외한
    loc-only 물체와 정적 추종 여유로 새 반사를 숨기지 않는다. 거리 고정 샘플은 빔이
    모서리만 스치는 점유 셀을 건너뛰므로 실제 통과 셀의 경계를 검사한다.
    """
    x0, y0, yaw = pose
    for angle, dist in scan_points:
        if (
            not math.isfinite(angle)
            or not math.isfinite(dist)
            or not 0 < dist < check_radius_m - margin_m
        ):
            continue
        d_x, d_y = math.cos(angle + yaw), math.sin(angle + yaw)
        expected = _expected_obstacle_distance(
            grid, blocked, (x0, y0), (d_x, d_y), check_radius_m, occ_thresh
        )
        if dist < expected - margin_m:
            hit = (x0 + d_x * dist, y0 + d_y * dist)
            # nav 점유 셀에서 `known_tolerance_m` 안의 반사는 «새 물체» 가 아니다 — 벽을 비스듬히
            # 스치는 빔은 방위 1° 차이로도 예상 거리가 크게 달라져, 벽 옆을 지날 때마다 «새
            # 장애물» 로 확정되고 정지·재계획했다(2026-10-03 집 지도 시뮬 8건, 모두 벽에서
            # 5~7cm). 허용치는 호출자가 `계획 여유 − 몸체 반경` 으로 준다 — 벽에서 그만큼 안의
            # 물체는 벽에서 계획 여유만큼 떨어진 경로와 몸체 반경 이상 떨어진다.
            # ⚠️ 확인된 동적 표시(`blocked`)는 «아는 것» 에 넣지 않는다 — 이미 반경으로 넓힌
            # 표시에 허용치를 더하면 그 바깥의 새 물체까지 숨긴다 (리뷰 지적).
            if known_tolerance_m > 0 and _near_known_obstacle(
                grid, hit, known_tolerance_m, occ_thresh
            ):
                continue
            return hit
    return None


def _near_known_obstacle(
    grid: OccupancyGrid, point: Point, radius_m: float, occ_thresh: float
) -> bool:
    """`point` 에서 `radius_m` 안에 nav 점유 셀이 있는가."""
    row, col = grid.to_cell(*point)
    ring = int(math.ceil(radius_m / grid.meta.resolution))
    height, width = grid.cells.shape
    r0, r1 = max(0, row - ring), min(height, row + ring + 1)
    c0, c1 = max(0, col - ring), min(width, col + ring + 1)
    if r0 >= r1 or c0 >= c1:
        return False
    rows, cols = np.mgrid[r0:r1, c0:c1]
    inside = (rows - row) ** 2 + (cols - col) ** 2 <= ring * ring
    known = grid.cells[r0:r1, c0:c1] >= occ_thresh
    return bool((known & inside).any())


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
