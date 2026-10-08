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
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np

from host.behavior.avoidance import LocalAvoidance
from host.behavior.blockage import Recovery
from host.behavior.commander import Commander
from host.behavior.heading import HeadingTracker
from host.behavior.live_nav import LocalScan, NavParams
from host.behavior.localization import LocalizationTrust
from host.behavior.nav_map import NavigationMap
from host.behavior.patrol_phase import FSM_STATE_FOR, GOAL_LABEL, HOME_LABEL, Phase
from host.behavior.planner import (
    Plan,
    PlanParams,
    min_forward_distance,
    plan_to,
    segment_clear,
)
from host.behavior.recovery import BlockageRecovery
from host.behavior.relaxed_follow import RelaxedFollower
from host.behavior.route_follow import RouteFollower
from host.behavior.routes import Route
from host.behavior.scan_gate import ScanGate
from host.behavior.zone_map import ZoneMap
from host.behavior.zones import ZoneStore, select_next
from host.common.config import ConfigError
from host.common.lidar_link import Scan
from host.common.logging_setup import EdgeTrigger, event_logger
from host.common.protocol import clamp
from host.common.units import deg_to_rad, rad_to_deg, wrap_pi
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import (
    MatchParams,
    Pose,
)
from host.slam.settings import (
    match_params_from_config,
    plan_params_from_config,
    range_from_config,
)

LOG = event_logger("mechadog.behavior.patrol")


