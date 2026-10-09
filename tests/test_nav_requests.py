"""관제 항법 요청 단독 검증 — `host.behavior.nav_requests.NavRequests` (Runtime 분할 11단계).

지도 이동·저장 동선·정지가 실제 길 찾기·FSM 과 맞물리는 시나리오는 `test_command_api.py`·
`test_route_runtime.py`·`test_point_hint.py` 가 `Runtime` 으로 본다. 여기서는 런타임 없이
예약 → 다음 틱 적용, 취소가 예약과 순찰 시작을 함께 거두는 것, 잠금·스냅숏 계약을 본다.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from host.behavior.nav_requests import NavRequests


class Navigator:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def goto(self, x: float, y: float) -> tuple[bool, str]:
        self.calls.append(("goto", x, y))
        return True, "ok"

    def cancel_goal(self, reason: str) -> None:
        self.calls.append(("cancel", reason))

    def hint_zone(self, zone: str, now_ms: int) -> None:
        self.calls.append(("hint_zone", zone, now_ms))

    def locate_zone_ids(self) -> list[str]:
        return ["A", "B"]


def _requests(
    navigator: Any = None, *, state: str = "IDLE", motion_lock: bool = False
) -> tuple[NavRequests, dict[str, Any]]:
    seen: dict[str, Any] = {"inputs": [], "patrol": 0, "dropped": 0}

    def ask_patrol() -> None:
        seen["patrol"] += 1

    def drop_patrol() -> None:
        seen["dropped"] += 1

    requests = NavRequests(
        navigator=lambda: navigator,
        behavior=SimpleNamespace(state=state),  # type: ignore[arg-type]
        zone_ppe=SimpleNamespace(),  # type: ignore[arg-type]
        clock=lambda: 42,
        motion_lock=motion_lock,
        record_input=lambda action, **_fields: seen["inputs"].append(action),
        reset_pending=lambda: False,
        ask_patrol=ask_patrol,
        drop_patrol=drop_patrol,
    )
    return requests, seen


def test_goto_is_only_reserved_until_the_loop_drains_it() -> None:
    navigator = Navigator()
    requests, seen = _requests(navigator)
    assert requests.ask_goto(1.23456, 2)[0]
    assert navigator.calls == [], "서버 스레드는 길 찾기를 건드리지 않는다"
    requests.drain(100)
    assert navigator.calls == [("goto", 1.23456, 2.0)]
    assert requests.goal_feedback == {
        "accepted": True,
        "detail": "ok",
        "goal": [1.235, 2.0],
        "at_ms": 100,
    }
    assert seen["inputs"] == ["ask_goto"]
    assert seen["patrol"] == 1, "순찰 중이 아니면 순찰 시작을 예약한다"


def test_motion_lock_refuses_goto_with_feedback() -> None:
    requests, seen = _requests(Navigator(), motion_lock=True)
    accepted, detail = requests.ask_goto(1, 2)
    assert not accepted
    assert requests.goal_feedback == {"accepted": False, "detail": detail, "at_ms": 42}
    assert seen["inputs"] == []


def test_cancel_drops_pending_goto_and_its_patrol_start() -> None:
    navigator = Navigator()
    requests, seen = _requests(navigator)
    requests.ask_goto(1, 2)
    requests.cancel()
    assert requests.goto_asked is None
    assert seen["dropped"] == 1
    requests.drain(100)
    assert navigator.calls == [("cancel", "patrol_stopped")]
    assert seen["patrol"] == 0


def test_alarm_exit_is_not_a_cancel_but_patrol_stop_is() -> None:
    requests, seen = _requests(Navigator())
    requests.mark_goal_cancel("PATROL", "ALERT")
    assert seen["dropped"] == 0
    requests.mark_goal_cancel("MANUAL", "IDLE")
    assert seen["dropped"] == 1


def test_locate_zone_checks_known_zones_and_applies_next_tick() -> None:
    navigator = Navigator()
    requests, _ = _requests(navigator)
    assert not requests.ask_locate_zone("Z")[0]
    assert requests.ask_locate_zone("B")[0]
    requests.drain(300)
    assert navigator.calls == [("hint_zone", "B", 300)]


def test_status_without_navigator_or_snapshot() -> None:
    requests, _ = _requests(None)
    assert requests.nav_status() == {"available": False, "starting": True}
    assert requests.build_snapshot(0) == {"available": False}
    assert not requests.ask_goto(0, 0)[0]
    requests.snapshot = {"available": True}
    requests.route_feedback = {"accepted": True}
    assert requests.nav_status() == {
        "available": True,
        "goal_feedback": None,
        "route_feedback": {"accepted": True},
    }
