"""순찰 컨트롤러 — **규약 준수를 시험한다** (PROTOCOL.md 2절 · 3절).

합치기 전 코드가 위반하던 것들을 하나씩 못박는다. 회귀 시험이 아니라 **규약
시험**이다 — 여기 있는 각 항목은 문서의 한 줄에 대응한다.

시험이 실제 전문을 `CommandDecoder` 에 물리는 것이 요점이다. 의도만 확인하면
"컨트롤러는 옳은데 나가는 전문은 틀린" 상태를 잡지 못한다. **로봇이 보는 것과
같은 것을 본다.**
"""

from __future__ import annotations

import json
import math
import random

import numpy as np
import pytest

from host.behavior.commander import Commander
from host.behavior.patrol import (
    FSM_STATE_FOR,
    DriveParams,
    PatrolController,
    Phase,
    steering_for,
)
from host.behavior.planner import PlanParams
from host.behavior.zones import ZoneStore
from host.common.protocol import (
    CLAMP_RANGES,
    FSM_STATES,
    CommandDecoder,
    CommandEncoder,
    Verdict,
)
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.scan_match import MatchParams
from host.telemetry.receiver import Reading as TelemetryReading

DRIVE = DriveParams(
    step_mm=60.0,
    turn_deg=20.0,
    reverse_mm=200.0,
    heading_tolerance_rad=math.radians(8),
    reverse_threshold_rad=math.radians(90),
    spin_threshold_rad=math.radians(45),
    spin_turn_deg=30.0,
    arrival_radius_m=0.3,
    waypoint_radius_m=0.1,
    lidar_estop_m=0.15,
    link_loss_ms=3000,
    cmd_timeout_ms=300,
    pose_timeout_ms=500,
    scan_stall_timeout_ms=1500,
    settle_delay_ms=750,
)
PLAN = PlanParams(occ_thresh=1.0, free_thresh=-1.0, clearance_m=0.25, simplify_eps_m=0.08)
MATCH = MatchParams(
    search_lin_m=0.12,
    search_lin_step_m=0.04,
    search_ang_rad=math.radians(8),
    search_ang_step_rad=math.radians(2),
    occ_thresh=1.0,
    min_known_cells=50,
)


class Reading:
    """텔레메트리 관측. 규약의 필드 이름을 그대로 쓴다."""

    def __init__(self, **fields: object) -> None:
        self.state = fields.get("state", "PATROL")
        self.safety_latched = fields.get("safety_latched", False)
        self.obstacle = fields.get("obstacle", False)
        self.dist_cm = fields.get("dist_cm", 100)
        self.pitch = fields.get("pitch", 0.0)
        self.roll = fields.get("roll", 0.0)
        self.yaw = fields.get("yaw", 0.0)
        self.last_cmd_age_ms = fields.get("last_cmd_age_ms", 20)


def open_room() -> OccupancyGrid:
    """6m x 5m 빈 방. 벽만 막고 안쪽은 **확실히 빈** 셀로 채운다.

    0(미관측)으로 두면 `planner.inflate` 가 전부 막으므로 A* 가 아무 경로도
    못 찾는다 — 그 판단이 옳고, 시험은 관측된 지도를 줘야 한다.
    """
    meta = MapMeta(resolution=0.05, origin_x=0.0, origin_y=0.0, width=120, height=100)
    grid = OccupancyGrid(meta)
    grid.cells[:, :] = -3.0
    grid.cells[0, :] = 3.0
    grid.cells[-1, :] = 3.0
    grid.cells[:, 0] = 3.0
    grid.cells[:, -1] = 3.0
    return grid


def build(**overrides: object) -> PatrolController:
    ready = overrides.pop("ready", True)
    grid = overrides.pop("grid", None) or open_room()
    zones = ZoneStore(("A", "B", "C"))
    zones.place(1.0, 1.0)
    zones.place(4.0, 1.0)
    zones.place(4.0, 3.5)
    controller = PatrolController(
        commander=Commander(CommandEncoder()),
        grid=grid,
        zones=zones,
        drive=DRIVE,
        plan_params=PLAN,
        match_params=MATCH,
        range_m=(0.12, 8.0),
        new_obstacle_margin_m=0.25,
        new_obstacle_confirmations=2,
        new_obstacle_check_radius_m=1.5,
        obstacle_mark_radius_m=0.3,
        forward_fan_rad=math.radians(20),
        rng=random.Random(7),
        **overrides,
    )
    if ready:
        # 순찰/안전의 다른 단위 시험은 정지 안정화와 유효 스캔이 끝난 상태에서 시작.
        # 실제 수신 부재/재출발 경계는 ready=False로 아래에서 따로 검증한다.
        from host.common.lidar_link import Scan

        controller.note_sent([CommandEncoder().encode("STOP")], 0)
        controller.observe_obstacle_scan(Scan("lidar-a", "boot-a", 1, 1000, ((0.0, 3.0),)), 1000)
    return controller


