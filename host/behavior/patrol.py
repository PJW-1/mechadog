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

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import numpy as np

from host.behavior.commander import Commander
from host.behavior.planner import (
    Plan,
    PlanParams,
    detect_new_obstacle,
    inflate,
    mark_obstacle,
    min_forward_distance,
    plan_to,
)
from host.behavior.zones import ZoneStore, select_next
from host.common.config import ConfigError
from host.common.lidar_link import Scan
from host.common.logging_setup import EdgeTrigger, event_logger
from host.common.protocol import FSM_STATES, clamp
from host.common.units import deg_to_rad, rad_to_deg, wrap_pi
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import MatchParams, Pose, match, preprocess
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
    """계획 → 의도. 소켓도 실시각도 만지지 않는다.

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
    _dynamic: np.ndarray | None = None
    _pending_hit: tuple[float, float] | None = None
    _pending_count: int = 0
    _last_pose_ms: int | None = None
    #: 직전 스캔 때의 IMU yaw (rad). 변화량을 내려면 이전 값이 있어야 한다.
    _last_imu_yaw: float | None = None
    _reverify_attempts: dict[str, int] = field(default_factory=dict)
    _reset_requested: bool = False
    _halt_reason: str = ""
    #: 직전 추종 틱이 제자리 회전이었나 (`steering_for(spinning=)`).
    _spinning: bool = False
    _edge: EdgeTrigger = field(default_factory=EdgeTrigger)
    _obstacles: list[tuple[float, float]] = field(default_factory=list)
    #: 아직 아무도 꺼내 가지 않은 신규 장애물 확정 (`take_new_obstacles`).
    _new_obstacles: list[tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        static = inflate(self.grid, self.plan_params)
        self._blocked = static
        self._dynamic = np.zeros_like(static, dtype=bool)

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

    def take_new_obstacles(self) -> tuple[tuple[float, float], ...]:
        """지난 호출 뒤 확정된 신규 장애물 `(x m, y m)` 을 꺼낸다 — 한 번 꺼내면 비워진다.

        ⚠️ **런타임을 받지 않고 꺼내 가게 한다.** 컨트롤러가 기록·방송 경로를 알면
        소켓 없이 닫히는 시험(이 클래스의 계약)이 깨진다.
        """
        taken = tuple(self._new_obstacles)
        self._new_obstacles.clear()
        return taken

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
        if self.phase is Phase.LOST:
            LOG.info("pose_reacquired", source="ros2")
            self.phase = Phase.PLANNING

    def observe_obstacle_scan(self, scan: Scan, now_ms: int) -> None:
        """외부 측위 모드에서 스캔을 신규 장애물 확인에만 쓴다.

        오래된 자세에 빔을 투영하면 정상 벽을 새 장애물로 찍으므로 유효한 최근
        자세가 있을 때만 지도에 반영한다. 즉시 위험 판정은 ``guard_scan`` 이 별도다.
        """
        self.stats.scans += 1
        if self._last_pose_ms is None or now_ms - self._last_pose_ms > self.drive.pose_timeout_ms:
            return
        self._check_new_obstacle(scan)

    def observe_scan(self, scan: Scan, now_ms: int) -> None:
        """내장 스캔 정합(시뮬레이션용)으로 측위하고 신규 장애물을 확인한다."""
        self.stats.scans += 1
        points = preprocess(scan.points, *self.range_m)
        result = match(
            self.grid,
            points,
            self.pose,
            self.match_params,
            # 변화량만 넘긴다 — 절대 yaw 는 지도 좌표계와 옵셋이 있다.
            yaw_delta=self._consume_yaw_delta(),
        )
        if result.skipped or result.score == 0:
            # 정합 실패 = 측위 상실 (FR-6.6). 점수 0 인 후보로 자세를 갱신하지 않는다.
            return
        self.observe_map_pose(result.pose, now_ms)
        self._check_new_obstacle(scan)

    def _consume_yaw_delta(self) -> float:
        """직전 스캔 이후 IMU 가 본 회전량을 소비한다(두 번 반영하지 않는다). 없으면 0."""
        current = self.safety.yaw_rad
        if current is None:
            self._last_imu_yaw = None
            return 0.0
        previous = self._last_imu_yaw
        self._last_imu_yaw = current
        return 0.0 if previous is None else wrap_pi(current - previous)

    def _check_new_obstacle(self, scan: Scan) -> None:
        """연속 확인 후에만 재계획한다. 한 번의 반사로 경로를 버리지 않는다."""
        hit = detect_new_obstacle(
            self.pose,
            scan.points,
            self.grid,
            self.blocked,
            check_radius_m=self.new_obstacle_check_radius_m,
            margin_m=self.new_obstacle_margin_m,
            occ_thresh=self.plan_params.occ_thresh,
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

        self._settle_reset()
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
        stale = self._last_pose_ms is None or (
            now_ms - self._last_pose_ms > self.drive.pose_timeout_ms
        )
        if stale:
            # 측위가 낡으면 선다 — 다음 스캔 정합이 자세를 되찾으면 이어 간다 (FR-6.6).
            self.commander.halt()
            if self._edge.changed("steer_pose_stale", True):
                self.stats.lost += 1
                LOG.warning("pose_stale", limit_ms=self.drive.pose_timeout_ms)
            return
        self._edge.changed("steer_pose_stale", False)
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

        # ③ 측위 상실 — `ESTOP` 이 아니라 `LOST` 다 (FR-6.6)
        stale = self._last_pose_ms is None or (
            now_ms - self._last_pose_ms > self.drive.pose_timeout_ms
        )
        if stale:
            if self.phase is not Phase.LOST:
                self.stats.lost += 1
                LOG.warning("pose_stale", limit_ms=self.drive.pose_timeout_ms)
            self.phase = Phase.LOST
            return ()

        return ()

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
                )
                if retry.reachable:
                    self.plan = retry
                    self.waypoint_index = 0
                    return

            # 다 썼다 — 이 사이클에서는 이 구역을 버린다. 다음 사이클이
            # 경계에서 표시를 지우고 처음부터 다시 확인한다.
            LOG.warning("zone_unreachable", label=label, reverify_attempts=attempts)
            self.visited = self.visited | {label}

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
        )
        self.waypoint_index = 0
        if self.plan.reachable:
            LOG.info(
                "zone_selected",
                label=self.plan.label,
                cycle=self.cycle,
                path_m=round(self.plan.length_m, 2),
            )
        elif self.visited:
            self._complete_cycle()
        else:
            LOG.error("no_reachable_zone", visited=sorted(self.visited))

    def _follow(self) -> None:
        assert self.plan.label is not None
        goal = self.zones.xy(self.plan.label)
        if math.hypot(goal[0] - self.pose[0], goal[1] - self.pose[1]) < self.drive.arrival_radius_m:
            self._arrive(self.plan.label)
            return

        waypoint = self._current_waypoint()
        heading = math.atan2(waypoint[1] - self.pose[1], waypoint[0] - self.pose[0])
        steering = steering_for(heading - self.pose[2], self.drive, spinning=self._spinning)
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
            if (
                math.hypot(candidate[0] - self.pose[0], candidate[1] - self.pose[1])
                >= self.drive.waypoint_radius_m
            ):
                break
            self.waypoint_index += 1
        return waypoints[min(self.waypoint_index, len(waypoints) - 1)]

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
    return PatrolController(
        commander=commander,
        grid=grid,
        zones=zones,
        drive=drive_params_from_config(config),
        plan_params=plan_params_from_config(config),
        match_params=match_params_from_config(config),
        range_m=range_from_config(config),
        new_obstacle_margin_m=float(lidar["new_obstacle_margin_mm"]) / 1000.0,
        new_obstacle_check_radius_m=float(lidar["new_obstacle_check_radius_mm"]) / 1000.0,
        new_obstacle_confirmations=int(lidar["new_obstacle_confirmations"]),
        obstacle_mark_radius_m=float(lidar["obstacle_mark_radius_mm"]) / 1000.0,
        forward_fan_rad=deg_to_rad(float(lidar["forward_fan_deg"])),
        # 회피 시퀀스와 **같은 값을 쓴다** — 갇힌 상황을 몇 번까지
        # 스스로 풀어 보고 사람에게 넘길지의 값이다 (FR-2.3).
        max_reverify_attempts=int(config["fsm"]["avoid_attempts"]),
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
