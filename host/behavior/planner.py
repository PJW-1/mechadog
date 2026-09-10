"""경로 계획 — 팽창 · A* · 웨이포인트 · 다음 구역 선정 (FR-7.2/7.3 · Phase 2).

**직선거리로 다음 구역을 고르지 않는다.** 벽 하나 건너 3m 인 구역이 돌아가면
9m 이므로, 가까운 미방문 구역은 **실제 A* 경로 길이**로 정한다 (FR-7.3). 그래서
`select_next` 가 후보마다 경로를 한 번씩 푼다 — 구역이 3개(`zones.ids`)이므로
사이클당 최대 3번이고, 이 규모에서는 비용이 문제되지 않는다.

⚠️ **scipy 를 쓰지 않는다.** 합치기 전 코드는 `scipy.ndimage.distance_transform_edt`
로 팽창을 계산했는데, scipy 는 `requirements.txt` 에 없다. 팽창 반경이 `0.15 m /
0.05 m = 3` 셀뿐이라 **원판 커널 하나로 끝나므로**, 의존성을 늘리기보다 numpy 로
직접 부풀린다. 새 런타임 의존성은 팀 전원의 환경 문제가 되고(CONTRIBUTING 8절
onnxruntime 사고가 그 예다), 얻는 것은 3셀짜리 거리변환 하나다.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

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
    #: 경로 중심선이 장애물에서 유지할 거리 = **로봇 반경 + 추종 여유**.
    #:
    #: ⚠️ **호스트 E-STOP 거리보다 커야 한다.** 같거나 작으면, 경로를 완벽히
    #: 따라 걷는 것만으로 LiDAR 가 E-STOP 거리를 읽어 **정상 순찰이 비상정지로
    #: 끝난다.** 실제로 그렇게 만들었더니 (반경 150mm = E-STOP 150mm) 한 사이클에
    #: E-STOP 이 6회 나고 구역 하나를 못 갔다. 여유가 0 이면 추종 오차가
    #: 0 이어야 하는데, 호 조향밖에 없는 로봇에 그것을 요구할 수 없다.
    #:
    #: `settings._validate` 가 이 관계를 기동 시에 확인한다.
    clearance_m: float
    simplify_eps_m: float


def inflate(grid: OccupancyGrid, params: PlanParams) -> np.ndarray:
    """이동 불가 마스크. **막힌 셀과 미관측 셀 둘 다 막는다.**

    미관측(값 0)을 통과 가능으로 두면 A* 가 지도 밖 여백을 최단 경로로 골라
    나가고, 실제로는 그곳에 무엇이 있는지 모른다. 순찰 로봇에서 *모르는 곳*은
    *빈 곳*이 아니다 — `free_thresh` 이하로 **확실히 비었다고 관측된** 셀만
    통과를 허용한다.
    """
    occupied = grid.cells >= params.occ_thresh
    unknown = grid.cells > params.free_thresh
    radius_cells = int(math.ceil(params.clearance_m / grid.meta.resolution))
    return unknown | dilate(occupied, radius_cells)


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

    ⚠️ 지도(`grid.cells`)를 고치지 않는 것이 요점이다. 순찰 중 지나가는 사람을
    지도에 벽으로 새기면 그 사람이 떠난 뒤에도 영원히 돌아가고, `slam_map.npy`
    를 다시 만들어야 한다. 동적 장애물은 **이번 순찰의 사실**이지 공간의 사실이
    아니다.
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

    ⚠️ **출발 셀이 막혀 있어도 탐색을 시작한다.** 측위 오차나 팽창 때문에 로봇이
    자기 위치를 막힌 셀로 볼 때가 있는데, 거기서 거절하면 로봇이 스스로 갇혀서
    영원히 못 움직인다. 목표 셀은 반대로 막혔으면 즉시 거절한다 — 갈 수 없는
    곳이 목적지면 탐색이 격자 전체를 훑고 나서야 실패한다.
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
    조향밖에 없는 로봇(DR-11)은 그 자리에서 진동한다.
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
    """한 구역으로 가는 계획. 경로가 없으면 `label` 이 `None` 이다."""

    label: str | None
    waypoints: tuple[Point, ...] = ()
    length_m: float = 0.0

    @property
    def reachable(self) -> bool:
        return self.label is not None and bool(self.waypoints)


def plan_to(
    label: str,
    goal: Point,
    start: Point,
    grid: OccupancyGrid,
    blocked: np.ndarray,
    params: PlanParams,
) -> Plan:
    path = astar(grid.to_cell(*start), grid.to_cell(*goal), blocked)
    if path is None:
        return Plan(None)
    return Plan(
        label,
        tuple(to_waypoints(path, grid, params.simplify_eps_m)),
        path_length_m(path, grid.meta.resolution),
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
) -> Point | None:
    """지도에 없던 장애물을 찾는다. 빔마다 **예상 거리와 실측을 비교한다.**

    `margin_m` 은 측위 오차를 감안한 여유다. 없으면 벽에서 몇 cm 어긋난 측위가
    벽 자체를 "새 장애물"로 보고 순찰 내내 재계획한다.
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
            row, col = grid.to_cell(x0 + d_x * step * res, y0 + d_y * step * res)
            if not grid.inside(row, col):
                break
            if grid.cells[row, col] >= occ_thresh or blocked[row, col]:
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