def decode_all(lines: list[str]) -> list[dict]:
    """로봇이 보는 것과 **같은 검증**을 통과한 메시지만 돌려준다."""
    decoder = CommandDecoder()
    out: list[dict] = []
    for line in lines:
        result = decoder.decode(line)
        assert result.accepted, f"로봇이 폐기한다: {line} — {result.reason}"
        assert result.verdict is not Verdict.CLAMP, f"클램핑되어 나갔다: {line}"
        assert result.message is not None
        out.append(result.message)
    return out


# ══════════════════════════════════════════════════════════════
#  상태 사상 — PROTOCOL.md 2절 `STATE`
# ══════════════════════════════════════════════════════════════


def test_every_mapped_state_is_in_the_thirteen() -> None:
    """FSM 13종에 없는 이름을 내려보내면 로봇이 폐기 + WARN 한다.

    합치기 전 코드는 `MOVING`·`ROTATING`·`PLANNING`·`ARRIVED`·`ESTOP` 을 상태로
    쓰고 있었다 — 다섯 개 전부 규약에 없다.
    """
    for phase, state in FSM_STATE_FOR.items():
        assert state in FSM_STATES, f"{phase} -> {state}"


def test_every_phase_is_mapped() -> None:
    """빠진 단계가 있으면 그 단계에서 `KeyError` 로 루프가 죽는다."""
    for phase in Phase:
        assert phase in FSM_STATE_FOR


def test_avoid_is_never_announced() -> None:
    """⚠️ ADR-22 — `AVOID` 를 내려보내면 그 값이 되돌아와 **해제를 알 수 없다.**"""
    assert "AVOID" not in FSM_STATE_FOR.values()


# ══════════════════════════════════════════════════════════════
#  세션 개시 — PROTOCOL.md 2절 「Host 재시작」
# ══════════════════════════════════════════════════════════════


def test_first_datagram_is_stop_with_seq_one() -> None:
    """첫 전문이 이것이 아니면 로봇이 우리 명령을 **통째로 폐기한다.**"""
    controller = build()
    first = json.loads(controller.commander.open_session())
    assert first["type"] == "STOP"
    assert first["seq"] == 1
    assert isinstance(first["ts"], int), "ts 는 정수 밀리초다 (초 단위 실수가 아니다)"


def test_startup_waits_for_first_telemetry_without_latching() -> None:
    """첫 패킷이 수 ms 늦었다는 이유로 수동 RESET이 필요한 상태가 되면 안 된다."""
    controller = build()
    controller.start()
    controller.pose = (2.0, 2.0, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)
    assert controller.phase is Phase.PLANNING
    assert controller.commander.intent.type_ == "STOP"

    controller.observe_telemetry(Reading(), 1010)
    controller.step(1010)
    assert controller.phase is Phase.MOVING


def test_timestamps_are_integer_milliseconds() -> None:
    """합치기 전 코드는 `time.time()` 을 그대로 실어 규칙 ⑤ 로 폐기됐다."""
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller._last_pose_ms = 1000
    lines = list(controller.step(1000)) + controller.commander.tick(1000)
    for message in decode_all(lines):
        assert isinstance(message["ts"], int)
        assert isinstance(message["seq"], int)
        assert message["ts"] >= 0


# ══════════════════════════════════════════════════════════════
#  호(arc) 조향 + 폐루프 제자리 회전 — ADR-11 (개정 2026-10-01)
# ══════════════════════════════════════════════════════════════


def test_straight_when_aligned() -> None:
    steering = steering_for(0.0, DRIVE)
    assert steering.step_mm == DRIVE.step_mm
    assert steering.angle_deg == 0.0


def test_arc_steering_keeps_walking() -> None:
    """회전 임계(45도) 안쪽은 **걸으면서** 호로 돈다. 조향 부호는 오차 부호를 따른다."""
    for error_deg in (15, 30, 44, -15, -30, -44):
        steering = steering_for(math.radians(error_deg), DRIVE)
        assert steering.step_mm > 0.0, error_deg
        assert steering.angle_deg != 0.0, error_deg
        assert math.copysign(1, steering.angle_deg) == math.copysign(1, error_deg), error_deg


