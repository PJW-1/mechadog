"""막힘 복구(`BlockageRecovery`) 단위 시험 — 시도·포기·알림을 순찰기 하나로 본다."""

from __future__ import annotations

from test_live_nav import ready

from host.behavior.blockage import Recovery
from host.behavior.patrol import HOME_LABEL, Phase


def test_report_is_sent_once_per_blockage_and_rate_limited_without_one() -> None:
    c = ready()
    recovery = c.recovery
    item = recovery.memory.remember(c.grid, [(2.6, 2.0)], 1000)
    assert item is not None
    attempt = Recovery("A", 1000, 0.0, item.id)
    recovery.report("obstacle_detour", attempt, target="A")
    recovery.report("obstacle_detour", attempt, target="A")
    (event,) = recovery.take_events()
    assert event["event"] == "obstacle_detour"
    assert event["judgement"]["blockage_id"] == item.id
    assert event["judgement"]["severity"] == "low"
    assert recovery.take_events() == (), "꺼내면 비워진다"
    recovery.report("patrol_unavailable", zone="전체")
    recovery.report("patrol_unavailable", zone="전체")
    assert [e["event"] for e in recovery.take_events()] == ["patrol_unavailable"]
    c._now_ms += c.nav_params.blockage_long_ms
    recovery.report("patrol_unavailable", zone="전체")
    (event,) = recovery.take_events()
    assert event["judgement"]["blockage_id"] is None and event["judgement"]["severity"] == "medium"


def test_wait_patrol_halts_remembers_blockages_and_reports_once() -> None:
    c = ready()
    recovery = c.recovery
    item = recovery.memory.remember(c.grid, [(2.6, 2.0)], 1000)
    assert item is not None
    recovery.returning_home = True
    recovery.wait_patrol()
    assert recovery.waiting and not recovery.returning_home
    assert recovery.retry_after_ms == c._now_ms + c.nav_params.blockage_long_ms
    assert recovery.wait_memory_ids == {item.id}
    assert c.plan.label is None and c.commander.intent.type_ == "STOP"
    recovery.wait_patrol()
    assert [e["event"] for e in recovery.take_events()] == ["patrol_unavailable"]


def test_return_home_plans_home_and_drops_the_goal() -> None:
    c = ready()
    c._goal = (4.0, 2.0)
    c.recovery.return_home()
    assert c.recovery.returning_home and c._goal is None
    assert c.plan.label == HOME_LABEL and c.phase is Phase.PLANNING


def test_status_summarises_the_attempt_and_the_controller_delegates() -> None:
    c = ready()
    assert c.blockage_status["recovery"] is None
    c.recovery.active = Recovery("A", 1000, 0.0, None, scanning=True)
    status = c.blockage_status
    assert status == c.recovery.status
    assert status["recovery"] == {
        "target": "A",
        "blockage_id": None,
        "scanning": True,
        "swept_deg": 0.0,
    }
    assert c._recovery is c.recovery.active, "런타임이 읽는 옛 이름"


def test_expire_ends_a_stuck_attempt_only_after_its_deadline() -> None:
    c = ready()
    c.recovery.begin("test")
    attempt = c.recovery.active
    assert attempt is not None and attempt.target == "A"
    assert c.expire_recovery(attempt.started_ms) is False
    assert c.recovery.active is attempt
    assert c.expire_recovery(attempt.started_ms + c.nav_params.recovery_scan_timeout_ms) is True
    assert c.recovery.active is None and c.skipped == {"A"}
    (event,) = c.take_navigation_events()
    assert event["event"] == "zone_skipped" and event["judgement"]["zone"] == "A"
