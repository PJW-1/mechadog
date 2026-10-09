"""구역 도착 — 도착 판정 뒤 조준·점검 준비·사이클 마감을 맡는다.

`PatrolController` 의 도착 쪽 상태를 여기에 모은다.

- **도착** — 찍은 곳·동선 지점이면 서서 기다리거나 조준으로, 구역이면 `aim_deg` 가 있을 때
  조준부터 한다 (`arrive`).
- **조준** — 도착 반경 안에 있는 동안만 제자리에서 돌아 구역 방위를 맞춘다 (`aim` →
  `steer_aim`). 반경 밖으로 밀리면 다시 접근한다.
- **점검 준비** — 카메라 점검을 시작해도 되는지 본다 (`ready`). 지금 점검 중인 구역은
  `zone` 이 쥔다.
- **마감** — 방문을 기록하고, 모든 구역을 돌았으면 사이클을 닫는다 (`finish` →
  `complete_cycle`).

자세·계획·방문 기록은 순찰기의 것을 그대로 쓰고 바꾼다. 상태 변경은 순찰 루프 스레드에서만
일어난다.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING

from host.behavior.patrol_phase import GOAL_LABEL, Phase
from host.behavior.planner import Plan
from host.common.logging_setup import event_logger
from host.common.protocol import clamp
from host.common.units import deg_to_rad, wrap_pi

if TYPE_CHECKING:
    from host.behavior.patrol import PatrolController, Steering

#: 순찰 컨트롤러와 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.behavior.patrol")


class ZoneArrival:
    """순찰기의 도착·조준·점검 준비 상태를 쥔다."""

    def __init__(self, patrol: PatrolController, steering_for: Callable[..., Steering]) -> None:
        self._patrol = patrol
        self._steering_for = steering_for
        #: 도착해 점검을 기다리는 구역 — 카메라에 넘기기 전까지 방문을 보존한다.
        self.zone: str | None = None

    def arrive(self, label: str) -> None:
        patrol = self._patrol
        patrol.commander.halt()
        patrol._spinning = False
        if label == GOAL_LABEL:
            if patrol.route.active:
                patrol.route.direct_stopped_ms = None
                patrol.route.direct_detour_start = None
                patrol.route.stage = "aiming"
                patrol.phase = Phase.AIMING
                return
            # 찍은 곳 — 구역 방문·점검이 아니다. 서서 기다린다.
            LOG.info("goal_reached", x=round(patrol.pose[0], 2), y=round(patrol.pose[1], 2))
            patrol._goal = None
            patrol._goal_hold = True
            patrol._goal_hold_reason = "reached"
            patrol.plan = Plan(None)
            patrol.phase = Phase.PLANNING
            return
        if patrol.zones.get(label).aim_deg is not None:
            patrol.phase = Phase.AIMING
            LOG.info("zone_aim_started", label=label, aim_deg=patrol.zones.get(label).aim_deg)
            return
        self.finish(label)

    def aim(self) -> None:
        """점검 전에만 돈다. step/steer의 기존 측위·장애물 관문을 통과한 뒤 호출된다."""
        patrol = self._patrol
        label = patrol.plan.label
        assert label is not None
        anchor = patrol.zones.xy(label)
        effective = patrol.plan.effective or anchor
        if min(math.dist(patrol.pose[:2], anchor), math.dist(patrol.pose[:2], effective)) >= (
            patrol.drive.arrival_radius_m
        ):
            # 측위가 도착 반경 밖으로 바뀌면 점검하지 않고 다시 접근한다.
            patrol.commander.halt()
            patrol._spinning = False
            patrol.phase = Phase.PLANNING
            patrol.plan = Plan(label)
            return
        if self.steer_aim(label):
            LOG.info("zone_aim_completed", label=label)
            self.finish(label)

    def aim_error(self, label: str) -> float:
        aim_deg = self._patrol.zones.get(label).aim_deg
        return (
            0.0
            if aim_deg is None
            else wrap_pi(deg_to_rad(aim_deg) - self._patrol.heading.steering_yaw())
        )

    def steer_aim(self, label: str) -> bool:
        patrol = self._patrol
        error = self.aim_error(label)
        if abs(error) <= patrol.drive.heading_tolerance_rad:
            patrol.commander.halt()
            patrol._spinning = False
            return True
        steering = self._steering_for(error, patrol.drive, spinning=True)
        patrol._spinning = True
        turn_limit = abs(patrol.drive.spin_turn_deg)
        patrol.commander.drive(0.0, clamp(steering.angle_deg, -turn_limit, turn_limit))
        return False

    def ready(self, label: str, now_ms: int) -> bool:
        """카메라 점검은 도착·조준 완료 뒤에만 시작한다 (느린 비전 프레임도 기다린다)."""
        patrol = self._patrol
        if patrol.route.active:
            point = patrol.route.point()
            return (
                patrol.phase is Phase.INSPECT
                and patrol.route.search_base is None
                and self.zone == label
                and (
                    patrol.relaxed.pose_available(now_ms)
                    if patrol.relaxed.active
                    else not patrol.pose_stale(now_ms)
                )
                and not patrol.safety.obstacle_active
                and (
                    patrol.relaxed.active
                    or math.dist(patrol.pose[:2], (point.x, point.y))
                    < patrol.drive.arrival_radius_m
                )
                and abs(patrol.route.aim_error()) <= patrol.drive.heading_tolerance_rad
            )
        return (
            patrol.phase is Phase.INSPECT
            and self.zone == label
            and not patrol.pose_stale(now_ms)
            and not patrol.safety.obstacle_active
            and abs(self.aim_error(label)) <= patrol.drive.heading_tolerance_rad
        )

    def finish(self, label: str) -> None:
        patrol = self._patrol
        patrol.phase = Phase.INSPECT
        self.zone = label
        patrol.visited = patrol.visited | {label}
        patrol.stats.zones_visited += 1
        patrol.plan = Plan(None)
        LOG.info("zone_arrived", label=label, cycle=patrol.cycle)
        if len(patrol.visited) >= len(patrol.zones):
            self.complete_cycle()

    def complete_cycle(self) -> None:
        patrol = self._patrol
        patrol.cycle += 1
        patrol.visited = frozenset()
        patrol.skipped = frozenset()
        patrol.stats.cycles += 1
        # 장애물은 재관측 시각 기준으로 만료한다. 사이클 경계가 실제 점을 지우지 않는다.
        LOG.info("cycle_completed", cycle=patrol.cycle)