def test_large_error_spins_in_place() -> None:
    """회전 임계를 넘으면 **`step=0` 제자리 회전**이다 (ADR-11 개정 2026-10-01).

    `step=0 angle=±30` 은 실기에서 실제로 제자리에서 돈다(2026-09-22 실측 7.37 도/s).
    예전의 후진 호는 180° 를 도는 동안 경로를 벗어나 가구 모서리에서 E-STOP 이 났다.
    """
    # 180 은 wrap 으로 -180 이 되어 어느 쪽으로 돌아도 맞으므로 부호 검사에서 뺀다.
    for error_deg in (46, 90, 100, 170, -46, -90, -100, -170):
        steering = steering_for(math.radians(error_deg), DRIVE)
        assert steering.step_mm == 0.0, error_deg
        assert steering.angle_deg == math.copysign(DRIVE.spin_turn_deg, error_deg), error_deg


def test_spin_continues_until_aligned() -> None:
    """한번 돌기 시작하면 **허용 오차 안까지** 계속 돈다 — 임계 바로 아래에서 호로 바꾸지 않는다."""
    steering = steering_for(math.radians(30), DRIVE, spinning=True)
    assert steering.step_mm == 0.0
    assert steering.angle_deg > 0
    aligned = steering_for(math.radians(5), DRIVE, spinning=True)
    assert aligned.step_mm == DRIVE.step_mm
    assert aligned.angle_deg == 0.0


def test_controller_spins_then_walks() -> None:
    """웨이포인트가 뒤에 있으면 컨트롤러가 제자리 회전을 보내고, 방위가 맞으면 걷는다."""
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller.pose = (2.0, 2.0, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)  # 계획을 세운다
    waypoint = controller._current_waypoint()
    toward = math.atan2(waypoint[1] - 2.0, waypoint[0] - 2.0)

    controller.pose = (2.0, 2.0, toward + math.pi)  # 웨이포인트를 등지게 돌려 둔다
    controller._last_pose_ms = 1100
    controller.step(1100)
    move = [m for m in decode_all(controller.commander.tick(1100)) if m["type"] == "MOVE"]
    assert move and move[-1]["step"] == 0 and move[-1]["angle"] != 0

    controller.pose = (2.0, 2.0, toward)  # 방위를 맞춰 주면 걷는다
    controller._last_pose_ms = 1200
    controller.step(1200)
    move = [m for m in decode_all(controller.commander.tick(1200)) if m["type"] == "MOVE"]
    assert move and move[-1]["step"] > 0 and move[-1]["angle"] == 0


def test_steering_stays_inside_protocol_ranges() -> None:
    """규약 범위(±100mm · ±30deg) 안이어야 클램핑 없이 나간다 (규칙 ②)."""
    low_step, high_step = CLAMP_RANGES["step"]
    low_angle, high_angle = CLAMP_RANGES["angle"]
    for error_deg in range(-180, 181, 5):
        steering = steering_for(math.radians(error_deg), DRIVE)
        assert low_step <= steering.step_mm <= high_step, error_deg
        assert low_angle <= steering.angle_deg <= high_angle, error_deg


def test_move_commands_pass_the_robot_decoder() -> None:
    """실제로 나가는 `MOVE` 가 로봇의 검증을 통과한다."""
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller.pose = (2.0, 2.0, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)
    messages = decode_all(controller.commander.tick(1000))
    types = {message["type"] for message in messages}
    assert types <= {"MOVE", "STOP", "STATE"}, types


# ══════════════════════════════════════════════════════════════
#  안전 — 판정 주체 (아키텍처 1.2 불변 규칙)
# ══════════════════════════════════════════════════════════════


def test_onboard_latch_wins_over_host_plan() -> None:
    """로봇이 래치를 보고하면 호스트의 계획과 무관하게 정지한다."""
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(state="FAILSAFE", safety_latched=True), 1000)
    controller.step(1000)
    assert controller.phase is Phase.HALTED
    assert controller.fsm_state == "FAILSAFE"
    assert controller.commander.intent.type_ == "STOP"


def test_host_does_not_judge_the_ultrasonic_threshold() -> None:
    """⚠️ **초음파 25cm 는 Tier 1 이다.**

    합치기 전 코드는 `dist_cm` 을 보고 호스트가 E-STOP 을 걸었다. 그것은 Tier 1
    판정을 호스트로 옮기는 것이고, 호스트가 꺼지면 판정이 사라진다. 지금은
    로봇이 `flags.obstacle` 로 **보고했을 때만** 따라간다.
    """
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(dist_cm=5, obstacle=False), 1000)
    controller.pose = (2.0, 2.0, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)
    assert controller.stats.estops == 0, "호스트가 거리를 보고 판정했다"
    assert controller.phase is not Phase.HALTED


def test_reported_obstacle_holds_the_walk() -> None:
    """로봇이 반사 정지를 보고하면 의도를 정지로 내린다 — 흉내내지 않고 따라간다."""
    controller = build()
    controller.start()
    controller.pose = (2.0, 2.0, 0.0)
    controller._last_pose_ms = 1000
    controller.observe_telemetry(Reading(state="AVOID", obstacle=True), 1000)
    controller.step(1000)
    assert controller.commander.intent.type_ == "STOP"