REPLAN_SETTLE_TIMEOUT_MS = 3000


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
    #: 이전 생성자 호환 필드 3개. AG에서는 오차/확인 횟수/별도 표시 반경을 쓰지 않는다.
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
    scan_gate: ScanGate = field(default_factory=ScanGate)
    #: 측위가 이만큼 끊기면 «검증됨»·사람 시드의 신뢰를 버린다 — 그 사이 로봇이 들려
    #: 옮겨졌을 수 있다. 0 이면 끈다 (리뷰 지적).
    trust_expiry_ms: int = 5000
    #: 전역 탐색 결과가 이보다 늦게 도착하면 버린다 — 그 사이 손으로 옮겨졌거나 돌았을 수
    #: 있는데 MOVE 수만으로는 모른다.
    global_result_max_age_ms: int = 3000
    #: 신뢰 복원 — 확인된 자세로 달리다 상실한 뒤, **이동 명령 없이(정지)·IMU 방위가 그대로**
    #: 인 동안 그 자세 반경·방위 창 안에서 다시 잡히면 신뢰를 되살린다. 집 지도는 스캔 하나로
    #: 구별 안 되는 자리가 많아(재생 거절 대부분이 정당) 전역 탐색만으로는 복구가 막힌다.
    #: 대신 «제자리에서 다시 맞는가» 만 묻는다 — 창 안 최고점이 전역 최고점의 비율 이상이어야
    #: 하고(다른 곳이 훨씬 잘 맞으면 거절) `reloc_votes` 번 연속 같은 답이어야 한다.
    reloc_restore_enabled: bool = False
    reloc_restore_max_age_ms: int = 30000
    reloc_restore_radius_m: float = 0.3
    reloc_restore_yaw_rad: float = math.radians(5.0)
    reloc_restore_score_ratio: float = 0.95
    #: 상실 동안 pitch·roll 이 기준보다 이만큼 넘게 바뀌면 «들어 올렸다» 로 보고 기준을 버린다.
    #: 같은 방위로 들어 옮기면 IMU yaw 로는 모르고, 대칭 구조에선 점수 비율도 통과한다
    #: (리뷰 지적) — 네 발 로봇을 들면 몸체가 기운다는 것에 기댄다.
    reloc_restore_tilt_rad: float = math.radians(8.0)
    #: 사람이 알려준 구역(대시보드 «위치 알려주기») 은 이 시간 안에 잡히지 않으면 버린다.
    zone_hint_ms: int = 60000
    #: 구역 영역 지도가 없을 때 구역 앵커 둘레 이 반경을 그 구역으로 본다.
    zone_hint_radius_m: float = 1.5
    #: 사람이 지도에서 알려준 점 둘레에서만 찾는 반경. 방위·표결 규칙은 그대로다.
    point_hint_radius_m: float = 0.6
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
    #: 이전 생성자 호환 필드. AG는 표시를 강제로 지우지 않고 재관측/TTL로만 갱신한다.
    max_reverify_attempts: int = 3
    random_after_first_cycle: bool = True
    rng: random.Random | None = None
    #: 런타임의 카메라가 도착을 소비할 때까지 정지한다. 독립 순찰 도구는 기다리지 않는다.
    wait_for_inspection: Callable[[str], bool] | None = None
    nav_params: NavParams = field(default_factory=NavParams)

    # ── 상태 ──────────────────────────────────────────────────
    pose: Pose = (0.0, 0.0, 0.0)
    phase: Phase = Phase.IDLE
    cycle: int = 0
    visited: frozenset[str] = frozenset()
    plan: Plan = field(default_factory=lambda: Plan(None))
    waypoint_index: int = 0
    safety: SafetyView = field(default_factory=SafetyView)
    stats: PatrolStats = field(default_factory=PatrolStats)
    _inspection_zone: str | None = None

    _last_scan_ms: int | None = None
    _local_scan_started_ms: int | None = None
    _stopped_since_ms: int | None = None
    _last_sent_moving: bool = False
    _replan_stop_required: bool = False
    _replan_wait_started_ms: int | None = None
    _replan_timeout_hold_ms: int | None = None
    #: 직전 스캔 때의 IMU yaw (rad). 변화량을 내려면 이전 값이 있어야 한다.
    _last_imu_yaw: float | None = None
    _reset_requested: bool = False
    #: 지금 처리 중인 스캔의 Host 시각 (`observe_scan` 이 찍는다).
    _scan_now_ms: int = 0
    #: 지도에서 찍은 목표 (순찰 좌표 m). 있으면 구역 순찰보다 먼저 그리로 간다.
    _goal: tuple[float, float] | None = None
    #: 찍은 곳에 도착(또는 못 가게 됨) — 사람이 순찰을 다시 시작할 때까지 그 자리에 선다.
    _goal_hold: bool = False
    #: 대기 이유 — «reached»(찍은 곳 도착) | «blocked»(가는 도중 길이 막힘) | None.
    _goal_hold_reason: str | None = None
    _now_ms: int = 0
    _halt_reason: str = ""
    #: 직전 추종 틱이 제자리 회전이었나 (`steering_for(spinning=)`).
    _spinning: bool = False
    _edge: EdgeTrigger = field(default_factory=EdgeTrigger)
    _local_scan: LocalScan = field(init=False)
    _local_scan_pose: Pose | None = None
    skipped: frozenset[str] = frozenset()
    _home: tuple[float, float] | None = None
    #: IMU 방위 앵커·조향 옵셋 (`host/behavior/heading.py`).
    heading: HeadingTracker = field(init=False, repr=False, compare=False)
    #: 측위 상태와 신뢰·전역 탐색 워커 (`host/behavior/localization.py`).
    localization: LocalizationTrust = field(init=False, repr=False, compare=False)
    #: 막힘 복구 시도·막힘 기억·항법 사건 (`host/behavior/recovery.py`).
    recovery: BlockageRecovery = field(init=False, repr=False, compare=False)
    #: 국소 판단 기록·진행 중인 회피·출발 탈출 (`host/behavior/avoidance.py`).
    avoidance: LocalAvoidance = field(init=False, repr=False, compare=False)
    #: 명시 동선과 그 진행 상태 (`host/behavior/route_follow.py`).
    route: RouteFollower = field(init=False, repr=False, compare=False)
    #: 느슨한 동선 추종 상태와 조향용 스캔 (`host/behavior/relaxed_follow.py`).
    relaxed: RelaxedFollower = field(init=False, repr=False, compare=False)
    #: 막힘 마스크·실시간 장애물·거리 비용 (`host/behavior/nav_map.py`).
    navmap: NavigationMap = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.heading = HeadingTracker(self)
        self.localization = LocalizationTrust(self)
        self.recovery = BlockageRecovery(self)
        self.avoidance = LocalAvoidance(self)
        self.route = RouteFollower(self, steering_for)
        self.relaxed = RelaxedFollower(self)
        self.navmap = NavigationMap(self)
        self._local_scan = LocalScan(self.nav_params)

    # ── 조회 ──────────────────────────────────────────────────
    @property
    def navigation_grid(self) -> OccupancyGrid:
        return self.navmap.live_clear.grid(self.grid, self._now_ms, self.plan_params.free_thresh)

    @property
    def local_status(self) -> dict[str, Any]:
        return {**self.avoidance.status, **self.scan_gate.status}

    @property
    def blockage_status(self) -> dict[str, Any]:
        return self.recovery.status

    def take_navigation_events(self) -> tuple[dict[str, Any], ...]:
        return self.recovery.take_events()

    @property
    def _recovery(self) -> Recovery | None:
        """런타임(`host/runtime.py`)이 읽는 옛 이름 — 시도는 `recovery.active` 가 쥔다."""
        return self.recovery.active

    def expire_recovery(self, now_ms: int) -> bool:
        """복구 대기의 시간 한도 (`BlockageRecovery.expire`) — 안전·FSM 관문 뒤에서도 부른다."""
        return self.recovery.expire(now_ms)

    @property
    def fsm_state(self) -> str:
        return FSM_STATE_FOR[self.phase]

    @property
    def blocked(self) -> np.ndarray:
        """정적 팽창 + 이번 순찰에서 발견한 동적 장애물."""
        assert self.navmap.static is not None and self.navmap.dynamic is not None
        return cast(np.ndarray, self.navmap.static | self.navmap.dynamic)

    @property
    def body_blocked(self) -> np.ndarray:
        """몸체 최소 여유 + 실시간 장애물. 동적 물체는 탈출 예외로 지우지 않는다."""
        assert self.navmap.body is not None and self.navmap.dynamic is not None
        return cast(np.ndarray, self.navmap.body | self.navmap.dynamic)

    @property
    def target(self) -> str | None:
        return self.plan.label

    @property
    def obstacles(self) -> tuple[tuple[float, float], ...]:
        return tuple(self.navmap.obstacles)

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def pose_ms(self) -> int | None:
        """`pose` 를 마지막으로 갱신한 시각. 정합에 실패한 스캔은 바꾸지 않는다."""
        return self.localization.last_pose_ms

    @property
    def match_frac(self) -> float:
        """마지막 정합 시도의 적중 비율 (0~1) — 진단·포즈 불신 표시에 쓴다."""
        return self.localization.match_frac

    @property
    def pose_verified(self) -> bool:
        """내장 전역 확인 또는 외부 측위 계층이 보증한 자세인가. 신선도는 별도 검사한다."""
        return self.localization.verified or (
            not self.localization.own_localization and self.localization.last_pose_ms is not None
        )

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
        return self.localization.last_pose_ms is None or (
            now_ms - self.localization.last_pose_ms > self.drive.pose_timeout_ms
        )

    @property
    def obstacle_pending(self) -> bool:
        """새 장애물을 확인 중인가 — 후보는 잡혔지만 연속 확정 횟수에 못 미쳤다."""
        return self.navmap.pending_hit is not None

    def take_new_obstacles(self) -> tuple[tuple[float, float], ...]:
        """지난 호출 뒤 확정된 신규 장애물 `(x m, y m)` 을 꺼낸다 — 한 번 꺼내면 비워진다.

        ⚠️ **런타임을 받지 않고 꺼내 가게 한다.** 컨트롤러가 기록·방송 경로를 알면
        소켓 없이 닫히는 시험(이 클래스의 계약)이 깨진다.
        """
        taken = tuple(self.navmap.new_obstacles)
        self.navmap.new_obstacles.clear()
        return taken

    def save_map(self, directory: Path | None = None) -> dict[str, Path]:
        """걸으며 자란 지도를 디스크에 쓴다 — 실측 스캔이 지도를 개선하게 하는 통로.

        **전역 확인된 자세로 적분한 세션만 쓴다** (`pose_verified`). 확인이 한 번도 없었으면
        이 세션의 적분은 전부 보류돼 있어 쓸 것도 없다. 처음 덮어쓰기 전에 원본을
        `slam_map.orig.*` 로 옆에 둔다. `maps_dir` 도 인자도 없으면 아무것도 안 한다.
        """
        target = directory or self.maps_dir
        if target is None or not self.live_map_write or not self.localization.verified:
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
        self.scan_gate.observe_imu(reading, now_ms)
        self.safety = SafetyView(
            latched=getattr(reading, "safety_latched", None),
            onboard_state=getattr(reading, "state", "") or "",
            obstacle=getattr(reading, "obstacle", None),
            dist_cm=getattr(reading, "dist_cm", None),
            yaw_deg=self._yaw_of(reading),
            last_cmd_age_ms=getattr(reading, "last_cmd_age_ms", None),
            last_seen_ms=now_ms,
        )
        pitch, roll = getattr(reading, "pitch", None), getattr(reading, "roll", None)
        if isinstance(pitch, int | float) and isinstance(roll, int | float):
            self.localization.observe_tilt(float(pitch), float(roll))
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
        self.localization.observe_pose(pose, now_ms)
        # 재개는 step의 전체 관문에서 한다. 새 tf 하나가 스캔 두절이나
        # 미관측 셀 정지를 해제해서는 안 된다.

    def note_sent(self, lines: Iterable[str], sent_ms: int) -> None:
        """성공적으로 보낸 명령의 시각을 받는다. 의도를 세운 시각과 구분한다.

        송신 성공은 기기의 물리 정지를 증명하지 않는다. 기존 안정 대기와
        온보드 정지 보고를 함께 쓰며, 실제 전달 지연은 실기 검증 대상이다.
        """
        for line in lines:
            message = json.loads(line)
            if message["type"] == "POSE":
                self.scan_gate.note_pose(message, sent_ms)
                # 명령 이전 전역 결과와 표를 복귀 후 신뢰 근거로 승격하지 않는다.
                self.localization.new_epoch()
                self._local_scan.clear_allowed = False
                self.scan_gate.status = {"scan_rejected": "pose_settling", "clear_allowed": False}
            if message["type"] == "MOVE" and not (message.get("step") or message.get("angle")):
                # MOVE {0,0} 은 «서 있으라» 다 — 걷는 중으로 치면 정지 감사가 막힌다 (리뷰 지적).
                self._note_stopped(sent_ms)
            elif message["type"] == "MOVE":
                self.scan_gate.note_move(sent_ms)
                self._last_sent_moving = True
                self._stopped_since_ms = None
                self.localization.note_move()
            elif message["type"] in ("STOP", "ESTOP", "RESET_SAFE"):
                self._note_stopped(sent_ms)

    def _note_stopped(self, now_ms: int) -> None:
        self._last_sent_moving = False
        if self._stopped_since_ms is None:
            self._stopped_since_ms = now_ms

    def _note_scan(self, scan: Scan, now_ms: int) -> bool:
        self._scan_now_ms = now_ms
        self._now_ms = now_ms
        if not self._local_scan.observe(scan, now_ms, self.range_m):
            return False
        self._local_scan_started_ms = (
            scan.started_ms if scan.started_ms is not None else self._local_scan.received_ms
        )
        rejected = self.scan_gate.check(scan, now_ms)
        self._local_scan.clear_allowed = rejected is None
        if rejected is None:
            self.relaxed.scan.observe(scan, now_ms, self.range_m)
            self.relaxed.scan_yaw = self.heading.steering_yaw()
        self._local_scan_pose = (*self.pose[:2], self.heading.steering_yaw())
        # 빈 전문/범위 밖 점만 있는 전문은 관측을 복구하지 않는다.
        if rejected is None and any(
            math.isfinite(angle)
            and math.isfinite(distance)
            and self.range_m[0] <= distance <= self.range_m[1]
            for angle, distance in scan.points
        ):
            self._last_scan_ms = now_ms
        return True

    def observe_obstacle_scan(self, scan: Scan, now_ms: int) -> None:
        """외부 측위 모드에서 스캔을 신규 장애물 확인에만 쓴다.

        오래된 자세에 빔을 투영하면 정상 벽을 새 장애물로 찍으므로 유효한 최근
        자세가 있을 때만 지도에 반영한다. 즉시 위험 판정은 ``guard_scan`` 이 별도다.
        """
        self.stats.scans += 1
        if not self._note_scan(scan, now_ms):
            return
        if not self._local_scan.clear_allowed or self.pose_stale(now_ms):
            return
        self.navmap.project_scan(scan)

    def observe_scan(self, scan: Scan, now_ms: int) -> None:
        """내장 스캔 정합(시뮬레이션용)으로 측위하고 신규 장애물을 확인한다."""
        self.stats.scans += 1
        if not self._note_scan(scan, now_ms):
            return
        self.localization.track_scan(scan, now_ms)

    @property
    def goal(self) -> tuple[float, float] | None:
        return self._goal

    @property
    def holding_goal(self) -> bool:
        return self._goal_hold

    @property
    def goal_hold_reason(self) -> str | None:
        return self._goal_hold_reason

    def goto(
        self, x: float, y: float, *, keep_route: bool = False, exact_goal: bool = False
    ) -> tuple[bool, str]:
        """지도에서 찍은 곳으로 간다. **루프 스레드에서만** 부른다.

        자기 위치를 보증받지 못했으면(전역 확인도 사람 시드도 없음) 거절한다 — 모르는 자리에서
        찍은 곳으로 가는 경로는 근거가 없다. 지금 자리에서 경로가 없으면 그 이유로 거절한다.
        도착하면 그 자리에 서서 기다리고, 순찰을 다시 시작하면 구역 순찰로 돌아간다.
        """
        if not (math.isfinite(x) and math.isfinite(y)):
            return False, "좌표가 숫자가 아니다"
        if self.localization.untrusted:
            return False, "로봇이 아직 자기 위치를 확인하지 못했다 — 위치를 먼저 잡아야 한다"
        trial = plan_to(
            GOAL_LABEL,
            (float(x), float(y)),
            (self.pose[0], self.pose[1]),
            self.navigation_grid,
            self.blocked,
            self.plan_params,
            snap_m=0.0 if exact_goal or keep_route else 0.6,
            body_blocked=self.body_blocked,
        )
        can_escape = (
            trial.fail_reason
            in {
                "start_occupied",
                "start_unobserved",
                "start_clearance_blocked",
                "no_path",
                "goal_unreachable",
            }
            and self.grid.inside(*self.grid.to_cell(x, y))
            and not self.pose_stale(self._now_ms)
            and self._local_scan.fresh(self._now_ms)
            and self.avoidance.corridor_to((x, y)) is not None
        )
        if not trial.reachable and not can_escape:
            return False, f"지금 자리에서 그곳으로 가는 길이 없다 ({trial.fail_reason})"
        if not keep_route:
            self.route.cancel("replaced", hold=False)
        self._inspection_zone = None
        self.recovery.active = None
        self.avoidance.active = None
        self._replan_stop_required = False
        self._replan_wait_started_ms = None
        self._goal = (float(x), float(y))
        self._goal_hold = False
        self._goal_hold_reason = None
        self.plan = Plan(GOAL_LABEL)
        self.waypoint_index = 0
        self.phase = Phase.PLANNING
        moved = trial.goal_moved_m
        LOG.info(
            "goal_set",
            x=round(x, 2),
            y=round(y, 2),
            path_m=round(trial.length_m, 2),
            goal_moved_m=round(moved, 2),
        )
        note = f" (막힌 자리라 {moved:.2f} m 옆 빈 곳으로)" if moved > 0.01 else ""
        return True, f"찍은 곳으로 간다 — 경로 {trial.length_m:.1f} m{note}"

    def cancel_goal(self, reason: str) -> None:
        """찍은 목표를 버리고 구역 순찰로 돌아갈 수 있게 한다 (순찰 정지·재시작)."""
        self.route.cancel("stopped", hold=False)
        self.avoidance.active = None
        self.recovery.active = None
        self._replan_stop_required = False
        self._replan_wait_started_ms = None
        if self._goal is None and not self._goal_hold:
            return
        LOG.info("goal_cleared", reason=reason)
        self._goal = None
        self._goal_hold = False
        self._goal_hold_reason = None
        if self.plan.label == GOAL_LABEL:
            self.plan = Plan(None)
        self.phase = Phase.PLANNING

    # ── 동선 (`RouteFollower` 에 위임 — 런타임·시험이 쓰는 이름) ──────────
    @property
    def route_active(self) -> bool:
        return self.route.active

    @property
    def route_visit(self) -> tuple[int, int, int] | None:
        return self.route.visit

    @property
    def route_controls_inspection(self) -> bool:
        return self.route.controls_inspection

    def route_status(self) -> dict[str, Any] | None:
        return self.route.summary()

    def start_route(self, route: Route, now_ms: int) -> tuple[bool, str]:
        return self.route.start(route, now_ms)

    def cancel_route(self, reason: str = "stopped", *, hold: bool = True) -> None:
        self.route.cancel(reason, hold=hold)

    def _target_xy(self, label: str) -> tuple[float, float]:
        if label == HOME_LABEL:
            assert self._home is not None
            return self._home
        if label == GOAL_LABEL:
            assert self._goal is not None
            return self._goal
        return self.zones.xy(label)

    def locate_zone_ids(self) -> tuple[str, ...]:
        """사람이 «여기» 라고 알려줄 수 있는 구역 — 좌표가 붙은 순찰 구역."""
        return tuple(self.zones.labels)

    def hint_zone(self, zone: str, now_ms: int) -> bool:
        """사람이 «로봇은 지금 이 구역 안» 이라고 알려줬다 (`LocalizationTrust.hint_zone`)."""
        return self.localization.hint_zone(zone, now_ms)

    def validate_hint_point(self, x: float, y: float) -> tuple[bool, str]:
        """알려준 위치가 지도 안의 비점유 셀인가 (`LocalizationTrust.validate_hint_point`)."""
        return self.localization.validate_hint_point(x, y)

    def hint_point(self, x: float, y: float, now_ms: int) -> bool:
        """지도에서 알려준 점 주변만 전역 탐색한다 (`LocalizationTrust.hint_point`)."""
        return self.localization.hint_point(x, y, now_ms)

    # 런타임(`host/runtime.py`)이 읽는 옛 이름 — 상태는 `localization` 이 쥔다.
    @property
    def _own_localization(self) -> bool:
        return self.localization.own_localization

    @property
    def _zone_hint(self) -> tuple[str, int] | None:
        return self.localization.zone_hint

    @property
    def _point_hint(self) -> tuple[float, float, int] | None:
        return self.localization.point_hint

    def _zone_filter(self, now_ms: int) -> Callable[[np.ndarray, np.ndarray], np.ndarray] | None:
        return self.localization.zone_filter(now_ms)

    # ── 항법 지도 (`NavigationMap` 에 위임 — 런타임·대시보드가 쓰는 이름) ──────────
    @property
    def navigation_costs(self) -> np.ndarray:
        return self.navmap.costs

    @property
    def local_costmap(self) -> dict[str, Any]:
        return self.navmap.local_window

    def _is_stationary(self) -> bool:
        """로봇이 서 있는가 — 감사 탐색(수 초)은 서 있을 때만 돌려도 안전하다."""
        return (
            self.phase not in (Phase.MOVING, Phase.AIMING) and not self._spinning
        ) or self._stopped_since_ms is not None

    # ── 조작자 ────────────────────────────────────────────────
    def start(self) -> None:
        if self.phase is Phase.IDLE:
            self._home = self.pose[:2]
            self.phase = Phase.PLANNING
            LOG.info("patrol_started", zones=list(self.zones.labels))

    def emergency_stop(self, reason: str) -> str:
        """`ESTOP` 전문을 돌려준다. 호출자가 즉시 보낸다."""
        self.phase = Phase.HALTED
        self._halt_reason = reason
        self.avoidance.active = None
        self.recovery.active = None
        self.route.direct_stopped_ms = None
        self.relaxed.escape_turn = False
        self.avoidance.decide("stop", "estop", self._local_scan.distance())
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
        self._now_ms = now_ms
        if not self.relaxed.active:
            self.recovery.expire(now_ms)
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
            self.route.direct_stopped_ms = None
        elif self.phase is Phase.HALTED or self.phase is Phase.IDLE or self.phase is Phase.LOST:
            self.commander.halt()
            self.route.direct_stopped_ms = None
        elif self.safety.obstacle_active:
            # 온보드가 이미 멈췄다 — 판정을 흉내내지 않고 의도만 정지로 내린다 (아키텍처 1.2).
            self.commander.halt()
            self.avoidance.decide(
                "stop",
                "onboard_ultrasonic",
                self.safety.dist_cm / 100 if self.safety.dist_cm is not None else None,
            )
            self.route.reset_dwell()
            self.route.direct_obstacle_stop()
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
        self._now_ms = now_ms
        if not self.relaxed.active:
            self.recovery.expire(now_ms)
        if self.phase in (Phase.IDLE, Phase.HALTED):
            self.commander.halt()
            self.route.direct_stopped_ms = None
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
            self.avoidance.decide(
                "stop",
                "onboard_ultrasonic",
                self.safety.dist_cm / 100 if self.safety.dist_cm is not None else None,
            )
            self.route.reset_dwell()
            self.route.direct_obstacle_stop()
            if self._edge.changed("obstacle", True):
                LOG.info("onboard_obstacle_hold", dist_cm=self.safety.dist_cm)
            return
        self._edge.forget("obstacle")
        self._advance()

    def resume(self, *, from_inspection: bool = False) -> None:
        """경로만 버리고 목표 구역은 둔 채 다시 계획하게 한다.

        추적·경보·구역 점검을 마치고 순찰로 돌아오면 로봇은 떠날 때와 다른 자리에
        있다 — 옛 경로의 웨이포인트를 따라가면 엉뚱한 곳으로 되돌아간다. 목표는
        그대로라 **아직 가지 않은 구역으로 지금 자리에서** 다시 푼다 (`_replan`).
        """
        if self.route.active and self.route.search_base is not None and not from_inspection:
            self.route.search_base = None
            self.route.next_point()
            return
        self.plan = Plan(self.plan.label)
        self.route.direct_stopped_ms = None
        if not from_inspection:
            self.route.reset_dwell()
        if not self.route.active:
            self._inspection_zone = None
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

    def _require_replan_stop(self) -> None:
        if not self._replan_stop_required or self._replan_wait_started_ms is None:
            self._replan_wait_started_ms = self._now_ms
        self._replan_stop_required = True
        self.commander.halt()

    def _guard_localization(self, now_ms: int) -> tuple[str, ...]:
        if self.relaxed.active:
            self.avoidance.active = self.recovery.active = None
            if not self.relaxed.pose_available(now_ms):
                self.relaxed.escape_turn = False
                self._lose("relaxed_pose_stale")
            elif self.phase is Phase.LOST:
                self.phase = Phase.PLANNING
            return ()
        if not self._local_scan.clear_allowed:
            self._lose("scan_rejected")
            return ()
        # ③ 측위 상실 — `ESTOP` 이 아니라 `LOST` 다 (FR-6.6)
        stale = self.pose_stale(now_ms)
        if stale:
            self._lose("pose_stale")
            return ()
        # 마스크 갱신도 정지를 요구할 수 있으므로 출발 관문보다 먼저 적용한다.
        self.navmap.refresh(now_ms)
        if self._replan_stop_required and self._replan_wait_started_ms is None:
            self._replan_wait_started_ms = now_ms
        if self._replan_stop_required and (
            self._last_sent_moving or self._stopped_since_ms is None
        ):
            # halt 의도가 다음 steer()에서 MOVE로 덮이면 실제 STOP이 송신되지 않는다.
            # 송신 기록이 STOP을 확인할 때까지 정지하며, 아래에서 새 스캔도 요구한다.
            self._lose("replan_waiting_for_sent_stop")
            return ()
        timed_out = (
            self._replan_stop_required
            and self._replan_wait_started_ms is not None
            and now_ms - self._replan_wait_started_ms >= REPLAN_SETTLE_TIMEOUT_MS
            and self._local_scan.fresh(now_ms)
            and self._local_scan.complete
            and self._local_scan.received_ms is not None
            and self._local_scan.received_ms >= self._replan_wait_started_ms
        )
        if (
            self._replan_stop_required
            and not timed_out
            and (
                self._local_scan.received_ms is None
                or self._stopped_since_ms is None
                or self._local_scan_started_ms is None
                or self._local_scan_started_ms < self._stopped_since_ms + self.drive.settle_delay_ms
                or not self._local_scan.complete
            )
        ):
            self._lose("replan_waiting_for_settled_scan")
            return ()

        if not self._local_scan.fresh(now_ms):
            self.avoidance.active = None
            self.avoidance.decide("stop", "scan_unavailable")
            self._lose("live_scan_unavailable")
            return ()
        row, col = self.grid.to_cell(*self.pose[:2])
        if not self.grid.inside(row, col):
            self._lose("pose_outside_map")
            return ()
        # Use the same boundary-cell test as plan_to/escape_start. A single
        # to_cell lookup misses an adjacent blocked cell on an exact grid line.
        self.avoidance.needs_escape = not segment_clear(
            self.navigation_grid, self.body_blocked, self.pose[:2], self.pose[:2]
        )
        if self.phase is Phase.LOST:
            LOG.info("localization_reacquired")
            self.phase = Phase.PLANNING
        self._replan_stop_required = False
        self._replan_wait_started_ms = None
        if timed_out:
            # 상한은 안정 대기만 해제한다. 이 틱은 STOP으로 끝내고 다음 틱에
            # 온보드/측위/스캔 관문을 다시 통과한 뒤 추종한다.
            self._replan_timeout_hold_ms = now_ms
            self.commander.halt()
            self.avoidance.decide("stop", "replan_settle_timeout", self._local_scan.distance())
            LOG.warning("replan_settle_timeout", limit_ms=REPLAN_SETTLE_TIMEOUT_MS)
        self._edge.forget("localization_problem")
        return ()

    def _lose(self, reason: str) -> None:
        self.avoidance.active = None
        self.route.direct_stopped_ms = None
        if reason != "live_scan_unavailable":
            self.avoidance.decide("stop", reason, self._local_scan.distance())
        self.route.reset_dwell()
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
        if self.relaxed.active:
            self.relaxed.follow()
            return
        if (
            self._replan_stop_required
            or self.phase is Phase.LOST
            or self._replan_timeout_hold_ms == self._now_ms
        ):
            self.commander.halt()
            return
        if self._home is None:
            self._home = self.pose[:2]
        if not len(self.zones) and not self.route.active:
            self.commander.halt()
            if self._edge.changed("no_zones", True):
                LOG.error("no_zones", hint="tools/ops/zone_select.py 를 먼저 실행한다")
            return
        if self.localization.untrusted:
            # 사람도 지도도 이 자세를 보증한 적이 없다 — 걷지 않는다. 서 있으면 전역 감사가
            # 돌아 확인되거나(재개) 다른 자리로 바로잡는다.
            self.commander.halt()
            self.avoidance.decide("stop", "pose_unverified", self._local_scan.distance())
            self.route.direct_stopped_ms = None
            self.route.reset_dwell()
            if self._edge.changed("pose_unverified_hold", True):
                LOG.warning("pose_unverified_hold", frac=round(self.localization.match_frac, 3))
            return
        self._edge.changed("pose_unverified_hold", False)
        if self._goal_hold:
            # 찍은 곳에 섰다 — 사람이 순찰을 다시 시작할 때까지 기다린다.
            self.commander.halt()
            return

        if self.recovery.waiting and self._end_recovery_wait():
            return

        if self.recovery.active is not None:
            self.recovery.step()
            return

        if self.avoidance.active is not None:
            self.avoidance.step()
            return
        if self.route.direct_moving:
            start = self.route.direct_detour_start
            if start is not None and math.dist(start, self.pose[:2]) >= self.nav_params.avoidance_m:
                # A* 우회도 설정된 회피 거리 이동 뒤 목표 방향 원판이 비었으면 직접 추종 복귀.
                scan_pose = self._local_scan_pose or self.pose
                dx, dy = self.pose[0] - scan_pose[0], self.pose[1] - scan_pose[1]
                c, s = math.cos(scan_pose[2]), math.sin(scan_pose[2])
                point = self.route.point()
                heading = math.atan2(point.y - self.pose[1], point.x - self.pose[0])
                if self._local_scan.corridor_clear(
                    self.plan_params.body_radius_m,
                    wrap_pi(heading - scan_pose[2]),
                    self.nav_params.avoidance_m,
                    origin=(dx * c + dy * s, -dx * s + dy * c),
                ):
                    self.route.direct_detour_start = None
                    self.route.direct_stopped_ms = None
                    self._spinning = False
            if self.route.direct_detour_start is None:
                self.route.follow_direct()
                return
        if self.avoidance.needs_escape:
            if not self.avoidance.start("start_escape"):
                self.recovery.begin("start_clearance_blocked")
            return
        if self.route.active and self.route.stage != "moving":
            self.route.advance_arrival()
            return

        if (
            self._goal is None
            and self._inspection_zone is not None
            and self.wait_for_inspection is not None
            and self.wait_for_inspection(self._inspection_zone)
        ):
            # STOP 안정화/장애물 재계획이 끝나도 아직 카메라에 넘기지 않은 방문은 보존한다.
            # step/steer의 안전 관문을 통과한 뒤에만 다시 방향을 맞추거나 점검을 기다린다.
            self.phase = Phase.INSPECT
            self._steer_zone_aim(self._inspection_zone)
            return

        if self.phase is Phase.INSPECT:
            # 도착하면 즉시 다음 구역으로 — 카메라 판독(FR-8)은 `change_detect` 소관이다.
            self.phase = Phase.PLANNING

        if self.phase is Phase.AIMING:
            self._aim_at_zone()
            return

        if self.phase is Phase.PLANNING or not self.plan.reachable:
            self._replan()
            if not self.plan.reachable:
                if self._goal_hold:
                    self.commander.halt()
                elif self.recovery.active is None and not self.recovery.waiting:
                    self.recovery.begin("global_path_blocked")
                return
            self.phase = Phase.MOVING

        self._follow()

    def _replan(self) -> None:
        self._inspection_zone = None
        # 새 경로는 새 방위 오차에서 시작한다 — 지난 경로의 회전을 이어 가지 않는다.
        self._spinning = False
        start = (self.pose[0], self.pose[1])
        if self.recovery.returning_home:
            assert self._home is not None
            self.plan = plan_to(
                HOME_LABEL,
                self._home,
                start,
                self.navigation_grid,
                self.blocked,
                self.plan_params,
                snap_m=0.0,
                body_blocked=self.body_blocked,
                costs=self.navmap.costs,
            )
            self.waypoint_index = 0
            if not self.plan.reachable:
                self.recovery.wait_patrol()
            return
        if self._goal is not None:
            self._replan_goal(start, self._goal)
            return
        candidates = {label: self.zones.xy(label) for label in self.zones.labels}

        # 목표가 이미 정해져 있으면 경로만 다시 푼다 (재계획).
        if self.plan.label is not None and self.plan.label not in self.visited | self.skipped:
            retry = plan_to(
                self.plan.label,
                candidates[self.plan.label],
                start,
                self.navigation_grid,
                self.blocked,
                self.plan_params,
                body_blocked=self.body_blocked,
                costs=self.navmap.costs,
            )
            if retry.reachable:
                self.plan = retry
                self.waypoint_index = 0
                return
            # 정지·재확인 뒤에만 이번 바퀴 제외 여부를 결정한다.
            self.plan = Plan(self.plan.label)
            self.recovery.begin("global_path_blocked")
            return

        self._select_zone(start, candidates)

    def _end_recovery_wait(self) -> bool:
        """복구 대기가 아직이면 True. 끝났으면 사이클을 닫고 계획 단계로 돌린다."""
        self.commander.halt()
        removed = bool(
            self.recovery.wait_memory_ids
        ) and not self.recovery.wait_memory_ids.intersection(
            b.id for b in self.recovery.memory.items
        )
        if self._now_ms < self.recovery.retry_after_ms and not removed:
            return True
        self.recovery.waiting = False
        self.recovery.wait_reported = False
        self._complete_cycle()
        if self.route.active:
            self.route.cycle += 1
            self.route.index = 0
            self.route.skipped_indices = frozenset()
            point = self.route.point()
            self._goal = (point.x, point.y)
        self.phase = Phase.PLANNING
        return False

    def _replan_goal(self, start: tuple[float, float], goal: tuple[float, float]) -> None:
        """찍은 곳(또는 동선 지점)까지 경로를 푼다."""
        if not self.grid.inside(*self.grid.to_cell(*goal)):
            self._goal = None
            self._goal_hold = True
            self._goal_hold_reason = "blocked"
            self.plan = Plan(None)
            self.commander.halt()
            return
        plan = plan_to(
            GOAL_LABEL,
            goal,
            start,
            self.navigation_grid,
            self.blocked,
            self.plan_params,
            snap_m=0.0 if self.route.active else 0.6,
            body_blocked=self.body_blocked,
            costs=self.navmap.costs,
        )
        self.waypoint_index = 0
        if plan.reachable:
            self.plan = plan
            return
        LOG.warning("goal_unreachable", reason=plan.fail_reason)
        self.plan = Plan(GOAL_LABEL)
        self.recovery.begin("global_path_blocked")

    def _select_zone(
        self, start: tuple[float, float], candidates: dict[str, tuple[float, float]]
    ) -> None:
        """다음 구역을 고르고, 고를 수 없으면 복구·귀환·사이클 마감으로 넘긴다."""
        skipped: list[tuple[str, str]] = []
        self.plan = select_next(
            cycle=self.cycle,
            visited=self.visited | self.skipped,
            start=start,
            candidates=candidates,
            order=self.zones.labels,
            grid=self.navigation_grid,
            blocked=self.blocked,
            params=self.plan_params,
            random_after_first_cycle=self.random_after_first_cycle,
            rng=self.rng,
            skipped_out=skipped,
            body_blocked=self.body_blocked,
            costs=self.navmap.costs,
        )
        self.waypoint_index = 0
        # 도달 불가 구역은 같은 목록이 바뀔 때만 기록한다 — 매 틱 재시도하면
        # 로그가 초당 10줄씩 쌓여 진짜 신호를 덮는다 (목업에서 실제로 확인).
        skip_key = ",".join(f"{label}:{reason}" for label, reason in skipped)
        if skipped:
            self.plan = Plan(skipped[0][0])
            self.recovery.begin("global_path_blocked")
            return
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
        elif len(self.visited | self.skipped) >= len(self.zones):
            if not self.visited:
                self.recovery.return_home()
            else:
                self._complete_cycle()
        elif self.visited or self.skipped:
            self.plan = Plan(
                next(
                    label for label in self.zones.labels if label not in self.visited | self.skipped
                )
            )
            self.recovery.begin("global_path_blocked")
        elif self._edge.changed("no_reachable_zone", True):
            LOG.error("no_reachable_zone", visited=sorted(self.visited))
            self.plan = Plan(
                next(z for z in self.zones.labels if z not in self.visited | self.skipped)
            )
            self.recovery.begin("global_path_blocked")

    def _follow(self) -> None:
        assert self.plan.label is not None
        anchor = self._target_xy(self.plan.label)
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
            if self.plan.label == HOME_LABEL:
                self.recovery.wait_patrol()
            else:
                self._arrive(self.plan.label)
            return

        waypoint = self._current_waypoint()
        if not self.navmap.path_clear(waypoint):
            self.recovery.begin("path_obstacle")
            return
        heading = math.atan2(waypoint[1] - self.pose[1], waypoint[0] - self.pose[0])
        steering = steering_for(
            heading - self.heading.steering_yaw(), self.drive, spinning=self._spinning
        )
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
        distance = self._local_scan.distance()
        desired_distance = self._local_scan.distance(wrap_pi(heading - self.heading.steering_yaw()))
        if desired_distance is not None:
            distance = min(distance, desired_distance) if distance is not None else desired_distance
        if distance is None or distance < self.nav_params.local_stop_m:
            self.commander.halt()
            self.avoidance.decide("stop", "local_stop_distance", distance)
            self.recovery.begin("path_obstacle")
            return
        scale = min(
            1.0,
            (distance - self.nav_params.local_stop_m)
            / (self.nav_params.local_slow_m - self.nav_params.local_stop_m),
        )
        steering = Steering(steering.step_mm * max(0.0, scale), steering.angle_deg)
        self.avoidance.decide(
            "slow" if scale < 1 else "clear",
            "local_slow_distance" if scale < 1 else "path_clear",
            distance,
        )
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

    def _arrive(self, label: str) -> None:
        self.commander.halt()
        self._spinning = False
        if label == GOAL_LABEL:
            if self.route.active:
                self.route.direct_stopped_ms = None
                self.route.direct_detour_start = None
                self.route.stage = "aiming"
                self.phase = Phase.AIMING
                return
            # 찍은 곳 — 구역 방문·점검이 아니다. 서서 기다린다.
            LOG.info("goal_reached", x=round(self.pose[0], 2), y=round(self.pose[1], 2))
            self._goal = None
            self._goal_hold = True
            self._goal_hold_reason = "reached"
            self.plan = Plan(None)
            self.phase = Phase.PLANNING
            return
        if self.zones.get(label).aim_deg is not None:
            self.phase = Phase.AIMING
            LOG.info("zone_aim_started", label=label, aim_deg=self.zones.get(label).aim_deg)
            return
        self._finish_arrival(label)

    def _aim_at_zone(self) -> None:
        """점검 전에만 돈다. step/steer의 기존 측위·장애물 관문을 통과한 뒤 호출된다."""
        label = self.plan.label
        assert label is not None
        anchor = self.zones.xy(label)
        effective = self.plan.effective or anchor
        if min(math.dist(self.pose[:2], anchor), math.dist(self.pose[:2], effective)) >= (
            self.drive.arrival_radius_m
        ):
            # 측위가 도착 반경 밖으로 바뀌면 점검하지 않고 다시 접근한다.
            self.commander.halt()
            self._spinning = False
            self.phase = Phase.PLANNING
            self.plan = Plan(label)
            return
        if self._steer_zone_aim(label):
            LOG.info("zone_aim_completed", label=label)
            self._finish_arrival(label)

    def _zone_aim_error(self, label: str) -> float:
        aim_deg = self.zones.get(label).aim_deg
        return (
            0.0 if aim_deg is None else wrap_pi(deg_to_rad(aim_deg) - self.heading.steering_yaw())
        )

    def _steer_zone_aim(self, label: str) -> bool:
        error = self._zone_aim_error(label)
        if abs(error) <= self.drive.heading_tolerance_rad:
            self.commander.halt()
            self._spinning = False
            return True
        steering = steering_for(error, self.drive, spinning=True)
        self._spinning = True
        turn_limit = abs(self.drive.spin_turn_deg)
        self.commander.drive(0.0, clamp(steering.angle_deg, -turn_limit, turn_limit))
        return False

    def inspection_ready(self, label: str, now_ms: int) -> bool:
        """카메라 점검은 도착·조준 완료 뒤에만 시작한다 (느린 비전 프레임도 기다린다)."""
        if self.route.active:
            point = self.route.point()
            return (
                self.phase is Phase.INSPECT
                and self.route.search_base is None
                and self._inspection_zone == label
                and (
                    self.relaxed.pose_available(now_ms)
                    if self.relaxed.active
                    else not self.pose_stale(now_ms)
                )
                and not self.safety.obstacle_active
                and (
                    self.relaxed.active
                    or math.dist(self.pose[:2], (point.x, point.y)) < self.drive.arrival_radius_m
                )
                and abs(self.route.aim_error()) <= self.drive.heading_tolerance_rad
            )
        return (
            self.phase is Phase.INSPECT
            and self._inspection_zone == label
            and not self.pose_stale(now_ms)
            and not self.safety.obstacle_active
            and abs(self._zone_aim_error(label)) <= self.drive.heading_tolerance_rad
        )

    def _finish_arrival(self, label: str) -> None:
        self.phase = Phase.INSPECT
        self._inspection_zone = label
        self.visited = self.visited | {label}
        self.stats.zones_visited += 1
        self.plan = Plan(None)
        LOG.info("zone_arrived", label=label, cycle=self.cycle)
        if len(self.visited) >= len(self.zones):
            self._complete_cycle()

    def _complete_cycle(self) -> None:
        self.cycle += 1
        self.visited = frozenset()
        self.skipped = frozenset()
        self.stats.cycles += 1
        # 장애물은 재관측 시각 기준으로 만료한다. 사이클 경계가 실제 점을 지우지 않는다.
        LOG.info("cycle_completed", cycle=self.cycle)


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
        nav_params=NavParams.of(config),
        grid=grid,
        zones=zones,
        drive=drive_params_from_config(config),
        plan_params=plan_params_from_config(config),
        match_params=match_params,
        scan_gate=ScanGate(
            max_imu_age_ms=int(lidar.get("scan_tilt_imu_max_age_ms", 250)),
            max_tilt_deg=float(lidar.get("scan_tilt_max_deg", 6)),
            walking_tilt_deg=float(lidar.get("scan_tilt_walking_max_deg", 12)),
            settle_ms=int(lidar.get("scan_tilt_settle_ms", 750)),
            imu_pitch_offset_deg=float(lidar.get("scan_tilt_pitch_offset_deg", 0)),
            imu_roll_offset_deg=float(lidar.get("scan_tilt_roll_offset_deg", 0)),
            pose_roll_offset_deg=float(config.get("posture", {}).get("roll_offset_deg", 0)),
        ),
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
        new_obstacle_margin_m=0.0,  # 이전 생성자 호환용; 새 정책에서는 사용하지 않는다.
        new_obstacle_check_radius_m=float(lidar["new_obstacle_check_radius_mm"]) / 1000.0,
        new_obstacle_confirmations=1,
        obstacle_mark_radius_m=float(lidar["robot_radius_mm"]) / 1000.0,
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
        reloc_restore_enabled=bool(lidar.get("reloc_restore_enabled", False)),
        reloc_restore_max_age_ms=int(lidar.get("reloc_restore_max_age_ms", 30000)),
        reloc_restore_radius_m=float(lidar.get("reloc_restore_radius_mm", 300)) / 1000.0,
        reloc_restore_yaw_rad=deg_to_rad(float(lidar.get("reloc_restore_yaw_deg", 5))),
        reloc_restore_score_ratio=float(lidar.get("reloc_restore_score_ratio", 0.95)),
        reloc_restore_tilt_rad=deg_to_rad(float(lidar.get("reloc_restore_tilt_deg", 8))),
        point_hint_radius_m=float(lidar.get("point_hint_radius_m", 0.6)),
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
