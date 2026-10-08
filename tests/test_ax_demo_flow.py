"""AX 시연 흐름: 가짜 시계/스캔/프레임으로 경보, 탐색, PPE, 우회를 검사한다."""

import math
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_lidar_patrol import Reading
from test_live_nav import revolution
from test_ppe_judge import _frame, _judge
from test_relaxed_follow import relaxed, tick
from test_route_runtime import route
from test_runtime import DEVICE, config
from test_runtime_lidar import _camera_aim_runtime, _camera_aim_tick, vision_result

from host.behavior.commander import Commander
from host.behavior.escalation import Escalation, Level
from host.behavior.fsm import Behavior, Event, Fsm
from host.behavior.mission import Mission
from host.behavior.routes import RoutePoint, load_routes, routes_content
from host.common.config import ConfigError, validate_base_config
from host.dashboard.state import DashboardState
from host.runtime import Runtime
from host.vision.ppe_detector import OK, VIOLATION, PpeVerdict

__all__ = ["config"]
pytestmark = pytest.mark.usefixtures("unlock_modes")


def alarm_behavior(cfg, *, enabled=True, state="ALERT"):
    escalation = Escalation(cfg)
    behavior = Behavior(
        Commander(), Fsm(initial=state), hold_on_latched_alarm=enabled, target_lost_ms=5000
    )
    behavior.set_alarm_guard(lambda: escalation.latched and escalation.level is Level.L3)
    behavior.register_sequence(state, lambda c, _now: c.drive(20, 30))
    behavior.note_target(1000)
    escalation.note_event("PERSON_DOWN", 1000)
    return behavior, escalation


@pytest.mark.parametrize("state", ["ALERT", "TRACK"])
def test_latched_alarm_holds_lost_target_and_all_motion(cfg, state):
    behavior, escalation = alarm_behavior(cfg, state=state)
    behavior.tick(7000)
    assert behavior.state == state
    assert behavior.commander.intent.type_ == "STOP"
    assert not behavior.event(Event.TARGET_LOST, 7100)
    assert not behavior.event(Event.PPE_SETTLED, 7100)
    assert not behavior.event(Event.HAZARD_ALARM, 7100)
    assert escalation.confirm_alarm(7200)
    assert behavior.event(Event.ALARM_CONFIRMED, 7200)
    assert behavior.state == "PATROL"


@pytest.mark.parametrize("event", [Event.ESTOP, Event.ONBOARD_FAILSAFE, Event.LINK_LOST])
def test_alarm_hold_preserves_safety_priority(cfg, event):
    behavior, _ = alarm_behavior(cfg)
    assert behavior.event(event, 1100)
    behavior.tick(1200)
    assert behavior.state == "FAILSAFE"
    assert behavior.commander.intent.type_ == "STOP"


def test_alarm_hold_link_watcher_still_runs(cfg):
    behavior, _ = alarm_behavior(cfg)
    behavior.note_telemetry(1000)
    behavior.tick(4000)
    assert behavior.state == "FAILSAFE"


def test_alarm_default_and_nonlatched_ppe_warning_do_not_hold(cfg):
    behavior, _ = alarm_behavior(cfg, enabled=False)
    behavior.tick(7000)
    assert behavior.state == "PATROL"
    escalation = Escalation(cfg)
    behavior = Behavior(Commander(), Fsm(initial="ALERT"), hold_on_latched_alarm=True)
    behavior.set_alarm_guard(lambda: escalation.latched)
    escalation.note_event("PPE_VIOLATION", 1000)
    assert escalation.level is Level.L3 and not behavior.alarm_holding
    assert behavior.event(Event.PPE_SETTLED, 2000)


def test_runtime_confirm_resumes_generic_alarm_and_cannot_clear_failsafe(config, clock):
    config = deepcopy(config)
    config["fsm"]["hold_on_latched_alarm"] = True
    runtime = Runtime(
        config, device_id=DEVICE, clock=clock, mission=Mission(config, mode="factory")
    )
    assert runtime.start_patrol(clock.ms)
    assert runtime._apply(Event.PERSON_FOUND, clock.ms)
    runtime._apply(Event.PERSON_DOWN, clock.ms)
    assert runtime.behavior.alarm_holding
    assert not runtime._apply(Event.TARGET_LOST, clock.ms + 6000)
    assert runtime.confirm_alarm(clock.ms + 6100)
    assert runtime.behavior.state == "PATROL"
    runtime._apply(Event.PERSON_FOUND, clock.ms + 6200)
    runtime._apply(Event.PERSON_DOWN, clock.ms + 6200)
    assert runtime._apply(Event.ESTOP, clock.ms + 6300)
    assert not runtime.confirm_alarm(clock.ms + 6400)
    assert runtime.escalation.level is Level.F