def test_obstacle_release_is_read_from_the_flag() -> None:
    """`flags.obstacle` 이 거짓으로 돌아오면 순찰을 재개한다 (ADR-22)."""
    controller = build()
    controller.start()
    controller.pose = (2.0, 2.0, 0.0)  # 구역 위가 아니어야 이동 의도가 나온다
    controller._last_pose_ms = 1000
    controller.observe_telemetry(Reading(state="AVOID", obstacle=True), 1000)
    controller.step(1000)
    controller.observe_telemetry(Reading(state="PATROL", obstacle=False), 1100)
    controller._last_pose_ms = 1100
    controller.step(1100)
    assert controller.commander.intent.type_ == "MOVE"


def test_lidar_danger_sends_estop_not_stop() -> None:
    """`STOP` 은 일반 보행 정지이고 FAILSAFE 를 걸지 않는다 (규약 2절)."""
    from host.common.lidar_link import Scan

    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller._last_pose_ms = 1000
    close = Scan("lidar-a", "b", 1, 1000, ((0.0, 0.10),))
    line = controller.guard_scan(close)
    assert line is not None
    message = json.loads(line)
    assert message["type"] == "ESTOP"
    assert controller.phase is Phase.HALTED


def test_localization_failure_is_lost_not_estop() -> None:
    """자기 위치를 모르는 것은 위험이 아니라 능력의 상실이다 (FR-6.6)."""
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller._last_pose_ms = 1000
    controller.step(1000)
    controller.observe_telemetry(Reading(), 2000)
    controller.step(2000)  # pose_timeout_ms(500) 초과
    assert controller.phase is Phase.LOST
    assert controller.fsm_state == "LOST"
    assert controller.stats.estops == 0
    assert controller.commander.intent.type_ == "STOP"


def test_external_map_pose_recovers_only_after_all_guards_and_normalizes_yaw() -> None:
    controller = build()
    controller.phase = Phase.LOST
    controller.observe_map_pose((1.5, 2.5, 3 * math.pi), 2000)
    assert controller.pose == pytest.approx((1.5, 2.5, -math.pi))
    assert controller._last_pose_ms == 2000
    assert controller.phase is Phase.LOST
    controller.observe_telemetry(Reading(), 2000)
    controller.step(2000)
    assert controller.phase is Phase.MOVING


def test_external_obstacle_scan_does_not_refresh_pose_timeout() -> None:
    from host.common.lidar_link import Scan

    controller = build(ready=False)
    controller.observe_map_pose((2.0, 2.0, 0.0), 1000)
    scan = Scan("lidar-a", "boot-a", 1, 1400, ((0.0, 1.0),))
    controller.observe_obstacle_scan(scan, 1400)
    assert controller._last_pose_ms == 1000
    controller.observe_obstacle_scan(scan, 1600)
    assert controller._last_pose_ms == 1000
    assert controller.stats.scans == 2


def external_observation(controller: PatrolController, now_ms: int) -> None:
    controller.observe_telemetry(Reading(), now_ms)
    controller.observe_map_pose((2.0, 1.0, math.pi), now_ms)


def stationary_scan(controller: PatrolController, now_ms: int) -> None:
    from host.common.lidar_link import Scan

    controller.observe_obstacle_scan(
        Scan("lidar-a", "boot-a", now_ms + 1, now_ms, ((math.pi / 2, 3.0),)), now_ms
    )


def test_fresh_external_pose_without_any_scan_never_starts() -> None:
    controller = build(ready=False)
    controller.note_sent([controller.commander.open_session()], 0)
    controller.start()
    for now in (1000, 1600, 3000):
        external_observation(controller, now)
        controller.step(now)
        assert controller.phase is Phase.LOST
        assert controller.commander.intent.type_ == "STOP"
    assert controller.stats.scans == 0
    assert controller.stats.estops == 0
    assert controller.stats.lost == 1, "새 pose마다 LOST를 풀었다 다시 세면 안 된다"


def test_start_requires_a_scan_after_the_successful_stop_settles() -> None:
    controller = build(ready=False)
    controller.start()
    # STOP 의도만 만들어 놓고 송신에 실패했으면 안정화 시계가 시작되지 않는다.
    external_observation(controller, 1000)
    stationary_scan(controller, 1000)
    controller.note_sent([], 0)
    controller.step(1000)
    assert controller.commander.intent.type_ == "STOP"

    controller.note_sent([controller.commander.open_session()], 1100)
    stationary_scan(controller, 1849)
    external_observation(controller, 1850)
    controller.step(1850)
    assert controller.phase is Phase.LOST

    # 정확히 750ms 경계에 받은 스캔은 허용한다.
    stationary_scan(controller, 1850)
    controller.step(1850)
    assert controller.phase is Phase.MOVING
    assert controller.commander.intent.type_ == "MOVE"


