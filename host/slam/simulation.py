"""가상 LiDAR 공간과 보행 모델 (WBS 6.1 · 실기 연결 전 검증).

**물리 시뮬레이터가 아니다.** `tools/mock_mechdog.py` 가 *프로토콜 참여자로서의
로봇*만 흉내내는 것과 같은 선을 여기서도 긋는다 — 이 모듈이 흉내내는 것은
*측정 대상으로서의 공간*뿐이다. 서보도 접지력도 계산하지 않는다.

⚠️ **여기 있는 보행 모델로 실기 성능을 말하지 않는다.** `forward_mm_per_sec` ·
`turn_deg_per_sec` 는 `config.yaml` 의 `lidar.sim:` 절에 있는 **명목값**이며,
실측은 개체 프로파일의 `gait_calibration` 소관이고 아직 비어 있다
(`config/devices/mechdog-01.yaml`). 시연할 바닥에서 재야 하는 값이라
카펫과 장판에서 달라진다.

호 조향 모델도 근사다 — 요 변화를 `step x angle` 에 비례한다고 두었다. 실기의
호 반경은 보행 시퀀스가 정하므로 이 비례는 **부호와 크기 순서만** 맞다.
알고리즘이 방위 오차를 줄이는 방향으로 도는지 확인하기에는 충분하고,
경로 추종의 정밀도를 논하기에는 부족하다.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from host.common.units import wrap_pi

Segment = tuple[float, float, float, float]
Pose = tuple[float, float, float]

#: 가상 공간 — 6m x 5m 방 하나 + 내부 장애물. 시연 규모에 맞춘 크기다
#: (`zones.ids` 가 3개인 것과 같은 근거: 방 하나에서 하는 온라인 시연).
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
    """가상 스캔을 **전선 형식으로** 만든다 — `[angle_deg, dist_mm]`.

    내부 단위(rad·m)로 돌려주지 않는 이유가 요점이다. 목업은 중계 노드의
    자리에 서므로 실제 노드가 보내는 것과 같은 형식을 내야 한다. 내부 단위로
    바로 넘기면 **`lidar_link` 의 검증과 단위 변환을 건너뛴 채** 알고리즘만
    시험하게 되고, 실기에서 처음 그 경로를 지나게 된다.
    """
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

    ⚠️ **`step` 은 한 걸음의 보폭이고 이동 거리가 아니다** (`actions.py` 머리말).
    그래서 속도는 보폭 비율 x 명목 속도로 근사한다 — 보폭을 절반으로 줄이면
    절반 속도로 걷는다고 본다.
    """
    x, y, yaw = pose
    if step_mm == 0.0:
        return pose
    # 보폭 100mm 를 명목 속도의 기준으로 둔다 (규약 상한 100mm 의 절반이 아니라
    # 상한 자체다 — 최대 보폭이 최대 속도라고 보는 것이 자연스럽다).
    speed_m_s = params.forward_mm_per_sec / 1000.0 * (step_mm / 100.0)
    # 요 변화는 `step x angle` 부호를 따른다 — 후진에서 조향 부호가 뒤집히는
    # 것을 이 곱이 재현한다 (`patrol.steering_for` 주석). 30deg 는 규약의
    # 조향 상한이므로 여기서 비율의 기준이 된다.
    yaw_rate = (
        math.radians(params.turn_deg_per_sec) * (angle_deg / 30.0) * (1.0 if step_mm > 0 else -1.0)
    )
    new_yaw = wrap_pi(yaw + yaw_rate * dt_s)
    travel = speed_m_s * dt_s
    return x + travel * math.cos(new_yaw), y + travel * math.sin(new_yaw), new_yaw


def waypoint_walk(
    pose: Pose, target: tuple[float, float], step_m: float, max_yaw_step: float
) -> Pose:
    """매핑용 — 사용자가 로봇을 끌고 다니는 것을 대신한다.

    **제자리에서 방향을 맞춘 뒤 직진한다.** 매핑은 사람이 로봇을 들거나 끌어
    옮기는 작업이므로(ADR-7 · 시스템 문서 5절) 호 조향 제약이 걸리지 않는다.
    순찰(`apply_move`)과 다른 모델인 것은 그래서 의도된 것이다.
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


def sim_params_from_config(config: dict, range_max_m: float) -> SimParams:
    sim = config["lidar"]["sim"]
    return SimParams(
        forward_mm_per_sec=float(sim["forward_mm_per_sec"]),
        turn_deg_per_sec=float(sim["turn_deg_per_sec"]),
        noise_mm=float(sim["scan_noise_mm"]),
        dropout_rate=float(sim["dropout_rate"]),
        beams=int(sim["beams"]),
        range_max_m=range_max_m,
    )
