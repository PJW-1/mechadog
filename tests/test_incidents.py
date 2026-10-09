"""사건 기록 단독 검증 — `host.report.incidents.IncidentLog` (Runtime 분할 10단계).

장면 사건·길 찾기 사건이 블랙박스·대시보드·이력 DB 로 가는 시나리오는 `test_runtime.py`·
`test_runtime_lidar.py`·`test_zone_region_policy.py` 가 `Runtime` 으로 본다. 여기서는 런타임
없이 순찰 한 판의 열고 닫기, 방문 구역, 전이·단계 사건, 프레임 보관, 색인 실패 격리를 본다.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from host.behavior.fsm import Event
from host.report.incidents import NAVIGATION_FRAMES_MAX, IncidentLog


class History:
    def __init__(self, *, fail_record: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.incidents: list[Any] = []
        self._fail_record = fail_record

    def open_run(self, robot_id: str, *, mode: str, started_at: int) -> str:
        self.calls.append(("open", robot_id, mode, started_at))
        return f"{robot_id}-{started_at}"

    def close_run(
        self, mission_id: str, *, ended_at: int, result: str, stop_reason: str | None
    ) -> bool:
        self.calls.append(("close", mission_id, ended_at, result, stop_reason))
        return True

    def visit_zone(self, mission_id: str, zone_id: str) -> bool:
        self.calls.append(("visit", mission_id, zone_id))
        return True

    def record_incident(self, row: Any) -> bool:
        if self._fail_record:
            raise OSError("disk full")
        self.incidents.append(row)
        return True


class Dashboard:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_event(self, payload: dict[str, Any]) -> int:
        self.events.append(payload)
        return len(self.events)


def _log(
    *, history: Any = None, dashboard: Any = None, zone: list[str | None] | None = None
) -> tuple[IncidentLog, SimpleNamespace, SimpleNamespace]:
    behavior = SimpleNamespace(state="IDLE")
    escalation = SimpleNamespace(
        level=SimpleNamespace(value="L0"),
        latched=False,
        reason=None,
        presentation=lambda: SimpleNamespace(warning=None),
    )
    played: list[str] = []
    speaker = SimpleNamespace(play=played.append, ppe_warning_key=lambda key: key)
    where = zone if zone is not None else [None]
    log = IncidentLog(
        device_id="mechdog-01",
        session_id="s",
        config_sha256="c",
        clock=lambda: 0,
        behavior=behavior,  # type: ignore[arg-type]
        escalation=escalation,  # type: ignore[arg-type]
        mission=SimpleNamespace(mode="factory"),  # type: ignore[arg-type]
        speaker=speaker,  # type: ignore[arg-type]
        zone_ppe=SimpleNamespace(),  # type: ignore[arg-type]
        zone_ids=frozenset({"A"}),
        zone=lambda: where[0],
        telemetry=dict,
        vision=None,
        recorder=None,
        dashboard=dashboard,
        history=history,
        blackbox=None,
        event_publisher=None,
    )
    return log, behavior, escalation


def test_run_opens_when_leaving_idle_and_patrol_stop_closes_as_stopped() -> None:
    history = History()
    log, _, _ = _log(history=history)
    log.track_run("PATROL", "PATROL_START", 100)
    assert log.mission_run == "mechdog-01-100"
    log.track_run("ALERT", "PERSON_FOUND", 150)  # 판 안의 전이는 판을 바꾸지 않는다
    with log.stopping_patrol():
        log.track_run("MANUAL", "MANUAL_ON", 200)
    assert log.mission_run is None
    log.track_run("PATROL", "PATROL_START", 300)
    log.track_run("MANUAL", "MANUAL_ON", 400)
    assert history.calls == [
        ("open", "mechdog-01", "factory", 100),
        ("close", "mechdog-01-100", 200, "stopped", "patrol_stop"),
        ("open", "mechdog-01", "factory", 300),
        ("close", "mechdog-01-300", 400, "manual", "MANUAL_ON"),
    ]


def test_shutdown_closes_only_an_open_run() -> None:
    history = History()
    log, _, _ = _log(history=history)
    log.close_run_on_shutdown()
    assert history.calls == []
    log.track_run("PATROL", "PATROL_START", 100)
    log.close_run_on_shutdown()
    assert history.calls[-1] == ("close", "mechdog-01-100", 0, "shutdown", "runtime_stopped")
    assert log.mission_run is None


def test_zone_visit_is_recorded_once_per_arrival_and_only_inside_a_run() -> None:
    history = History()
    zone: list[str | None] = ["A"]
    log, _, _ = _log(history=history, zone=zone)
    log.note_zone_visit()  # 판이 없으면 남기지 않지만 구역은 기억한다
    log.track_run("PATROL", "PATROL_START", 100)
    log.note_zone_visit()
    zone[0] = "B"
    log.note_zone_visit()
    log.note_zone_visit()
    assert [c for c in history.calls if c[0] == "visit"] == [("visit", "mechdog-01-100", "B")]


def test_history_failure_is_logged_not_raised() -> None:
    log, _, _ = _log(history=History(fail_record=True), dashboard=Dashboard())
    log.feed_event("auth_granted", 100)  # 색인 실패가 제어로 번지지 않는다


def test_transition_feed_names_auth_and_failsafe_only() -> None:
    dashboard = Dashboard()
    history = History()
    log, behavior, _ = _log(history=history, dashboard=dashboard)
    behavior.state = "PATROL"
    log.announce_transition(Event.AUTH_OK, "AUTH_WAIT", 100)
    log.announce_transition(Event.PERSON_FOUND, "PATROL", 110)
    behavior.state = "FAILSAFE"
    log.announce_transition(Event.LINK_LOST, "PATROL", 120)
    assert [(e["event"], e["previous"]) for e in dashboard.events] == [
        ("auth_granted", "AUTH_WAIT"),
        ("failsafe_entered", "PATROL"),
    ]
    assert [row.event_type for row in history.incidents] == ["auth_granted", "failsafe_entered"]


def test_escalation_feed_fires_on_level_or_latch_edges_only() -> None:
    dashboard = Dashboard()
    log, _, escalation = _log(dashboard=dashboard)
    log.announce_escalation(100)
    log.announce_escalation(200)
    escalation.latched = True
    log.announce_escalation(300)
    assert [(e["ts_ms"], e["escalation"]) for e in dashboard.events] == [(100, "L0"), (300, "L0")]


def test_navigation_frames_keep_the_first_frame_and_drop_the_oldest() -> None:
    log, _, _ = _log()
    first, later = SimpleNamespace(n=1), SimpleNamespace(n=2)
    log.keep_navigation_frame(7, None)
    assert log.navigation_frames == {}
    log.keep_navigation_frame(7, first)  # type: ignore[arg-type]
    log.keep_navigation_frame(7, later)  # type: ignore[arg-type]
    assert log.navigation_frames[7] is first
    for key in range(100, 100 + NAVIGATION_FRAMES_MAX):
        log.keep_navigation_frame(key, later)  # type: ignore[arg-type]
    assert 7 not in log.navigation_frames
    assert len(log.navigation_frames) == NAVIGATION_FRAMES_MAX