def test_scans_may_pause_while_moving_but_cannot_release_the_next_stop() -> None:
    controller = build(ready=False)
    controller.note_sent([controller.commander.open_session()], 0)
    controller.start()
    external_observation(controller, 1000)
    stationary_scan(controller, 1000)
    controller.step(1000)
    controller.note_sent(controller.commander.tick(1000), 1000)

    # 실제 MOVE가 송신된 동안에는 4초 스캔 공백도 두절로 오판하지 않는다.
    external_observation(controller, 5000)
    controller.step(5000)
    assert controller.phase is Phase.MOVING
    assert controller.commander.intent.type_ == "MOVE"

    controller.commander.halt()
    controller.note_sent(controller.commander.tick(5000), 5000)
    external_observation(controller, 5100)
    controller.step(5100)
    assert controller.phase is Phase.LOST
    assert controller.commander.intent.type_ == "STOP"
    controller.note_sent(controller.commander.tick(5100), 5100)
    # 반복 STOP이 안정화 시계를 매번 초기화하면 영원히 재개할 수 없다.
    stationary_scan(controller, 5749)
    external_observation(controller, 5750)
    controller.step(5750)
    assert controller.phase is Phase.LOST
    stationary_scan(controller, 5750)
    controller.step(5750)
    assert controller.phase is Phase.MOVING


def test_old_stationary_scan_and_pose_only_recovery_remain_stopped() -> None:
    controller = build()
    controller.start()
    external_observation(controller, 2500)
    controller.step(2500)  # 마지막 스캔 1000 + 허용 1500ms의 경계
    assert controller.commander.intent.type_ == "MOVE"
    external_observation(controller, 2501)
    controller.step(2501)
    assert controller.phase is Phase.LOST
    external_observation(controller, 2600)
    controller.step(2600)
    assert controller.commander.intent.type_ == "STOP"
    stationary_scan(controller, 2600)
    controller.step(2600)
    assert controller.phase is Phase.MOVING


@pytest.mark.parametrize("value", [0.0, 3.0])
def test_current_unknown_or_occupied_cell_cannot_be_an_astar_escape(value: float) -> None:
    grid = open_room()
    grid.cells[grid.to_cell(2.0, 1.0)] = value
    controller = build(grid=grid)
    controller.start()
    external_observation(controller, 1000)
    controller.step(1000)
    assert controller.blocked[grid.to_cell(2.0, 1.0)]
    assert controller.phase is Phase.LOST
    assert controller.commander.intent.type_ == "STOP"
    assert controller.target is None


@pytest.mark.parametrize("xy", [(-0.1, 1.0), (6.1, 1.0), (2.0, -0.1), (2.0, 5.1)])
def test_current_pose_outside_map_stops_without_index_wrapping(xy: tuple[float, float]) -> None:
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller.observe_map_pose((*xy, 0.0), 1000)
    controller.step(1000)
    assert controller.phase is Phase.LOST
    assert controller.commander.intent.type_ == "STOP"


@pytest.mark.parametrize("points", [(), ((0.0, 0.01),), ((0.0, 10.0),)])
def test_empty_or_out_of_range_scans_do_not_release_start(
    points: tuple[tuple[float, float], ...],
) -> None:
    from host.common.lidar_link import Scan

    controller = build(ready=False)
    controller.note_sent([controller.commander.open_session()], 0)
    controller.start()
    external_observation(controller, 1000)
    controller.observe_obstacle_scan(Scan("lidar-a", "boot-a", 1, 1000, points), 1000)
    controller.step(1000)
    assert controller.phase is Phase.LOST
    assert controller.commander.intent.type_ == "STOP"


def test_a_fresh_scan_does_not_release_a_stale_pose() -> None:
    controller = build()
    controller.start()
    external_observation(controller, 1000)
    controller.observe_telemetry(Reading(), 1501)
    stationary_scan(controller, 1501)
    controller.step(1501)
    assert controller.phase is Phase.LOST
    assert controller.commander.intent.type_ == "STOP"


def test_reset_confirmation_does_not_bypass_the_stationary_scan_guard() -> None:
    controller = build(ready=False)
    controller.start()
    controller.note_sent([controller.emergency_stop("test")], 1000)
    controller.note_sent([controller.request_reset()], 1100)
    external_observation(controller, 2000)
    controller.step(2000)
    assert controller.phase is Phase.LOST
    assert controller.commander.intent.type_ == "STOP"
    stationary_scan(controller, 2000)
    controller.step(2000)
    assert controller.phase is Phase.MOVING


