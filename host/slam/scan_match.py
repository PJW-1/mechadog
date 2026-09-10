"""스캔 정합 측위와 매핑 — **P2 에서 `slam_toolbox` 로 교체될 자리** (FR-6 · ADR-7 · ADR-9).

⚠️ **이 모듈은 잠정 구현이다.** [ADR-9](../../docs/DECISIONS.md) 가 ROS2 를
Phase 2 의 `slam_toolbox` 하나로 한정해 *"스캔을 넣으면 맵과 위치를 반환하는
블랙박스"* 로 쓰기로 정했고, **여기서 하는 일이 정확히 그것**이다. 그러므로
`docker/ros2/`(WBS 5.4.1)가 서면 이 파일이 빠진다.

그때 남는 것과 빠지는 것을 미리 갈라 두었다 (`host/slam/__init__.py` 표).
**교체 접점은 지도 형식 하나다** — `occupancy.OccupancyGrid.load()` 가
`slam_toolbox` 의 `*.pgm`+`*.yaml` 을 읽으므로, 구역 지정과 경로계획은
코드 변경 없이 그대로 돌아간다.

그래서 이 파일에 **성능을 더 들이지 않는다.** 루프 클로저도, 분기 한정도 넣지
않았다 — 넣으면 교체할 때 버리는 일이 늘고, 교체하지 않을 이유를 만든다.

---

**멈춰서 재고 다시 걷는다** (ADR-7). 4족 보행은 매 스텝 피치·롤이 변하고 2D
LiDAR 는 스캔면이 수평이라고 가정하므로, 걸으면서 모은 스캔은 정합이 성립하지
않는다. 그래서 이동 → 정지 → `settle_delay_ms` 대기 → 복수 스캔 → 병합 → 정합의
순서를 지킨다. 대기 시간은 `config.localization.settle_delay_ms` 에서 온다.

정합은 **상관 기반 완전 탐색**이다. ICP 를 쓰지 않는 이유는 초기 추정이 나쁠 때
지역해로 빠지는데, 4족 보행의 오도메트리가 (`gait_calibration` 이 비어 있어서)
사실상 없기 때문이다. 탐색 범위는 한 사이클의 이동량(`move_increment_mm`)에서
나오므로 격자 탐색이 감당할 크기다.

**IMU yaw 는 변화량으로만 쓴다 — 절대값으로 쓰면 안 된다.**

⚠️ 이것을 틀리면 조용히 망가진다. 처음에는 `imu.yaw` 를 각도 탐색의 **중심**으로
썼는데, 그러면 **지도 좌표계의 방위가 IMU 값에서 탐색 범위(±8°) 이상 벗어날 수
없다.** 그런데 지도 좌표계는 매핑을 시작한 자리가 정하고 IMU 의 0 은 부팅한
자리가 정하므로, **둘 사이에는 임의의 옵셋이 있다.** 로봇이 지도 기준 90° 를
보고 있는데 IMU 가 0° 를 보고하면 정합은 90° 를 절대 찾지 못하고, 엉뚱하게
회전된 자세를 최선으로 고른다.

실제로 그렇게 만들었더니 **지도에 없던 장애물이 30건 오탐되었다** — 자세가
회전되어 있으니 광선이 엉뚱한 방향의 지도 벽과 비교되고, 실측 거리가 예상보다
가까워 보였다. 자세 오차가 장애물 오탐으로 나타나므로 원인을 찾기 어렵다.

그래서 받는 것은 **직전 스캔 이후의 yaw 변화량**(`yaw_delta`)이고, 기준은 언제나
지도 좌표계의 이전 방위다. 표류도 이렇게 하면 한 주기분만 들어온다 — 절대값을
쓰면 표류가 누적되어 방이 부채꼴로 휜다.

**이 모듈은 소켓도 시각도 만지지 않는다** (ENGINEERING_GUIDE 2.1).
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


@dataclass(frozen=True, slots=True)
class MatchResult:
    """정합 결과. **점수를 함께 돌려주는 것이 요점이다.**

    점수 0 은 *"아는 벽 위에 한 점도 얹히지 않았다"* 이고 그것이 곧 측위 상실
    (FR-6.6 · `Event.POSE_STALE` → `LOST`)이다. 자세만 돌려주면 호출자가 실패를
    구분할 수 없어 **틀린 위치를 믿고 순찰한다.**
    """

    pose: Pose
    score: int
    #: 정합을 시도조차 못한 경우 (지도가 아직 비었다 · 점이 없다).
    skipped: bool = False


def preprocess(points: tuple[tuple[float, float], ...], min_m: float, max_m: float) -> np.ndarray:
    """유효 거리만 남기고 로봇 기준 XY 로 바꾼다 (m).

    ⚠️ **유효 범위 밖을 0 으로 바꾸지 않고 버린다.** 0 으로 남기면 로봇 발밑에
    벽이 있다고 지도에 찍힌다 — 합치기 전 코드에서 `filter_scan` 이 이미 옳게
    버리고 있었고, 그 판단을 그대로 가져왔다.
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

    base_yaw = wrap_pi(center[2] + yaw_delta)
    best_score = -1
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
                score = grid.score(rotated + np.array([x, y]), params.occ_thresh)
                if score > best_score:
                    best_score = score
                    best_pose = (x, y, wrap_pi(yaw))
    return MatchResult(best_pose, max(best_score, 0))


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
