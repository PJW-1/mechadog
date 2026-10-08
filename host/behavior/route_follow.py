"""동선 추종 — 사람이 그린 동선의 지점을 차례로 걷고, 도착하면 겨누고 머물며 둘러본다.

`PatrolController` 의 명시 동선 진행을 여기에 모은다.

- **시작·취소** — 동선과 첫 접근을 검증해 예약하고 (`start`), 끝나거나 막히면 그 자리에
  선다 (`cancel`). 관제 스냅샷용 진행 상황은 `summary`.
- **도착** — 지점 방향으로 겨누고, 머무는 동안 좌우로 사람을 찾고, 점검을 기다린 뒤 다음
  지점으로 넘어간다 (`advance_arrival` → `search_person` → `next_point`). 막힌 지점은
  건너뛴다 (`skip_point`).
- **직접 추종** — 검증된 동선 구간은 A* 없이 지점으로 곧장 걷고, 실제로 멈춘 채 정해진
  시간이 지나면 기존 회피로 넘긴다 (`follow_direct`·`direct_obstacle_stop`).

자세·계획·단계·명령은 순찰기의 것을 그대로 쓰고 바꾼다. 상태 변경은 순찰 루프 스레드에서만
일어난다.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from host.behavior.patrol_phase import GOAL_LABEL, Phase
from host.behavior.planner import Plan, body_collision_mask, inflate, segment_clear
from host.behavior.routes import Route, RoutePoint, validate_route
from host.common.logging_setup import event_logger
from host.common.protocol import clamp
from host.common.units import deg_to_rad, rad_to_deg, wrap_pi

if TYPE_CHECKING:
    from collections.abc import Callable

    from host.behavior.patrol import PatrolController, Steering

#: 순찰 컨트롤러와 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.behavior.patrol")


class RouteFollower:
    """순찰기의 명시 동선과 그 진행 상태를 쥔다."""

    def __init__(self, patrol: PatrolController, steering_for: Callable[..., Steering]) -> None:
        self._patrol = patrol
        #: 방위 오차 → 보행 의도 — 순찰기가 `host.behavior.patrol.steering_for` 를 넘겨준다
        #: (이 모듈이 patrol 을 import 하면 순환 import 가 된다).
        self._steering_for = steering_for
        #: 진행 중이거나 마지막으로 끝난 동선 — 한 번도 시작하지 않았으면 None.
        self.current: Route | None = None
        #: 지금 향하는 지점 번호와 반복 회차.
        self.index: int = 0
        self.cycle: int = 1
        #: 동선을 시작한 횟수 — 같은 지점의 다른 방문을 가른다 (`visit`).
        self.run: int = 0
        #: «idle» | «active» | 끝난 이유 («completed»·«stopped»·«replaced» …).
        self.status: str = "idle"
        #: 지점 안 단계 — «moving» | «aiming» | «dwell» | «search» | «inspection» | 끝난 이유.
        self.stage: str = "moving"
        #: 머무르기 마감 시각 — 도착 전이면 None.
        self.dwell_until_ms: int | None = None
        #: 직접 추종 중 장애물 정지를 처음 본 시각.
        self.direct_stopped_ms: int | None = None
        #: 직접 추종에서 회피·우회로 넘어간 자리 — 우회 중이 아니면 None.
        self.direct_detour_start: tuple[float, float] | None = None
        #: 도착 방향 사람 찾기 — 기준 방위, 좌·우·중앙 중 몇 번째, 다음 회전 허용 시각, 시작 시각.
        self.search_base: float | None = None
        self.search_index: int = 0
        self.search_pause_until: int = 0
        self.search_started_ms: int = 0
        #: 이번 회차에 건너뛴 지점 번호.
        self.skipped_indices: frozenset[int] = frozenset()

    @property
    def active(self) -> bool:
        return self.current is not None and self.status == "active"

    @property
    def visit(self) -> tuple[int, int, int] | None:
        """같은 구역도 명시 동선의 다른 방문이면 카메라 점검을 다시 연다."""
        return (self.run, self.cycle, self.index) if self.active else None

    @property
    def controls_inspection(self) -> bool:
        return self.active or bool(
            self._patrol._goal_hold_reason and self._patrol._goal_hold_reason.startswith("route_")
        )

    def summary(self) -> dict[str, Any] | None:
        """루프가 관제 스냅샷에 넣는 동선 진행 상황. 좌표는 순찰 좌표다."""
        route = self.current
        if route is None:
            return None
        return {
            "id": route.id,
            "name": route.name,
            "point_index": self.index,
            "point_count": len(route.points),
            "cycle": self.cycle,
            "repeat": route.repeat,
            "status": self.status,
            "phase": self.stage,
            "points": [point.model_dump() for point in route.points],
            "dwell_remaining_s": max(
                0.0, ((self.dwell_until_ms or self._patrol._now_ms) - self._patrol._now_ms) / 1000
            ),
        }

    def start(self, route: Route, now_ms: int) -> tuple[bool, str]:
        """동선과 첫 접근을 검증하고 직접 추종 또는 기존 goto로 예약한다."""
        patrol = self._patrol
        if patrol.phase is Phase.HALTED or patrol.safety.latched:
            return False, "안전 정지를 해제한 뒤 동선을 시작해야 한다"
        located = (
            patrol._relaxed_pose_available(now_ms)
            if patrol.nav_params.relaxed_follow
            else not patrol.pose_stale(now_ms) and not patrol.localization.untrusted
        )
        if not located:
            return False, "로봇이 아직 자기 위치를 확인하지 못했다 — 위치를 먼저 잡아야 한다"
        validation = validate_route(route, patrol.navigation_grid, patrol.plan_params)
        if not validation["valid"]:
            return False, "동선이 벽·미관측 공간·로봇 여유 반경을 지난다 — 지점을 고쳐야 한다"
        first = route.points[0]
        if patrol.nav_params.relaxed_follow or patrol.nav_params.route_direct:
            # 현재→첫 점도 직선으로 갈 수 있어야 한다. 기억/동적 마스크는
            # 미리보기의 저장 지도 검증에 더하지 않고 실시간 거리로 다룬다.
            if not patrol.nav_params.relaxed_follow and not segment_clear(
                patrol.navigation_grid,
                inflate(patrol.navigation_grid, patrol.plan_params)
                | body_collision_mask(patrol.navigation_grid, patrol.plan_params),
                patrol.pose[:2],
                (first.x, first.y),
            ):
                return False, "첫 지점까지 직접 가는 선이 벽·미관측 공간을 지난다"
            self.cancel("replaced", hold=False)
            patrol._inspection_zone = None
            patrol._goal = (first.x, first.y)
            patrol._goal_hold = False
            patrol._goal_hold_reason = None
            patrol.plan, patrol.phase = Plan(GOAL_LABEL), Phase.PLANNING
            patrol._replan_stop_required = False
            patrol._replan_wait_started_ms = None
            patrol._spinning = False
        else:
            accepted, detail = patrol.goto(first.x, first.y, exact_goal=True)
            if not accepted:
                return False, detail
        self.current = route
        self.run += 1
        self.index = 0
        self.cycle = 1
        self.status = "active"
        patrol._relaxed_progress, patrol._relaxed_stuck = None, False
        patrol._relaxed_blocked_reported = False
        patrol.skipped = frozenset()
        self.skipped_indices = frozenset()
        self.stage = "moving"
        self.dwell_until_ms = None
        self.direct_stopped_ms = None
        self.direct_detour_start = None
        patrol._relaxed_leg_start = patrol.pose[:2]
        self.search_base = None
        patrol._relaxed_detouring = False
        patrol._relaxed_distance = math.inf
        patrol._relaxed_blocked_reported = False
        patrol._now_ms = now_ms
        patrol.commander.halt()
        return True, f"동선 ‘{route.name}’의 {len(route.points)}개 지점을 차례로 주행한다"

    def cancel(self, reason: str = "stopped", *, hold: bool = True) -> None:
        """동선과 대기를 취소한다. 완료·막힘도 자동 구역 순찰로 흘러가지 않는다."""
        patrol = self._patrol
        patrol._relaxed_escape_turn = False
        self.search_base = None
        patrol._relaxed_detouring = False
        patrol.avoidance.active = None
        patrol.recovery.active = None
        patrol.recovery.waiting = patrol.recovery.returning_home = False
        self.direct_stopped_ms = None
        self.direct_detour_start = None
        if not self.active:
            return
        self.status = reason
        self.stage = reason
        patrol._relaxed_progress, patrol._relaxed_stuck = None, False
        self.dwell_until_ms = None
        patrol._inspection_zone = None
        patrol._goal = None
        patrol._goal_hold = hold
        patrol._goal_hold_reason = f"route_{reason}" if hold else None
        patrol.plan = Plan(None)
        patrol.phase = Phase.PLANNING
        patrol.commander.halt()

    def point(self) -> RoutePoint:
        assert self.current is not None
        return self.current.points[self.index]

    def aim_error(self) -> float:
        aim = self.point().aim_deg
        return (
            0.0 if aim is None else wrap_pi(deg_to_rad(aim) - self._patrol.heading.steering_yaw())
        )

    def reset_dwell(self) -> None:
        if self.active and self.stage != "moving":
            self.stage = "aiming"
            self.dwell_until_ms = None
            self._patrol._inspection_zone = None
            self.search_base = None

    def search_person(self) -> None:
        """도착 방향의 좌우를 제자리 탐색한다. dwell 마감은 회전 중에도 유지한다."""
        patrol = self._patrol
        assert self.search_base is not None
        if patrol._now_ms >= (self.dwell_until_ms or 0):
            self.search_base = None
            patrol.commander.halt()  # 탐색 회전 의도가 다음 지점 첫 틱에 남지 않게 (10-06 현장 검수)
            self.next_point()
            return
        if (
            not patrol._local_scan.clear_allowed
            or patrol._local_scan.received_ms is None
            or not 0
            <= patrol._now_ms - patrol._local_scan.received_ms
            <= patrol.nav_params.scan_max_age_ms
        ):
            patrol.commander.halt()
            return
        extent = deg_to_rad(self.point().search_deg or 0.0)
        offsets = (extent, -extent, 0.0)
        # 짧은 dwell에서도 한쪽 회전만 하다 끝나지 않게 좌/우/중앙에 시간을 나눈다.
        duration = max(1, (self.dwell_until_ms or 0) - self.search_started_ms)
        slot = min(2, (patrol._now_ms - self.search_started_ms) * 3 // duration)
        self.search_index = max(self.search_index, slot)
        patrol.phase = Phase.INSPECT
        self.stage = "search"
        if patrol._now_ms < self.search_pause_until or self.search_index >= len(offsets):
            patrol.commander.halt()
            return
        error = wrap_pi(
            self.search_base + offsets[self.search_index] - patrol.heading.steering_yaw()
        )
        if abs(error) <= patrol.drive.heading_tolerance_rad:
            patrol.commander.halt()
            self.search_index += 1
            self.search_pause_until = patrol._now_ms + patrol.nav_params.route_search_pause_ms
            return
        # 회전 중에도 일정 간격으로 멈춰 새 카메라 프레임을 받는다.
        elapsed = patrol._now_ms - self.search_pause_until
        if elapsed >= patrol.nav_params.route_search_pause_ms:
            patrol.commander.halt()
            self.search_pause_until = patrol._now_ms + patrol.nav_params.route_search_pause_ms
            return
        limit = min(abs(patrol.drive.spin_turn_deg), patrol.nav_params.route_search_turn_deg)
        patrol.commander.drive(0.0, math.copysign(min(limit, rad_to_deg(abs(error))), error))

    def advance_arrival(self) -> None:
        patrol = self._patrol
        point = self.point()
        if self.search_base is not None:
            self.search_person()
            return
        if not patrol._relaxed_route and (
            math.dist(patrol.pose[:2], (point.x, point.y)) >= patrol.drive.arrival_radius_m
        ):
            # 경보·측위 복구 뒤 다른 자리에 있으면 먼저 같은 지점으로 다시 접근한다.
            self.stage = "moving"
            self.dwell_until_ms = None
            patrol._inspection_zone = None
            patrol.phase = Phase.PLANNING
            patrol.plan = Plan(GOAL_LABEL)
            patrol.commander.halt()
            return
        error = self.aim_error()
        if abs(error) > patrol.drive.heading_tolerance_rad:
            self.stage = "aiming"
            self.dwell_until_ms = None
            patrol._inspection_zone = None
            patrol.phase = Phase.AIMING
            steering = self._steering_for(error, patrol.drive, spinning=True)
            patrol._spinning = True
            patrol.commander.drive(
                0.0,
                clamp(
                    steering.angle_deg,
                    -abs(patrol.drive.spin_turn_deg),
                    abs(patrol.drive.spin_turn_deg),
                ),
            )
            return
        patrol.commander.halt()
        patrol._spinning = False
        patrol.phase = Phase.INSPECT
        if self.dwell_until_ms is None:
            self.stage = "dwell"
            self.dwell_until_ms = patrol._now_ms + int((point.dwell_s or 0.0) * 1000)
            if patrol.nav_params.route_person_search and point.search_deg:
                self.search_base = patrol.heading.steering_yaw()
                self.search_index = 0
                self.search_started_ms = patrol._now_ms
                self.search_pause_until = patrol._now_ms + patrol.nav_params.route_search_pause_ms
                self.stage = "search"
            # 영역 지도가 있으면 이름 없는 동선 점도 실제 소속 구역을 점검한다.
            # 지도 없는 기존 동선은 명시한 앵커 라벨만 사용한다.
            patrol._inspection_zone = (
                patrol.current_zone
                if patrol.zone_map is not None
                else point.label
                if point.label in patrol.zones.labels
                else None
            )
            # 적어도 한 틱 동안 도착 상태를 공개하여 다음 카메라 프레임이 소비하게 한다.
            return
        if patrol._now_ms < self.dwell_until_ms:
            return
        label = patrol._inspection_zone
        if (
            label is not None
            and patrol.wait_for_inspection is not None
            and patrol.wait_for_inspection(label)
        ):
            self.stage = "inspection"
            return
        self.next_point()

    def next_point(self) -> None:
        patrol = self._patrol
        route = self.current
        assert route is not None
        if patrol._inspection_zone is not None:
            patrol.stats.zones_visited += 1
        index = self.index + 1
        if index == len(route.points):
            patrol.stats.cycles += 1
            if route.repeat and self.cycle >= route.repeat:
                self.cancel("completed")
                return
            index = 0
            self.cycle += 1
            patrol.skipped = frozenset()
            self.skipped_indices = frozenset()
        patrol._relaxed_leg_start = (self.point().x, self.point().y)
        patrol._relaxed_distance = math.inf
        patrol._relaxed_blocked_reported = False
        self.index = index
        self.search_base = None
        patrol._relaxed_detouring = False
        point = route.points[index]
        # 동적 막힘은 다음 지점도 정지·스캔·우회/건너뛰기로 처리한다.
        patrol._goal = (point.x, point.y)
        patrol._goal_hold = False
        patrol._goal_hold_reason = None
        patrol.plan, patrol.phase = Plan(GOAL_LABEL), Phase.PLANNING
        self.stage = "moving"
        self.dwell_until_ms = None
        self.direct_stopped_ms = None
        self.direct_detour_start = None
        patrol._spinning = False

    @property
    def direct_moving(self) -> bool:
        patrol = self._patrol
        return (
            patrol.nav_params.route_direct
            and self.active
            and self.stage == "moving"
            and patrol._goal is not None
            and not patrol.recovery.returning_home
        )

    def direct_obstacle_stop(self) -> None:
        """장애물 정지만 계시한다. 측위/스캔/안전 상실은 회피 자격이 아니다."""
        patrol = self._patrol
        if patrol._relaxed_route:
            self.direct_stopped_ms = None
            patrol._relaxed_escape_turn = False
            return
        if patrol.localization.untrusted:
            self.direct_stopped_ms = None
            return
        if not self.direct_moving or self.direct_detour_start is not None:
            return
        if self.direct_stopped_ms is None:
            self.direct_stopped_ms = patrol._now_ms
        stopped = patrol._stopped_since_ms
        if (
            patrol._last_sent_moving
            or stopped is None
            or patrol._now_ms - max(stopped, self.direct_stopped_ms)
            < patrol.nav_params.route_direct_stop_ms
        ):
            return
        self.direct_detour_start = patrol.pose[:2]
        # AO의 실제 끝점 통로를 먼저 사용하고, 없으면 기존 정지/스캔/우회로 간다.
        if not patrol.avoidance.start("lidar_corridor"):
            patrol.recovery.begin("route_direct_obstacle")

    def follow_direct(self) -> None:
        patrol = self._patrol
        point = self.point()
        target = (point.x, point.y)
        patrol.plan = Plan(
            GOAL_LABEL,
            (patrol.pose[:2], target),
            math.dist(patrol.pose[:2], target),
            effective=target,
        )
        patrol.phase = Phase.MOVING
        distance = patrol._local_scan.distance()
        if distance is None or distance <= patrol.nav_params.local_stop_m:
            patrol.commander.halt()
            patrol.avoidance.decide("stop", "local_stop_distance", distance)
            if distance is not None:
                self.direct_obstacle_stop()
            else:
                self.direct_stopped_ms = None
            return
        self.direct_stopped_ms = None
        if math.dist(patrol.pose[:2], target) < patrol.drive.arrival_radius_m:
            patrol._arrive(GOAL_LABEL)
            return
        heading = math.atan2(point.y - patrol.pose[1], point.x - patrol.pose[0])
        error = wrap_pi(heading - patrol.heading.steering_yaw())
        steering = self._steering_for(error, patrol.drive, spinning=patrol._spinning)
        patrol._spinning = steering.step_mm == 0 and steering.angle_deg != 0
        if patrol._edge.changed("spin", patrol._spinning) and patrol._spinning:
            LOG.info("spin_in_place", error_deg=round(rad_to_deg(error), 1), target=GOAL_LABEL)
        scale = min(
            1.0,
            (distance - patrol.nav_params.local_stop_m)
            / (patrol.nav_params.local_slow_m - patrol.nav_params.local_stop_m),
        )
        patrol.avoidance.decide("slow" if scale < 1 else "clear", "route_direct", distance)
        limit = abs(patrol.drive.spin_turn_deg if patrol._spinning else patrol.drive.turn_deg)
        patrol.commander.drive(
            clamp(steering.step_mm * scale, 0.0, abs(patrol.drive.step_mm)),
            clamp(steering.angle_deg, -limit, limit),
        )

    def skip_point(self) -> None:
        patrol = self._patrol
        assert self.current is not None
        self.direct_stopped_ms = None
        patrol._inspection_zone = None
        label = self.point().label
        self.skipped_indices |= {
            i
            for i, p in enumerate(self.current.points)
            if i == self.index or (label is not None and p.label == label)
        }
        if len(self.skipped_indices) == len(self.current.points):
            patrol._goal = None
            patrol.recovery.return_home()
            return
        index = self.index + 1
        while index < len(self.current.points) and index in self.skipped_indices:
            index += 1
        if index == len(self.current.points):
            if all(
                (p.label or f"지점 {i + 1}") in patrol.skipped
                for i, p in enumerate(self.current.points)
            ):
                patrol._goal = None
                patrol.recovery.return_home()
                return
            if self.current.repeat and self.cycle >= self.current.repeat:
                self.cancel("completed_with_skips")
                return
            index = 0
            self.cycle += 1
            patrol.skipped = frozenset()
            self.skipped_indices = frozenset()
        self.index = index
        point = self.current.points[index]
        patrol._goal = (point.x, point.y)
        self.stage = "moving"
        self.dwell_until_ms = None
        patrol.plan, patrol.phase = Plan(GOAL_LABEL), Phase.PLANNING