def search_controller(*, enabled=True, extent=60, dwell=8):
    c = relaxed(RoutePoint(x=2, y=2, aim_deg=0, dwell_s=dwell, search_deg=extent, label="A"))
    c.nav_params = replace(c.nav_params, route_person_search=enabled)
    tick(c, 1000)
    tick(c, 1100)
    return c


def test_search_turns_both_ways_with_camera_pauses_and_timeout():
    c = search_controller()
    tick(c, 2100)
    assert c.commander.intent.fields["step"] == 0
    assert 0 < c.commander.intent.fields["angle"] <= 10
    tick(c, 3100)
    assert c.commander.intent.type_ == "STOP"
    tick(c, 4100)
    assert c.commander.intent.fields["step"] == 0
    assert c.commander.intent.fields["angle"] < 0
    assert not c.inspection_ready("A", 4100), "구역 점검이 탐색을 가로채지 않는다"
    tick(c, 9100)
    assert c.route.status == "completed"


@pytest.mark.parametrize("enabled,extent", [(False, 60), (True, None), (True, 0)])
def test_search_requires_both_switch_and_point_opt_in(enabled, extent):
    c = search_controller(enabled=enabled, extent=extent)
    tick(c, 2100)
    assert c.commander.intent.type_ == "STOP"
    assert c.route.search_base is None


def test_person_response_finishes_search_on_patrol_resume():
    c = search_controller()
    tick(c, 2100)
    # Runtime의 ALERT→PATROL 훅이 호출하는 기존 resume 경로다.
    c.resume()
    assert c.route.status == "completed"
    assert c.commander.intent.type_ == "STOP"


def test_runtime_search_yields_to_person_found_and_ppe_judgement(config, clock):
    runtime, navigator, vision = _camera_aim_runtime(config, clock)
    navigator.nav_params = replace(navigator.nav_params, route_person_search=True)
    navigator.observe_map_pose((1, 1, math.pi / 2), clock.ms)
    assert navigator.start_route(
        route(RoutePoint(x=1, y=1, aim_deg=90, dwell_s=8, search_deg=60, label="A")), clock.ms
    )[0]
    for _ in range(3):
        _camera_aim_tick(runtime, navigator, vision, clock, yaw=math.pi / 2)
    assert navigator.route.stage == "search"
    assert runtime.behavior.state == "PATROL"
    clock.advance(100)
    navigator.observe_map_pose((1, 1, math.pi / 2), clock.ms)
    navigator.observe_obstacle_scan(revolution(clock.ms), clock.ms)
    frame = vision_result(
        clock.ms, clock.ms, present=True, hits=3, last_seen_ms=clock.ms, box=(100, 200, 200, 400)
    )
    vision.result = replace(frame, ppe=PpeVerdict(1, VIOLATION, confirmed=True))
    runtime.tick(clock.ms)
    assert runtime.behavior.state == "ALERT"
    assert runtime.escalation.level is Level.L3 and not runtime.escalation.latched
    assert runtime.commander.intent.fields["step"] == 0


@pytest.mark.parametrize("gate", ["ultrasonic", "estop", "stale_scan", "stale_pose"])
def test_search_retains_motion_guards(gate):
    c = search_controller()
    if gate == "ultrasonic":
        tick(c, 2100, obstacle=True)
    elif gate == "estop":
        c.emergency_stop("test")
        tick(c, 2100)
    else:
        c.observe_telemetry(Reading(), 3101)
        if gate == "stale_scan":
            c.observe_map_pose((2, 2, 0), 3101)
        c.step(3101)
    assert c.commander.intent.type_ == "STOP"


def recheck_judge(cfg, *, enabled=True, warnings=2):
    cfg = deepcopy(cfg)
    cfg["escalation"].update(ppe_recheck=enabled, ppe_recheck_max_warnings=warnings)
    judge = _judge(cfg)
    escalation = Escalation(cfg)
    events = []
    records = []

    def apply(event, now):
        events.append(event)
        escalation.note_event(event.name, now)
        if event is Event.PPE_SETTLED:
            judge._behavior.state = "PATROL"
        return True

    judge._apply = apply
    judge._escalation = escalation
    judge._record = lambda event, _result, judgement: records.append((event, judgement))
    judge.set_requirements(("vest",))  # 판정 자세가 필요 없는 실제 구역 정책
    frame = _frame()
    frame.ppe.required = ("vest",)
    frame.ppe.state = VIOLATION
    frame.ppe.confirmed = True
    judge.judge(frame, 1000)
    return judge, frame, events, records


