"""구역 순찰 제어 — 계획을 규약 의도로 바꾼다 (FR-7 · Phase 2).

이 파일은 전문을 만들지 않는다 — 전문은 전부 `Commander` 가 만들고 여기서는 의도만
세운다 (ENGINEERING_GUIDE 2.1 · PROTOCOL.md 6절).

- 방향 전환은 호(arc) 조향 `MOVE{step, angle}` 이고, 크게 틀어졌을 때만 **제자리 회전**
  `MOVE{0, angle}` 으로 측위 방위를 보며 맞춘다 (ADR-11 개정 2026-10-01).
- 초음파 근거리 정지는 로봇의 `flags.obstacle` 을 따라가고 판정하지 않는다 (아키텍처 1.2 · ADR-22).
- 위험 시 `ESTOP`(래치)을 보내고, 해제는 사람 확인 뒤 텔레메트리 `safety_latched=false` 로 확인한다 (ADR-21).
- 내부 단계는 FSM 13상태로 사상해 `STATE` 로 내려보낸다 (`FSM_STATE_FOR`).
- 측위 실패는 `ESTOP` 이 아니라 `LOST` 정지다 — 다음 스캔에서 재측위될 수 있다 (FR-6.6).
"""

from __future__ import annotations

import json
import math
import random
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import numpy as np

from host.behavior.commander import Commander
from host.behavior.planner import (
    Plan,
    PlanParams,
    body_collision_mask,
    detect_new_obstacle,
    escape_start,
    inflate,
    mark_obstacle,
    min_forward_distance,
    plan_to,
    segment_clear,
)
from host.behavior.zone_map import ZoneMap
from host.behavior.zones import ZoneStore, select_next
from host.common.config import ConfigError
from host.common.lidar_link import Scan
from host.common.logging_setup import EdgeTrigger, event_logger
from host.common.protocol import FSM_STATES, clamp
from host.common.units import deg_to_rad, rad_to_deg, wrap_pi
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import (
    MatchParams,
    MatchResult,
    Pose,
    global_match,
    integrate_scan,
    match,
    preprocess,
)
from host.slam.settings import (
    match_params_from_config,
    plan_params_from_config,
    range_from_config,
)

LOG = event_logger("mechadog.behavior.patrol")


class Phase(StrEnum):
    """순찰 내부 단계. 규약의 상태가 아니며 `FSM_STATE_FOR` 로 사상해 내려보낸다."""

    IDLE = "IDLE"  # 기동 후 순찰 시작 전
    PLANNING = "PLANNING"  # 다음 구역 선정 · 경로 생성
    MOVING = "MOVING"  # 웨이포인트 추종
    INSPECT = "INSPECT"  # 구역 도착 — 카메라 훅
    LOST = "LOST"  # 측위 실패
    HALTED = "HALTED"  # 안전 래치 (사람이 풀어야 한다)


#: 내부 단계 → FSM 상태 13종 (PROTOCOL.md 2절 `STATE`). 13종 밖의 이름은 로봇이 폐기한다.
#: `AVOID` 는 내려보내지 않는다 — 반향되면 해제를 알 수 없다 (ADR-22).
FSM_STATE_FOR: Mapping[Phase, str] = {
    Phase.IDLE: "IDLE",
    Phase.PLANNING: "PATROL",
    Phase.MOVING: "PATROL",
    Phase.INSPECT: "ZONE_INSPECT",
    Phase.LOST: "LOST",
    Phase.HALTED: "FAILSAFE",
}

# 사상표가 규약과 어긋나면 import 시점에 기동을 막는다.
assert set(FSM_STATE_FOR.values()) <= FSM_STATES, "FSM 13종에 없는 상태를 사상하고 있다"


@dataclass(frozen=True, slots=True)
class DriveParams:
    """보행·안전 파라미터 — `config.yaml` 에서 온다 (NFR-3①). 규약 범위는 인코더가 자른다."""

    step_mm: float
    turn_deg: float
    reverse_mm: float
    heading_tolerance_rad: float
    #: 호 조향의 조향·보폭 비례 기준 (`scale = 오차 / 이 값`).
    reverse_threshold_rad: float
    #: 이 이상 틀어져 있으면 걷지 않고 **제자리에서 돈다** (ADR-11 개정). `heading_tolerance` 안에
    #: 들어올 때까지 계속 돈다 — 중간에 호로 바꾸면 다시 큰 호를 그린다.
    spin_threshold_rad: float
    #: 제자리 회전 지시의 `MOVE angle` (deg). 2026-09-22 실측은 ±30 에서 7.37 도/s.
    spin_turn_deg: float
    arrival_radius_m: float
    waypoint_radius_m: float
    #: 호스트측 LiDAR 위험 거리. 온보드 초음파(`obstacle_stop_cm`)와 별개다.
    lidar_estop_m: float
    #: 텔레메트리가 이만큼 조용하면 링크 두절로 본다 (`safety.link_loss_failsafe_ms`).
    link_loss_ms: int
    #: 로봇이 보고하는 마지막 수락 명령의 나이가 이것을 넘으면 폐기 중이다.
    cmd_timeout_ms: int
    #: 측위 자세가 이만큼 갱신되지 않으면 `LOST` (`localization.pose_timeout_ms`).
    pose_timeout_ms: int
    #: 정지 후 기대한 스캔의 수신 한도. 이동 중 스캔 공백에는 적용하지 않는다.
    scan_stall_timeout_ms: int
    #: 실제 STOP 송신 후 이 시간이 지난 스캔으로만 재출발한다.
    settle_delay_ms: int


#: 최대 조향에서 보폭을 이만큼 줄인다 (0.5 = 절반). 호 반경은 대략 `step / angle` 이고
#: 조향은 규약 상한(±30deg)에 걸리므로, 더 급히 도는 손잡이는 보폭뿐이다. 호 반경
#: 실측 전이라 설정으로 빼지 않았다.
TURN_STEP_REDUCTION: float = 0.5

#: 정합 점수가 전체 점수의 이 비율 이상일 때만 그 바퀴를 지도에 적분한다. 낮은 점수의
#: 자세로 적분하면 틀린 위치에 벽을 찍어 지도가 스스로 오염된다 — 아래에서는
#: «이 위치가 맞다» 는 정합만 공간 사실로 승격한다.
MAP_WRITE_MIN_SCORE_FRAC: float = 0.6

#: 지도가 자랐을 때 팽창 마스크를 다시 만드는 간격(적분한 바퀴 수). A* 는
#: `_blocked` 를 보고 푸는데, 적분으로 새로 «확실히 빈» 셀이 생겨도 팽창이
#: 옛 지도 그대로면 로봇이 선 자리가 계속 막혀 보인다 (2026-10-02 실기:
#: 측위는 잠겼는데 시작 셀이 미관측이라 `no_reachable_zone`).
MAP_INFLATE_EVERY: int = 4


@dataclass(frozen=True, slots=True)
class Steering:
    """한 틱의 보행 의도. mm · deg (전선 단위)."""

    step_mm: float
    angle_deg: float


def steering_for(
    heading_error_rad: float, params: DriveParams, *, spinning: bool = False
) -> Steering:
    """방위 오차를 보행 의도로 바꾼다. 세 구간으로 나뉜다.

    | 오차 | 보행 | 근거 |
    | :--- | :--- | :--- |
    | 허용 오차 이내 | 직진 | 조향을 넣으면 목표를 지나쳐 진동한다 |
    | 그 밖 ~ 회전 임계 | 전진 + 비례 조향(호) | 걸으면서 방위를 줄인다 |
    | 회전 임계 초과 | **제자리 회전** `step=0` | 큰 호는 경로를 벗어나 가구 모서리로 밀려간다 |

    `spinning` 이면(직전 틱이 제자리 회전) 허용 오차 안에 들 때까지 계속 돈다 — 임계 바로
    아래에서 호로 바꾸면 그 호가 다시 경로를 벗어난다.

    ⚠️ **제자리 회전은 폐루프로만 쓴다** (ADR-11 개정 2026-10-01). 각속도 산포가 82% 라
    «몇 초 돌면 몇 도» 로 쓰면 틀리지만, 매 틱 측위 방위로 오차를 다시 재므로 산포가 결과를
    바꾸지 않는다 — ADR-40 조준과 같은 근거다. 예전의 «후진 호» 구간은 회전 임계가 후진
    임계보다 작아 닿지 않으므로 지웠다(2026-10-01 집 지도 시뮬: 후진·전진 호로 180° 를 도는
    동안 20~60초 맴돌며 가구 10cm 안까지 가서 E-STOP).

    조향 부호는 오차 부호를 따른다 — `angle` 은 각속도 명령이라 양수가 반시계다
    (`docs/PROTOCOL.md` 부호 규약). 호 구간은 크게 틀어질수록 보폭을 줄여
    (`TURN_STEP_REDUCTION`) 호 반경을 줄인다.
    """
    error = wrap_pi(heading_error_rad)
    if abs(error) <= params.heading_tolerance_rad:
        return Steering(params.step_mm, 0.0)

    direction = 1.0 if error > 0 else -1.0
    if spinning or abs(error) > params.spin_threshold_rad:
        return Steering(0.0, direction * params.spin_turn_deg)
    # 오차에 비례해 조향을 키우고 같은 비율로 보폭을 줄인다.
    scale = min(1.0, abs(error) / params.reverse_threshold_rad)
    return Steering(
        params.step_mm * (1.0 - TURN_STEP_REDUCTION * scale),
        direction * params.turn_deg * scale,
    )


@dataclass
class SafetyView:
    """로봇이 보고한 안전 관측. 호스트는 전압·기울기를 판정하지 않는다 (아키텍처 1.2)."""

    latched: bool | None = None
    onboard_state: str = ""
    obstacle: bool | None = None
    dist_cm: float | None = None
    yaw_deg: float | None = None
    last_cmd_age_ms: int | None = None
    last_seen_ms: int | None = None

    @property
    def yaw_rad(self) -> float | None:
        return None if self.yaw_deg is None else deg_to_rad(self.yaw_deg)

    @property
    def obstacle_active(self) -> bool:
        """근거리 반사 정지가 걸려 있는가.

        `obstacle` 이 없는 구형 펌웨어에서는 `state == AVOID` 로 폴백한다 — 이 컨트롤러는
        `AVOID` 를 내려보내지 않으므로 반향이 섞이지 않는다 (ADR-22).
        """
        if self.obstacle is not None:
            return self.obstacle
        return self.onboard_state == "AVOID"


@dataclass
class PatrolStats:
    cycles: int = 0
    zones_visited: int = 0
    replans: int = 0
    estops: int = 0
    scans: int = 0
    lost: int = 0