def test_onboard_obstacle_hold_requires_a_new_settled_scan_before_release() -> None:
    controller = build()
    controller.start()
    external_observation(controller, 1000)
    controller.step(1000)
    controller.note_sent(controller.commander.tick(1000), 1000)

    # 온보드가 멈췄다는 보고도 정지 구간이다. 이전 MOVE 상태로 우회하면 안 된다.
    controller.observe_telemetry(Reading(obstacle=True), 2000)
    controller.observe_map_pose((2.0, 1.0, math.pi), 2000)
    controller.step(2000)
    assert controller.commander.intent.type_ == "STOP"
    external_observation(controller, 2100)
    controller.step(2100)
    assert controller.phase is Phase.LOST
    stationary_scan(controller, 2750)
    external_observation(controller, 2750)
    controller.step(2750)
    assert controller.phase is Phase.MOVING


def test_telemetry_silence_halts_but_keeps_sending() -> None:
    """⚠️ **명령 송신을 멈추지 않는다.** 10Hz 송신이 곧 링크 신호다 (규약 1절)."""
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller._last_pose_ms = 1000
    controller.step(1000)
    controller.step(1000 + DRIVE.link_loss_ms + 1)
    assert controller.phase is Phase.HALTED
    lines = controller.commander.tick(1000 + DRIVE.link_loss_ms + 1)
    assert lines, "링크가 끊겨도 전문은 계속 나가야 한다"
    assert any(json.loads(line)["type"] == "STOP" for line in lines)


# ══════════════════════════════════════════════════════════════
#  안전 해제 — PROTOCOL.md 2절 「안전 정지와 해제」
# ══════════════════════════════════════════════════════════════


def test_reset_requires_a_human_and_sends_reset_safe() -> None:
    controller = build()
    controller.emergency_stop("시험")
    line = controller.request_reset()
    assert json.loads(line)["type"] == "RESET_SAFE"


def test_reset_waits_for_the_robot_to_confirm_release() -> None:
    """**패킷 수락과 실제 안전 해제는 다르다.**

    규약이 못박은 대로 `state == IDLE` 과 `safety_latched == false` 를 확인해야
    한다. 보내자마자 재개하면 래치가 걸린 로봇에게 `MOVE` 를 쏟아붓는다.
    """
    controller = build()
    controller.emergency_stop("시험")
    controller.request_reset()

    # 아직 래치가 걸려 있다고 보고 → 재개하지 않는다
    controller.observe_telemetry(Reading(state="FAILSAFE", safety_latched=True), 2000)
    controller.step(2000)
    assert controller.phase is Phase.HALTED

    # 로봇이 해제를 확인했다 → 그때 재개한다
    controller.observe_telemetry(Reading(state="IDLE", safety_latched=False), 2100)
    controller._last_pose_ms = 2100
    controller.step(2100)
    assert controller.phase is not Phase.HALTED


def test_reset_without_safety_latched_warns_that_it_cannot_verify() -> None:
    """구형 펌웨어에는 `safety_latched` 가 없어 **해제를 검증할 수 없다.**

    ⚠️ `state != FAILSAFE` 로 대신하면 안 된다 — 우리가 `FAILSAFE` 를 `STATE` 로
    내려보낸 뒤이므로 반향이 계속 `FAILSAFE` 로 돌아와 **영원히 해제되지
    않는다.** `AVOID` 와 정확히 같은 문제다 (ADR-22).

    그래서 사람의 확인(`request_reset` 호출)을 최종 근거로 삼고, 검증이
    불가능함을 로그로 남긴다.
    """
    controller = build()
    controller.emergency_stop("시험")
    controller.request_reset()
    controller.observe_telemetry(Reading(safety_latched=None, state="FAILSAFE"), 2000)
    controller._last_pose_ms = 2000
    controller.step(2000)
    assert controller.phase is not Phase.HALTED, "검증 불가라도 사람이 확인했으면 재개한다"


def test_estop_does_not_wait_for_the_tick() -> None:
    """`ESTOP` 은 즉시 나간다. 100ms 를 기다리게 만들면 안 되는 유일한 부류다."""
    controller = build()
    line = controller.emergency_stop("즉시")
    assert json.loads(line)["type"] == "ESTOP"
    assert controller.commander.intent.type_ == "STOP", "반복 의도도 정지로 내려야 한다"


# ══════════════════════════════════════════════════════════════
#  텔레메트리 읽기 — 규약 5절
# ══════════════════════════════════════════════════════════════


