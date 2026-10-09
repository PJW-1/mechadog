"""느슨한 동선 추종 — 동선 지점으로 곧장 걷되 LiDAR 로 열린 방향을 골라 비켜 간다.

`nav.relaxed_follow` 가 켜진 동선에서 `PatrolController` 가 쓰는 단순 추종을 여기에 모은다
(2026-10-06 현장 설계). 안전·측위 관문 말고는 A*·막힘 복구 관문을 열지 않는다.

- **추종** — 지점을 지나쳤거나 닿았으면 도착, 아니면 열린 방향을 골라 걷는다 (`follow` →
  `walk`). 정면이 막히면 서고, 실제로 멈춘 채 3초가 지나면 탈출 회전을 한다.
- **길 막힘** — 목표 거리가 줄지 않은 채 정면이 막혀 있으면 한 번 알리고 선다
  (`stuck_check`). 앞이 열리면 다시 따라간다.
- **조향용 스캔** — 기울지 않은 마지막 바퀴와 그때의 방위를 따로 쥔다 (`scan`·`scan_yaw`).

자세·계획·명령은 순찰기의 것을 그대로 쓰고 바꾼다. 상태 변경은 순찰 루프 스레드에서만
일어난다.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from host.behavior.live_nav import LocalScan
from host.behavior.patrol_phase import GOAL_LABEL, Phase
from host.behavior.planner import Plan, min_forward_distance
from host.common.units import wrap_pi

if TYPE_CHECKING:
    from host.behavior.patrol import PatrolController


class RelaxedFollower:
    """순찰기의 느슨한 동선 추종 상태를 쥔다."""

    def __init__(self, patrol: PatrolController) -> None:
        self._patrol = patrol
        #: 지금 구간의 시작점 — 지점을 지나쳤는지 이 선분으로 판단한다.
        self.leg_start: tuple[float, float] = (0.0, 0.0)
        #: 직전 틱의 목표 거리 — 지나친 뒤 멀어지기 시작했는지 본다.
        self.last_distance: float = math.inf
        #: «길 막힘» 을 이미 알렸는가.
        self.blocked_reported: bool = False
        #: (목표, 최소 거리, 그때 시각) — 진척이 없는 시간을 잰다.
        self.progress: tuple[tuple[float, float], float, int] | None = None
        #: 길 막힘으로 서 있는가.
        self.stuck: bool = False
        #: 정면 정지 뒤 탈출 회전 중인가.
        self.escape_turn: bool = False
        #: 조향에 쓰는 마지막 정상 스캔과 그때의 방위.
        self.scan = LocalScan(patrol.nav_params)
        self.scan_yaw: float = 0.0
        #: 지금 장애물을 비켜 가는 중인가 — 우회 알림을 한 번만 낸다.
        self.detouring: bool = False

    @property
    def active(self) -> bool:
        patrol = self._patrol
        return (
            patrol.nav_params.relaxed_follow
            and patrol.route.active
            and not patrol.recovery.returning_home
        )

    def pose_available(self, now_ms: int) -> bool:
        verified = self._patrol.localization.last_verified_pose_ms
        return (
            self._patrol.localization.last_pose_ms is not None
            and verified is not None
            and 0 <= now_ms - verified <= 2000
        )

    def stuck_check(
        self, target: tuple[float, float], distance: float, front: float | None
    ) -> bool:
        """목표까지 거리가 정해진 시간 동안 0.1m 도 줄지 않으면 «길 막힘» 을 한 번 알리고 선다.

        앞(±30°)이 0.45m 넘게 열리면 풀고 다시 따라간다 — 사람이 장애물을 치운 경우다.
        정면 정지·탈출 회전을 끝없이 되풀이하는 것을 막는다(2026-10-06 목업: 침실 문 막힘).
        """
        patrol = self._patrol
        prog = self.progress
        if prog is None or prog[0] != target or distance < prog[1] - 0.10:
            self.progress = (target, distance, patrol._now_ms)
            if prog is not None and prog[0] != target:
                self.stuck = False
            prog = self.progress
        if self.stuck:
            if front is None or front > 0.45:
                self.stuck = False
                self.blocked_reported = False
                self.progress = (target, distance, patrol._now_ms)
                return False
            patrol.commander.halt()
            patrol.avoidance.decide("stop", "relaxed_stuck_wait", front)
            return True
        # 정면이 실제로 막혔을 때만 «길 막힘» — 문틀 앞에서 느려진 것을 막힘으로 보지 않는다
        # (2026-10-06 실기: 침실 문 앞 정면 0.56m 에서 오판해 계속 대기).
        if front is None or front > 0.35:
            self.progress = (
                (target, min(distance, prog[1]), patrol._now_ms)
                if front is None or front > 0.45
                else prog
            )
            return False
        if patrol._now_ms - prog[2] >= patrol.nav_params.relaxed_stuck_ms:
            self.stuck = True
            patrol.commander.halt()
            patrol.avoidance.decide("stop", "relaxed_stuck_wait", front)
            if not self.blocked_reported:
                patrol.recovery.report("path_blocked", message="길 막힘")
                self.blocked_reported = True
            return True
        return False

    def follow(self) -> None:
        """동선만 단순 추종한다. 안전/측위 외에는 복구 관문을 열지 않는다."""
        patrol = self._patrol
        patrol.avoidance.active = patrol.recovery.active = None
        patrol.avoidance.needs_escape = patrol._replan_stop_required = False
        scan = self.scan
        front = min_forward_distance(patrol._local_scan.points, math.radians(30))
        stopped = front is not None and front <= 0.25
        if stopped and patrol.route.direct_stopped_ms is None:
            patrol.route.direct_stopped_ms = patrol._now_ms
        if not stopped:
            patrol.route.direct_stopped_ms = None
            self.escape_turn = False
        if patrol.route.stage != "moving":
            if stopped:
                patrol.commander.halt()
                patrol.avoidance.decide("stop", "relaxed_front_stop", front)
            else:
                patrol.route.advance_arrival()
            return
        point = patrol.route.point()
        target = (point.x, point.y)
        distance = math.dist(patrol.pose[:2], target)
        sx, sy = self.leg_start
        vx, vy = point.x - sx, point.y - sy
        px, py = patrol.pose[0] - sx, patrol.pose[1] - sy
        length = math.hypot(vx, vy)
        passed = (
            length > 1e-6
            and px * vx + py * vy >= length * length
            and abs(px * vy - py * vx) / length <= patrol.drive.arrival_radius_m
            and distance > self.last_distance
        )
        self.last_distance = distance
        if distance <= patrol.drive.arrival_radius_m or passed:
            self.progress, self.stuck = None, False
            patrol.arrival.arrive(GOAL_LABEL)
            return
        if patrol.nav_params.relaxed_stuck_hold and self.stuck_check(target, distance, front):
            return
        patrol.plan = Plan(GOAL_LABEL, (patrol.pose[:2], target), distance, effective=target)
        patrol.phase = Phase.MOVING
        # 기울어진 스캔은 조향에 덮어쓰지 않는다. 마지막 정상 바퀴도 2초를 넘기면 정지.
        max_age = patrol.nav_params.scan_max_age_ms if patrol._local_scan.clear_allowed else 2000
        if scan.received_ms is None or not 0 <= patrol._now_ms - scan.received_ms <= max_age:
            patrol.commander.halt()
            patrol.route.direct_stopped_ms = None
            patrol.avoidance.decide("stop", "relaxed_scan_unavailable", front)
            return
        heading = math.atan2(point.y - patrol.pose[1], point.x - patrol.pose[0])
        scan_yaw = self.scan_yaw
        goal_heading = wrap_pi(heading - scan_yaw)
        chosen = scan.open_heading(
            goal_heading, travel_m=0.7 if patrol.nav_params.relaxed_walk_detour else 0.35
        )
        if chosen is None:
            patrol.commander.halt()
            patrol.avoidance.decide("stop", "relaxed_path_blocked", front)
            count = round(360 / patrol.nav_params.gap_bin_deg)
            covered = {round(a / (2 * math.pi / count)) % count for a, _ in scan.points}
            if len(covered) >= 0.9 * count and not self.blocked_reported:
                patrol.recovery.report("path_blocked", message="길 막힘")
            self.blocked_reported = True
            return
        self.blocked_reported = False
        deviation = abs(wrap_pi(chosen - goal_heading))
        target_obstacle = min(
            (
                d
                for a, d in scan.points
                if d * math.cos(a - goal_heading) > 0
                and abs(d * math.sin(a - goal_heading)) <= 0.20
            ),
            default=math.inf,
        )
        detouring = (
            deviation >= math.radians(patrol.nav_params.relaxed_detour_angle_deg)
            and target_obstacle <= patrol.nav_params.relaxed_detour_distance_m
        )
        if (
            patrol.nav_params.relaxed_detour_notice
            and detouring
            and not self.detouring
            and not stopped
        ):
            patrol.recovery.report("obstacle_detour", target=patrol.plan.label)
        self.detouring = detouring
        error = wrap_pi(scan_yaw + chosen - patrol.heading.steering_yaw())
        if stopped:
            patrol.commander.halt()
            patrol.avoidance.decide("stop", "relaxed_front_stop", front)
            since = patrol.route.direct_stopped_ms
            actual_stop = patrol._stopped_since_ms
            if not self.escape_turn and (
                since is None
                or actual_stop is None
                or patrol._last_sent_moving
                or patrol._now_ms - max(since, actual_stop) < 3000
            ):
                return
            self.escape_turn = True
            patrol.commander.drive(0.0, math.copysign(abs(patrol.drive.spin_turn_deg), error))
            patrol.avoidance.decide("turn", "relaxed_escape_turn", front)
            return
        self.walk(error, heading, deviation, front)

    def walk(self, error: float, heading: float, deviation: float, front: float | None) -> None:
        """고른 열린 방향으로 걷는다 — 크게 틀어졌으면 제자리 회전, 아니면 걸으며 조향한다."""
        patrol = self._patrol
        # 2026-10-06 실기: 걸으며 호 조향은 약해서 보행 우편향을 못 이기고(오른쪽으로 밀려 방1 벽 앞
        # 정지), 몸도 기운다(10-04 roll +17~26°). 오차가 8° 넘으면 멈춰서 제자리 회전으로 바로잡고,
        # 그 안이면 조향 없이 직진한다 — 매 틱 측위 방위로 다시 재므로 폐루프다.
        # 2026-10-06 사용자: 제자리 회전으로 자주 멈추면 느리다 → 30° 안은 걸으면서 조향.
        # 예전 호 조향은 약해서(turn_deg·비율) 우편향을 못 이겼다 — 오차 1°당 1.2° 로 강하게, 상한 25°.
        spin = abs(error) > math.radians(30) or (patrol._spinning and abs(error) > math.radians(15))
        walking_detour = (
            patrol.nav_params.relaxed_walk_detour
            and 0 < deviation <= math.radians(60)
            and abs(error) <= math.radians(60)
            and abs(wrap_pi(heading - patrol.heading.steering_yaw())) <= math.radians(60)
        )
        if walking_detour:
            spin = False
        patrol._spinning = spin
        if spin:
            patrol.commander.drive(0.0, math.copysign(patrol.drive.spin_turn_deg, error))
        else:
            angle = max(-25.0, min(25.0, math.degrees(error) * 1.2))
            if abs(math.degrees(error)) <= 3.0:
                angle = 0.0
            patrol.commander.drive(patrol.drive.step_mm * (0.5 if walking_detour else 1.0), angle)
        patrol.avoidance.decide("turn" if spin else "clear", "relaxed_follow", front)
