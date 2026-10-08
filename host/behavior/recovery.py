"""막힘 복구 — 진행 선분이 막혔을 때 멈춰 확인하고, 둘러보고, 우회하거나 포기한다.

`PatrolController` 의 막힘 대응을 여기에 모은다.

- **시도** — 멈춤 → 정착 스캔 → 안전하면 제자리 한 바퀴 → 최신 스캔으로 재계획 또는
  실측 통로 우회 (`begin` → `step`). 모든 대기는 시간 한도가 있다 (`expire`).
- **포기** — 동선 지점·구역 건너뛰기, 찍은 목표 포기, 모두 막히면 귀환·대기 (`fail`).
- **기억과 알림** — 막힘 기억(`BlockageMemory`)과 대시보드로 나가는 항법 사건
  (`report`·`take_events`), 상태 요약 (`status`).

계획·단계·명령·스캔은 순찰기의 것을 그대로 쓰고 바꾼다. 상태 변경은 순찰 루프 스레드에서만
일어난다.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from host.behavior.blockage import BlockageMemory, Recovery
from host.behavior.patrol_phase import GOAL_LABEL, HOME_LABEL, Phase
from host.behavior.planner import Plan, plan_to
from host.common.logging_setup import event_logger
from host.common.units import rad_to_deg, wrap_pi

if TYPE_CHECKING:
    from host.behavior.patrol import PatrolController

#: 순찰 컨트롤러와 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.behavior.patrol")


class BlockageRecovery:
    """순찰기의 막힘 복구 시도·막힘 기억·항법 사건을 쥔다."""

    def __init__(self, patrol: PatrolController) -> None:
        self._patrol = patrol
        #: 진행 중인 복구 시도 — 없으면 None.
        self.active: Recovery | None = None
        #: 막힘 기억 — 확인된 막힘은 계획 마스크에 더해지고 일정 시간 뒤 잊힌다.
        self.memory = BlockageMemory(patrol.nav_params)
        #: 모든 구역이 막혀 집으로 돌아가는 중인가.
        self.returning_home: bool = False
        #: 집도 막혀 순찰을 쉬는 중인가 — `retry_after_ms` 뒤 다시 본다.
        self.waiting: bool = False
        self.retry_after_ms: int = 0
        #: «순찰 불가» 를 이미 알렸는가 (대기마다 한 번).
        self.wait_reported: bool = False
        #: 대기 시작 때 기억하던 막힘 id — 그것들이 사라지면 대기를 푼다.
        self.wait_memory_ids: set[int] = set()
        #: 아직 아무도 꺼내 가지 않은 항법 사건 (`take_events`).
        self.events: list[dict[str, Any]] = []
        #: 막힘 기억에 묶이지 않은 사건의 다음 허용 시각 — 같은 알림을 되풀이하지 않는다.
        self.unlocated_reports: dict[str, int] = {}

    @property
    def status(self) -> dict[str, Any]:
        return {
            "obstacles": self.memory.status(),
            "skipped_zones": sorted(self._patrol.skipped),
            "returning_home": self.returning_home,
            "waiting": self.waiting,
            "recovery": None
            if self.active is None
            else {
                "target": self.active.target,
                "blockage_id": self.active.blockage_id,
                "scanning": self.active.scanning,
                "swept_deg": round(rad_to_deg(self.active.swept_rad), 1),
            },
        }

    def take_events(self) -> tuple[dict[str, Any], ...]:
        events = tuple(self.events)
        self.events.clear()
        return events

    def report(self, kind: str, recovery: Recovery | None = None, **extra: Any) -> None:
        patrol = self._patrol
        item = next(
            (b for b in self.memory.items if recovery and b.id == recovery.blockage_id), None
        )
        key = kind if kind != "zone_skipped" else f"{kind}:{extra.get('zone')}"
        if item is not None:
            if key in item.reported:
                return
            item.reported.add(key)
        elif patrol._now_ms < self.unlocated_reports.get(key, -1):
            return
        else:
            self.unlocated_reports[key] = patrol._now_ms + patrol.nav_params.blockage_long_ms
        x, y = item.centre if item is not None else patrol.pose[:2]
        judgement = {
            "x": round(x, 3),
            "y": round(y, 3),
            "frame": "patrol",
            "source": "lidar",
            "blockage_id": item.id if item else None,
            "severity": "low" if kind == "obstacle_detour" else "medium",
            "at_ms": patrol._now_ms,
            **extra,
        }
        self.events.append({"event": kind, "judgement": judgement})
        LOG.warning(kind, **judgement)

    def begin(self, reason: str) -> None:
        patrol = self._patrol
        patrol.commander.halt()
        patrol.avoidance.active = None
        if self.active is not None:
            return
        target = patrol.plan.label or (
            GOAL_LABEL
            if patrol._goal is not None
            else next(
                (z for z in patrol.zones.labels if z not in patrol.visited | patrol.skipped),
                HOME_LABEL,
            )
        )
        # 진행 선분을 실제로 막는 반사점만 기억한다. 옆 벽 전체를 사건으로 만들지 않는다.
        endpoint = (
            patrol.plan.waypoints[min(patrol.waypoint_index, len(patrol.plan.waypoints) - 1)]
            if patrol.plan.waypoints
            else patrol._target_xy(target)
        )
        points = []
        ax, ay = patrol.pose[:2]
        dx, dy = endpoint[0] - ax, endpoint[1] - ay
        denominator = dx * dx + dy * dy
        for x, y in patrol.navmap.obstacles:
            t = (
                min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / denominator))
                if denominator
                else 0.0
            )
            if (
                math.hypot(x - ax - t * dx, y - ay - t * dy)
                <= patrol.plan_params.body_radius_m + patrol.grid.meta.resolution
            ):
                points.append((x, y))
        if not points and patrol._local_scan.distance() is not None:
            front = [
                (a, d)
                for a, d in patrol._local_scan.points
                if abs(a) <= math.radians(patrol.nav_params.local_fan_deg)
            ]
            if front and min(d for _, d in front) < patrol.nav_params.local_slow_m:
                a, d = min(front, key=lambda p: p[1])
                points = [
                    (ax + math.cos(patrol.pose[2] + a) * d, ay + math.sin(patrol.pose[2] + a) * d)
                ]
        item = self.memory.remember(patrol.grid, points, patrol._now_ms)
        self.active = Recovery(
            target,
            patrol._now_ms,
            patrol.heading.steering_yaw(),
            item.id if item else None,
            reason=reason,
        )
        patrol.phase = Phase.PLANNING
        patrol._require_replan_stop()
        patrol.avoidance.decide("stop", "blockage_confirm", patrol._local_scan.distance())

    def expire(self, now_ms: int) -> bool:
        """Bound every recovery wait, including waits behind safety/FSM gates.

        A timeout only ends the attempt with STOP; it never substitutes for
        sent STOP or a fresh settled scan as permission to move.
        """
        patrol = self._patrol
        recovery = self.active
        if recovery is None:
            return False
        deadline = recovery.started_ms + patrol.nav_params.recovery_scan_timeout_ms
        if not recovery.scanning and recovery.settling_ms is None:
            deadline = min(
                deadline,
                recovery.started_ms
                + max(patrol.drive.settle_delay_ms, patrol.nav_params.blockage_confirm_ms)
                + patrol.nav_params.recovery_motion_timeout_ms,
            )
        elif recovery.settling_ms is not None:
            deadline = min(
                deadline,
                recovery.settling_ms
                + patrol.drive.settle_delay_ms
                + patrol.nav_params.recovery_motion_timeout_ms,
            )
        if now_ms < deadline:
            return False
        patrol._now_ms = now_ms
        phase = patrol.phase
        reason = (
            "recovery_stop_unconfirmed"
            if patrol._last_sent_moving or patrol._stopped_since_ms is None
            else "recovery_timeout"
        )
        self.fail(recovery, reason)
        # Completing an attempt must not release an existing safety latch/loss.
        if phase in (Phase.HALTED, Phase.LOST):
            patrol.phase = phase
        return True

    def step(self) -> None:
        patrol = self._patrol
        if self.expire(patrol._now_ms):
            return
        recovery = self.active
        assert recovery is not None
        if patrol._last_sent_moving or patrol._stopped_since_ms is None:
            patrol.commander.halt()
            # 회전 중에는 STOP을 기다리는 대신 실제 자세 변화량을 모은다.
            if not recovery.scanning:
                return
        if not recovery.scanning and recovery.settling_ms is None:
            stopped = (
                patrol._stopped_since_ms if patrol._stopped_since_ms is not None else patrol._now_ms
            )
            if patrol._now_ms - max(stopped, recovery.started_ms) < max(
                patrol.drive.settle_delay_ms, patrol.nav_params.blockage_confirm_ms
            ):
                patrol.commander.halt()
                return
            if (
                patrol._local_scan.received_ms is None
                or patrol._local_scan.received_ms < stopped + patrol.drive.settle_delay_ms
            ):
                patrol.commander.halt()
                return
            recovery.scanning = True
            patrol._replan_stop_required = False
            patrol._replan_wait_started_ms = None
            recovery.yaw = patrol.heading.steering_yaw()
            recovery.last_motion_ms = patrol._now_ms
            if recovery.blockage_id is not None and not any(
                b.id == recovery.blockage_id for b in self.memory.items
            ):
                recovery.settling_ms = patrol._now_ms
                patrol.commander.halt()
                return
        if recovery.settling_ms is None:
            delta = wrap_pi(patrol.heading.steering_yaw() - recovery.yaw)
            recovery.yaw = patrol.heading.steering_yaw()
            # 큰 측위 점프를 실제 회전으로 세지 않는다.
            if 0 < delta <= math.radians(45):
                recovery.swept_rad += delta
                recovery.last_motion_ms = patrol._now_ms
            gap = patrol._local_scan.gap(
                patrol.plan_params.body_radius_m,
                patrol.plan_params.body_radius_m + patrol.grid.meta.resolution,
            )
            rotation_safe = (
                gap is not None
                and gap[2] >= 2 * math.pi - 1e-6
                and min(d for _, d in patrol._local_scan.points)
                > patrol.plan_params.body_radius_m + patrol.grid.meta.resolution
            )
            done = recovery.swept_rad >= 2 * math.pi - patrol.drive.heading_tolerance_rad
            timed_out = (
                patrol._now_ms - recovery.started_ms >= patrol.nav_params.recovery_scan_timeout_ms
                or patrol._now_ms
                - (
                    recovery.last_motion_ms
                    if recovery.last_motion_ms is not None
                    else patrol._now_ms
                )
                >= patrol.nav_params.recovery_motion_timeout_ms
            )
            if rotation_safe and not done and not timed_out:
                patrol.commander.drive(
                    0, min(patrol.drive.spin_turn_deg, patrol.nav_params.avoidance_turn_deg)
                )
                patrol.avoidance.decide(
                    "scan", "blockage_rotation_scan", patrol._local_scan.distance()
                )
                return
            # 주변 몸체 공간을 보증할 수 없으면 회전하지 않고 최신 360도 스캔으로 판단한다.
            recovery.settling_ms = patrol._now_ms
            patrol.commander.halt()
            return
        patrol.commander.halt()
        if (
            patrol._last_sent_moving
            or patrol._local_scan.received_ms is None
            or patrol._local_scan.received_ms < recovery.settling_ms + patrol.drive.settle_delay_ms
        ):
            return
        retry = plan_to(
            recovery.target,
            patrol._target_xy(recovery.target),
            patrol.pose[:2],
            patrol.navigation_grid,
            patrol.blocked,
            patrol.plan_params,
            snap_m=0.0 if patrol.route.active else 0.6,
            body_blocked=patrol.body_blocked,
            costs=patrol.navmap.costs,
        )
        self.active = None
        patrol._replan_stop_required = False
        patrol._replan_wait_started_ms = None
        if retry.reachable:
            patrol.plan, patrol.waypoint_index, patrol.phase = retry, 0, Phase.MOVING
            if any(b.id == recovery.blockage_id for b in self.memory.items):
                self.report("obstacle_detour", recovery, target=recovery.target)
            patrol.avoidance.decide(
                "avoid" if recovery.blockage_id is not None else "clear",
                "detour_planned",
                patrol._local_scan.distance(),
            )
            return
        # 전역 격자/기억이 막아도 최신 실제 끝점 사이로 몸이 들어가면 우회한다.
        patrol.plan = Plan(recovery.target)
        if patrol.avoidance.start("lidar_corridor"):
            self.report("obstacle_detour", recovery, target=recovery.target)
            return
        if not patrol._local_scan.complete:
            self.active = recovery
            patrol.avoidance.decide(
                "stop", "corridor_scan_incomplete", patrol._local_scan.distance()
            )
            return
        self.fail(recovery, retry.fail_reason)

    def fail(self, recovery: Recovery, reason: str) -> None:
        """End a failed attempt without treating a skipped goal as an arrival."""
        patrol = self._patrol
        self.active = None
        patrol.avoidance.active = None
        patrol._replan_wait_started_ms = None
        patrol._replan_stop_required = patrol._last_sent_moving or patrol._stopped_since_ms is None
        patrol.commander.halt()
        if recovery.target == HOME_LABEL:
            self.wait_patrol()
        elif patrol.route.active:
            point = patrol.route.point()
            zone = point.label or f"지점 {patrol.route.index + 1}"
            patrol.skipped |= {zone}
            self.report("zone_skipped", recovery, zone=zone, reason=reason)
            patrol.route.skip_point()
        elif patrol._goal is not None:
            patrol._goal = None
            patrol._goal_hold = True
            patrol._goal_hold_reason = "blocked"
            patrol.plan = Plan(None, fail_reason=reason)
            patrol.phase = Phase.IDLE
            patrol.avoidance.decide("stop", "goal_unreachable", patrol._local_scan.distance())
            self.report("zone_skipped", recovery, zone=GOAL_LABEL, reason=reason)
        else:
            patrol.skipped |= {recovery.target}
            self.report("zone_skipped", recovery, zone=recovery.target, reason=reason)
            patrol.plan, patrol.phase = Plan(None), Phase.PLANNING

    def return_home(self) -> None:
        patrol = self._patrol
        patrol._goal = None
        self.returning_home = True
        patrol.plan = Plan(HOME_LABEL)
        patrol.phase = Phase.PLANNING
        patrol.commander.halt()

    def wait_patrol(self) -> None:
        patrol = self._patrol
        self.returning_home = False
        self.waiting = True
        patrol.plan = Plan(None)
        patrol.commander.halt()
        self.retry_after_ms = patrol._now_ms + patrol.nav_params.blockage_long_ms
        self.wait_memory_ids = {b.id for b in self.memory.items}
        if not self.wait_reported:
            self.report("patrol_unavailable", zone="전체")
            self.wait_reported = True