def test_yaw_comes_from_the_imu_object() -> None:
    """합치기 전 코드는 `tlm["yaw"]` 를 읽었다 — 규약에 없는 필드다.

    조용히 `None` 이 되어 IMU 보조가 내내 꺼져 있었다. 단위도 deg 이므로
    rad 로 바꿔야 한다.
    """
    controller = build()
    controller.observe_telemetry(Reading(pitch=1.0, roll=0.0, yaw=90.0), 1000)
    assert controller.safety.yaw_deg == 90.0
    assert controller.safety.yaw_rad == pytest.approx(math.pi / 2)


def test_missing_imu_yaw_is_none_not_zero() -> None:
    """0 으로 때우면 로봇이 정북을 보고 있다고 믿고 정합 중심을 잘못 잡는다."""
    controller = build()
    controller.observe_telemetry(Reading(pitch=1.0, roll=0.0, yaw=None), 1000)
    assert controller.safety.yaw_rad is None


def test_yaw_of_reads_the_real_receiver_reading() -> None:
    """이 파일의 목업이 아니라 **실제 리시버가 만드는 객체**를 넣는다.

    `host.telemetry.receiver.Reading` 은 `imu` 속성이 없고 평탄한 `yaw` 필드를
    쓴다 (`Reading.of` 가 전문의 `msg["imu"]["yaw"]` 를 여기 담는다). 목업만
    맞고 실기 경로(`serve_real`)는 계속 깨져 있는 상태를 이 시험이 잡는다.
    """
    controller = build()
    reading = TelemetryReading(
        device_id="mechdog-a",
        boot_id="boot-a-001",
        seq=1,
        state="PATROL",
        batt_v=8.0,
        dist_cm=100,
        tipped=False,
        lowbatt=False,
        link_ok=True,
        yaw=90.0,
    )
    controller.observe_telemetry(reading, 1000)
    assert controller.safety.yaw_deg == 90.0
    assert controller.safety.yaw_rad == pytest.approx(math.pi / 2)


def test_old_firmware_without_safety_latched_still_works() -> None:
    """`safety_latched` 는 하위 호환으로 추가된 필드다 — 없으면 `None` 이다."""
    controller = build()
    controller.observe_telemetry(Reading(safety_latched=None, state="PATROL"), 1000)
    assert controller.safety.latched is None
    controller.start()
    controller._last_pose_ms = 1000
    controller.step(1000)
    assert controller.phase is not Phase.HALTED


# ══════════════════════════════════════════════════════════════
#  순찰 순서 — FR-7.3
# ══════════════════════════════════════════════════════════════


def test_first_cycle_follows_the_configured_order() -> None:
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    # 구역 B 에 더 가까운 자리에서 시작한다 — 그래도 첫 사이클은 A 로 간다.
    controller.pose = (3.5, 1.5, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)
    assert controller.target == "A", "첫 사이클은 zones.ids 순서다"


def test_sequential_when_random_is_disabled() -> None:
    """`zones.random_after_first_cycle: false` 를 코드가 실제로 지킨다."""
    controller = build(random_after_first_cycle=False)
    controller.cycle = 3
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller.pose = (4.0, 3.5, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)
    assert controller.target == "A"


def test_blocked_zone_does_not_end_the_cycle_early() -> None:
    """⚠️ **막힌 구역 하나가 뒤에 남은 구역까지 건너뛰게 하면 안 된다.**

    첫 사이클에서 다음 구역(B)이 막혀 있으면 계획이 비었고, 방문한 구역이 있다는
    이유로 사이클이 끝나 **갈 수 있는 C 를 한 번도 안 갔다.**
    """
    grid = open_room()
    grid.cells[16:25, 76:85] = 3.0  # B(4.0, 1.0) 를 장애물로 덮는다
    controller = build(grid=grid)
    controller.start()
    controller.visited = frozenset({"A"})
    controller.observe_telemetry(Reading(), 1000)
    controller.pose = (2.0, 2.0, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)
    assert controller.target == "C"
    assert controller.cycle == 0, "C 를 두고 사이클을 끝냈다"


def test_arrival_marks_the_zone_and_moves_on() -> None:
    controller = build()
    controller.start()
    controller.observe_telemetry(Reading(), 1000)
    controller.pose = (1.0, 1.0, 0.0)
    controller._last_pose_ms = 1000
    controller.step(1000)  # A 를 목표로 잡는다 (이미 A 위에 있다)
    assert "A" in controller.visited
    assert controller.fsm_state == "ZONE_INSPECT"