def test_ppe_warning_rechecks_same_person_and_confirms_continuous_ok(cfg):
    judge, frame, events, records = recheck_judge(cfg)
    start = 1000 + judge._warning_ms
    judge.settle(start)
    assert Event.PPE_SETTLED not in events
    frame.ppe.state = OK
    judge.judge(frame, start)
    assert Event.PPE_SETTLED not in events
    for elapsed in range(100, judge._unknown_ms + 100, 100):
        judge.settle(start + elapsed)
        frame.completed_ms = start + elapsed
        judge.judge(frame, start + elapsed)
    assert events[-1] is Event.PPE_SETTLED
    assert records[-1][0] == "PPE_SETTLED"
    assert records[-1][1]["reason"] == "착용 확인"
    assert records[-1][1]["rechecked"] is True


def test_ppe_repeat_warning_is_bounded(cfg):
    judge, frame, events, _ = recheck_judge(cfg)
    start = 1000 + judge._warning_ms
    judge.settle(start)
    judge.judge(frame, start)
    assert events.count(Event.PPE_VIOLATION) == 2
    judge.settle(start + judge._warning_ms)
    assert events[-1] is Event.PPE_SETTLED
    assert judge._behavior.state == "PATROL"


def test_one_warning_limit_still_rechecks_but_does_not_warn_again(cfg):
    judge, frame, events, records = recheck_judge(cfg, warnings=1)
    start = 1000 + judge._warning_ms
    judge.settle(start)
    assert Event.PPE_SETTLED not in events
    judge.judge(frame, start)
    assert events == [Event.PPE_VIOLATION, Event.PPE_SETTLED]
    assert records[-1][1]["reason"] == "경고 횟수 한도"


def test_latched_alarm_preempts_pending_ppe_recheck(cfg):
    judge, frame, events, _ = recheck_judge(cfg)
    judge._behavior.alarm_holding = True
    start = 1000 + judge._warning_ms
    judge.settle(start)
    frame.ppe.state = OK
    judge.judge(frame, start)
    assert events == [Event.PPE_VIOLATION]


@pytest.mark.parametrize("missing", ["none", "other_track", "old_frame"])
def test_recheck_does_not_accept_other_person_or_stale_verdict(cfg, missing):
    judge, frame, events, records = recheck_judge(cfg)
    start = 1000 + judge._warning_ms
    judge.settle(start)
    frame.ppe.state = OK
    if missing == "none":
        frame.ppe = None
    elif missing == "other_track":
        frame.ppe.track_id = 2
    else:
        frame.completed_ms = start - 1
    judge.judge(frame, start + 100)
    judge.settle(start + judge._lost_ms)
    assert events[-1] is Event.PPE_SETTLED
    assert not any(j.get("reason") == "착용 확인" for _, j in records)


def test_recheck_ok_window_resets_on_violation(cfg):
    judge, frame, events, _ = recheck_judge(cfg)
    start = 1000 + judge._warning_ms
    judge.settle(start)
    frame.ppe.state = OK
    judge.judge(frame, start)
    frame.ppe.state = VIOLATION
    frame.ppe.confirmed = False
    judge.judge(frame, start + judge._unknown_ms - 1)
    frame.ppe.state = OK
    judge.judge(frame, start + judge._unknown_ms)
    assert Event.PPE_SETTLED not in events


def test_recheck_disabled_keeps_warning_timeout(cfg):
    judge, _frame_value, events, _ = recheck_judge(cfg, enabled=False)
    judge.settle(1000 + judge._warning_ms)
    assert events == [Event.PPE_VIOLATION, Event.PPE_SETTLED]


def box_scan(now, distance, width, *, side=1):
    """정면 상자, 한쪽 벽 0.3m, 반대편 벽 0.8m인 합성 통로의 빔 교차."""
    full = revolution(now)
    points = []
    for angle, _ in full.points:
        x, y = math.cos(angle), math.sin(angle)
        hit = 3.0
        if x > 1e-9 and abs(distance * y / x) <= width / 2:
            hit = min(hit, distance / x)
        if abs(y) > 1e-9:
            wall = 0.8 if y * side > 0 else 0.3
            hit = min(hit, wall / abs(y))
        points.append((angle, hit))
    return replace(full, points=tuple(points))


