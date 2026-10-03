"""스캔 정합 측위와 매핑 — **P2 에서 `slam_toolbox` 로 교체될 자리** (FR-6 · ADR-7 · ADR-9).

잠정 구현이다 — `slam_toolbox` 가 서면(WBS 5.4.1) 빠진다 (ADR-9). 교체 접점은 지도 형식
하나(`occupancy.OccupancyGrid.load()`)라 루프 클로저 등 성능을 더 들이지 않는다.

---

멈춰서 재고 다시 걷는다 (ADR-7) — 이동 → 정지 → `settle_delay_ms` 대기 → 복수 스캔 → 병합
→ 정합. 정합은 상관 기반 완전 탐색이다(오도메트리가 약해 ICP 는 지역해에 빠진다). 탐색
범위는 한 사이클의 이동량(`move_increment_mm`)에서 나온다.

IMU yaw 는 직전 스캔 이후의 변화량(`yaw_delta`)으로만 쓴다 — 지도 좌표계와 IMU 의 0 사이에는
임의의 옵셋이 있어 절대값을 탐색 중심으로 쓰면 자세가 회전된 채 잡힌다.

소켓도 시각도 만지지 않는다 (ENGINEERING_GUIDE 2.1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from host.common.units import wrap_pi
from host.slam.occupancy import OccupancyGrid

#: `(x, y, yaw)` — m · m · rad. 내부 단위다 (`units.py`).
Pose = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class MatchParams:
    """정합 탐색 파라미터. 전부 `config.yaml` 의 `lidar:` 절에서 온다."""

    search_lin_m: float
    search_lin_step_m: float
    search_ang_rad: float
    search_ang_step_rad: float
    occ_thresh: float
    #: 이만큼 관측된 셀이 있어야 정합을 시도한다. 빈 지도에 정합하면 점수가
    #: 전부 0 이라 **탐색 격자의 첫 후보가 그대로 채택된다** — 우연히 정해진
    #: 자세로 지도를 시작하는 셈이라, 차라리 예측값을 그대로 쓰는 것이 낫다.
    min_known_cells: int
    #: 우도장 폭(m). 0 이면 옛 «적중 개수» 점수, 양수면 벽까지의 거리로 매긴 매끄러운
    #: 점수(`OccupancyGrid.likelihood_field`)다 — 5cm 비껴간 점도 부분 점수를 받아
    #: 봉우리가 하나로 모인다.
    sigma_m: float = 0.0


@dataclass(frozen=True, slots=True)
class MatchResult:
    """정합 결과. **점수를 함께 돌려주는 것이 요점이다.**

    점수 0 은 *"아는 벽 위에 한 점도 얹히지 않았다"* 이고 그것이 곧 측위 상실
    (FR-6.6 · `Event.POSE_STALE` → `LOST`)이다. 자세만 돌려주면 호출자가 실패를
    구분할 수 없어 **틀린 위치를 믿고 순찰한다.**
    """

    pose: Pose
    #: 설명된 점의 수 — 적중 개수 점수면 정수 그대로, 우도장 점수면 점당 0~1 합을 반올림한 값.
    #: `score / len(points)` 가 두 방식 모두 «정합률» 이다.
    score: int
    #: 정합을 시도조차 못한 경우 (지도가 아직 비었다 · 점이 없다).
    skipped: bool = False
    #: 최고점의 90% 이상을 받으면서 최고점에서 **0.5m 넘게 떨어진** 후보 수 — 전역 탐색의
    #: 모호성 지표. 같은 봉우리의 이웃 칸(우도장은 넓은 봉우리를 만든다)은 세지 않고
    #: **경쟁하는 다른 자리**만 센다. 높으면 어디를 골라도 근거가 없다.
    peers: int = 0


def preprocess(points: tuple[tuple[float, float], ...], min_m: float, max_m: float) -> np.ndarray:
    """유효 거리만 남기고 로봇 기준 XY 로 바꾼다 (m).

    유효 범위 밖은 0 으로 바꾸지 않고 버린다(발밑에 벽이 찍히지 않게).
    """
    kept = [
        (angle, dist) for angle, dist in points if math.isfinite(dist) and min_m <= dist <= max_m
    ]
    if not kept:
        return np.zeros((0, 2), dtype=np.float64)
    return np.array([[d * math.cos(a), d * math.sin(a)] for a, d in kept], dtype=np.float64)


def merge_batch(
    batch: list[tuple[tuple[float, float], ...]],
    *,
    bins: int = 360,
) -> tuple[tuple[float, float], ...]:
    """정지 상태에서 모은 복수 스캔을 각도 빈별 **중앙값**으로 합친다.

    평균이 아니라 중앙값인 이유 — 반사·먼지로 인한 한 발의 이상치가 평균은
    끌고 가지만 중앙값은 못 움직인다. 스캔을 여러 장 모으는 목적 자체가 그
    이상치를 지우는 것이므로 평균을 쓰면 모은 값이 반만 쓰인다.
    """
    buckets: list[list[float]] = [[] for _ in range(bins)]
    for scan in batch:
        for angle, dist in scan:
            index = int((angle % (2 * math.pi)) / (2 * math.pi) * bins) % bins
            buckets[index].append(dist)
    merged: list[tuple[float, float]] = []
    for index, values in enumerate(buckets):
        if values:
            angle = (index + 0.5) / bins * 2 * math.pi
            merged.append((angle, float(np.median(values))))
    return tuple(merged)


def rotate(points: np.ndarray, yaw: float) -> np.ndarray:
    cos_y, sin_y = math.cos(yaw), math.sin(yaw)
    return points @ np.array([[cos_y, -sin_y], [sin_y, cos_y]], dtype=np.float64).T


def to_world(points_robot: np.ndarray, pose: Pose) -> np.ndarray:
    """로봇 기준 점을 실공간으로 옮긴다."""
    if points_robot.size == 0:
        return points_robot
    x, y, yaw = pose
    return rotate(points_robot, yaw) + np.array([x, y], dtype=np.float64)


def predict(previous: Pose, current: Pose) -> Pose:
    """등속 가정으로 다음 자세를 예측한다. 탐색의 **중심**이다.

    오도메트리가 없으므로(`gait_calibration` 미실측) 직전 두 자세의 차이를
    그대로 쓴다. 예측이 틀려도 탐색 범위가 덮으면 정합이 바로잡는다.
    """
    return (
        current[0] + (current[0] - previous[0]),
        current[1] + (current[1] - previous[1]),
        current[2],
    )


def match(
    grid: OccupancyGrid,
    points_robot: np.ndarray,
    center: Pose,
    params: MatchParams,
    *,
    yaw_delta: float = 0.0,
) -> MatchResult:
    """지도에 스캔을 정합해 최적 자세를 찾는다.

    `yaw_delta` 는 **직전 스캔 이후 IMU 가 본 회전량**이며 지도 좌표계의 이전
    방위에 더해진다. 절대 yaw 를 받지 않는 이유는 머리말에 적었다 — 좌표계가
    다르므로 절대값을 중심으로 쓰면 정답이 탐색 범위 밖에 있게 된다.
    """
    if points_robot.size == 0 or grid.known_cells() < params.min_known_cells:
        return MatchResult(center, 0, skipped=True)

    scorer = _scorer(grid, params.occ_thresh, params.sigma_m)
    base_yaw = wrap_pi(center[2] + yaw_delta)
    best_score = -1.0
    best_pose = center

    angle_offsets = np.arange(
        -params.search_ang_rad,
        params.search_ang_rad + 1e-9,
        params.search_ang_step_rad,
    )
    linear_offsets = np.arange(
        -params.search_lin_m,
        params.search_lin_m + 1e-9,
        params.search_lin_step_m,
    )

    for d_yaw in angle_offsets:
        yaw = base_yaw + float(d_yaw)
        rotated = rotate(points_robot, yaw)
        for d_x in linear_offsets:
            for d_y in linear_offsets:
                x = center[0] + float(d_x)
                y = center[1] + float(d_y)
                score = scorer(rotated + np.array([x, y]))
                if score > best_score:
                    best_score = score
                    best_pose = (x, y, wrap_pi(yaw))
    if best_score <= 0:
        # 겹친 점이 하나도 없으면 첫 탐색 후보가 우연히 best_pose가 된다.
        # 실패 결과에는 입력 중심을 보존해야 위치가 탐색 창 끝으로 튀지 않는다.
        return MatchResult(center, 0)
    return MatchResult(best_pose, _as_count(best_score))


def _scorer(grid: OccupancyGrid, occ_thresh: float, sigma_m: float):
    """점수 함수 하나를 고른다 — 우도장(σ>0) 또는 적중 개수. 우도장은 정합당 한 번만 꺼낸다."""
    if sigma_m > 0:
        field = grid.likelihood_field(occ_thresh, sigma_m)
        return lambda points_world: grid.score_field(points_world, field)
    return lambda points_world: float(grid.score(points_world, occ_thresh))


def _as_count(score: float) -> int:
    """실수 점수를 «설명된 점의 수» 정수로. 0 보다 큰 점수는 최소 1 — 성공을 0 과 구분한다."""
    return max(1, int(round(score))) if score > 0 else 0


def global_match(
    grid: OccupancyGrid,
    points_robot: np.ndarray,
    *,
    lin_step_m: float,
    ang_step_rad: float,
    occ_thresh: float,
    min_known_cells: int,
    max_points: int = 160,
    #: LD19 는 한 패킷에 ~72점을 준다 — 그 이하(잘린 스캔·막힌 시야)면 전역 정합의
    #: 변별력이 없다.
    min_points: int = 48,
    #: 로봇이 서 있을 셀의 로그오즈 상한 — **관측된 빈 바닥**만 후보다. 미관측 셀을
    #: 허용하면 지도 밖 여백에서 벽 조각에 우연히 얹히는 가짜 최적이 나온다(실측 확인).
    free_thresh: float = -0.5,
    refine_ang_step_rad: float = math.radians(2.0),
    sigma_m: float = 0.0,
) -> MatchResult | None:
    """지도 전체를 거친 격자로 훑어 최적 자세를 찾는다 — 잠김 복구·임의 배치 재측위용.

    `match` 는 추정 위치 ±`search_lin_m` 창 안에서만 답을 찾으므로 처음 수렴한 자리가
    틀리면(대칭 코너 오정합) 영원히 못 나온다. 이것은 알려진 셀이 있는 한 지도 전체를
    `lin_step_m` × `ang_step_rad` 로 훑은 뒤 최고점 주변을 좁은 창으로 다시 다듬는다.

    수 초 걸리는 복구용 일회성 탐색이다 — 매 스캔 부르는 용도가 아니다.
    점수가 0 이거나 지도/스캔이 비어 있으면 `None` 을 돌려준다.

    점이 `min_points` 미만인 스캔(가구에 둘러싸인 시야 등)은 짧은 범위 덩어리라
    지도의 조밀한 벽 포켓 어디에나 얹힌다 — 그런 스캔으로 전역 정합하면 frac 은
    높게 나오는데 자리는 틀리므로 아예 시도하지 않는다.
    """
    if points_robot.shape[0] < min_points or grid.known_cells() < min_known_cells:
        return None
    pts = points_robot
    if pts.shape[0] > max_points:
        pts = pts[np.linspace(0, pts.shape[0] - 1, max_points).astype(np.int64)]
    meta = grid.meta
    # 한 번이라도 관측된 셀의 경계상자만 훑는다 — 미지 영역은 점수가 없으니
    # 들를 이유가 없고, 격자가 자라도 탐색량이 팽창하지 않는다.
    known_rows, known_cols = np.nonzero(np.abs(grid.cells) > 0.01)
    if known_rows.size < min_known_cells:
        return None
    x_lo = meta.origin_x + known_cols.min() * meta.resolution - lin_step_m
    x_hi = meta.origin_x + (known_cols.max() + 1) * meta.resolution + lin_step_m
    y_lo = meta.origin_y + known_rows.min() * meta.resolution - lin_step_m
    y_hi = meta.origin_y + (known_rows.max() + 1) * meta.resolution + lin_step_m
    xs = np.arange(x_lo, x_hi, lin_step_m)
    ys = np.arange(y_lo, y_hi, lin_step_m)
    scorer = _scorer(grid, occ_thresh, sigma_m)
    best_score = 0.0
    best_pose: Pose | None = None
    rows, cols = grid.cells.shape
    n_candidates = xs.size * ys.size * int(np.ceil(2 * math.pi / ang_step_rad))
    scores = np.empty(n_candidates)
    positions = np.empty((n_candidates, 2))
    filled = 0
    for yaw in np.arange(-math.pi, math.pi, ang_step_rad):
        rotated = rotate(pts, float(yaw))
        for x in xs:
            for y in ys:
                # 로봇은 관측된 빈 바닥 위에만 설 수 있다 — 벽 안도, 미관측 셀도
                # 스캔 점수를 볼 것 없이 제외한다.
                row, col = grid.to_cell(float(x), float(y))
                score = 0.0
                if 0 <= row < rows and 0 <= col < cols and grid.cells[row, col] <= free_thresh:
                    score = scorer(rotated + np.array([x, y]))
                scores[filled] = score
                positions[filled] = (x, y)
                filled += 1
                if score > best_score:
                    best_score = score
                    best_pose = (float(x), float(y), wrap_pi(float(yaw)))
    if best_pose is None:
        return None
    strong = scores[:filled] >= best_score * 0.9
    far = np.hypot(positions[:filled, 0] - best_pose[0], positions[:filled, 1] - best_pose[1]) > 0.5
    peers = int(np.count_nonzero(strong & far))
    # 거친 격자의 최고점 주변을 세밀히 다듬는다 — 다운샘플 점수가 아닌 전체 점으로.
    refined = match(
        grid,
        points_robot,
        best_pose,
        MatchParams(
            search_lin_m=lin_step_m * 2,
            search_lin_step_m=meta.resolution,
            search_ang_rad=ang_step_rad * 2,
            search_ang_step_rad=refine_ang_step_rad,
            occ_thresh=occ_thresh,
            min_known_cells=min_known_cells,
            sigma_m=sigma_m,
        ),
    )
    # 거친 격자는 다운샘플 점으로 매겼으니 전체 점으로 다듬은 결과와 같은 자로 비교한다.
    coarse_full = scorer(rotate(points_robot, best_pose[2]) + np.array(best_pose[:2]))
    if refined.score > 0 and refined.score >= _as_count(coarse_full):
        return MatchResult(refined.pose, refined.score, peers=peers)
    return MatchResult(best_pose, _as_count(coarse_full), peers=peers)


def integrate_scan(
    grid: OccupancyGrid,
    pose: Pose,
    points_robot: np.ndarray,
    *,
    hit: float,
    miss: float,
    pad_cells: int,
) -> None:
    """정합된 자세로 스캔을 지도에 반영한다."""
    grid.integrate(
        (pose[0], pose[1]),
        to_world(points_robot, pose),
        hit=hit,
        miss=miss,
        pad_cells=pad_cells,
    )
