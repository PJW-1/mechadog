"""구역 순찰 제어 — 계획을 규약 의도로 바꾼다 (FR-7 · Phase 2).

**이 파일은 전문을 만들지 않는다.** `MOVE`·`STOP`·`ESTOP`·`RESET_SAFE`·`STATE` 는
전부 `Commander`(WBS 4.3.2)가 만들고, 여기서는 *무엇을 할 의도인지*만 세운다.
규약 구현을 둘로 갈라지게 하지 않는 것이 이 구조의 목적이다
(ENGINEERING_GUIDE 2.1 · PROTOCOL.md 6절).

합치기 전 코드에서 **고친 것들** — 전부 규약 위반이었다.

| 합치기 전 | 왜 안 되는가 | 지금 |
| :--- | :--- | :--- |
| `{"cmd": "FORWARD", "ts": time.time()}` | 규약에 없는 스키마다. `type` 이 없고 `seq` 가 없고 `ts` 가 초 단위 실수라 규칙 ②·⑤ 로 폐기된다 | `Commander` 가 만드는 `MOVE` |
| `TURN_LEFT` · `TURN_RIGHT` | **제자리 회전은 지원하지 않는다** (DR-11). 로봇이 할 수 없는 동작이다 | 호(arc) 조향 `MOVE{step, angle}` |
| 위험 시 `STOP` | `STOP` 은 일반 보행 정지이고 FAILSAFE 를 걸지 않는다 | `ESTOP` (즉시 · 래치) |
| 첫 전문이 아무거나 | 로봇이 seq 역전으로 **통째로 폐기한다** | `open_session()` = `STOP` seq=1 |
| 초음파 25cm 를 호스트가 판정 | **Tier 1 을 호스트로 옮기는 것**이 된다 (아키텍처 1.2 불변 규칙) | `flags.obstacle` 을 따라간다 |
| 상태 이름 `MOVING`·`ROTATING`·`ARRIVED` | FSM 13종에 없어 로봇이 폐기 + WARN 한다 | 13종으로 사상 (아래 표) |
| RESET 을 스스로 | 사람 확인 없는 자동 해제 (DR-16) | 조작자 확인 + 텔레메트리로 해제 확인 |

**측위 실패는 `ESTOP` 이 아니다.** 자기 위치를 모르는 것은 위험이 아니라 능력의
상실이므로 `LOST` 로 가고 정지한다 (FR-6.6). `ESTOP` 을 걸면 사람이 와서 풀어야
하는데, 다음 스캔에서 재측위될 수 있는 상황에 그것은 과하다.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

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
from host.common.lidar_link import Scan
from host.common.logging_setup import EdgeTrigger, event_logger
from host.common.protocol import FSM_STATES, clamp
from host.common.units import deg_to_rad, rad_to_deg, wrap_pi
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import MatchParams, Pose, match, preprocess

LOG = event_logger("mechadog.behavior.patrol")


class Phase(StrEnum):
    """순찰 내부 단계. **규약의 상태가 아니다** — 아래 표로 사상해서 내려보낸다."""

    IDLE = "IDLE"  # 기동 후 순찰 시작 전
    PLANNING = "PLANNING"  # 다음 구역 선정 · 경로 생성
    MOVING = "MOVING"  # 웨이포인트 추종
    INSPECT = "INSPECT"  # 구역 도착 — 카메라 훅
    LOST = "LOST"  # 측위 실패
    HALTED = "HALTED"  # 안전 래치 (사람이 풀어야 한다)


#: 내부 단계 → **FSM 상태 13종** (PROTOCOL.md 2절 `STATE`).
#:
#: ⚠️ 여기 없는 이름을 내려보내면 로봇이 폐기 + WARN 하고, 텔레메트리의 `state`
#: 는 이전 값에 머문다. 그러면 대시보드가 순찰 중인 로봇을 `IDLE` 로 표시한다.
#:
#: `PLANNING`·`MOVING` 을 둘 다 `PATROL` 로 보내는 것은 정보 손실이 아니다 —
#: 로봇은 이 값을 판단에 쓰지 않고 받아적을 뿐이고, 계획과 이동의 구분은
#: 호스트 로그에 남는다.
FSM_STATE_FOR: Mapping[Phase, str] = {
    Phase.IDLE: "IDLE",
    Phase.PLANNING: "PATROL",
    Phase.MOVING: "PATROL",
    Phase.INSPECT: "ZONE_INSPECT",
    Phase.LOST: "LOST",
    Phase.HALTED: "FAILSAFE",
}

# 사상표가 규약과 어긋나면 **기동을 막는다.** 오타 하나가 실기에서 폐기되는
# `STATE` 로 나타나는 것보다, import 시점에 죽는 것이 낫다.
assert set(FSM_STATE_FOR.values()) <= FSM_STATES, "FSM 13종에 없는 상태를 사상하고 있다"


@dataclass(frozen=True, slots=True)
class DriveParams:
    """보행·안전 파라미터. **`config.yaml` 에서 온다** (NFR-3①).

    `step_mm`·`turn_deg` 는 `gait.step_length_mm`·`gait.turn_angle_deg` 이고
    규약의 클램프 범위(±100mm · ±30deg) 안이다. 그 범위를 여기서 다시 적지
    않는 이유는 `CommandEncoder` 가 이미 자르기 때문이다 (규칙 ②).
    """

    step_mm: float
    turn_deg: float
    reverse_mm: float
    heading_tolerance_rad: float
    #: 이 이상 틀어져 있으면 전진 호로는 못 돌아선다 — 후진 호로 바꾼다.
    reverse_threshold_rad: float
    arrival_radius_m: float
    waypoint_radius_m: float
    #: 호스트측 LiDAR 위험 거리. 온보드 초음파(`obstacle_stop_cm`)와 **다른 것**이다.
    lidar_estop_m: float
    #: 텔레메트리가 이만큼 조용하면 링크 두절로 본다 (`safety.link_loss_failsafe_ms`).
    link_loss_ms: int
    #: 로봇이 보고하는 마지막 수락 명령의 나이가 이것을 넘으면 폐기 중이다.
    cmd_timeout_ms: int
    #: 측위 자세가 이만큼 갱신되지 않으면 `LOST` (`localization.pose_timeout_ms`).
    pose_timeout_ms: int


#: 최대 조향에서 보폭을 이만큼 줄인다 (0.5 = 절반).
#:
#: **호의 반경은 대략 `step / angle` 이므로 보폭을 줄이는 것이 더 급히 도는
#: 것이다.** 조향은 규약 상한(±30deg)에 걸려 더 키울 수 없으니, 반경을 줄이는
#: 손잡이는 보폭뿐이다.
#:
#: ⚠️ 이 값은 `config` 로 빼지 않았다. **실측 없이 튜닝할 값이 아니기 때문이다** —
#: 실제 호 반경은 보행 시퀀스가 정하고 `gait_calibration` 이 아직 비어 있다.
#: 지금 설정 항목으로 만들면 근거 없는 숫자에 설정의 권위가 붙는다. 실측
#: (`2.4.1` · `gait_calibration`) 후에 옮긴다.
TURN_STEP_REDUCTION: float = 0.5


@dataclass(frozen=True, slots=True)
class Steering:
    """한 틱의 보행 의도. mm · deg — **전선 단위다** (규약이 그렇게 받는다)."""

    step_mm: float
    angle_deg: float


def steering_for(heading_error_rad: float, params: DriveParams) -> Steering:
    """방위 오차를 호(arc) 조향으로 바꾼다.

    **제자리 회전이 없다** (DR-11). 그래서 세 구간으로 나뉜다.

    | 오차 | 보행 | 근거 |
    | :--- | :--- | :--- |
    | 허용 오차 이내 | 직진 | 조향을 넣으면 목표를 지나쳐 진동한다 |
    | 그 밖 ~ 후진 임계 | 전진 + 최대 조향 | 호를 그리며 방위를 줄인다 |
    | 후진 임계 초과 | **후진 + 같은 방향 조향** | 목표가 거의 뒤에 있으면 전진 호는 멀어진다 |

    ⚠️ **조향 부호는 걸음의 방향과 무관하다 — 2026-09-11 실측이 이것을 바로잡았다.**

    여기에는 *"후진에서는 조향 부호를 뒤집는다"* 고 적혀 있었고 근거는 요 변화가
    `step × angle` 에 비례한다는 추정이었다. **실기에서 반증됐다** — `move(-60,+20)`
    과 `move(-60,-20)` 을 몰아 보니 **후진에서도 `angle` 양수가 반시계**였다. 벤더
    API 원형이 `move(float speed_x, float angle_rate)` 인 것과도 맞는다: `angle` 은
    **각속도 명령**이라 걸음의 부호가 곱해지지 않는다 (`docs/PROTOCOL.md` 부호 규약).

    그래서 방위 오차를 줄이는 조향은 **전진이든 후진이든 같은 부호**다. 뒤집으면
    로봇이 목표에서 **더 멀어지는 쪽으로** 후진하며 영원히 못 도착한다 — 뒤집힌
    코드가 정확히 그 상태였고, **시험도 그 부호를 굳혀 두고 있었다**(`teleop` 의
    좌우가 뒤바뀐 채 시험에 박혀 있던 것과 같은 형태다).

    ⚠️ **크게 틀어져 있으면 보폭을 줄인다.** 호의 반경은 대략 `step / angle` 이므로
    보폭을 줄이는 것이 곧 **더 급히 도는 것**이다. 조향만 키우고 보폭을 그대로
    두면 조향 상한(±30deg)에 걸려 반경이 더 줄지 않고, 로봇이 큰 호를 그리며
    벽으로 밀려간다 — 실제로 그렇게 만들었더니 방 안쪽 장애물을 돌지 못해
    E-STOP 이 났다. 회피 시퀀스가 후진 거리를 확보하는 것과 같은 이유의 제약이다
    (DR-11 · `gait.reverse_distance_mm`).
    """
    error = wrap_pi(heading_error_rad)
    if abs(error) <= params.heading_tolerance_rad:
        return Steering(params.step_mm, 0.0)

    direction = 1.0 if error > 0 else -1.0
    if abs(error) <= params.reverse_threshold_rad:
        # 오차에 비례해 조향을 키우고 **같은 비율로 보폭을 줄인다.**
        # 조향만 키우면 상한에 걸려 반경이 더 줄지 않는다.
        scale = min(1.0, abs(error) / params.reverse_threshold_rad)
        return Steering(
            params.step_mm * (1.0 - TURN_STEP_REDUCTION * scale),
            direction * params.turn_deg * scale,
        )
    # 조향 부호는 전진과 같다 — 요가 `angle` 단독으로 결정되기 때문이다(위 주석).
    return Steering(-params.step_mm * (1.0 - TURN_STEP_REDUCTION), direction * params.turn_deg)


@dataclass
class SafetyView:
    """로봇이 **보고한** 것. 호스트가 판정한 것이 아니다 (아키텍처 1.2).

    ⚠️ 전압·기울기를 여기서 판정하지 않는다. `TelemetryReceiver` 가 같은 이유로
    같은 선을 긋고 있다 — 호스트가 안전을 판정하면 호스트가 꺼졌을 때 판정이
    사라진다.
    """

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

        `obstacle` 이 없는 구형 펌웨어에서는 `state == AVOID` 로 폴백한다.
        ⚠️ 그 폴백은 **해제를 알 수 없다** (ADR-22) — 호스트가 `AVOID` 를 `STATE`
        로 내려보내면 그 값이 되돌아오기 때문이다. 그래서 우리는 `AVOID` 를
        내려보내지 않는다 (`FSM_STATE_FOR` 에 없다).
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
    """계획 → 의도. **소켓도 실시각도 만지지 않는다.**

    운용 루프(`tools/patrol_run.py`)가 하는 일은 셋뿐이다.

        ① 소켓에서 받은 바이트를 `observe_scan` · `observe_telemetry` 로 넣는다
        ② `step(now_ms)` 를 부르고 돌아온 **즉시 전문**을 그 자리에서 보낸다
        ③ `commander.tick(now_ms)` 의 전문을 10Hz 로 보낸다

    ②와 ③이 나뉘어 있는 이유 — `ESTOP` 은 다음 틱을 기다릴 수 없는 유일한
    부류다 (`Commander.emergency_stop` 주석).
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
    #: 이 거리 안의 빔만 신규 장애물 후보로 본다. 멀리 있는 것은 다음 사이클에
    #: 다시 보게 되고, 측위 오차가 거리에 비례해 커지므로 멀리서 판정하면
    #: 오탐이 는다.
    new_obstacle_check_radius_m: float
    forward_fan_rad: float
    #: 구역이 동적 장애물로 막혔을 때 **표시를 버리고 다시 확인할 최대 횟수**.
    #: `config.fsm.avoid_attempts` 에서 온다 — 회피 시퀀스를 몇 번 되풀이할지와
    #: 같은 값이고 같은 이유다: "다 쓰고도 못 빠져나오면 멈춘 채로 둔다."
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
    _edge: EdgeTrigger = field(default_factory=EdgeTrigger)
    _obstacles: list[tuple[float, float]] = field(default_factory=list)

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
        return self._blocked | self._dynamic

    @property
    def target(self) -> str | None:
        return self.plan.label

    @property
    def obstacles(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._obstacles)

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    # ── 입력: 텔레메트리 ──────────────────────────────────────
    def observe_telemetry(self, reading: Any, now_ms: int) -> None:
        """`host.telemetry.receiver.Reading` 을 받아 안전 관측을 갱신한다.

        타입을 고정하지 않는 이유는 이 컨트롤러가 `Reading` 의 **필드만** 보기
        때문이다. 목업·시뮬레이션이 같은 모양의 객체를 넣을 수 있어야 한다.
        """
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
            # ⚠️ 우리는 10Hz 로 보내는데 로봇이 받아들이지 않고 있다. 조용한
            # 고장이라 큰 소리를 낸다 (`runtime._watch_command_uptake` 와 같은 판단).
            LOG.warning("command_not_taken", last_cmd_age_ms=age, hint="seq 세션 확인")

    @staticmethod
    def _yaw_of(reading: Any) -> float | None:
        """`imu.yaw` 를 꺼낸다. **`reading.yaw` 는 없다.**

        합치기 전 코드가 `tlm.get("yaw")` 로 읽고 있었는데 규약에 그런 필드가
        없다 — `imu` 안에 `pitch`·`roll`·`yaw` 가 들어 있고 단위는 deg 다.
        조용히 `None` 이 되어 IMU 보조가 내내 꺼져 있었다.
        """
        imu = getattr(reading, "imu", None)
        if isinstance(imu, Mapping):
            value = imu.get("yaw")
            return float(value) if isinstance(value, int | float) else None
        return None

    # ── 입력: 스캔 ────────────────────────────────────────────
    def observe_scan(self, scan: Scan, now_ms: int) -> None:
        """스캔 하나로 측위하고 신규 장애물을 확인한다."""
        self.stats.scans += 1
        points = preprocess(scan.points, *self.range_m)
        result = match(
            self.grid,
            points,
            self.pose,
            self.match_params,
            # **변화량만 넘긴다.** 절대 yaw 는 지도 좌표계와 옵셋이 있어
            # 탐색 중심으로 쓸 수 없다 (`slam.py` 머리말).
            yaw_delta=self._consume_yaw_delta(),
        )
        if result.skipped or result.score == 0:
            # 정합이 아는 벽 위에 한 점도 얹지 못했다 = 측위 상실 (FR-6.6).
            # ⚠️ 자세를 갱신하지 않는다 — 점수 0 인 후보는 탐색 격자의 첫 칸일
            # 뿐이고, 그것을 믿고 이동하면 지도와 무관한 방향으로 걸어간다.
            return
        self.pose = result.pose
        self._last_pose_ms = now_ms
        if self.phase is Phase.LOST:
            LOG.info("pose_reacquired", score=result.score)
            self.phase = Phase.PLANNING
        self._check_new_obstacle(scan)

    def _consume_yaw_delta(self) -> float:
        """직전 스캔 이후 IMU 가 본 회전량. 없으면 0.

        **소비한다** — 같은 변화량을 두 번 더하면 회전이 두 배로 반영된다.
        """
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
        """`ESTOP` 전문을 돌려준다. **호출자가 즉시 보낸다.**"""
        self.phase = Phase.HALTED
        self._halt_reason = reason
        self.stats.estops += 1
        self.plan = Plan(None)
        LOG.error("estop", reason=reason)
        return self.commander.emergency_stop()

    def request_reset(self) -> str:
        """`RESET_SAFE` 전문을 돌려준다. **사람이 확인했을 때만 부른다** (DR-16).

        ⚠️ **패킷 수락과 실제 안전 해제는 다르다.** 규약이 못박은 대로, 보낸
        뒤에는 텔레메트리의 `state == IDLE` 과 `safety_latched == false` 를
        확인해야 한다. 그 확인을 `_settle_reset` 이 한다 — 보내자마자 순찰을
        재개하면 래치가 걸린 로봇에게 `MOVE` 를 쏟아붓는다.
        """
        self._reset_requested = True
        LOG.info("reset_requested", reason=self._halt_reason)
        return self.commander.clear_safe()

    def _settle_reset(self) -> None:
        """해제가 **로봇 쪽에서** 확인됐을 때만 순찰로 돌아간다.

        ⚠️ **`state` 로는 확인할 수 없다.** 우리가 `FAILSAFE` 를 `STATE` 로
        내려보내므로 로봇이 그 값을 되돌려주고, 그러면 **되돌아온 값이 로봇의
        판정인지 우리 말의 반향인지 구분할 수 없다** — `AVOID` 와 정확히 같은
        문제다 (ADR-22). 규약이 *"송신측은 `safety_latched=false` 를 확인해야
        한다"* 고 못박은 이유가 이것이다.
        """
        if not self._reset_requested:
            return
        latched = self.safety.latched
        if latched is None:
            # 구형 펌웨어 — `safety_latched` 가 없다. **확인할 방법이 없다.**
            #
            # 예전에는 `state != FAILSAFE` 로 대신했는데 그것은 틀렸다. 우리가
            # `FAILSAFE` 를 내려보낸 뒤이므로 반향이 계속 `FAILSAFE` 로 돌아와
            # **영원히 해제되지 않는다.** 반대로 반향을 신뢰하면 로봇이 실제로
            # 풀리지 않았는데 순찰을 재개한다. 어느 쪽도 안전하지 않다.
            #
            # 그래서 **사람의 확인을 최종 근거로 삼는다** — 이미
            # `request_reset()` 을 부른 것이 그 확인이다. 대신 검증이 불가능함을
            # 크게 남긴다. 규약이 이 필드를 요구하는 것이 곧 그 뜻이다.
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
        """한 주기의 판단. **즉시 보낼 전문**만 돌려준다 (보통 비어 있다).

        주기 전문은 호출자가 `commander.tick(now_ms)` 로 따로 받는다. 둘을 합치면
        `ESTOP` 도 주기에 실려 최대 100ms 늦어진다.
        """
        urgent = self._guard(now_ms)
        if urgent:
            return urgent

        self._settle_reset()
        if self.safety.last_seen_ms is None:
            # 최초 텔레메트리 전에 움직이면 로봇이 아직 부팅 중인 정상 상황에도
            # 경로 추종이 시작된다. 링크가 확인될 때까지 정지한다.
            self.commander.halt()
        elif self.phase is Phase.HALTED or self.phase is Phase.IDLE or self.phase is Phase.LOST:
            self.commander.halt()
        elif self.safety.obstacle_active:
            # **온보드가 이미 멈췄다.** 호스트는 그 판정을 흉내내지 않고 의도만
            # 정지로 내린다 (아키텍처 1.2 · `actions.py` 머리말과 같은 판단).
            self.commander.halt()
            if self._edge.changed("obstacle", True):
                LOG.info("onboard_obstacle_hold", dist_cm=self.safety.dist_cm)
        else:
            self._edge.forget("obstacle")
            self._advance()

        # **상태는 마지막에 알린다.** 이번 틱의 판단이 반영된 값이어야 한다.
        self.commander.announce(self.fsm_state)
        return ()

    def _guard(self, now_ms: int) -> tuple[str, ...]:
        """안전 점검. 단계를 옮기고, 즉시 보낼 전문이 있으면 돌려준다.

        순서가 규약의 우선순위다 — **로봇이 보고한 래치가 가장 먼저**다
        (Tier 1 판정이 항상 우선 · 아키텍처 1.2 불변 규칙).

        ⚠️ **여기서 `ESTOP` 을 보내지 않는다.** 온보드가 이미 래치를 걸었다고
        보고한 상태에 `ESTOP` 을 더 보내는 것은 아무것도 바꾸지 않고, 링크
        두절이면 애초에 닿지 않는다. 호스트가 `ESTOP` 을 만드는 곳은 사람이
        누른 경우와 `guard_scan` 의 LiDAR 판정 둘뿐이다. 그래서 이 함수는
        보통 빈 튜플을 돌려준다.
        """
        # ① 로봇이 래치를 걸었다고 보고했다 — 우리가 판정하지 않는다
        if self.safety.latched or self.safety.onboard_state == "FAILSAFE":
            if self.phase is not Phase.HALTED:
                self._halt_reason = "온보드 FAILSAFE 보고"
                self.phase = Phase.HALTED
                LOG.error("onboard_failsafe", state=self.safety.onboard_state)
            return ()

        # ② 텔레메트리 침묵 — 링크가 끊겼다.
        #    ⚠️ **명령 송신을 멈추지 않는다.** 10Hz 송신이 곧 링크 신호이므로
        #    (PROTOCOL 1절), 멈추면 링크가 돌아왔을 때 로봇이 그것을 모른다.
        # 기동 직후 아직 한 건도 받지 못한 상태는 두절과 다르다. 여기서
        # HALTED로 보내면 첫 패킷이 수 ms 늦은 정상 상황도 수동 리셋이 필요하다.
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
        """LiDAR 전방 위험거리 — **호스트측 판정이다.**

        초음파(`obstacle_stop_cm`)와 달리 LiDAR 는 호스트에 붙은 센서이므로
        판정 주체가 호스트일 수밖에 없다. Tier 1 을 옮기는 것이 아니라, 온보드가
        볼 수 없는 것을 보는 것이다 — 초음파는 정면 근거리만 본다(DR-15).
        온보드 판정을 **대체하지 않고 더한다.**
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
                LOG.error("no_zones", hint="tools/zone_select.py 를 먼저 실행한다")
            return

        if self.phase is Phase.INSPECT:
            # 도착 훅이 끝나면 다음 구역으로. 지금은 즉시 넘어간다 —
            # 카메라 판독(FR-8)은 `change_detect` 소관이며 여기서 기다리는
            # 시간을 정하면 그 값이 두 곳에 생긴다.
            self.phase = Phase.PLANNING

        if self.phase is Phase.PLANNING or not self.plan.reachable:
            self._replan()
            if not self.plan.reachable:
                self.commander.halt()
                return
            self.phase = Phase.MOVING

        self._follow()

    def _replan(self) -> None:
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

            # ⚠️ **동적 장애물 때문에 막힌 것인지 확인한다 — 단, 횟수를 센다.**
            #
            # 여기서 바로 포기하면 **누적된 오탐이 구역을 영구히 봉인한다.**
            # 측위가 10~20cm 흔들리는 구간에서 오탐이 열 번 찍히자 통로가 막혀
            # 구역 하나를 매 사이클 건너뛰었고, 로그에는 `zone_unreachable` 만
            # 남아 지도가 잘못된 것처럼 보였다. 동적 장애물은 **이번 순찰의
            # 사실이지 공간의 사실이 아니다** (`planner.mark_obstacle` 주석).
            #
            # ⚠️ **그런데 무한히 다시 확인해서도 안 된다.** 표시를 버리고 다시
            # 계획하면 로봇이 그 장애물로 되돌아가고, 진짜 장애물이면 다시
            # 찍히고 다시 버려져 **되돌아가기를 되풀이한다.** 실제로 그렇게
            # 만들었더니 한 번의 순찰에서 E-STOP 이 2538회 났다 — 실기라면
            # 서보 기어가 상하는 동작이다.
            #
            # 그래서 `config.fsm.avoid_attempts` 를 그대로 쓴다. 회피 시퀀스가
            # 같은 문제를 이미 그 값으로 풀었고, 근거도 같다 —
            # *"다 쓰고도 못 빠져나오면 멈춘 채로 둔다. 계속 흔들면 기어만
            # 상하고, 갇힌 상황은 사람이 봐야 한다."*
            label = self.plan.label
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
        steering = steering_for(heading - self.pose[2], self.drive)
        # 규약 범위는 인코더가 자르지만(규칙 ②), 잘려서 나가는 것을 로그로
        # 보고 싶지는 않으므로 여기서 설정값 안에 둔다.
        self.commander.drive(
            clamp(steering.step_mm, -abs(self.drive.step_mm), abs(self.drive.step_mm)),
            clamp(steering.angle_deg, -abs(self.drive.turn_deg), abs(self.drive.turn_deg)),
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
        # **새 사이클은 공간을 다시 확인한다.** 지난 사이클에 사람이 서 있던
        # 자리를 이번 사이클에도 막힌 것으로 두면, 순찰이 돌 때마다 통행 가능한
        # 영역이 단조 감소한다 — 오래 돌린 로봇이 점점 좁은 길만 다닌다.
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
    """`config.yaml` 에서 보행·안전 파라미터를 만든다.

    ⚠️ **없는 키를 기본값으로 때우지 않는다.** `fsm.py._lookup` 이 같은 이유로
    `KeyError` 를 그대로 올린다 — 기본값을 두면 설정에서 항목을 지워도 동작이
    그대로라 설정이 정본이 아니게 된다.
    """
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
        arrival_radius_m=float(zones["arrival_radius_mm"]) / 1000.0,
        waypoint_radius_m=float(lidar["waypoint_radius_mm"]) / 1000.0,
        lidar_estop_m=float(lidar["estop_distance_mm"]) / 1000.0,
        link_loss_ms=int(safety["link_loss_failsafe_ms"]),
        cmd_timeout_ms=int(safety["cmd_timeout_ms"]),
        pose_timeout_ms=int(localization["pose_timeout_ms"]),
    )


def describe(controller: PatrolController) -> str:
    """한 줄 상태 표기. 콘솔 관측용이며 로그의 정본은 JSONL 이다.

    **내부 단계와 내려보내는 `STATE` 를 나란히 찍는다.** 둘이 다르다는 것이
    사상표의 요점이고, 실기 시험에서 텔레메트리의 `state` 와 대조할 값은
    오른쪽이다.
    """
    x, y, yaw = controller.pose
    intent = controller.commander.intent
    fields = " ".join(f"{k}={v:g}" for k, v in intent.fields.items())
    return (
        f"{controller.phase.value:<9}-> STATE={controller.fsm_state:<12} "
        f"target={controller.target or '-':<3} "
        f"pose=({x:+.2f}, {y:+.2f}, {rad_to_deg(yaw):+6.1f}deg) "
        f"intent={intent.type_}{' ' + fields if fields else ''}"
    )
