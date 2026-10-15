"""가상 LiDAR 공간과 보행 모델 (WBS 6.1 · 실기 연결 전 검증).

**물리 시뮬레이터가 아니다.** `tools/mock/mock_mechdog.py` 가 *프로토콜 참여자로서의
로봇*만 흉내내는 것과 같은 선을 여기서도 긋는다 — 이 모듈이 흉내내는 것은
*측정 대상으로서의 공간*뿐이다. 서보도 접지력도 계산하지 않는다.

보행 모델은 `lidar.sim:` 절의 명목값이라 실기 성능을 말하지 않는다(실측은 개체의
`gait_calibration` 소관). 요 변화는 `angle` 단독에 비례한다 — `angle` 은 각속도 명령이라
후진에서도 양수가 반시계다 (PROTOCOL 부호 규약). 실기의 방향별 비대칭은 넣지 않는다.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from host.common.units import wrap_pi

Segment = tuple[float, float, float, float]
Pose = tuple[float, float, float]

#: 가상 공간 — 6m x 5m 방 하나 + 내부 장애물 (시연 규모).
DEFAULT_ROOM: tuple[Segment, ...] = (
    (0.0, 0.0, 6.0, 0.0),
    (6.0, 0.0, 6.0, 5.0),
    (6.0, 5.0, 0.0, 5.0),
    (0.0, 5.0, 0.0, 0.0),
    (2.5, 1.5, 3.5, 1.5),
    (3.5, 1.5, 3.5, 2.5),
    (3.5, 2.5, 2.5, 2.5),
    (2.5, 2.5, 2.5, 1.5),
)

#: 지도 생성 **이후에** 나타나는 장애물. 재계획을 시험하는 데 쓴다.
LATE_OBSTACLE: tuple[Segment, ...] = ((2.5, 0.6, 2.5, 1.8),)


@dataclass(frozen=True, slots=True)
class SimParams:
    forward_mm_per_sec: float
    turn_deg_per_sec: float
    noise_mm: float
    dropout_rate: float
    beams: int
    range_max_m: float
    #: `step=0` 제자리 회전의 각속도(`angle=±30` 기준, 도/s). 0 이면 제자리에서 돌지 않는다
    #: (2026-10-01 전의 모형). 설정은 2026-09-22 실측 평균을 쓴다 (ADR-11).
    spin_deg_per_sec: float = 0.0


def ray_hit(
    origin: tuple[float, float], direction: tuple[float, float], segment: Segment
) -> float | None:
    """광선과 선분의 교점까지 거리. 없으면 `None`."""
    ox, oy = origin
    dx, dy = direction
    x1, y1, x2, y2 = segment
    ex, ey = x2 - x1, y2 - y1
    denominator = ex * dy - ey * dx
    if abs(denominator) < 1e-9:
        return None
    ax, ay = x1 - ox, y1 - oy
    t = (ex * ay - ey * ax) / denominator
    u = (dx * ay - dy * ax) / denominator
    if t >= 0 and 0.0 <= u <= 1.0:
        return t
    return None


def scan_world(
    pose: Pose,
    segments: Sequence[Segment],
    params: SimParams,
    rng: random.Random,
) -> list[list[float]]:
    """가상 스캔을 전선 형식(`[angle_deg, dist_mm]`)으로 만든다 — `lidar_link` 의 검증과 단위
    변환을 실기와 같이 거치게."""
    x, y, yaw = pose
    points: list[list[float]] = []
    for index in range(params.beams):
        angle = index / params.beams * 2 * math.pi
        direction = (math.cos(angle + yaw), math.sin(angle + yaw))
        nearest = params.range_max_m
        for segment in segments:
            distance = ray_hit((x, y), direction, segment)
            if distance is not None and distance < nearest:
                nearest = distance
        if nearest >= params.range_max_m or rng.random() < params.dropout_rate:
            continue
        noisy_mm = nearest * 1000.0 + rng.gauss(0.0, params.noise_mm)
        if noisy_mm <= 0:
            continue
        points.append([round(math.degrees(angle), 2), int(round(noisy_mm))])
    return points


def apply_move(
    pose: Pose, step_mm: float, angle_deg: float, dt_s: float, params: SimParams
) -> Pose:
    """호 조향 `MOVE` 를 자세에 반영한다.

    속도는 보폭 비율 × 명목 속도로 근사한다.
    """
    x, y, yaw = pose
    if step_mm == 0.0:
        # 제자리 회전 — 실측은 `angle=±30` 한 점뿐이라 각도에 비례한다고 둔다(가정).
        if angle_deg == 0.0 or params.spin_deg_per_sec <= 0.0:
            return pose
        rate = math.radians(params.spin_deg_per_sec) * (angle_deg / 30.0)
        return x, y, wrap_pi(yaw + rate * dt_s)
    # 규약 상한 100mm 를 명목 속도의 기준 보폭으로 둔다.
    speed_m_s = params.forward_mm_per_sec / 1000.0 * (step_mm / 100.0)
    # 요 변화는 `angle` 단독으로 정해진다(`step` 부호를 곱하지 않는다). 30deg 는 조향 상한이다.
    yaw_rate = math.radians(params.turn_deg_per_sec) * (angle_deg / 30.0)
    new_yaw = wrap_pi(yaw + yaw_rate * dt_s)
    travel = speed_m_s * dt_s
    return x + travel * math.cos(new_yaw), y + travel * math.sin(new_yaw), new_yaw


def waypoint_walk(
    pose: Pose, target: tuple[float, float], step_m: float, max_yaw_step: float
) -> Pose:
    """매핑용 — 사용자가 로봇을 끌고 다니는 것을 대신한다.

    제자리에서 방향을 맞춘 뒤 직진한다 — 매핑은 사람이 로봇을 옮기므로 호 조향 제약이 없다 (ADR-7).
    """
    x, y, yaw = pose
    tx, ty = target
    dx, dy = tx - x, ty - y
    distance = math.hypot(dx, dy)
    if distance < step_m:
        return tx, ty, yaw
    target_yaw = math.atan2(dy, dx)
    delta = wrap_pi(target_yaw - yaw)
    if abs(delta) > max_yaw_step:
        return x, y, wrap_pi(yaw + max_yaw_step * (1.0 if delta > 0 else -1.0))
    return (
        x + step_m * math.cos(target_yaw),
        y + step_m * math.sin(target_yaw),
        target_yaw,
    )


def sim_params_from_config(config: dict[str, Any], range_max_m: float) -> SimParams:
    sim = config["lidar"]["sim"]
    return SimParams(
        forward_mm_per_sec=float(sim["forward_mm_per_sec"]),
        turn_deg_per_sec=float(sim["turn_deg_per_sec"]),
        noise_mm=float(sim["scan_noise_mm"]),
        dropout_rate=float(sim["dropout_rate"]),
        beams=int(sim["beams"]),
        range_max_m=range_max_m,
        spin_deg_per_sec=float(sim["spin_deg_per_sec"]),
    )