def test_full_cycle_visits_every_zone_once() -> None:
    """세 구역을 다 돌면 사이클이 하나 올라간다."""
    controller = build()
    controller.start()
    # ⚠️ 시작 자세를 반드시 준다. 기본값 `(0, 0, 0)` 은 이 지도의 **벽 모서리**라
    # 팽창 때문에 사방이 막혀 A* 가 아무 경로도 못 찾는다 — 실기에서도 로봇을
    # 벽에 붙여 놓고 순찰을 시작하면 같은 일이 난다.
    controller.pose = (2.0, 2.0, 0.0)
    now = 1000
    for _ in range(400):
        controller.observe_telemetry(Reading(), now)
        controller._last_pose_ms = now
        controller.step(now)
        # 목표에 순간이동시켜 도착 판정만 본다 — 보행 모델은 별 시험이다.
        if controller.target is not None:
            goal = controller.zones.xy(controller.target)
            controller.pose = (goal[0], goal[1], controller.pose[2])
        now += 100
        if controller.stats.cycles >= 1:
            break
    assert controller.stats.cycles >= 1
    assert controller.stats.zones_visited >= 3


def test_dynamic_obstacle_does_not_change_the_map() -> None:
    """⚠️ 지나가는 사람을 지도에 벽으로 새기면 영원히 돌아간다."""
    controller = build()
    before = controller.grid.cells.copy()
    from host.behavior.planner import mark_obstacle

    mark_obstacle(controller._dynamic, controller.grid, (2.5, 2.5), 0.3)
    assert np.array_equal(controller.grid.cells, before), "지도는 그대로여야 한다"
    assert controller._dynamic.any(), "동적 마스크에만 찍혀야 한다"


# ══════════════════════════════════════════════════════════════
#  런타임 진입점 — `steer` · `resume` · `take_new_obstacles`
# ══════════════════════════════════════════════════════════════


def _steerable(now: int) -> PatrolController:
    controller = build()
    controller.resume()
    controller.observe_telemetry(Reading(), now)
    controller.pose = (2.0, 2.0, 0.0)
    controller._last_pose_ms = now
    return controller


def test_steer_walks_without_announcing_state() -> None:
    """런타임에서는 FSM 이 `STATE` 를 쥔다 — 길 찾기가 따로 알리면 둘이 서로 덮는다."""
    controller = _steerable(1000)
    controller.steer(1000)
    assert controller.target is not None
    assert controller.commander.intent.type_ == "MOVE"
    assert "STATE" not in [m["type"] for m in decode_all(controller.commander.tick(1000))]


def test_steer_leaves_the_latch_to_the_runtime() -> None:
    """래치는 FSM 이 `FAILSAFE` 로 처리한다 — `steer` 는 단계를 `HALTED` 로 옮기지 않는다."""
    controller = _steerable(1000)
    controller.observe_telemetry(Reading(safety_latched=True, state="FAILSAFE"), 1000)
    controller.steer(1000)
    assert controller.phase is not Phase.HALTED


def test_steer_halts_on_a_stale_pose() -> None:
    controller = _steerable(1000)
    controller.steer(1000 + DRIVE.pose_timeout_ms + 1)
    assert controller.commander.intent.type_ == "STOP"
    assert controller.stats.lost == 1


def test_steer_holds_while_the_onboard_obstacle_flag_is_up() -> None:
    controller = _steerable(1000)
    controller.observe_telemetry(Reading(obstacle=True), 1000)
    controller.steer(1000)
    assert controller.commander.intent.type_ == "STOP"


def test_resume_replans_from_here_to_the_same_unvisited_zone() -> None:
    """경보·점검에서 돌아오면 옛 경로를 버리고 지금 자리에서 같은 목표로 다시 푼다."""
    controller = _steerable(1000)
    controller.steer(1000)
    target = controller.target
    assert target is not None
    controller.pose = (3.0, 3.0, 0.0)
    controller.resume()
    assert controller.phase is Phase.PLANNING
    assert controller.target == target
    assert not controller.plan.reachable, "옛 경로는 버린다"
    controller.steer(1000)
    assert controller.target == target
    assert controller.plan.reachable
    assert target not in controller.visited


def test_new_obstacles_are_taken_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """확정된 장애물은 한 번만 꺼내진다 — 런타임이 경고를 두 번 내지 않게."""
    import host.behavior.patrol as patrol

    controller = _steerable(1000)
    controller.steer(1000)
    monkeypatch.setattr(patrol, "detect_new_obstacle", lambda *_args, **_kw: (2.5, 2.0))
    from host.common.lidar_link import Scan

    scan = Scan("lidar-01", "0" * 16, 1, 1000, ())
    controller._check_new_obstacle(scan)
    assert controller.take_new_obstacles() == ()  # 한 번으로는 확정하지 않는다
    controller._check_new_obstacle(scan)
    assert controller.take_new_obstacles() == ((2.5, 2.0),)
    assert controller.take_new_obstacles() == ()