@pytest.mark.parametrize("distance,width", [(0.6, 0.3), (0.7, 0.4), (0.8, 0.3), (0.8, 0.4)])
@pytest.mark.parametrize("side", [-1, 1])
def test_box_detour_walks_into_free_side_then_returns_to_goal(distance, width, side):
    c = relaxed()
    c.nav_params = replace(c.nav_params, relaxed_walk_detour=True, relaxed_detour_notice=True)
    c._relaxed_scan.params = c.nav_params
    events = []
    for now in (1100, 1200, 1300):
        c.observe_telemetry(Reading(), now)
        c.observe_map_pose((2, 2, 0), now)
        c.observe_obstacle_scan(box_scan(now, distance, width, side=side), now)
        c.step(now)
        assert c.commander.intent.type_ == "MOVE"
        assert c.commander.intent.fields["step"] > 0
        assert c.commander.intent.fields["angle"] * side > 0
        assert not c._relaxed_escape_turn
        events.extend(c.take_navigation_events())
    assert [e["event"] for e in events] == ["obstacle_detour"]
    tick(c, 1400, pose=(2.5, 2 + side * 0.2, 0))
    assert c.commander.intent.fields["step"] > 0
    assert c.commander.intent.fields["angle"] * side < 0
    assert not c.take_navigation_events()
    tick(c, 1500, pose=(4, 2, 0))
    tick(c, 1600, pose=(4, 2, 0))
    tick(c, 1700, pose=(4, 2, 0))
    assert c.route.status == "completed"


def test_detour_notice_requires_angle_and_near_target_obstacle(monkeypatch):
    c = relaxed()
    c.nav_params = replace(c.nav_params, relaxed_detour_notice=True)
    monkeypatch.setattr(c._relaxed_scan, "open_heading", lambda _target, **_kw: math.radians(35))
    tick(c, 1100)  # 빈 공간 또는 문틀의 먼 편향은 우회 방송이 아니다.
    assert not c.take_navigation_events()
    monkeypatch.setattr(c._relaxed_scan, "open_heading", lambda _target, **_kw: math.radians(10))
    tick(c, 1200, front=0.7)
    assert not c.take_navigation_events()


def test_fully_blocked_with_walk_detour_enabled_stops_and_reports_path_blocked():
    c = relaxed()
    c.nav_params = replace(c.nav_params, relaxed_walk_detour=True, relaxed_detour_notice=True)
    tick(c, 1100, distance=0.24)
    assert c.commander.intent.type_ == "STOP"
    assert [e["event"] for e in c.take_navigation_events()] == ["path_blocked"]


def test_wearing_confirmation_dashboard_and_unassigned_sound_key(config, clock):
    dashboard = DashboardState(DEVICE, stale_after_ms=3000, clock=clock)
    runtime = Runtime(config, device_id=DEVICE, clock=clock, dashboard=dashboard)
    played = []
    runtime.speaker.play = played.append
    runtime.incidents.record_scene(
        "PPE_SETTLED",
        SimpleNamespace(),
        {"state": OK, "track_id": 1, "reason": "착용 확인", "rechecked": True},
    )
    assert played == ["ppe_settled"]
    assert dashboard.events_since(0)[0][-1]["judgement"]["reason"] == "착용 확인"


@pytest.mark.parametrize("value", [-1, 61, True, "60", float("nan")])
def test_search_point_rejects_invalid_extent(value):
    with pytest.raises(ValidationError):
        RoutePoint(x=0, y=0, search_deg=value)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("fsm", "hold_on_latched_alarm", "true"),
        ("escalation", "ppe_recheck", 1),
        ("escalation", "ppe_recheck_max_warnings", 0),
        ("escalation", "ppe_recheck_max_warnings", True),
    ],
)
def test_demo_config_rejects_wrong_types(cfg, section, key, value):
    cfg = deepcopy(cfg)
    cfg[section][key] = value
    with pytest.raises(ConfigError):
        validate_base_config(cfg)


def test_search_extent_survives_route_save_load(tmp_path):
    expected = route(RoutePoint(x=0, y=0, dwell_s=8, aim_deg=-180, search_deg=60))
    (tmp_path / "routes.json").write_bytes(routes_content({expected.id: expected}))
    assert load_routes(tmp_path)[expected.id] == expected
