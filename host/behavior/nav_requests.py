"""관제의 항법 요청 — 위치 알려주기·지도 이동·저장 동선과 그 결과·항법 상태 묶음.

⚠️ **두 스레드가 만난다.** `ask_*` 는 대시보드 스레드가 부르고 예약만 한다. 길 찾기·측위 상태는
루프 스레드만 바꾸므로 적용은 다음 틱의 `drain` 이 한다. 예약 칸은 `lock` 으로 묶는다.
`nav_status` 는 루프가 틱마다 만든 한 시점의 묶음(`snapshot`)을 참조로만 읽는다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from host.behavior.fsm import Behavior
from host.behavior.patrol import PatrolController
from host.behavior.routes import Route, load_routes, route_digest
from host.behavior.zone_policy import ZonePpePolicy
from host.common.logging_setup import event_logger
from host.common.units import rad_to_deg

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class NavRequests:
    """관제가 넣은 항법 요청을 들고 있다가 루프 스레드에서 길 찾기에 넘긴다."""

    def __init__(
        self,
        *,
        navigator: Callable[[], PatrolController | None],
        behavior: Behavior,
        zone_ppe: ZonePpePolicy,
        clock: Callable[[], int],
        motion_lock: bool,
        record_input: Callable[..., None],
        reset_pending: Callable[[], bool],
        ask_patrol: Callable[[], None],
        drop_patrol: Callable[[], None],
    ) -> None:
        #: 길 찾기 — 호출 때 찾는다 (시험이 만든 뒤에 대역으로 바꿔 끼운다).
        self._navigator = navigator
        self._behavior = behavior
        self._zone_ppe = zone_ppe
        self._clock = clock
        self._motion_lock = motion_lock
        self._record_input = record_input
        self._reset_pending = reset_pending
        #: 순찰 예약은 런타임이 쥔다 — 이동 요청이 순찰 시작을 걸고, 정지가 그것을 버린다.
        self._ask_patrol = ask_patrol
        self._drop_patrol = drop_patrol
        #: 대시보드가 알려준 «지금 이 구역» — 서버 스레드가 넣고 루프가 꺼낸다.
        self.lock = threading.Lock()
        self._locate_asked: str | None = None
        self._locate_point_asked: tuple[float, float] | None = None
        #: 지도에서 찍은 목표 — 서버 스레드가 넣고 루프가 꺼낸다. 결과는 피드백으로.
        self.goto_asked: tuple[float, float] | None = None
        self.goal_feedback: dict[str, Any] | None = None
        self.route_asked: Route | None = None
        self.route_feedback: dict[str, Any] | None = None
        self._navigation_epoch = 0
        #: 루프가 틱마다 만든 항법 상태 묶음 (서버는 참조만 읽는다).
        self.snapshot: dict[str, Any] | None = None
        #: 사람이 순찰을 멈췄다(PATROL → IDLE) — 찍은 목표를 버린다 (루프에서).
        self._goal_cancel_asked = False

    def ask_goto(self, x: float, y: float) -> tuple[bool, str]:
        """지도에서 찍은 곳(순찰 좌표)을 예약한다. **다른 스레드에서 부른다.**"""
        if self._motion_lock:
            detail = "보행 잠금 중 — 이동할 수 없다"
            with self.lock:
                self.goal_feedback = {
                    "accepted": False,
                    "detail": detail,
                    "at_ms": self._clock(),
                }
            return False, detail
        self._record_input("ask_goto", x=x, y=y)
        navigator = self._navigator()
        if navigator is None or not hasattr(navigator, "goto"):
            return False, "LiDAR 측위 순찰이 아니라 지도 이동을 할 수 없다"
        with self.lock:
            self.goto_asked = (float(x), float(y))
            self.route_asked = None
            self.goal_feedback = None
        return True, "찍은 곳으로 갈 수 있는지 확인한다 — 결과는 지도에 표시된다"

    def ask_route(self, route_id: str, expected_digest: str | None = None) -> tuple[bool, str]:
        """명시적으로 고른 저장 동선을 읽고 루프에 검증·시작을 예약한다."""
        navigator = self._navigator()
        if not isinstance(navigator, PatrolController) or navigator.maps_dir is None:
            return False, "LiDAR 지도와 동선 저장소가 연결되지 않았다"
        if self._motion_lock:
            return False, "보행 잠금 중에는 동선을 시작할 수 없다"
        if self._behavior.state not in ("IDLE", "PATROL") or self._reset_pending():
            return False, "대기 또는 순찰 상태에서만 동선을 시작할 수 있다"
        with self.lock:
            if self._goal_cancel_asked:
                return False, "정지를 처리하고 있다 — 정지 완료 뒤 동선을 시작해야 한다"
            epoch = self._navigation_epoch
        try:
            route = load_routes(navigator.maps_dir).get(route_id)
        except (OSError, ValueError) as exc:
            LOG.warning("route_load_failed", error=type(exc).__name__)
            return False, "저장 동선을 읽을 수 없다"
        if route is None:
            return False, "저장된 동선을 찾을 수 없다"
        if expected_digest is not None and route_digest(route) != expected_digest:
            return False, "동선이 변경되었다 — 다시 불러와 확인한 뒤 시작해야 한다"
        snapshot = self.snapshot
        if (
            snapshot is None
            or snapshot.get("stale")
            or (
                navigator._own_localization
                and not (snapshot.get("verified") or snapshot.get("seeded"))
            )
        ):
            return False, "로봇이 아직 자기 위치를 확인하지 못했다 — 위치를 먼저 잡아야 한다"
        with self.lock:
            if epoch != self._navigation_epoch:
                return False, "정지 요청으로 동선 시작을 취소했다"
            self.route_asked = route
            self.goto_asked = None
            self.route_feedback = None
        return True, "동선 주행을 예약했다 — 현재 위치와 경로 검증 결과는 지도에 표시된다"

    def ask_locate_zone(self, zone: str) -> tuple[bool, str]:
        """사람이 알려준 «지금 이 구역» 을 예약한다. **다른 스레드에서 부른다.**

        길 찾기·측위 상태는 루프 스레드만 바꾼다 — 적용은 다음 틱(`drain`).
        """
        self._record_input("ask_locate_zone", zone=zone)
        navigator = self._navigator()
        if navigator is None or not hasattr(navigator, "hint_zone"):
            return False, "LiDAR 측위 순찰이 아니라 위치를 알려줄 대상이 없다"
        known = navigator.locate_zone_ids()
        if zone not in known:
            return (
                False,
                f"구역 {zone!r} 이 없다 — 알려줄 수 있는 구역: {', '.join(known) or '없음'}",
            )
        with self.lock:
            self._locate_asked = zone
            self._locate_point_asked = None
        return True, f"구역 {zone} 안에서 위치를 다시 찾는다 — 찾을 때까지 로봇은 선다"

    def ask_locate_point(self, x: float, y: float) -> tuple[bool, str]:
        """사람이 찍은 현재 위치를 예약한다. 적용은 다음 틱이며 **다른 스레드에서 부른다.**"""
        navigator = self._navigator()
        if navigator is None or not hasattr(navigator, "hint_point"):
            return False, "LiDAR 측위 순찰이 아니라 위치를 알려줄 대상이 없다"
        accepted, detail = navigator.validate_hint_point(x, y)
        if not accepted:
            return False, detail
        with self.lock:
            self._locate_point_asked = (x, y)
            self._locate_asked = None
        return True, "찍은 점 주변에서 위치를 다시 찾는다 — 찾을 때까지 로봇은 선다"

    def cancel(self) -> None:
        """예약한 이동·동선·순찰 시작을 버리고 다음 틱에 목표를 취소하게 한다.

        세대(`_navigation_epoch`)를 올려, 동선 파일을 읽는 사이에 들어온 정지가 시작을 되살리지
        못하게 한다.
        """
        with self.lock:
            self._navigation_epoch += 1
            self._goal_cancel_asked = True
            self.route_asked = None
            self.goto_asked = None
            self._drop_patrol()

    def mark_goal_cancel(self, previous: str, target: str) -> None:
        """사람이 멈췄다 — `PATROL`→`MANUAL`/`IDLE`, `MANUAL`→`IDLE`. 경보·추적으로 잠시 나간 것은 아니다.

        수동 조종도 취소다 — 사람이 로봇을 옮긴 뒤 옛 목표로 걸어가면 안 된다. 표시만 하고 루프가 처리한다.
        """
        navigator = self._navigator()
        if (
            previous == "PATROL"
            and target == "IDLE"
            and isinstance(navigator, PatrolController)
            and navigator.goal_hold_reason == "blocked"
            and navigator.goal is None
        ):
            # Automatic failure completion keeps the result visible in nav status.
            return
        if (previous == "PATROL" and target in ("IDLE", "MANUAL")) or (
            previous == "MANUAL" and target == "IDLE"
        ):
            self.cancel()

    def drain(self, now_ms: int) -> None:
        """예약한 위치 힌트·취소·동선·이동을 길 찾기에 넘긴다. **루프 스레드에서만 부른다.**"""
        navigator = self._navigator()
        with self.lock:
            zone, self._locate_asked = self._locate_asked, None
            point, self._locate_point_asked = self._locate_point_asked, None
        if zone is not None and navigator is not None:
            navigator.hint_zone(zone, now_ms)
        if point is not None and navigator is not None:
            navigator.hint_point(*point, now_ms)
        with self.lock:
            goto, self.goto_asked = self.goto_asked, None
            route, self.route_asked = self.route_asked, None
        if self._goal_cancel_asked:
            self._goal_cancel_asked = False
            if navigator is not None and hasattr(navigator, "cancel_goal"):
                navigator.cancel_goal("patrol_stopped")
            # 정지를 누르기 전에 들어온 이동 요청·그것이 건 순찰 시작도 버린다.
            if goto is not None or route is not None or self.goal_feedback is not None:
                goto = None
                self._drop_patrol()
            route = None
        if route is not None and isinstance(navigator, PatrolController):
            if self._behavior.state not in ("IDLE", "PATROL") or self._reset_pending():
                accepted, detail = False, "상태가 바뀌어 동선 시작을 취소했다"
            else:
                accepted, detail = navigator.start_route(route, now_ms)
            self.route_feedback = {
                "accepted": accepted,
                "detail": detail,
                "route_id": route.id,
                "at_ms": now_ms,
            }
            if accepted and self._behavior.state != "PATROL":
                self._ask_patrol()
        if goto is not None and navigator is not None:
            accepted, detail = navigator.goto(*goto)
            self.goal_feedback = {
                "accepted": accepted,
                "detail": detail,
                "goal": [round(goto[0], 3), round(goto[1], 3)],
                "at_ms": now_ms,
            }
            LOG.info("goto_requested", accepted=accepted, detail=detail)
            if accepted and self._behavior.state != "PATROL":
                # 순찰 중이 아니면 순찰을 시작해야 길 찾기가 돈다 — 같은 예약 경로(리셋 정착 대기 포함).
                self._ask_patrol()

    def nav_status(self) -> dict[str, Any]:
        """관제 지도가 그릴 자기 위치·신뢰·목표. **다른 스레드에서 부른다.**

        루프가 틱마다 만든 한 시점의 묶음을 돌려준다 — 서버 스레드가 컨트롤러 필드를 하나씩 읽으면
        옛 좌표에 새 신뢰·구역·경로가 섞인다 (리뷰 지적).
        """
        snapshot = self.snapshot
        if snapshot is None:
            return {"available": self._navigator() is not None, "starting": True}
        return {
            **snapshot,
            "goal_feedback": self.goal_feedback,
            "route_feedback": self.route_feedback,
        }

    def build_snapshot(self, now_ms: int) -> dict[str, Any]:
        """루프 스레드에서 한 시점의 항법 상태를 묶는다."""
        navigator = self._navigator()
        if navigator is None:
            return {"available": False}
        x, y, yaw = navigator.pose
        goal = getattr(navigator, "goal", None)
        # 스캔이 오지 않아도 힌트의 유효기간은 흐른다. 상태 변경은 이 루프에서만 한다.
        navigator._zone_filter(now_ms)
        hint = getattr(navigator, "_zone_hint", None)
        point_hint = navigator._point_hint
        return {
            "available": True,
            "frame": "patrol",
            "pose": [round(x, 3), round(y, 3), round(rad_to_deg(yaw), 1)],
            "stale": bool(navigator.pose_stale(now_ms)),
            "verified": bool(getattr(navigator, "pose_verified", False)),
            "seeded": bool(getattr(navigator, "pose_seeded", False)),
            "phase": str(getattr(navigator, "phase", "")),
            "local_navigation": getattr(navigator, "local_status", {}),
            "blockage": getattr(navigator, "blockage_status", {}),
            "target": getattr(navigator, "target", None),
            "zone": self._zone_ppe.current_zone(now_ms),
            "ppe_required": list(self._zone_ppe.required(now_ms)),
            "goal": None if goal is None else [round(goal[0], 3), round(goal[1], 3)],
            "holding_goal": bool(getattr(navigator, "holding_goal", False)),
            "goal_hold_reason": getattr(navigator, "goal_hold_reason", None),
            "zone_hint": None if hint is None else hint[0],
            "point_hint": (
                None
                if point_hint is None
                else {
                    "x": point_hint[0],
                    "y": point_hint[1],
                    "radius": navigator.point_hint_radius_m,
                }
            ),
            "match_frac": round(float(getattr(navigator, "match_frac", 0.0)), 3),
            "path": [
                [round(px, 3), round(py, 3)]
                for px, py in getattr(getattr(navigator, "plan", None), "waypoints", ())
            ],
            "goal_feedback": self.goal_feedback,
            "route": navigator.route_status() if isinstance(navigator, PatrolController) else None,
            "route_feedback": self.route_feedback,
            "fsm": self._behavior.state,
        }