@dataclass
class PatrolController:
    """계획 → 의도. 소켓 없이 동작하며 워커 경과 시계는 주입할 수 있다.

    운용 루프(`tools/ops/patrol_run.py`)가 한 스레드에서 ① 지도 자세·스캔·텔레메트리를
    넣고 ② `step(now_ms)`가 돌려준 즉시 전문(`ESTOP`)을 바로 보내고
    ③ `commander.tick(now_ms)`의 주기 전문을 10Hz로 보낸다.
    """

    commander: Commander
    grid: OccupancyGrid
    zones: ZoneStore
    drive: DriveParams
    plan_params: PlanParams
    match_params: MatchParams
    #: `(min_m, max_m)` LiDAR 유효 거리.
    range_m: tuple[float, float]
    new_obstacle_margin_m: float
    new_obstacle_confirmations: int
    obstacle_mark_radius_m: float
    #: 이 거리 안의 빔만 신규 장애물 후보로 본다 — 측위 오차는 거리에 비례해 커진다.
    new_obstacle_check_radius_m: float
    forward_fan_rad: float
    #: 걸으면서 자라는 지도 — 적분 가중치는 `lidar.hit_logodds / miss_logodds` 다.
    #: ⚠️ 라이브 기록은 `live_map_write` 가 참일 때만이다. 틀린 자세로 쓰면 지도가 그 자리를
    #: «맞는 것처럼» 굳혀 정합기가 스스로를 확신한다(실측 재현: 0.66→0.95). 지금 단계는
    #: 세션을 기록만 하고 `tools/lidar/localization_bench.py` 로 검토한 뒤 반영한다.
    live_map_write: bool = False
    map_hit_logodds: float = 0.0
    map_miss_logodds: float = 0.0
    map_pad_cells: int = 0
    #: 첫 정합 전용 넓은 탐색 창 — 한 번도 측위된 적이 없을 때만 쓴다. 배치 오차가
    #: `match_params` 의 좁은 창(한 사이클 이동량)을 넘는 자리에 놓여도 잡히게 한다.
    #: `reloc_interval_ms` 가 켜져 있으면 대신 지도 전역 정합(`global_match`)이 쓰인다.
    first_match_params: MatchParams | None = None
    #: 정합 채택 최소 적중 비율 — 스캔 끝점의 이 비율 미만만 벽에 오면 그 자세는 틀린
    #: 잠금으로 버린다. 0 이면 «점수 0 만 거절»하는 옛 동작이다.
    min_match_frac: float = 0.0
    #: 측위가 낡았을 때(한 번도 못 잡았거나 추적이 끊겼을 때) 지도 전체를 거칠게
    #: 훑어 다시 잠그는 재측위 주기 — 0 이면 끈다.
    reloc_interval_ms: int = 0
    global_match_lin_step_m: float = 0.10
    global_match_ang_step_rad: float = math.radians(15.0)
    #: Opt-in until offline acceptance/false-fix validation has passed.
    global_full_scan_ambiguity: bool = False
    #: 전역 재측위 채택 한도 — 최고점 90% 이상인 후보가 이 수를 넘으면 스캔이 지도를
    #: 구분하지 못하는 것(대칭·벽 포켓)이라 어떤 자세도 근거 없이 고르는 셈이라 거절한다.
    reloc_max_peers: int = 60
    #: 정지 중 주기적 전역 감사 — 국소 창이 틀린 잠금을 유지하는지 확인한다.
    #: 0 이면 끈다. 걷는 중에는 돌리지 않는다 (수 초짜리 탐색이 루프를 막는다).
    verify_interval_ms: int = 0
    #: 전역 탐색 결과를 **채택하기 전에 같은 답이 연속으로 나와야 하는 횟수.** 지도가 덜
    #: 채워진 자리에서는 바퀴마다 전역 최적이 다른 벽에 얹혀 자리가 널뛴다(실측: 제자리
    #: 로봇이 16초에 세 자리) — 한 번의 최고점은 증거가 아니고 반복돼야 증거다.
    reloc_votes: int = 3
    #: 투표에서 «같은 답» 으로 볼 방위 차 — 위치만 보면 58°·36° 처럼 방향이 다른 답이
    #: 같은 표로 묶였다(2026-10-03 s0_live_4).
    reloc_vote_yaw_rad: float = math.radians(10.0)
    #: 로봇이 표 사이에 움직이지 않았으면(같은 장면) 이 수 이하의 경쟁 후보일 때만 표로
    #: 센다 — 서 있는 로봇의 연속 스캔은 독립 증거가 아니라 모호한 답을 세 번 세게 된다.
    reloc_stationary_max_peers: int = 10
    #: IMU 가 신선하면 국소 정합의 방위 탐색을 이 창으로 좁힌다 (IMU 예측 ± 창).
    #: None 이면 `match_params` 의 창 그대로. 우도장 지형은 방위 능선이 생겨(30~72° 가
    #: 0.05 안) 정합만으로는 방위가 흘렀다 — 정지 중 23°, 걸은 세션 IMU 대비 132°.
    imu_match_params: MatchParams | None = None
    #: 텔레메트리 IMU 가 이보다 오래되면 방위 사전으로 쓰지 않는다.
    imu_fresh_ms: int = 300
    #: 측위가 이만큼 끊기면 «검증됨»·사람 시드의 신뢰를 버린다 — 그 사이 로봇이 들려
    #: 옮겨졌을 수 있다. 0 이면 끈다 (Codex 검토 P1: 검증 상태에 유효기간이 없었다).
    trust_expiry_ms: int = 5000
    #: 전역 탐색 결과가 이보다 늦게 도착하면 버린다 — 그 사이 손으로 옮겨졌거나 돌았을 수
    #: 있는데 MOVE 수만으로는 모른다.
    global_result_max_age_ms: int = 3000
    #: 워커는 실시간으로 실행된다. 가속된 시뮬레이션의 루프 시계와 섞지 않는다.
    wall_clock_ms: Callable[[], int] = field(default=lambda: time.monotonic_ns() // 1_000_000)
    #: 걸으며 자란 지도를 돌려 쓸 폴더 — None 이면 세션 끝에 자란 내용을 버린다.
    maps_dir: Path | None = None
    #: 측위 전용 지도 (`slam_map_loc.npy`) — 세션 SLAM 병합으로 가구 다리까지 담은
    #: 지도. 정합(match·global_match)은 이것을 쓰고, 경로 계획·팽창·동적 장애물은
    #: `grid`(항법용)를 쓴다. 병합 셀은 정렬 오차로 실제 통로에 몇 cm 튀어 있을 수
    #: 있어 (실측: 궤적과 1~4cm 겹침) 항법 지도에 섞으면 복도가 봉쇄된다.
    #: 같은 좌표계(같은 origin·해상도)라 자세는 그대로 통한다.
    loc_grid: OccupancyGrid | None = None
    #: 구역 영역 지도 — 있으면 `current_zone` 이 «지금 어느 방인가» 를 답한다.
    zone_map: ZoneMap | None = None
    #: 사람이 `--pose-seed` 로 시작 자세를 줬는가. 전역 재측위가 모호할 때(책상 밑 등)
    #: 시드 주변 넓은 창으로 **추적은 시작하되** 전역 확인 전에는 지도에 쓰지 않는다.
    pose_seeded: bool = False
    #: 구역이 동적 장애물로 막혔을 때 표시를 버리고 다시 확인할 최대 횟수
    #: (`config.fsm.avoid_attempts` 와 같은 값).
    max_reverify_attempts: int = 3
    random_after_first_cycle: bool = True
    rng: random.Random | None = None

    # ── 상태 ──────────────────────────────────────────────────
    pose: Pose = (0.0, 0.0, 0.0)
    phase: Phase = Phase.IDLE
    cycle: int = 0
    visited: frozenset[str] = frozenset()
    plan: Plan = field(default_factory=lambda: Plan(None))
    waypoint_index: int = 0
    safety: SafetyView = field(default_factory=SafetyView)
    stats: PatrolStats = field(default_factory=PatrolStats)

    _blocked: np.ndarray | None = None
    _body_blocked: np.ndarray | None = None
    _mask_frame: tuple[float, float, float] | None = None
    _dynamic: np.ndarray | None = None
    _pending_hit: tuple[float, float] | None = None
    _pending_count: int = 0
    _last_pose_ms: int | None = None
    _last_scan_ms: int | None = None
    _stopped_since_ms: int | None = None
    _last_sent_moving: bool = False
    _replan_stop_required: bool = False
    #: 직전 스캔 때의 IMU yaw (rad). 변화량을 내려면 이전 값이 있어야 한다.
    _last_imu_yaw: float | None = None
    #: 마지막 정합 자세와 그때의 IMU yaw 의 차이 — 스캔 사이에 IMU 로 방위를 전파할 때
    #: 지도 좌표계로 되돌리는 옵셋이다. 정합될 때마다 다시 맞춘다.
    _imu_offset: float | None = None
    #: 지도에 적분한 뒤 팽창을 아직 안 다시 한 바퀴 수.
    _updates_since_inflate: int = 0
    _reverify_attempts: dict[str, int] = field(default_factory=dict)
    _reset_requested: bool = False
    #: 다음 지도 전역 재측위를 허용하는 시각 — 비싼 탐색이라 주기를 제한한다.
    _reloc_next_ms: int = 0
    #: 다음 정지 중 전역 감사 시각.
    _verify_next_ms: int = 0
    #: 마지막 정합 시도의 적중 비율 — 진단·불신 판정에 쓴다.
    _match_frac: float = 0.0
    #: 현재 자세가 지도 전역 탐색으로 확인됐는가 — 참일 때만 스캔을 지도에 적분한다.
    #: 국소 추적만으로는 틀린 자리를 자신 있게 따라갈 수 있어, 그 동안의 적분은 지도를
    #: 틀린 모양으로 굳힌다. 전역 확인 전에는 지도를 읽기만 한다.
    _pose_verified: bool = False
    #: 마지막 전역 확인 시점의 지도 — 그 뒤 적분이 틀린 것으로 드러나면 여기로 되돌린다.
    _verified_snapshot: tuple[np.ndarray, Any] | None = None
    #: 마지막으로 알린 구역 — 바뀔 때만 `zone_entered` 를 남긴다.
    _last_zone: str | None = None
    #: 연속된 전역 탐색 결과 — 서로 0.3m 안에서 `reloc_votes` 번 모이면 채택한다.
    _global_votes: list[Pose] = field(default_factory=list)
    # ── 전역 탐색 워커 ─────────────────────────────────────────
    # `global_match` 는 지도 전체를 훑어 실기 PC 에서도 **1~2초**가 걸린다.
    # `observe_scan` 이 명령 루프 스레드에서 도는데 여기서 동기로 기다리면 명령
    # 간격이 온보드 워치독(600ms)을 넘어 `ONBOARD_FAILSAFE` 를 낸다(2026-10-04
    # 실측: 틱 1.9s → 명령 거부 → 래치). 그래서 전역 탐색만 데몬 스레드로 보내고
    # 결과는 루프 스레드가 `_poll_global` 로 가져와 적용한다 — **상태 변경은
    # 루프 스레드에서만** 일어나는 규칙을 지키면서 명령은 계속 나간다.
    #: 제출됐지만 아직 루프가 소비하지 않은 전역 탐색이 있는가 (루프 스레드만 만진다).
    _global_inflight: bool = False
    #: 요청 사서함 — 종류, 점, 스캔, 요청 자세, 독립된 읽기 전용 지도 사본.
    _global_req: tuple[str, np.ndarray, Scan, Pose, OccupancyGrid] | None = None
    #: 워커가 둔 결과 — (종류, MatchResult|None, 요청 점, 요청 스캔, 요청 때 자세).
    _global_result: tuple[str, MatchResult | None, np.ndarray, Scan, Pose] | None = None
    #: 요청 때의 (신선한 IMU yaw | None, 이동 명령 수, 측위 세대, monotonic ms).
    _global_req_context: tuple[float | None, int, int, int] | None = None
    #: 측위 세대 — 신뢰 만료 때 올린다. 이전 세대에 요청한 전역 결과는 버린다 (Codex 검토 2 P1).
    _loc_epoch: int = 0
    #: 지금 처리 중인 스캔의 Host 시각 (`observe_scan` 이 찍는다).
    _scan_now_ms: int = 0
    #: 마지막 표 뒤에 로봇이 움직였는가 (MOVE 송신 또는 IMU 방위 변화) — 표의 독립성.
    _moved_since_vote: bool = True
    _imu_at_vote: float | None = None
    #: 이번 스캔의 IMU 변화량이 실제 측정인가 (신선한 텔레메트리 기준).
    _imu_delta_fresh: bool = False
    #: 지금 `pose` 가 관측된 시점의 IMU yaw — 다음 정합의 회전 예측은 «지금 IMU − 이 값».
    #: 정합이 실패해도 앵커는 그대로라 회전량을 잃지 않고, 전역 채택 때는 그 스캔 시점의
    #: IMU 로 다시 묶어 이중 반영하지 않는다 (Codex 검토 P1).
    _imu_anchor: float | None = None
    #: 실제로 나간 이동 명령(MOVE 0 이 아님)의 누적 수 — 비동기 결과가 낡았는지 본다.
    _move_seq: int = 0
    #: 신뢰를 잃었다고 기록했는가 (`trust_expiry_ms` 넘은 상실).
    _trust_expired: bool = False
    #: 지금 적용 중인 전역 결과의 요청 시점 IMU (`_poll_global` 이 채운다).
    _result_imu: float | None = None
    _global_cv: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock())
    )
    _global_thread: threading.Thread | None = None
    #: 자세가 **내장 스캔 정합**(`observe_scan`)에서 나오는가. 외부 측위(ROS2 `MAP_POSE`·
    #: 시뮬레이션)가 `observe_map_pose` 로 넣는 자세는 그쪽이 보증하므로 확인 보류를
    #: 적용하지 않는다.
    _own_localization: bool = False
    _halt_reason: str = ""
    #: 직전 추종 틱이 제자리 회전이었나 (`steering_for(spinning=)`).
    _spinning: bool = False
    _edge: EdgeTrigger = field(default_factory=EdgeTrigger)
    _obstacles: list[tuple[float, float]] = field(default_factory=list)
    #: 아직 아무도 꺼내 가지 않은 신규 장애물 확정 (`take_new_obstacles`).
    _new_obstacles: list[tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        static = inflate(self.grid, self.plan_params, obstacle_grid=self.loc_grid)
        self._blocked = static
        self._body_blocked = body_collision_mask(
            self.grid, self.plan_params, obstacle_grid=self.loc_grid
        )
        self._dynamic = np.zeros_like(static, dtype=bool)
        self._mask_frame = (
            self.grid.meta.resolution,
            self.grid.meta.origin_x,
            self.grid.meta.origin_y,
        )

    # ── 조회 ──────────────────────────────────────────────────
    @property
    def fsm_state(self) -> str:
        return FSM_STATE_FOR[self.phase]

    @property
    def blocked(self) -> np.ndarray:
        """정적 팽창 + 이번 순찰에서 발견한 동적 장애물."""
        assert self._blocked is not None and self._dynamic is not None
        return cast(np.ndarray, self._blocked | self._dynamic)

    @property
    def body_blocked(self) -> np.ndarray:
        """몸체 최소 여유 + 실시간 장애물. 동적 물체는 탈출 예외로 지우지 않는다."""
        assert self._body_blocked is not None and self._dynamic is not None
        return cast(np.ndarray, self._body_blocked | self._dynamic)

    @property
    def target(self) -> str | None:
        return self.plan.label

    @property
    def obstacles(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._obstacles)

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def pose_ms(self) -> int | None:
        """`pose` 를 마지막으로 갱신한 시각. 정합에 실패한 스캔은 바꾸지 않는다."""
        return self._last_pose_ms

    @property
    def match_frac(self) -> float:
        """마지막 정합 시도의 적중 비율 (0~1) — 진단·포즈 불신 표시에 쓴다."""
        return self._match_frac

    @property
    def pose_verified(self) -> bool:
        """현재 자세가 지도 전역 탐색으로 한 번이라도 확인됐는가."""
        return self._pose_verified

    @property
    def current_zone(self) -> str | None:
        """지금 서 있는 구역 id — 구역 영역 지도가 없거나 미도달 셀이면 `None`."""
        if self.zone_map is None:
            return None
        return self.zone_map.zone_at(self.pose[0], self.pose[1])

    @property
    def match_grid(self) -> OccupancyGrid:
        """정합(match·global_match)에 쓰는 지도 — `loc_grid` 가 있으면 그쪽, 아니면 `grid`."""
        return self.loc_grid if self.loc_grid is not None else self.grid

    def pose_stale(self, now_ms: int) -> bool:
        """측위가 신선하지 않은가 — 한 번도 못 잡았거나 `pose_timeout_ms` 를 넘겼다."""
        return self._last_pose_ms is None or (
            now_ms - self._last_pose_ms > self.drive.pose_timeout_ms
        )

    def take_new_obstacles(self) -> tuple[tuple[float, float], ...]:
        """지난 호출 뒤 확정된 신규 장애물 `(x m, y m)` 을 꺼낸다 — 한 번 꺼내면 비워진다.

        ⚠️ **런타임을 받지 않고 꺼내 가게 한다.** 컨트롤러가 기록·방송 경로를 알면
        소켓 없이 닫히는 시험(이 클래스의 계약)이 깨진다.
        """
        taken = tuple(self._new_obstacles)
        self._new_obstacles.clear()
        return taken

    def save_map(self, directory: Path | None = None) -> dict[str, Path]:
        """걸으며 자란 지도를 디스크에 쓴다 — 실측 스캔이 지도를 개선하게 하는 통로.

        **전역 확인된 자세로 적분한 세션만 쓴다** (`pose_verified`). 확인이 한 번도 없었으면
        이 세션의 적분은 전부 보류돼 있어 쓸 것도 없다. 처음 덮어쓰기 전에 원본을
        `slam_map.orig.*` 로 옆에 둔다. `maps_dir` 도 인자도 없으면 아무것도 안 한다.
        """
        target = directory or self.maps_dir
        if target is None or not self.live_map_write or not self._pose_verified:
            return {}
        target = Path(target)
        # 적분은 정합 지도(match_grid)에 쌓인다 — loc_grid 가 따로 있으면
        # 그쪽을 `slam_map_loc.*` 이름으로 저장해 항법 지도(slam_map.npy)는 건드리지 않는다.
        stem = "slam_map_loc" if self.loc_grid is not None else "slam_map"
        source_npy = target / f"{stem}.npy"
        backup_npy = target / f"{stem}.orig.npy"
        if source_npy.exists() and not backup_npy.exists():
            shutil.copy2(source_npy, backup_npy)
        return self.match_grid.save(target, stem=stem)

    # ── 입력: 텔레메트리 ──────────────────────────────────────
    def observe_telemetry(self, reading: Any, now_ms: int) -> None:
        """`Reading` 모양의 객체(필드만 본다)를 받아 안전 관측을 갱신한다."""
        self.safety = SafetyView(
            latched=getattr(reading, "safety_latched", None),
            onboard_state=getattr(reading, "state", "") or "",
            obstacle=getattr(reading, "obstacle", None),
            dist_cm=getattr(reading, "dist_cm", None),
            yaw_deg=self._yaw_of(reading),
            last_cmd_age_ms=getattr(reading, "last_cmd_age_ms", None),
            last_seen_ms=now_ms,
        )
        if self.safety.obstacle_active or self.safety.latched:
            self._note_stopped(now_ms)
        age = self.safety.last_cmd_age_ms
        if age is not None and age > self.drive.cmd_timeout_ms:
            # 보내는 명령을 로봇이 받아들이지 않고 있다 — 조용한 고장이라 경고한다.
            LOG.warning("command_not_taken", last_cmd_age_ms=age, hint="seq 세션 확인")

    @staticmethod
    def _yaw_of(reading: Any) -> float | None:
        """`reading.yaw`(deg)를 꺼낸다 — `Reading` 은 `imu.yaw` 를 평탄한 `yaw` 로 담는다."""
        value = getattr(reading, "yaw", None)
        return float(value) if isinstance(value, int | float) else None

    # ── 입력: 스캔 ────────────────────────────────────────────
    def observe_map_pose(self, pose: Pose, now_ms: int) -> None:
        """외부 측위가 낸 ``map -> base_link`` 자세를 반영한다 (WBS 5.4.4)."""
        if not all(math.isfinite(value) for value in pose):
            return
        self.pose = (float(pose[0]), float(pose[1]), wrap_pi(float(pose[2])))
        self._last_pose_ms = now_ms
        # 앵커는 **신선한** IMU 일 때만 — 끊긴 IMU 의 옛 값을 묶어 두면 재개 때 그사이 회전
        # (LiDAR 가 이미 pose 에 반영한 것)을 다시 더한다 (Codex 검토 2 P1).
        self._imu_anchor = self.safety.yaw_rad if self._imu_is_fresh(now_ms) else None
        self._trust_expired = False
        zone = self.current_zone
        if zone != self._last_zone:
            self._last_zone = zone
            LOG.info("zone_entered", zone=zone, x=round(self.pose[0], 2), y=round(self.pose[1], 2))
        imu = self.safety.yaw_rad
        if imu is not None:
            # IMU 절대 yaw 는 지도 좌표계와 옵셋이 다르다 — 옵셋을 정합 시점에 맞춰 두면
            # 다음 스캔 전까지 `imu - offset` 이 지도 방위의 연속 추정치가 된다.
            self._imu_offset = wrap_pi(imu - self.pose[2])
        # 재개는 step의 전체 관문에서 한다. 새 tf 하나가 스캔 두절이나
        # 미관측 셀 정지를 해제해서는 안 된다.

    def note_sent(self, lines: Iterable[str], sent_ms: int) -> None:
        """성공적으로 보낸 명령의 시각을 받는다. 의도를 세운 시각과 구분한다.

        송신 성공은 기기의 물리 정지를 증명하지 않는다. 기존 안정 대기와
        온보드 정지 보고를 함께 쓰며, 실제 전달 지연은 실기 검증 대상이다.
        """
        for line in lines:
            message = json.loads(line)
            if message["type"] == "MOVE" and not (message.get("step") or message.get("angle")):
                # MOVE {0,0} 은 «서 있으라» 다 — 걷는 중으로 치면 정지 감사가 막힌다 (Codex 검토 2 P2).
                self._note_stopped(sent_ms)
            elif message["type"] == "MOVE":
                self._last_sent_moving = True
                self._stopped_since_ms = None
                self._moved_since_vote = True
                self._move_seq += 1
            elif message["type"] in ("STOP", "ESTOP", "RESET_SAFE"):
                self._note_stopped(sent_ms)

    def _note_stopped(self, now_ms: int) -> None:
        self._last_sent_moving = False
        if self._stopped_since_ms is None:
            self._stopped_since_ms = now_ms

    def _note_scan(self, scan: Scan, now_ms: int) -> None:
        # 빈 전문/범위 밖 점만 있는 전문은 관측을 복구하지 않는다.
        if any(
            math.isfinite(angle)
            and math.isfinite(distance)
            and self.range_m[0] <= distance <= self.range_m[1]
            for angle, distance in scan.points
        ):
            self._last_scan_ms = now_ms

    def observe_obstacle_scan(self, scan: Scan, now_ms: int) -> None:
        """외부 측위 모드에서 스캔을 신규 장애물 확인에만 쓴다.

        오래된 자세에 빔을 투영하면 정상 벽을 새 장애물로 찍으므로 유효한 최근
        자세가 있을 때만 지도에 반영한다. 즉시 위험 판정은 ``guard_scan`` 이 별도다.
        """
        self.stats.scans += 1
        self._note_scan(scan, now_ms)
        if self.pose_stale(now_ms):
            return
        self._check_new_obstacle(scan)

    def observe_scan(self, scan: Scan, now_ms: int) -> None:
        """내장 스캔 정합(시뮬레이션용)으로 측위하고 신규 장애물을 확인한다."""
        self.stats.scans += 1
        self._note_scan(scan, now_ms)
        self._own_localization = True
        _t_all = time.perf_counter()
        points = preprocess(scan.points, *self.range_m)
        if not points.size:
            return
        self._scan_now_ms = now_ms
        self._expire_trust(now_ms)
        # 워커가 끝낸 전역 탐색 결과를 먼저 가져온다 — 비어 있으면 즉시 돌아간다.
        _t0 = time.perf_counter()
        self._poll_global(now_ms)
        _t_poll = time.perf_counter() - _t0
        # ⚠️ **전역 탐색이 도는 동안에도 국소 추적을 멈추지 않는다.** 예전엔 파이썬 루프
        # 탐색이 GIL 을 쥐어 국소 정합이 17ms→1~2.5s 로 부풀었고, 그래서 탐색 동안 국소
        # 정합을 쉬게 했다 — 그것이 정지 감사마다(15초) 약 3.3초 LOST 를 만들었다(live_4
        # 26회). 탐색을 벡터화해 같은 시간 국소 정합이 9ms 로 유지되므로 쉴 이유가 없다.
        if self.pose_stale(now_ms) and self.reloc_interval_ms > 0:
            # 한 번도 못 잡았거나 추적이 끊긴 상태 — 국소 창은 추정 위치가 틀리면
            # 답을 못 찾으니 지도 전체를 거칠게 훑어 다시 잠근다 (FR-6.6).
            # 비싼 탐색이라 주기를 제한한다.
            _t1 = time.perf_counter()
            submitted = False
            if now_ms >= self._reloc_next_ms:
                self._reloc_next_ms = now_ms + self.reloc_interval_ms
                self._relocalize(points, scan, now_ms)
                submitted = True
            _t_stale = time.perf_counter() - _t1
            if _t_stale > 0.2:
                LOG.warning(
                    "observe_scan_stale_slow",
                    ms=round(_t_stale * 1000, 1),
                    seeded=self.pose_seeded,
                    last_pose=self._last_pose_ms is not None,
                )
            # 탐색을 던졌거나 잡힌 자세 자체가 없으면 여기서 끝. 자세가 있으면 계속
            # 내려가 국소 창도 돌린다 — 재측위(~1.5초)가 `pose_timeout_ms`(0.5초)보다
            # 길어 상실 표시가 붙는 사이에도, 시드·갱신된 자세라면 여기서 신선도가
            # 회복되어 상실이 풀린다 (자세가 정말 틀렸으면 점수 0 이라 갱신이 없어
            # 상실이 유지되고 다음 전역 탐색이 다시 찾는다). 이 fallthrough 가 없으면
            # 전역 탐색이 국소 정합을 영원히 굶기는 기아 루프가 된다 (2026-10-03 실측).
            if self._last_pose_ms is None:
                return
            del submitted
        # ── 정지 중 주기 감사 ── 국소 창만으론 «틀린 자리에 수렴한 잠금»을 스스로
        # 못 푼다. 서 있는 동안 지도 전체를 훑어 현재 자세가 전역으로도 맞는지 본다.
        if self.verify_interval_ms > 0 and now_ms >= self._verify_next_ms and self._is_stationary():
            self._verify_next_ms = now_ms + self.verify_interval_ms
            self._verify_pose(points, scan, now_ms)
        # 아직 한 번도 정합되지 않았으면 넓은 창으로 찾는다 — 시드 자세가 배치 오차만큼
        # 어긋나 있어도 첫 고정은 잡히게. 잡힌 뒤에는 좁은 창으로 추적한다.
        # 변화량만 넘긴다 — 절대 yaw 는 지도 좌표계와 옵셋이 있다.
        yaw_delta = self._consume_yaw_delta(now_ms)
        params = self.match_params
        if self._last_pose_ms is None and self.first_match_params is not None:
            params = self.first_match_params
        elif self._imu_delta_fresh and self.imu_match_params is not None:
            # 신선한 IMU 가 회전량을 쟀다 — 방위는 IMU 예측 근처에서만 찾는다.
            params = self.imu_match_params
        _t2 = time.perf_counter()
        result = match(self.match_grid, points, self.pose, params, yaw_delta=yaw_delta)
        _t_match = time.perf_counter() - _t2
        if _t_match > 0.2:
            LOG.warning(
                "observe_scan_match_slow",
                ms=round(_t_match * 1000, 1),
                first=params is self.first_match_params,
                window_m=params.search_lin_m,
            )
        self._match_frac = result.score / len(points)
        if result.skipped or result.score == 0:
            # 정합 실패 = 측위 상실 (FR-6.6). 점수 0 인 후보로 자세를 갱신하지 않는다.
            return
        # ⚠️ 약한 정합(frac 낮음)으로 추적을 끊지 않는다 — 가구 다리가 덜 그려진 자리에서는
        # **참 위치도 점수가 낮다.** 끊으면 재측위가 더 높은 점수의 엉뚱한 벽으로 로봇을
        # 옮긴다(실측 재현). 자세의 신뢰는 frac 이 아니라 전역 감사의 반복 일치
        # (`pose_verified`)로 판단하고, 지도 기록·자율 주행이 그 플래그를 본다.
        self.observe_map_pose(result.pose, now_ms)
        self._grow_map(points, result.score)
        _t0 = time.perf_counter()
        self._check_new_obstacle(scan)
        _t_obs = time.perf_counter() - _t0
        if _t_obs > 0.2 or _t_poll > 0.2:
            LOG.warning(
                "observe_scan_phase_slow",
                poll_ms=round(_t_poll * 1000, 1),
                obstacle_ms=round(_t_obs * 1000, 1),
            )

    def _imu_is_fresh(self, now_ms: int) -> bool:
        seen = self.safety.last_seen_ms
        return (
            self.safety.yaw_rad is not None
            and seen is not None
            and 0 <= now_ms - seen <= self.imu_fresh_ms
        )

    def _adopt_with_request_imu(self) -> None:
        """전역 결과로 자세를 바꾼 직후 — IMU 앵커와 조향 오프셋을 **요청 시점** IMU 로 함께 맞춘다.

        `observe_map_pose` 는 현재 IMU 로 오프셋을 만들지만, 자세는 요청 때 스캔의 것이다.
        둘이 다르면 다음 정합 실패 동안 조향 방위가 요청 이후 회전을 빠뜨린다 (Codex 검토 2 P1).
        """
        self._imu_anchor = self._result_imu
        if self._result_imu is not None:
            self._imu_offset = wrap_pi(self._result_imu - self.pose[2])

    def _expire_trust(self, now_ms: int) -> None:
        """측위가 `trust_expiry_ms` 넘게 끊겼다 — «검증됨»·사람 시드의 신뢰를 버린다.

        그 사이 로봇이 들려 옮겨졌을 수 있어, 예전 확인이나 처음 받은 시드를 근거로 계속
        주행·지도 기록을 허용하면 안 된다. 자세값 자체는 두고(전역 탐색의 출발점) 신뢰만 내린다.
        """
        if (
            self.trust_expiry_ms <= 0
            or self._trust_expired
            or self._last_pose_ms is None
            or now_ms - self._last_pose_ms <= self.trust_expiry_ms
        ):
            return
        self._trust_expired = True
        self._loc_epoch += 1
        if self._pose_verified or self.pose_seeded:
            LOG.warning(
                "localization_trust_expired",
                lost_ms=now_ms - self._last_pose_ms,
                was_verified=self._pose_verified,
                was_seeded=self.pose_seeded,
            )
        self._pose_verified = False
        self.pose_seeded = False
        self._global_votes.clear()

    def _min_match_score(self, points: np.ndarray) -> int:
        """**새 고정**(전역 재측위·시드 시작)의 채택 하한 — 연속 추적에는 적용하지 않는다."""
        return max(1, math.ceil(self.min_match_frac * len(points)))

    def _relocalize(self, points: np.ndarray, scan: Scan, now_ms: int) -> None:
        """지도 전역 재측위 — 국소 창이 풀 수 없는 잠김(오정합·임의 배치)을 연다.

        신뢰 순서는 **사람이 준 시드 > 전역 탐색 > 국소 추적**이다. 지도가 아직 덜 채워진
        자리(가구 다리·책상 밑)에서는 전역 최적이 엉뚱한 벽에 얹힐 수 있어, 첫 고정은
        시드가 있으면 시드 주변에서 잡고 전역 탐색은 그 뒤 **감사**로만 쓴다.
        """
        if self._last_pose_ms is None and self._seed_fallback(points, scan, now_ms):
            return
        self._submit_global("reloc", points, scan)

    # ── 전역 탐색 비동기 실행 ─────────────────────────────────
    # 동기로 돌리면 1~2초 동안 명령이 멈춰 온보드 워치독이 `ONBOARD_FAILSAFE` 를
    # 건다 — 탐색만 워커 스레드로 보내고, 결과 해석(투표·채택·지도 쓰기·장애물
    # 표시)은 루프 스레드가 `_poll_global` 에서 한다. 워커는 `match_grid` 를
    # 요청 시점의 독립 사본만 읽는다. 적분·팽창·복원이 워커의 셀/메타를 바꿀 수 없다.

    def _ensure_global_worker(self) -> None:
        if self._global_thread is not None and self._global_thread.is_alive():
            return
        self._global_thread = threading.Thread(
            target=self._global_worker_main,
            name="patrol-global-match",
            daemon=True,
        )
        self._global_thread.start()

    def _global_worker_main(self) -> None:
        while True:
            with self._global_cv:
                while self._global_req is None:
                    self._global_cv.wait()
                kind, points, scan, asked_pose, match_grid = self._global_req
                self._global_req = None
            try:
                result = global_match(
                    match_grid,
                    points,
                    lin_step_m=self.global_match_lin_step_m,
                    ang_step_rad=self.global_match_ang_step_rad,
                    occ_thresh=self.match_params.occ_thresh,
                    min_known_cells=self.match_params.min_known_cells,
                    free_thresh=self.plan_params.free_thresh,
                    sigma_m=self.match_params.sigma_m,
                    full_scan_ambiguity=self.global_full_scan_ambiguity,
                )
            except Exception as exc:  # noqa: BLE001 — 결과를 안 두면 inflight 가 영영 안 풀린다
                LOG.error("global_match_failed", error=f"{type(exc).__name__}: {exc}")
                result = None
            with self._global_cv:
                self._global_result = (kind, result, points, scan, asked_pose)

    def _submit_global(self, kind: str, points: np.ndarray, scan: Scan) -> bool:
        """전역 탐색을 워커에 맡긴다. 이미 한 건이 진행 중이면 놓친다(False)."""
        if self._global_inflight:
            return False
        cells, meta = self.match_grid.snapshot()
        cells.setflags(write=False)
        match_grid = OccupancyGrid(meta, cells)
        self._ensure_global_worker()
        self._global_inflight = True
        self._global_req_context = (
            self.safety.yaw_rad if self._imu_is_fresh(self._scan_now_ms) else None,
            self._move_seq,
            self._loc_epoch,
            self.wall_clock_ms(),
        )
        with self._global_cv:
            self._global_req = (kind, points.copy(), scan, self.pose, match_grid)
            self._global_cv.notify()
        return True

    def _poll_global(self, now_ms: int) -> None:
        """워커가 끝낸 결과를 루프 스레드에서 해석·적용한다 — 상태 변경은 여기서만."""
        with self._global_cv:
            done = self._global_result
            self._global_result = None
        if done is None:
            return
        self._global_inflight = False
        kind, result, points, scan, asked_pose = done
        wall_now_ms = self.wall_clock_ms()
        asked_imu, asked_moves, asked_epoch, asked_ms = self._global_req_context or (
            None,
            self._move_seq,
            self._loc_epoch,
            wall_now_ms,
        )
        if (
            asked_moves != self._move_seq
            or asked_epoch != self._loc_epoch
            or not 0 <= wall_now_ms - asked_ms <= self.global_result_max_age_ms
        ):
            # 요청 뒤에 로봇이 걸었다 — 그 스캔의 답을 지금 자세로 올리면 안 된다 (Codex 검토 P1).
            if self._edge.changed("global_result_stale", True):
                LOG.info("global_result_stale", kind=kind, moves=self._move_seq - asked_moves)
            if kind == "reloc":
                self._global_votes.clear()
            return
        self._edge.changed("global_result_stale", False)
        self._result_imu = asked_imu
        if kind == "verify":
            self._apply_verify_result(result, points, now_ms, asked_pose)
        else:
            self._apply_reloc_result(result, points, scan, now_ms)

    def _apply_reloc_result(
        self, result: MatchResult | None, points: np.ndarray, scan: Scan, now_ms: int
    ) -> None:
        """전역 재측위 결과 해석 — 요청 때의 점·스캔 기준으로 판정한다."""
        # 다음 시도 간격은 «결과가 나온 시점»부터 센다 — 탐색(~1.5초)이 간격(1초)보다
        # 길면 도착 직후 곧바로 재제출돼 국소 정합이 한 스캔도 못 도는 기아 루프가 된다.
        self._reloc_next_ms = max(self._reloc_next_ms, now_ms + self.reloc_interval_ms)
        self._match_frac = result.score / len(points) if result is not None else 0.0
        if result is None or result.score < self._min_match_score(points):
            if self._edge.changed("relocalize_failed", True):
                LOG.warning("relocalize_failed", frac=round(self._match_frac, 3))
            self._global_votes.clear()
            self._seed_fallback(points, scan, now_ms)
            return
        if result.unresolved or result.peers > self.reloc_max_peers:
            self._global_votes.clear()
            # 스캔이 지도를 구분 못 한다 — 어느 자세든 우연이다 (벽 포켓·대칭).
            if self._edge.changed("relocalize_ambiguous", True):
                LOG.warning(
                    "relocalize_ambiguous",
                    frac=round(self._match_frac, 3),
                    peers=result.peers,
                    competing_peaks=result.competing_peaks,
                    search_complete=result.search_complete,
                )
            self._seed_fallback(points, scan, now_ms)
            return
        if not self._vote_global(result.pose, result.peers):
            return
        self._edge.changed("relocalize_failed", False)
        LOG.info(
            "relocalized",
            x=round(result.pose[0], 2),
            y=round(result.pose[1], 2),
            yaw_deg=round(rad_to_deg(result.pose[2]), 1),
            frac=round(self._match_frac, 3),
            votes=self.reloc_votes,
        )
        self.observe_map_pose(result.pose, now_ms)
        # 이 자세는 요청 때 스캔의 것이다 — IMU 앵커·조향 오프셋도 그때 값으로 (이중 반영 방지).
        self._adopt_with_request_imu()
        # 전역 탐색이 변별력 있게 잡은 자세다 — 여기부터의 적분은 믿을 수 있다.
        self._mark_verified()
        self._grow_map(points, result.score)
        self._check_new_obstacle(scan)

    def _seed_fallback(self, points: np.ndarray, scan: Scan, now_ms: int) -> bool:
        """사람이 준 시드 주변 넓은 창으로 첫 추적을 시작한다. 잡았으면 True.

        아직 한 번도 못 잡은 경우에만, 시드가 있을 때만이다. 이렇게 잡은 자세는
        `pose_verified` 가 아니라 지도에 쓰지 않고, 서 있을 때 전역 감사가 확인한다.
        """
        if (
            not self.pose_seeded
            or self._last_pose_ms is not None
            or self.first_match_params is None
        ):
            return False
        result = match(self.match_grid, points, self.pose, self.first_match_params)
        # 사람의 말이 근거라 정합 하한은 절반만 요구한다 — 지도에 가구 다리가 빠진 자리면
        # 참 위치도 점수가 낮다. 어차피 미검증이라 지도에는 쓰지 않는다.
        if result.skipped or result.score < max(1, self._min_match_score(points) // 2):
            return False
        self._match_frac = result.score / len(points)
        LOG.info(
            "seed_tracking_started",
            x=round(result.pose[0], 2),
            y=round(result.pose[1], 2),
            frac=round(self._match_frac, 3),
            hint="전역 확인 전 — 지도에 쓰지 않음",
        )
        self.observe_map_pose(result.pose, now_ms)
        self._check_new_obstacle(scan)
        return True

    def _vote_global(self, pose: Pose, peers: int = 0) -> bool:
        """전역 탐색 결과 한 표. 직전 표와 0.3m 또는 `reloc_vote_yaw_rad` 넘게 다르면 다시 센다.

        로봇이 직전 표 뒤에 움직이지 않았으면(같은 장면) 경쟁 후보가
        `reloc_stationary_max_peers` 이하인 **유일한 답**일 때만 표로 센다 — 같은 장면의
        모호한 답을 세 번 세는 것은 증거가 아니다. `reloc_votes` 표가 모이면 True 를
        돌리고 비운다 — 호출자가 그 자세를 채택한다.
        """
        votes = self._global_votes
        imu = self.safety.yaw_rad
        if (
            imu is not None
            and self._imu_at_vote is not None
            and abs(wrap_pi(imu - self._imu_at_vote)) > math.radians(5.0)
        ):
            # 명령 없이 방위가 바뀌었다 — 사람이 들어 옮긴 경우도 «움직임» 이다.
            self._moved_since_vote = True
        # **모든 표**(첫 표 기준)와 맞아야 한다 — 직전 표만 보면 0, 0.29, 0.58m 가 한 무리가 된다.
        if votes and any(
            math.hypot(pose[0] - v[0], pose[1] - v[1]) > 0.3
            or abs(wrap_pi(pose[2] - v[2])) > self.reloc_vote_yaw_rad
            for v in votes
        ):
            votes.clear()
        if votes and not self._moved_since_vote and peers > self.reloc_stationary_max_peers:
            if self._edge.changed("vote_not_independent", True):
                LOG.info("global_vote_not_independent", peers=peers)
            return False
        self._edge.changed("vote_not_independent", False)
        self._moved_since_vote = False
        self._imu_at_vote = imu
        votes.append(pose)
        if len(votes) < self.reloc_votes:
            LOG.info(
                "global_vote",
                n=len(votes),
                need=self.reloc_votes,
                x=round(pose[0], 2),
                y=round(pose[1], 2),
            )
            return False
        votes.clear()
        return True

    def _mark_verified(self) -> None:
        """자세가 전역 확인됐다 — 지도 적분을 허용하고 되돌릴 기준점을 새로 찍는다."""
        self._pose_verified = True
        self._global_votes.clear()
        self._verified_snapshot = self.match_grid.snapshot()

    def _rebuild_masks(self) -> None:
        """격자 모양·내용이 바뀐 직후 팽창·동적 마스크를 새로 만든다."""
        static = inflate(self.grid, self.plan_params, obstacle_grid=self.loc_grid)
        body = body_collision_mask(self.grid, self.plan_params, obstacle_grid=self.loc_grid)
        frame = (self.grid.meta.resolution, self.grid.meta.origin_x, self.grid.meta.origin_y)
        changed = (
            self._mask_frame != frame
            or not np.array_equal(self._blocked, static)
            or not np.array_equal(self._body_blocked, body)
        )
        self._blocked, self._mask_frame = static, frame
        self._body_blocked = body
        dynamic = np.zeros_like(self._blocked, dtype=bool)
        for hit in self._obstacles:
            mark_obstacle(dynamic, self.grid, hit, self.obstacle_mark_radius_m)
        self._dynamic = dynamic
        self._updates_since_inflate = 0
        if changed and self.plan.reachable:
            # 새 정적 장애물은 신규 탐지에서 이미 아는 것으로 빠진다. 옛 경로까지
            # 유지하면 그 장애물을 그대로 통과하므로 목표만 보존해 다시 계획한다.
            self.plan = Plan(self.plan.label)
            self.waypoint_index = 0
            if self.phase is Phase.MOVING:
                self.phase = Phase.PLANNING
                self._replan_stop_required = True
                self.commander.halt()
            LOG.info("navigation_mask_changed_replan", target=self.plan.label)

    def _is_stationary(self) -> bool:
        """로봇이 서 있는가 — 감사 탐색(수 초)은 서 있을 때만 돌려도 안전하다."""
        return self.phase is not Phase.MOVING or self._stopped_since_ms is not None

    def _verify_pose(self, points: np.ndarray, scan: Scan, _now_ms: int) -> bool:
        """정지 중 자세 감사 요청 — 전역 탐색을 워커로 보낸다.

        결과는 `_apply_verify_result` 가 뒤늦게 적용한다. 제출에 성공했으면 True 를
        돌려 이번 스캔의 국소 정합도 건너뛴다 — 워커 기동 직후 같이 돌면 GIL 경합으로
        국소 정합이 수십 배 느려져 명령 주기를 해친다.
        """
        return self._submit_global("verify", points, scan)

    def _apply_verify_result(
        self,
        result: MatchResult | None,
        points: np.ndarray,
        now_ms: int,
        asked_pose: Pose | None = None,
    ) -> bool:
        """전역 감사 결과 해석 — 다른 자리를 가리키면 자세를 바로잡고 True 를 돌린다."""
        self._match_frac = result.score / len(points) if result is not None else 0.0
        if (
            result is None
            or result.score < self._min_match_score(points)
            or result.unresolved
            or result.peers > self.reloc_max_peers
        ):
            # 구분력 없는 스캔으로는 현재 자세를 의심도 확신도 못 한다 — 유지한다.
            self._global_votes.clear()
            if self._edge.changed("pose_verify_ambiguous", True):
                LOG.warning(
                    "pose_verify_ambiguous",
                    frac=round(self._match_frac, 3),
                    peers=0 if result is None else result.peers,
                )
            return False
        self._edge.changed("pose_verify_ambiguous", False)
        # 탐색은 **요청 때의 스캔**으로 했다 — 그 사이 국소 추적이 자세를 옮겼을 수 있으니
        # 요청 때 자세와 비교한다. 그 사이 로봇이 실제로 움직였으면 이 감사는 낡았다.
        reference = self.pose if asked_pose is None else asked_pose
        drift = math.hypot(self.pose[0] - reference[0], self.pose[1] - reference[1])
        if drift > 0.1 or abs(wrap_pi(self.pose[2] - reference[2])) > math.radians(5):
            return False
        moved = math.hypot(result.pose[0] - reference[0], result.pose[1] - reference[1])
        turned = abs(wrap_pi(result.pose[2] - reference[2]))
        if moved <= 0.4 and turned <= self.reloc_vote_yaw_rad:
            # 국소 창의 잠금이 전역에서도 맞다 — 여기까지의 적분을 확정한다.
            # (신선도는 국소 추적이 계속 갱신한다 — 감사가 대신 찍지 않는다.)
            self._mark_verified()
            return False
        if self.pose_seeded and not self._pose_verified:
            # 사람이 준 자리를 아직 전역이 한 번도 확인하지 못했다 — 지도가 덜 채워진
            # 자리(가구 다리 등)면 전역 최적 쪽이 틀릴 수 있어 **덮어쓰지 않는다.**
            # 지도에도 쓰지 않은 채 추적만 이어 가고, 트인 곳에 나오면 감사가 맞춰 준다.
            if self._edge.changed("pose_verify_disagree", True):
                LOG.warning(
                    "pose_verify_disagree",
                    seeded=[round(self.pose[0], 2), round(self.pose[1], 2)],
                    global_best=[round(result.pose[0], 2), round(result.pose[1], 2)],
                    moved_m=round(moved, 2),
                    peers=result.peers,
                    hint="시드 자리 유지 — 지도 미기록",
                )
            return False
        self._edge.changed("pose_verify_disagree", False)
        if not self._vote_global(result.pose, result.peers):
            # 한 번 다른 답이 나온 것으로는 안 옮긴다 — 같은 답이 `reloc_votes` 번 반복돼야 한다.
            return False
        LOG.warning(
            "pose_corrected",
            old=[round(self.pose[0], 2), round(self.pose[1], 2)],
            new=[round(result.pose[0], 2), round(result.pose[1], 2)],
            moved_m=round(moved, 2),
            peers=result.peers,
        )
        if self._verified_snapshot is not None:
            # 마지막 확인 뒤의 적분은 틀린 자세로 한 것이다 — 지도를 그 시점으로 되돌린다.
            self.match_grid.restore(self._verified_snapshot)
            self._rebuild_masks()
            LOG.warning("map_rolled_back", to="last_verified_snapshot")
        self.observe_map_pose(result.pose, now_ms)
        self._adopt_with_request_imu()
        self._mark_verified()
        return True

    def _consume_yaw_delta(self, now_ms: int | None = None) -> float:
        """직전 스캔 이후 IMU 가 본 회전량을 소비한다(두 번 반영하지 않는다). 없으면 0.

        `_imu_delta_fresh` 는 이 변화량이 신선한 텔레메트리 두 표본의 차일 때만 참이다 —
        참이면 국소 정합이 방위를 IMU 예측 근처(`imu_match_params`)에서만 찾는다.
        """
        current = self.safety.yaw_rad
        seen = self.safety.last_seen_ms
        fresh = (
            current is not None
            and now_ms is not None
            and seen is not None
            and 0 <= now_ms - seen <= self.imu_fresh_ms
        )
        anchor = self._imu_anchor
        if current is None or anchor is None:
            self._imu_delta_fresh = False
            if self._imu_anchor is None:
                # 아직 앵커가 없으면 지금 값으로 — 다음부터 회전량이 이어진다.
                self._imu_anchor = current
            return 0.0
        # 앵커(지금 `pose` 의 관측 시점) 이후 회전량 — 정합 실패가 이어져도 누적이 남는다.
        self._imu_delta_fresh = bool(fresh)
        return wrap_pi(current - anchor)

    def _steering_yaw(self) -> float:
        """조향에 쓸 방위 — 스캔 사이에는 IMU 전파값, 없으면 정합 방위 그대로.

        정합 방위는 바퀴(수백 ms)마다만 갱신되므로 그 사이의 휨을 IMU 가 즉시 본다.
        옵셋은 `observe_map_pose` 에서 정합될 때마다 다시 맞춘다.
        """
        imu = self.safety.yaw_rad
        if imu is not None and self._imu_offset is not None:
            return wrap_pi(imu - self._imu_offset)
        return self.pose[2]

    def _grow_map(self, points: np.ndarray, score: int) -> None:
        """정합이 확실한 바퀴를 지도에 적분하고 주기적으로 팽창을 새로 만든다.

        보행 속도에서 한 바퀴(~0.1초) 사이의 이동은 수 cm 이하라 이동 중 적분 오차는
        셀 하나 이하다 — 지도는 로봇이 지나간 곳을 «확실히 빈» 셀로 채워 나간다.
        """
        if (
            not self.live_map_write
            or self.map_hit_logodds <= 0
            or not self._pose_verified
            or score < MAP_WRITE_MIN_SCORE_FRAC * len(points)
        ):
            return
        integrate_scan(
            self.match_grid,
            self.pose,
            points,
            hit=self.map_hit_logodds,
            miss=self.map_miss_logodds,
            pad_cells=self.map_pad_cells,
        )
        self._updates_since_inflate += 1
        # `integrate` 가 격자를 늘리면 원점이 옮겨져 두 마스크의 셀 좌표가 전부 어긋난다 —
        # 주기와 무관하게 모양이 달라진 즉시 둘 다 새로 만든다. 장애물 표시는 월드 좌표라
        # (`self._obstacles`) 새 격자에 다시 찍을 수 있다.
        if self._updates_since_inflate >= MAP_INFLATE_EVERY or (
            self._blocked is not None and self._blocked.shape != self.grid.cells.shape
        ):
            self._rebuild_masks()

    def _check_new_obstacle(self, scan: Scan) -> None:
        """연속 확인 후에만 재계획한다. 한 번의 반사로 경로를 버리지 않는다."""
        # 계획에서 제외한 loc-only 셀도 실제 반사가 있으면 새 물체다.
        # 정적 추종 여유가 예상 거리를 줄여 물체를 숨기지 않도록 nav 점유와
        # 확인된 동적 표시로 비교한다. 측위 지도 자체로 탐지를 억제하지 않는다.
        hit = detect_new_obstacle(
            self.pose,
            scan.points,
            self.grid,
            self._dynamic,
            check_radius_m=self.new_obstacle_check_radius_m,
            margin_m=self.new_obstacle_margin_m,
            occ_thresh=self.plan_params.occ_thresh,
            # 벽 스침 반사 허용치 = 계획 여유 − 몸체 반경 (planner.detect_new_obstacle 주석).
            known_tolerance_m=max(
                0.0, self.plan_params.clearance_m - self.plan_params.body_radius_m
            ),
        )
        if hit is None:
            self._pending_hit, self._pending_count = None, 0
            return
        near = (
            self._pending_hit is not None
            and math.hypot(hit[0] - self._pending_hit[0], hit[1] - self._pending_hit[1]) < 0.3
        )
        if near:
            self._pending_count += 1
        else:
            self._pending_hit, self._pending_count = hit, 1
        if self._pending_count < self.new_obstacle_confirmations:
            return

        assert self._dynamic is not None
        mark_obstacle(self._dynamic, self.grid, hit, self.obstacle_mark_radius_m)
        self._obstacles.append(hit)
        self._new_obstacles.append(hit)
        self._pending_hit, self._pending_count = None, 0
        self.stats.replans += 1
        LOG.info("obstacle_confirmed", x=round(hit[0], 2), y=round(hit[1], 2))
        self.plan = Plan(self.plan.label)  # 목표는 유지하고 경로만 버린다
        if self.phase is Phase.MOVING:
            self.commander.halt()
            self._replan_stop_required = True
            self.phase = Phase.PLANNING

    # ── 조작자 ────────────────────────────────────────────────
    def start(self) -> None:
        if self.phase is Phase.IDLE:
            self.phase = Phase.PLANNING
            LOG.info("patrol_started", zones=list(self.zones.labels))

    def emergency_stop(self, reason: str) -> str:
        """`ESTOP` 전문을 돌려준다. 호출자가 즉시 보낸다."""
        self.phase = Phase.HALTED
        self._halt_reason = reason
        self._spinning = False
        self.stats.estops += 1
        self.plan = Plan(None)
        LOG.error("estop", reason=reason)
        return self.commander.emergency_stop()

    def request_reset(self) -> str:
        """`RESET_SAFE` 전문을 돌려준다. 사람이 확인했을 때만 부른다 (ADR-21).

        순찰 재개는 `_settle_reset` 이 로봇의 래치 해제를 확인한 뒤다.
        """
        self._reset_requested = True
        LOG.info("reset_requested", reason=self._halt_reason)
        return self.commander.clear_safe()

    def _settle_reset(self) -> None:
        """로봇이 `safety_latched=false` 를 보고했을 때만 순찰로 돌아간다 (ADR-21).

        반향될 수 있는 `state` 로는 확인하지 않는다.
        """
        if not self._reset_requested:
            return
        latched = self.safety.latched
        if latched is None:
            # ⚠️ 구형 펌웨어(`safety_latched` 없음) — 로봇 해제를 확인할 수 없어 사람의
            # 확인(`request_reset`)을 근거로 재개하고, 검증 불가를 경고로 남긴다.
            if self._edge.changed("latch_unverifiable", True):
                LOG.warning(
                    "safety_latch_unverifiable",
                    hint="텔레메트리에 safety_latched 가 없다 — 펌웨어를 갱신해야 검증된다",
                )
            self._finish_reset()
            return
        if not latched and self.safety.onboard_state in ("IDLE", "PATROL"):
            self._finish_reset()

    def _finish_reset(self) -> None:
        self._reset_requested = False
        self._halt_reason = ""
        self.phase = Phase.PLANNING
        LOG.info("reset_settled", onboard_state=self.safety.onboard_state)

    # ── 한 틱 ─────────────────────────────────────────────────
    def step(self, now_ms: int) -> tuple[str, ...]:
        """한 주기의 판단. 즉시 보낼 전문만 돌려준다(보통 비어 있다).

        주기 전문은 호출자가 `commander.tick(now_ms)` 로 따로 받는다.
        """
        urgent = self._guard(now_ms)
        if urgent:
            return urgent

        was_halted = self.phase is Phase.HALTED
        self._settle_reset()
        if was_halted and self.phase is not Phase.HALTED:
            # 래치 해제와 측위 복구는 별개다. 위 _guard는 HALTED에서 조기에
            # 돌아오므로 해제가 확인된 같은 틱에도 출발 관문을 적용한다.
            self._guard_localization(now_ms)
        if self.safety.last_seen_ms is None:
            # 첫 텔레메트리로 링크가 확인될 때까지 정지한다.
            self.commander.halt()
        elif self.phase is Phase.HALTED or self.phase is Phase.IDLE or self.phase is Phase.LOST:
            self.commander.halt()
        elif self.safety.obstacle_active:
            # 온보드가 이미 멈췄다 — 판정을 흉내내지 않고 의도만 정지로 내린다 (아키텍처 1.2).
            self.commander.halt()
            if self._edge.changed("obstacle", True):
                LOG.info("onboard_obstacle_hold", dist_cm=self.safety.dist_cm)
        else:
            self._edge.forget("obstacle")
            self._advance()

        # 상태는 이번 틱의 판단이 반영된 뒤 마지막에 알린다.
        self.commander.announce(self.fsm_state)
        return ()

    def steer(self, now_ms: int) -> None:
        """길 찾기만 하는 한 틱 — 호스트 런타임(`host/runtime.py`)의 `PATROL` 이 부른다.

        `step()` 과 달리 **`STATE` 를 알리지 않고 래치·링크도 보지 않는다.** 런타임에서는
        FSM 이 상태 알림과 래치·링크 감시를 쥐고, 이것은 FSM 이 `PATROL` 일 때만 불린다.
        여기서 그것들을 다시 판정하면 상태 알림이 둘이 되어 서로 덮는다.
        """
        if self.phase in (Phase.IDLE, Phase.HALTED):
            self.commander.halt()
            return
        # FSM/링크 판정은 런타임에 남기되, 출발 자격은 step()과 같은 관문을 거친다.
        # 자세만 신선해도 미관측 공간·정지 후 스캔 부재라면 실제 주행을 허용할 수 없다.
        self._guard_localization(now_ms)
        if self.phase is Phase.LOST:
            self.commander.halt()
            return
        if self.safety.obstacle_active:
            # 온보드가 이미 멈췄다 — 판정을 흉내내지 않고 의도만 정지로 내린다 (아키텍처 1.2).
            self.commander.halt()
            if self._edge.changed("obstacle", True):
                LOG.info("onboard_obstacle_hold", dist_cm=self.safety.dist_cm)
            return
        self._edge.forget("obstacle")
        self._advance()

    def resume(self) -> None:
        """경로만 버리고 목표 구역은 둔 채 다시 계획하게 한다.

        추적·경보·구역 점검을 마치고 순찰로 돌아오면 로봇은 떠날 때와 다른 자리에
        있다 — 옛 경로의 웨이포인트를 따라가면 엉뚱한 곳으로 되돌아간다. 목표는
        그대로라 **아직 가지 않은 구역으로 지금 자리에서** 다시 푼다 (`_replan`).
        """
        self.plan = Plan(self.plan.label)
        self.waypoint_index = 0
        self.phase = Phase.PLANNING
        LOG.info("patrol_resumed", target=self.plan.label)

    def _guard(self, now_ms: int) -> tuple[str, ...]:
        """안전 점검. 단계를 옮기고, 즉시 보낼 전문이 있으면 돌려준다.

        순서가 우선순위다 — 로봇이 보고한 래치가 가장 먼저다 (아키텍처 1.2). 여기서는
        `ESTOP` 을 만들지 않는다(사람이 누른 경우와 `guard_scan` 만 만든다).
        """
        # ① 로봇이 래치를 걸었다고 보고했다 — 우리가 판정하지 않는다
        if self.safety.latched or self.safety.onboard_state == "FAILSAFE":
            if self.phase is not Phase.HALTED:
                self._halt_reason = "온보드 FAILSAFE 보고"
                self.phase = Phase.HALTED
                LOG.error("onboard_failsafe", state=self.safety.onboard_state)
            return ()

        # ② 텔레메트리 침묵 — 링크가 끊겼다. 명령 송신은 멈추지 않는다(10Hz 송신이 곧
        #    하트비트다 · PROTOCOL 1절). 한 건도 받기 전은 두절로 보지 않는다.
        silent = self.safety.last_seen_ms is not None and (
            now_ms - self.safety.last_seen_ms > self.drive.link_loss_ms
        )
        if silent and self.phase not in (Phase.IDLE, Phase.HALTED):
            if self._edge.changed("link", False):
                LOG.error("telemetry_silent", limit_ms=self.drive.link_loss_ms)
            self._halt_reason = "텔레메트리 두절"
            self.phase = Phase.HALTED
            return ()
        if not silent:
            self._edge.changed("link", True)

        if self.phase in (Phase.IDLE, Phase.HALTED):
            return ()

        return self._guard_localization(now_ms)

    def _guard_localization(self, now_ms: int) -> tuple[str, ...]:
        # ③ 측위 상실 — `ESTOP` 이 아니라 `LOST` 다 (FR-6.6)
        stale = self.pose_stale(now_ms)
        if stale:
            self._lose("pose_stale")
            return ()
        if self._replan_stop_required and self._last_sent_moving:
            # halt 의도가 다음 steer()에서 MOVE로 덮이면 실제 STOP이 송신되지 않는다.
            # 송신 기록이 STOP을 확인할 때까지 정지하며, 아래에서 새 스캔도 요구한다.
            self._lose("replan_waiting_for_sent_stop")
            return ()

        # 경로 재계획 전에도 검사한다. 이미 가진 경로를 추종하는 중에 자세가 미지/
        # 지도 밖으로 옮겨지면 plan_to()를 다시 부르지 않아도 즉시 정지해야 한다.
        row, col = self.grid.to_cell(self.pose[0], self.pose[1])
        height, width = self.grid.cells.shape
        if not (0 <= row < height and 0 <= col < width) or (
            self.grid.cells[row, col] > self.plan_params.free_thresh
        ):
            self._lose("pose_outside_observed_free_space")
            return ()

        # 몸체 침범은 추종 여유와 별개다. 미관측 가장자리도 매 틱 막는다.
        if self.body_blocked[row, col]:
            self._lose("pose_inside_navigation_clearance")
            return ()

        # 추종 오차로 여유 띠에 들어와도 기존 경로로 안전하게 복귀할 수 있으면
        # 계속 걷는다. 출발 탈출과 달리 매번 STOP/새 스캔/재계획을 요구하지 않는다.
        if self.blocked[row, col]:
            following = self._can_follow_from_margin()
            if not following and self._last_sent_moving:
                self.plan = Plan(self.plan.label)
                self._replan_stop_required = True
                self._lose("escape_replan_required")
                return ()
            if not following and (
                escape_start(
                    self.grid,
                    self.blocked,
                    self.body_blocked,
                    self.pose[:2],
                    self.plan_params.start_escape_max_m,
                )
                is None
            ):
                self._lose("pose_inside_navigation_clearance")
                return ()

        # 이동 중에는 스캔이 게이팅되어 없어도 된다. 최초 출발/다시 멈춘 뒤에는
        # 안정화 이후 관측을 요구한다. MOVE 중의 오래된 스캔으로는 재출발 못 한다.
        if not self._last_sent_moving:
            stopped = self._stopped_since_ms
            scan_time = self._last_scan_ms
            if (
                stopped is None
                or scan_time is None
                or (
                    scan_time < stopped + self.drive.settle_delay_ms
                    or not 0 <= now_ms - scan_time <= self.drive.scan_stall_timeout_ms
                )
            ):
                self._lose("stationary_scan_unavailable")
                return ()

        if self.phase is Phase.LOST:
            LOG.info("localization_reacquired")
            self.phase = Phase.PLANNING
        self._replan_stop_required = False
        self._edge.forget("localization_problem")
        return ()

    def _lose(self, reason: str) -> None:
        if self.phase is not Phase.LOST:
            self.stats.lost += 1
        if self._edge.changed("localization_problem", reason):
            LOG.warning(reason)
        self.phase = Phase.LOST

    def guard_scan(self, scan: Scan) -> str | None:
        """LiDAR 전방 위험거리 판정 — 온보드 초음파 판정을 대체하지 않고 더하는 호스트측 판정이다.

        위험하면 `ESTOP` 전문을 돌려주고 호출자가 즉시 보낸다.
        """
        if self.phase in (Phase.IDLE, Phase.HALTED):
            return None
        forward = min_forward_distance(scan.points, self.forward_fan_rad)
        if forward is not None and forward < self.drive.lidar_estop_m:
            return self.emergency_stop(f"LiDAR 전방 {forward:.2f} m")
        return None

    # ── 순찰 진행 ─────────────────────────────────────────────
    def _advance(self) -> None:
        if not len(self.zones):
            self.commander.halt()
            if self._edge.changed("no_zones", True):
                LOG.error("no_zones", hint="tools/ops/zone_select.py 를 먼저 실행한다")
            return
        if self._own_localization and not (self._pose_verified or self.pose_seeded):
            # 사람도 지도도 이 자세를 보증한 적이 없다 — 걷지 않는다. 서 있으면 전역 감사가
            # 돌아 확인되거나(재개) 다른 자리로 바로잡는다.
            self.commander.halt()
            if self._edge.changed("pose_unverified_hold", True):
                LOG.warning("pose_unverified_hold", frac=round(self._match_frac, 3))
            return
        self._edge.changed("pose_unverified_hold", False)

        if self.phase is Phase.INSPECT:
            # 도착하면 즉시 다음 구역으로 — 카메라 판독(FR-8)은 `change_detect` 소관이다.
            self.phase = Phase.PLANNING

        if self.phase is Phase.PLANNING or not self.plan.reachable:
            self._replan()
            if not self.plan.reachable:
                self.commander.halt()
                return
            self.phase = Phase.MOVING

        self._follow()

    def _replan(self) -> None:
        # 새 경로는 새 방위 오차에서 시작한다 — 지난 경로의 회전을 이어 가지 않는다.
        self._spinning = False
        start = (self.pose[0], self.pose[1])
        candidates = {label: self.zones.xy(label) for label in self.zones.labels}

        # 목표가 이미 정해져 있으면 경로만 다시 푼다 (재계획).
        if self.plan.label is not None and self.plan.label not in self.visited:
            retry = plan_to(
                self.plan.label,
                candidates[self.plan.label],
                start,
                self.grid,
                self.blocked,
                self.plan_params,
                body_blocked=self.body_blocked,
            )
            if retry.reachable:
                self.plan = retry
                self.waypoint_index = 0
                return

            # 동적 장애물 표시를 버리고 다시 계획해 본다 — 누적된 오탐이 구역을 영구히
            # 봉인하지 않게. ⚠️ 횟수는 `max_reverify_attempts` 로 제한한다 — 진짜 장애물로
            # 되돌아가기를 되풀이하면 E-STOP 이 반복되고 서보 기어가 상한다.
            label = cast(str, self.plan.label)
            attempts = self._reverify_attempts.get(label, 0)
            if attempts < self.max_reverify_attempts and self._clear_dynamic("zone_reverify"):
                self._reverify_attempts[label] = attempts + 1
                retry = plan_to(
                    label,
                    candidates[label],
                    start,
                    self.grid,
                    self.blocked,
                    self.plan_params,
                    body_blocked=self.body_blocked,
                )
                if retry.reachable:
                    self.plan = retry
                    self.waypoint_index = 0
                    return

            # 다 썼다 — 이 사이클에서는 이 구역을 버린다. 다음 사이클이
            # 경계에서 표시를 지우고 처음부터 다시 확인한다.
            LOG.warning(
                "zone_unreachable",
                label=label,
                reverify_attempts=attempts,
                reason=retry.fail_reason,
                goal_moved_m=round(retry.goal_moved_m, 2),
            )
            self.visited = self.visited | {label}

        skipped: list[tuple[str, str]] = []
        self.plan = select_next(
            cycle=self.cycle,
            visited=self.visited,
            start=start,
            candidates=candidates,
            order=self.zones.labels,
            grid=self.grid,
            blocked=self.blocked,
            params=self.plan_params,
            random_after_first_cycle=self.random_after_first_cycle,
            rng=self.rng,
            skipped_out=skipped,
            body_blocked=self.body_blocked,
        )
        self.waypoint_index = 0
        # 도달 불가 구역은 같은 목록이 바뀔 때만 기록한다 — 매 틱 재시도하면
        # 로그가 초당 10줄씩 쌓여 진짜 신호를 덮는다 (목업에서 실제로 확인).
        skip_key = ",".join(f"{label}:{reason}" for label, reason in skipped)
        if skipped and self._edge.changed("zones_unreachable", skip_key):
            LOG.warning(
                "zones_unreachable",
                skipped=[label for label, _ in skipped],
                reasons=[reason for _, reason in skipped],
            )
        if not skipped:
            self._edge.changed("zones_unreachable", "")
        if self.plan.reachable:
            self._edge.changed("no_reachable_zone", False)
            LOG.info(
                "zone_selected",
                label=self.plan.label,
                cycle=self.cycle,
                path_m=round(self.plan.length_m, 2),
                goal_moved_m=round(self.plan.goal_moved_m, 2),
                start_moved_m=round(self.plan.start_moved_m, 2),
                # 찍은 앵커와 실제 향하는 셀이 다르면 둘 다 남긴다 — 목표가 조용히
                # 옮겨지는 일은 없다 (가구 다리 병합으로 앵커가 팽창 안에 들어갈 수 있다).
                requested=(
                    [round(v, 2) for v in self.plan.requested]
                    if self.plan.requested is not None
                    else None
                ),
                effective=(
                    [round(v, 2) for v in self.plan.effective]
                    if self.plan.effective is not None
                    else None
                ),
            )
        elif self.visited:
            self._complete_cycle()
        elif self._edge.changed("no_reachable_zone", True):
            LOG.error("no_reachable_zone", visited=sorted(self.visited))

    def _follow(self) -> None:
        assert self.plan.label is not None
        anchor = self.zones.xy(self.plan.label)
        # 스냅된 목표(plan.effective)에 도착해도 «구역에 갔다» 로 인정한다 — 앵커가
        # 가구 다리·팽창 안에 묻혔으면 그 자리엔 영원히 못 선다. 둘 중 가까운 곳이
        # 도착 반경 안이면 도착이다.
        effective = self.plan.effective or anchor
        arrived = (
            min(
                math.hypot(anchor[0] - self.pose[0], anchor[1] - self.pose[1]),
                math.hypot(effective[0] - self.pose[0], effective[1] - self.pose[1]),
            )
            < self.drive.arrival_radius_m
        )
        if arrived and (
            self.plan.escape_end_index < 0 or self.waypoint_index > self.plan.escape_end_index
        ):
            self._arrive(self.plan.label)
            return

        waypoint = self._current_waypoint()
        if not segment_clear(self.grid, self.body_blocked, self.pose[:2], waypoint):
            self.commander.halt()
            self._replan_stop_required = True
            self.plan = Plan(self.plan.label)
            self.phase = Phase.PLANNING
            return
        heading = math.atan2(waypoint[1] - self.pose[1], waypoint[0] - self.pose[0])
        steering = steering_for(heading - self._steering_yaw(), self.drive, spinning=self._spinning)
        spinning = steering.step_mm == 0.0 and steering.angle_deg != 0.0
        if self._edge.changed("spin", spinning) and spinning:
            LOG.info(
                "spin_in_place",
                error_deg=round(rad_to_deg(wrap_pi(heading - self.pose[2])), 1),
                target=self.plan.label,
            )
        self._spinning = spinning
        # 규약 범위는 인코더가 자르지만(규칙 ②), 잘려서 나가는 것을 로그로
        # 보고 싶지는 않으므로 여기서 설정값 안에 둔다.
        turn_limit = abs(self.drive.spin_turn_deg if spinning else self.drive.turn_deg)
        self.commander.drive(
            clamp(steering.step_mm, -abs(self.drive.step_mm), abs(self.drive.step_mm)),
            clamp(steering.angle_deg, -turn_limit, turn_limit),
        )

    def _current_waypoint(self) -> tuple[float, float]:
        waypoints = self.plan.waypoints
        while self.waypoint_index < len(waypoints) - 1:
            candidate = waypoints[self.waypoint_index]
            radius = self.drive.waypoint_radius_m
            if math.hypot(candidate[0] - self.pose[0], candidate[1] - self.pose[1]) >= radius:
                break
            # 정상 도달 반경을 쓰되 다음 점으로 자르는 구간 전체의 몸체 여유를
            # 확인한다. 탈출 끝점을 건너뛰어도 벽/미관측 코너를 가로지를 수 없다.
            if not segment_clear(
                self.grid, self.body_blocked, self.pose[:2], waypoints[self.waypoint_index + 1]
            ):
                break
            self.waypoint_index += 1
        return waypoints[min(self.waypoint_index, len(waypoints) - 1)]

    def _can_follow_from_margin(self) -> bool:
        """계획 선분 가까이에서 몸체 충돌 없이 전방 웨이포인트로 복귀 가능한가."""
        if not self.plan.reachable or self.phase is not Phase.MOVING:
            return False
        target = self._current_waypoint()
        index = min(self.waypoint_index, len(self.plan.waypoints) - 1)
        if index == 0:
            return False
        previous = self.plan.waypoints[index - 1]
        dx, dy = target[0] - previous[0], target[1] - previous[1]
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return False
        progress = (
            (self.pose[0] - previous[0]) * dx + (self.pose[1] - previous[1]) * dy
        ) / length_sq
        # 목표를 이미 지나친 뒤 뒤로 되돌아가는 연결은 정상 추종으로 보지 않는다.
        if progress > 1:
            return False
        nearest = (previous[0] + max(0.0, progress) * dx, previous[1] + max(0.0, progress) * dy)
        if math.dist(self.pose[:2], nearest) > self.drive.waypoint_radius_m:
            return False
        return segment_clear(self.grid, self.body_blocked, self.pose[:2], target)

    def _arrive(self, label: str) -> None:
        self.commander.halt()
        self._spinning = False
        self.phase = Phase.INSPECT
        self.visited = self.visited | {label}
        self.stats.zones_visited += 1
        self.plan = Plan(None)
        LOG.info("zone_arrived", label=label, cycle=self.cycle)
        if len(self.visited) >= len(self.zones):
            self._complete_cycle()

    def _complete_cycle(self) -> None:
        self.cycle += 1
        self.visited = frozenset()
        self.stats.cycles += 1
        # 새 사이클은 동적 장애물을 지우고 공간을 다시 확인한다 — 통행 영역이 단조 감소하지 않게.
        self._clear_dynamic("cycle_boundary")
        self._reverify_attempts.clear()
        LOG.info("cycle_completed", cycle=self.cycle)

    def _clear_dynamic(self, reason: str) -> bool:
        """동적 장애물 표시를 버린다. 버릴 것이 있었으면 참.

        지도(`grid.cells`)는 건드리지 않는다 — 애초에 거기에 쓰지 않았다.
        """
        assert self._dynamic is not None
        if not self._dynamic.any():
            return False
        LOG.info("dynamic_obstacles_cleared", reason=reason, count=len(self._obstacles))
        self._dynamic[:, :] = False
        self._obstacles.clear()
        self._pending_hit, self._pending_count = None, 0
        return True


# ══════════════════════════════════════════════════════════════
#  설정에서 파라미터를 조립한다 — 숫자는 코드에 박지 않는다 (NFR-3①)
# ══════════════════════════════════════════════════════════════


def drive_params_from_config(config: Mapping[str, Any]) -> DriveParams:
    """`config.yaml` 에서 보행·안전 파라미터를 만든다. 없는 키는 `KeyError` 다(기본값 없음)."""
    gait = config["gait"]
    safety = config["safety"]
    localization = config["localization"]
    zones = config["zones"]
    lidar = config["lidar"]
    return DriveParams(
        step_mm=float(gait["step_length_mm"]),
        turn_deg=float(gait["turn_angle_deg"]),
        reverse_mm=float(gait["reverse_distance_mm"]),
        heading_tolerance_rad=deg_to_rad(float(lidar["heading_tolerance_deg"])),
        reverse_threshold_rad=deg_to_rad(float(lidar["reverse_threshold_deg"])),
        spin_threshold_rad=deg_to_rad(float(lidar["spin_threshold_deg"])),
        spin_turn_deg=float(lidar["spin_turn_deg"]),
        arrival_radius_m=float(zones["arrival_radius_mm"]) / 1000.0,
        waypoint_radius_m=float(lidar["waypoint_radius_mm"]) / 1000.0,
        lidar_estop_m=float(lidar["estop_distance_mm"]) / 1000.0,
        link_loss_ms=int(safety["link_loss_failsafe_ms"]),
        cmd_timeout_ms=int(safety["cmd_timeout_ms"]),
        pose_timeout_ms=int(localization["pose_timeout_ms"]),
        scan_stall_timeout_ms=int(lidar["scan_stall_timeout_ms"]),
        settle_delay_ms=int(localization["settle_delay_ms"]),
    )


def load_patrol_map(config: Mapping[str, Any], maps: Path) -> tuple[OccupancyGrid, ZoneStore]:
    """순찰 지도와 구역 좌표. 구역이 하나도 없으면 `ConfigError` 다."""
    grid = OccupancyGrid.load(maps)
    labels = tuple(str(label) for label in config["zones"]["ids"])
    zones = ZoneStore.load(maps, labels)
    if not len(zones):
        raise ConfigError(
            f"구역 좌표가 없다: {maps / 'zones.json'} — tools/ops/zone_select.py 를 먼저 실행한다"
        )
    return grid, zones


def controller_from_config(
    config: Mapping[str, Any],
    commander: Commander,
    grid: OccupancyGrid,
    zones: ZoneStore,
    rng: random.Random | None,
) -> PatrolController:
    """설정으로 컨트롤러를 조립한다. 순찰 도구와 호스트 런타임이 같이 쓴다."""
    lidar = config["lidar"]
    match_params = match_params_from_config(config)
    return PatrolController(
        commander=commander,
        grid=grid,
        zones=zones,
        drive=drive_params_from_config(config),
        plan_params=plan_params_from_config(config),
        match_params=match_params,
        # 첫 정합은 로봇이 어디 놓였는지 몰라도 잡아야 한다 — 좁은 추적 창 대신
        # ±0.8m · ±40° 로 한 번 넓게 찾는다. 잡힌 뒤에는 쓰지 않는다.
        first_match_params=MatchParams(
            search_lin_m=0.8,
            search_lin_step_m=match_params.search_lin_step_m * 1.5,
            search_ang_rad=deg_to_rad(40.0),
            search_ang_step_rad=match_params.search_ang_step_rad * 2.0,
            occ_thresh=match_params.occ_thresh,
            min_known_cells=match_params.min_known_cells,
            sigma_m=match_params.sigma_m,
        ),
        range_m=range_from_config(config),
        new_obstacle_margin_m=float(lidar["new_obstacle_margin_mm"]) / 1000.0,
        new_obstacle_check_radius_m=float(lidar["new_obstacle_check_radius_mm"]) / 1000.0,
        new_obstacle_confirmations=int(lidar["new_obstacle_confirmations"]),
        obstacle_mark_radius_m=float(lidar["obstacle_mark_radius_mm"]) / 1000.0,
        forward_fan_rad=deg_to_rad(float(lidar["forward_fan_deg"])),
        # 회피 시퀀스와 **같은 값을 쓴다** — 갇힌 상황을 몇 번까지
        # 스스로 풀어 보고 사람에게 넘길지의 값이다 (FR-2.3).
        max_reverify_attempts=int(config["fsm"]["avoid_attempts"]),
        live_map_write=bool(lidar.get("live_map_write", False)),
        map_hit_logodds=float(lidar["hit_logodds"]),
        map_miss_logodds=float(lidar["miss_logodds"]),
        map_pad_cells=int(lidar["expand_pad_cells"]),
        # 틀린 잠금 차단: 적중 비율이 `min_match_frac` 미만인 정합은 버리고,
        # 추적이 끊기면 `reloc_interval_ms` 주기로 지도 전역 재측위를 돌린다.
        min_match_frac=float(lidar.get("min_match_frac", 0.45)),
        reloc_interval_ms=int(lidar.get("reloc_interval_ms", 1000)),
        global_match_lin_step_m=float(lidar.get("global_match_step_mm", 100)) / 1000.0,
        global_match_ang_step_rad=deg_to_rad(float(lidar.get("global_match_angle_deg", 15))),
        global_full_scan_ambiguity=bool(lidar.get("global_full_scan_ambiguity", False)),
        reloc_max_peers=int(lidar.get("reloc_max_peers", 60)),
        verify_interval_ms=int(lidar.get("verify_interval_ms", 15000)),
        reloc_votes=int(lidar.get("reloc_votes", 3)),
        reloc_vote_yaw_rad=deg_to_rad(float(lidar.get("reloc_vote_yaw_deg", 10))),
        trust_expiry_ms=int(lidar.get("trust_expiry_ms", 5000)),
        global_result_max_age_ms=int(lidar.get("global_result_max_age_ms", 3000)),
        reloc_stationary_max_peers=int(lidar.get("reloc_stationary_max_peers", 10)),
        imu_match_params=(
            None
            if float(lidar.get("imu_yaw_window_deg", 0)) <= 0
            else MatchParams(
                search_lin_m=match_params.search_lin_m,
                search_lin_step_m=match_params.search_lin_step_m,
                search_ang_rad=deg_to_rad(float(lidar["imu_yaw_window_deg"])),
                search_ang_step_rad=deg_to_rad(float(lidar.get("imu_yaw_step_deg", 1))),
                occ_thresh=match_params.occ_thresh,
                min_known_cells=match_params.min_known_cells,
                sigma_m=match_params.sigma_m,
            )
        ),
        random_after_first_cycle=bool(config["zones"]["random_after_first_cycle"]),
        rng=rng,
    )


def describe(controller: PatrolController) -> str:
    """한 줄 상태 표기(콘솔용). 내부 단계와 내려보내는 `STATE` 를 나란히 찍는다."""
    x, y, yaw = controller.pose
    intent = controller.commander.intent
    fields = " ".join(f"{k}={v:g}" for k, v in intent.fields.items())
    return (
        f"{controller.phase.value:<9}-> STATE={controller.fsm_state:<12} "
        f"target={controller.target or '-':<3} "
        f"pose=({x:+.2f}, {y:+.2f}, {rad_to_deg(yaw):+6.1f}deg) "
        f"intent={intent.type_}{' ' + fields if fields else ''}"
    )
