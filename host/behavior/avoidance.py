"""국소 회피 — 실측 LiDAR 통로로 틀어 짧게 걷고, 근거리 판단을 대시보드에 남긴다.

`PatrolController` 의 국소 항법 판단을 여기에 모은다.

- **판단 기록** — 진행·감속·정지·회피 판단과 이유, 앞 거리 (`decide` → `status`).
  바뀔 때만 `local_navigation` 로그를 남긴다.
- **회피** — 목표 쪽 실측 통로(`corridor_to`)나 가장 넓은 틈으로 방향을 잡고 (`start`),
  정해진 거리·시간만큼 틀어 걸은 뒤 재계획으로 돌아간다 (`step`).
- **출발 탈출** — 출발 자리가 몸 원판에 막혔으면 다음 계획 전에 먼저 빠져나간다
  (`needs_escape`).

자세·스캔·명령은 순찰기의 것을 그대로 쓰고 바꾼다. 상태 변경은 순찰 루프 스레드에서만
일어난다.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from host.behavior.patrol_phase import GOAL_LABEL, HOME_LABEL, Phase
from host.behavior.planner import Plan
from host.common.logging_setup import event_logger
from host.common.units import rad_to_deg, wrap_pi

if TYPE_CHECKING:
    from host.behavior.nav_state import PatrolNavState
    from host.behavior.patrol import PatrolController

#: 순찰 컨트롤러와 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.behavior.patrol")


class LocalAvoidance:
    """순찰기의 국소 판단 기록과 진행 중인 회피를 쥔다."""

    def __init__(self, patrol: PatrolController, state: PatrolNavState) -> None:
        self._patrol = patrol
        #: 순찰기와 함께 쓰는 항법 상태 — 값은 복사하지 않고 매번 여기서 읽는다.
        self._state = state
        #: 마지막 국소 판단 — 대시보드 `local_status` 로 나간다.
        self.status: dict[str, Any] = {}
        #: 진행 중인 회피 (시작 x, y, 목표 방향, 시작 시각, 이유) — 없으면 None.
        self.active: tuple[float, float, float, int, str] | None = None
        #: 다음 계획 전에 출발 자리에서 먼저 빠져나가야 하는가.
        self.needs_escape: bool = False

    def decide(
        self, action: str, reason: str, distance: float | None = None, gap_rad: float | None = None
    ) -> None:
        patrol = self._patrol
        self.status = {
            "action": action,
            "reason": reason,
            "distance_m": None if distance is None else round(distance, 3),
            "gap_deg": None if gap_rad is None else round(rad_to_deg(gap_rad), 1),
            "scan_age_ms": None
            if self._state.local_scan.received_ms is None
            else self._state.now_ms - self._state.local_scan.received_ms,
            "slow_m": patrol.nav_params.local_slow_m,
            "stop_m": patrol.nav_params.local_stop_m,
            "message": (f"앞 {distance:.2f}m — " if distance is not None else "")
            + {
                "clear": "진행",
                "slow": "감속",
                "stop": "정지",
                "avoid": "회피 중",
                "escape": "출발 탈출",
            }.get(action, reason),
        }
        if self._state.edge.changed("local_navigation", (action, reason)):
            LOG.info("local_navigation", **self.status)

    def corridor_to(self, target: tuple[float, float]) -> tuple[float, float, float] | None:
        patrol = self._patrol
        scan_pose = self._state.local_scan_pose or patrol.pose
        dx, dy = patrol.pose[0] - scan_pose[0], patrol.pose[1] - scan_pose[1]
        c, s = math.cos(scan_pose[2]), math.sin(scan_pose[2])
        heading = wrap_pi(
            math.atan2(target[1] - patrol.pose[1], target[0] - patrol.pose[0]) - scan_pose[2]
        )
        gap = self._state.local_scan.corridor(
            patrol.plan_params.body_radius_m,
            heading,
            patrol.nav_params.avoidance_m,
            origin=(dx * c + dy * s, -dx * s + dy * c),
        )
        if gap is None:
            return None
        return wrap_pi(gap[0] + scan_pose[2] - patrol.heading.steering_yaw()), gap[1], gap[2]

    def start(self, reason: str) -> bool:
        patrol = self._patrol
        if not self._state.local_scan.fresh(self._state.now_ms) or patrol.pose_stale(
            self._state.now_ms
        ):
            patrol.commander.halt()
            return False
        if reason in {"start_escape", "lidar_corridor"}:
            target = patrol.plan.label or (
                GOAL_LABEL
                if self._state.goal.xy is not None
                else next(
                    (z for z in patrol.zones.labels if z not in patrol.visited | patrol.skipped),
                    HOME_LABEL,
                )
            )
            gap = self.corridor_to(patrol.target_xy(target))
        else:
            gap = self._state.local_scan.gap(patrol.plan_params.body_radius_m)
        if gap is None:
            patrol.commander.halt()
            self.decide("stop", "no_observed_gap", self._state.local_scan.distance())
            return False
        angle, _distance, _ = gap
        self.active = (
            patrol.pose[0],
            patrol.pose[1],
            wrap_pi(patrol.heading.steering_yaw() + angle),
            self._state.now_ms,
            reason,
        )
        patrol.commander.halt()
        self.decide(
            "escape" if reason == "start_escape" else "avoid",
            reason,
            self._state.local_scan.distance(),
            angle,
        )
        return True

    def step(self) -> None:
        patrol = self._patrol
        assert self.active is not None
        x, y, heading, started, reason = self.active
        if (
            math.hypot(patrol.pose[0] - x, patrol.pose[1] - y) >= patrol.nav_params.avoidance_m
            or self._state.now_ms - started >= patrol.nav_params.avoidance_timeout_ms
        ):
            self.active = None
            patrol.plan = Plan(patrol.plan.label)
            patrol.phase = Phase.PLANNING
            patrol.commander.halt()
            if patrol.route.direct_moving:
                patrol.route.direct_stopped_ms = None
                patrol.route.direct_detour_start = None
                self._state.spinning = False
                self.decide("clear", "route_direct_resumed", self._state.local_scan.distance())
                return
            patrol.stats.replans += 1
            LOG.info("local_avoidance_replan", reason=reason)
            if reason in {"start_escape", "lidar_corridor"}:
                patrol.require_replan_stop()
                patrol.replan()
            return
        error = wrap_pi(heading - patrol.heading.steering_yaw())
        turn_needed = abs(error) > patrol.drive.heading_tolerance_rad
        if reason in {"start_escape", "lidar_corridor"}:
            scan_pose = self._state.local_scan_pose or patrol.pose
            dx, dy = patrol.pose[0] - scan_pose[0], patrol.pose[1] - scan_pose[1]
            c, s = math.cos(scan_pose[2]), math.sin(scan_pose[2])
            # 일반 추종 허용각 안에서도 좁은 통로의 실제 직진 원판이 닿으면 더 맞춘다.
            if not turn_needed and not self._state.local_scan.corridor_clear(
                patrol.plan_params.body_radius_m,
                wrap_pi(patrol.heading.steering_yaw() - scan_pose[2]),
                patrol.nav_params.avoidance_m,
                origin=(dx * c + dy * s, -dx * s + dy * c),
            ):
                turn_needed = True
            actual_heading = heading if turn_needed else patrol.heading.steering_yaw()
            if not self._state.local_scan.corridor_clear(
                patrol.plan_params.body_radius_m,
                wrap_pi(actual_heading - scan_pose[2]),
                patrol.nav_params.avoidance_m,
                origin=(dx * c + dy * s, -dx * s + dy * c),
            ):
                self.active = None
                patrol.commander.halt()
                self.decide("stop", "corridor_closed", self._state.local_scan.distance(error))
                patrol.recovery.begin("corridor_closed")
                return
        if turn_needed:
            patrol.commander.drive(
                0,
                math.copysign(
                    min(
                        patrol.nav_params.avoidance_turn_deg,
                        patrol.drive.spin_turn_deg,
                        rad_to_deg(abs(error)),
                    ),
                    error,
                ),
            )
            self.decide(
                "escape" if reason == "start_escape" else "avoid",
                reason,
                self._state.local_scan.distance(error),
                error,
            )
            return
        distance = self._state.local_scan.distance()
        if distance is None or distance < patrol.nav_params.local_stop_m:
            self.active = None
            patrol.commander.halt()
            self.decide("stop", "local_stop_distance", distance)
            return
        scale = min(
            1.0,
            max(
                0.0,
                (distance - patrol.nav_params.local_stop_m)
                / (patrol.nav_params.local_slow_m - patrol.nav_params.local_stop_m),
            ),
        )
        patrol.commander.drive(
            patrol.drive.step_mm * min(patrol.nav_params.avoidance_step_scale, scale), 0
        )
        self.decide("escape" if reason == "start_escape" else "avoid", reason, distance, error)
