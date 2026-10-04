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
    # B(4.0, 1.0) 를 스냅 반경(plan_to snap_m=0.6m)보다 넓은 장애물로 덮는다 —
    # 주변에 자유 셀이 하나도 안 남아야 «도달 불가» 다. 좁으면 스냅이 옆 자리를
    # 찾아 B 가 도달 가능으로 바뀐다(그 경우 B 에 가는 게 의도된 새 동작이다).
    grid.cells[10:30, 65:95] = 3.0
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


def _detect_room() -> tuple[OccupancyGrid, np.ndarray]:
    """`detect_new_obstacle` 시험용 — 테두리만 벽인 6×5m 빈 방과 blocked 마스크."""
    from host.behavior.planner import inflate

    grid = open_room()
    blocked = inflate(grid, PLAN)
    return grid, blocked


def test_excluded_loc_leg_remains_a_live_lidar_obstacle() -> None:
    """계획에서 제외한 loc 셀에 실제 반사가 있으면 새 물체로 감지한다."""
    from host.behavior.planner import detect_new_obstacle, inflate

    grid, blocked = _detect_room()
    loc = OccupancyGrid(grid.meta)
    loc.cells[:, :] = grid.cells
    leg_row, leg_col = loc.to_cell(4.0, 2.5)
    loc.cells[leg_row, leg_col] = 3.0  # 측위 지도만 아는 다리
    # 로봇 (3.0,2.5) 동향 — 다리 방향 빔이 1.0m 에서 멈춘다.
    scan = ((0.0, 1.0), (math.pi / 2, 4.0), (math.pi, 4.0), (-math.pi / 2, 4.0))
    hit = detect_new_obstacle(
        (3.0, 2.5, 0.0),
        scan,
        grid,
        blocked,
        check_radius_m=1.5,
        margin_m=0.25,
        occ_thresh=1.0,
    )
    assert hit is not None, "항법 지도만 보면 다리는 «새 장애물»"
    blocked = inflate(grid, PLAN, obstacle_grid=loc)
    assert not blocked[leg_row, leg_col]
    assert (
        detect_new_obstacle(
            (3.0, 2.5, 0.0),
            scan,
            grid,
            blocked,
            check_radius_m=1.5,
            margin_m=0.25,
            occ_thresh=1.0,
        )
        is not None
    ), "loc가 아는 물체라도 계획에서 제외했으면 LiDAR 감지를 억제하지 않는다"


def test_loc_map_margin_absorbs_alignment_error() -> None:
    """다리의 지도 자리와 실제가 ~10cm 어긋나도 margin 이 흡수한다."""
    from host.behavior.planner import detect_new_obstacle, inflate

    grid, blocked = _detect_room()
    loc = OccupancyGrid(grid.meta)
    loc.cells[:, :] = grid.cells
    leg_row, leg_col = loc.to_cell(4.0, 2.5)
    loc.cells[leg_row, leg_col] = 3.0
    grid.cells[leg_row, leg_col] = 3.0  # nav도 아는 물체에만 기존 정렬 여유가 적용된다.
    blocked = inflate(grid, PLAN, obstacle_grid=loc)
    # 실제 다리는 지도 자리보다 10cm 가깝다 (정렬 오차).
    scan = ((0.0, 0.9),)
    assert (
        detect_new_obstacle(
            (3.0, 2.5, 0.0),
            scan,
            grid,
            blocked,
            check_radius_m=1.5,
            margin_m=0.25,
            occ_thresh=1.0,
        )
        is None
    )


def test_genuinely_new_obstacle_still_detected_with_loc_map() -> None:
    """어느 지도에도 없는 물체는 측위 지도를 써도 여전히 잡힌다."""
    from host.behavior.planner import detect_new_obstacle, inflate

    grid, blocked = _detect_room()
    loc = OccupancyGrid(grid.meta)
    loc.cells[:, :] = grid.cells
    # 북쪽 1.0m 의 정말 새 물체 — 다리는 동쪽에만 둔다.
    leg_row, leg_col = loc.to_cell(4.0, 2.5)
    loc.cells[leg_row, leg_col] = 3.0
    blocked = inflate(grid, PLAN, obstacle_grid=loc)
    scan = ((math.pi / 2, 1.0), (0.0, 1.0))
    hit = detect_new_obstacle(
        (3.0, 2.5, 0.0),
        scan,
        grid,
        blocked,
        check_radius_m=1.5,
        margin_m=0.25,
        occ_thresh=1.0,
    )
    assert hit is not None
    assert hit == pytest.approx((3.0, 3.5), abs=1e-6)


def test_controller_detects_excluded_legs_and_replans_without_erasing_originals() -> None:
    """정적 계획 예외가 실제 물체 감지·동적 충돌 마스크를 억제하지 않는다."""
    from host.behavior.planner import plan_to
    from host.common.lidar_link import Scan

    grid, loc = open_room(), open_room()
    leg = loc.to_cell(4.0, 2.5)
    loc.cells[leg] = 3.0
    controller = build(grid=grid, loc_grid=loc)
    controller.observe_map_pose((3.0, 2.5, 0.0), 1000)
    assert grid.cells[leg] < PLAN.free_thresh
    assert not controller.blocked[leg]
    plan = plan_to("A", (4.5, 2.5), (3.0, 2.5), grid, controller.blocked, PLAN)
    assert plan.reachable
    assert plan.length_m == pytest.approx(1.5)
    controller.plan = plan
    controller.phase = Phase.MOVING
    controller.commander.drive(40, 0)

    scan = Scan("lidar-01", "boot", 1, 1000, ((0.0, 1.0),))
    controller._check_new_obstacle(scan)
    controller._check_new_obstacle(scan)
    assert controller.take_new_obstacles() == ((4.0, 2.5),)
    assert controller._dynamic[leg] and controller.blocked[leg]
    assert controller.phase is Phase.PLANNING and controller.commander.intent.type_ == "STOP"
    controller._obstacles.append((2.0, 2.0))
    controller._rebuild_masks()
    assert controller._clear_dynamic("test")
    assert not controller.blocked[leg]
    assert loc.cells[leg] == 3.0, "동적 복구도 원본 loc 증거를 지우지 않는다"


def test_steer_escapes_tracking_margin_but_stops_on_body_overlap():
    grid = open_room()
    grid.cells[:, 40] = 5.0
    controller = build(grid=grid)
    controller.zones = ZoneStore(("A",))
    controller.zones.place(4.0, 2.0)
    start = grid.to_world(40, 44)
    controller.resume()
    controller.observe_map_pose((*start, 0.0), 1000)
    controller.steer(1000)
    assert controller.phase is Phase.MOVING
    assert controller.plan.escape_end_index > 0
    assert controller.plan.waypoints[0] == start
    assert controller.commander.intent.type_ == "MOVE"
    controller.observe_map_pose((*grid.to_world(40, 42), 0.0), 1100)
    controller.steer(1100)
    assert controller.phase is Phase.LOST and controller.commander.intent.type_ == "STOP"


def test_confirmed_leg_waits_for_actual_stop_and_new_settled_scan_before_replan():
    from host.common.lidar_link import Scan

    controller = build()
    controller.zones = ZoneStore(("A",))
    controller.zones.place(4.5, 2.5)
    controller.observe_map_pose((3.0, 2.5, 0.0), 1000)
    controller.phase = Phase.MOVING
    controller.note_sent([CommandEncoder().encode("MOVE", step=40, angle=0)], 900)
    scan = Scan("lidar-01", "boot", 1, 1000, ((0.0, 1.0),))
    controller.observe_obstacle_scan(scan, 1000)
    controller.observe_obstacle_scan(scan, 1010)
    controller.steer(1010)
    assert controller.phase is Phase.LOST and controller.commander.intent.type_ == "STOP"
    controller.note_sent([CommandEncoder().encode("STOP")], 1020)
    controller.observe_map_pose((3.0, 2.5, 0.0), 1200)
    controller.steer(1200)
    assert controller.phase is Phase.LOST and controller.commander.intent.type_ == "STOP"
    controller.observe_map_pose((3.0, 2.5, 0.0), 1800)
    controller.observe_obstacle_scan(Scan("lidar-01", "boot", 2, 1800, ((0.0, 1.0),)), 1800)
    controller.steer(1800)
    assert controller.phase is Phase.MOVING and controller.commander.intent.type_ == "MOVE"
    assert not controller._replan_stop_required


def test_new_loc_hit_before_mask_rebuild_is_still_detected() -> None:
    """라이브 loc 적분과 주기적 팽창 사이에도 양쪽에서 다리가 빠지는 틈이 없다."""
    from host.common.lidar_link import Scan

    grid, loc = open_room(), open_room()
    controller = build(grid=grid, loc_grid=loc)
    controller.observe_map_pose((3.0, 2.5, 0.0), 1000)
    leg = loc.to_cell(4.0, 2.5)
    loc.cells[leg] = 3.0
    assert not controller.blocked[leg]
    scan = Scan("lidar-01", "boot", 1, 1000, ((0.0, 1.0),))
    controller._check_new_obstacle(scan)
    controller._check_new_obstacle(scan)
    assert controller.take_new_obstacles() == ((4.0, 2.5),)
    assert controller.blocked[leg]


def test_new_static_loc_obstacle_invalidates_the_existing_route() -> None:
    from host.behavior.planner import plan_to

    grid, loc = open_room(), open_room()
    grid.cells[grid.to_cell(4.0, 2.5)] = 0.0  # free 예외가 아닌 점유 추가는 기존 경로를 폐기한다.
    controller = build(grid=grid, loc_grid=loc)
    controller.observe_map_pose((3.0, 2.5, 0.0), 1000)
    controller.zones = ZoneStore(("A",))
    controller.zones.place(4.5, 2.5)
    controller.plan = plan_to("A", (4.5, 2.5), (3.0, 2.5), grid, controller.blocked, PLAN)
    original = controller.plan
    controller.phase = Phase.MOVING
    controller._rebuild_masks()
    assert controller.plan is original, "변하지 않은 마스크로 매번 경로를 버리지는 않는다"

    loc.cells[loc.to_cell(4.0, 2.5)] = 3.0
    controller._rebuild_masks()

    assert controller.phase is Phase.PLANNING
    assert controller.target == "A" and not controller.plan.reachable
    assert controller.commander.intent.type_ == "STOP"
    controller.steer(1000)
    assert controller.plan.reachable
    assert controller.plan.length_m > original.length_m


def test_tracking_cannot_follow_an_old_route_from_loc_clearance() -> None:
    """계획 후 신선한 자세가 loc 충돌 영역에 들어가도 매 틱 관문에서 정지한다."""
    grid, loc = open_room(), open_room()
    loc.cells[loc.to_cell(3.0, 2.0)] = 3.0
    grid.cells[grid.to_cell(3.0, 2.0)] = 3.0
    controller = build(grid=grid, loc_grid=loc)
    controller.resume()
    controller.observe_map_pose((2.0, 2.0, 0.0), 1000)
    controller.steer(1000)
    assert controller.plan.reachable
    controller.observe_map_pose((3.0, 2.0, 0.0), 1000)

    controller.steer(1000)

    assert controller.phase is Phase.LOST
    assert controller.commander.intent.type_ == "STOP"


# ══════════════════════════════════════════════════════════════
#  측위 신뢰 (2026-10-03 Claude 수정 — 투표 방위·독립성, IMU 방위 사전, 감사 기준 자세)
# ══════════════════════════════════════════════════════════════


def test_global_votes_must_agree_on_heading() -> None:
    """위치가 같아도 방위가 다르면 다른 답이다 — 58°·36° 가 한 표로 묶였던 실기(s0_live_4)."""
    controller = build(reloc_votes=3)
    controller._moved_since_vote = True
    assert controller._vote_global((1.9, -2.5, math.radians(58)), peers=0) is False
    controller._moved_since_vote = True
    assert controller._vote_global((1.95, -2.5, math.radians(36)), peers=0) is False
    assert len(controller._global_votes) == 1, "방위가 22° 다르면 처음부터 다시 센다"


def test_stationary_ambiguous_votes_are_not_independent() -> None:
    """움직이지 않은 로봇의 같은 장면 — 모호한(경쟁 후보 많은) 답은 반복돼도 표가 늘지 않는다."""
    controller = build(reloc_votes=3, reloc_stationary_max_peers=10)
    pose = (1.0, 1.0, 0.5)
    controller._vote_global(pose, peers=50)
    for _ in range(5):
        assert controller._vote_global(pose, peers=50) is False
    assert len(controller._global_votes) == 1
    # 유일한 답이면 서 있어도 센다 (들어 옮겨진 로봇은 움직이지 않고도 다시 찾아야 한다).
    assert controller._vote_global(pose, peers=2) is False
    assert controller._vote_global(pose, peers=2) is True


def test_motion_between_votes_makes_them_independent() -> None:
    controller = build(reloc_votes=2, reloc_stationary_max_peers=10)
    pose = (1.0, 1.0, 0.5)
    controller._vote_global(pose, peers=50)
    controller.note_sent([CommandEncoder().encode("MOVE", step=40, angle=0)], 2000)
    assert controller._vote_global(pose, peers=50) is True


def test_imu_rotation_is_measured_from_the_pose_anchor() -> None:
    """회전 예측은 «지금 IMU − 지금 자세를 잡은 때의 IMU» — 정합이 실패해도 누적이 남는다.

    예전엔 스캔마다 직전 IMU 를 소비해, 실패한 스캔 동안 돈 20° 를 다음 정합이 잃었다
    (Codex 검토 P1).
    """
    controller = build(imu_fresh_ms=300)
    controller.observe_telemetry(Reading(yaw=0.0), 1000)
    controller.observe_map_pose((1.0, 1.0, 0.0), 1000)  # 앵커 = IMU 0°
    controller.observe_telemetry(Reading(yaw=20.0), 1100)
    assert controller._consume_yaw_delta(1150) == pytest.approx(math.radians(20))  # 정합 실패 가정
    controller.observe_telemetry(Reading(yaw=22.0), 1200)
    assert controller._consume_yaw_delta(1250) == pytest.approx(math.radians(22)), (
        "20° 를 잃지 않는다"
    )
    assert controller._imu_delta_fresh is True
    controller._consume_yaw_delta(2000)
    assert controller._imu_delta_fresh is False, "텔레메트리가 0.8초 묵었다"


def test_global_result_is_dropped_if_the_robot_walked_since_the_request() -> None:
    from host.slam.scan_match import MatchResult

    controller = build(reloc_votes=1, min_match_frac=0.0, wall_clock_ms=lambda: 1500)
    controller._global_inflight = True
    controller._global_req_context = (None, controller._move_seq, controller._loc_epoch, 1000)
    controller.note_sent([CommandEncoder().encode("MOVE", step=40, angle=0)], 1500)
    scan = __import__("host.common.lidar_link", fromlist=["Scan"]).Scan(
        "l", "b", 1, 1, ((0.0, 1.0),)
    )
    controller._global_result = (
        "reloc",
        MatchResult((3.0, 3.0, 0.0), 100, peers=0),
        np.zeros((100, 2)),
        scan,
        controller.pose,
    )
    before = controller.pose
    controller._poll_global(2000)
    assert controller.pose == before, "걷기 전 스캔의 답을 지금 자세로 올리지 않는다"


def test_trust_expires_after_a_long_loss() -> None:
    controller = build(trust_expiry_ms=5000)
    controller.observe_map_pose((1.0, 1.0, 0.0), 1000)
    controller._pose_verified = True
    controller.pose_seeded = True
    controller._expire_trust(5000)
    assert controller._pose_verified is True
    controller._expire_trust(7000)
    assert controller._pose_verified is False and controller.pose_seeded is False


def test_verify_needs_matching_heading_to_confirm() -> None:
    """같은 자리 반대 방향은 확인이 아니다 — XY 만 보던 감사가 반대 방향을 통과시켰다."""
    from host.slam.scan_match import MatchResult

    controller = build(reloc_votes=3, min_match_frac=0.0)
    controller.pose = (1.0, 1.0, 0.0)
    flipped = MatchResult((1.05, 1.0, math.pi), 100, peers=0)
    controller._apply_verify_result(flipped, np.zeros((100, 2)), 5000, controller.pose)
    assert controller._pose_verified is False


def test_votes_must_agree_with_every_earlier_vote() -> None:
    """0, 0.29, 0.58m 처럼 한 칸씩 미끄러지는 답은 한 무리가 아니다."""
    controller = build(reloc_votes=3)
    for x in (0.0, 0.29):
        controller._moved_since_vote = True
        controller._vote_global((x, 0.0, 0.0), peers=0)
    controller._moved_since_vote = True
    assert controller._vote_global((0.58, 0.0, 0.0), peers=0) is False
    assert len(controller._global_votes) == 1


def test_verify_result_against_pose_at_request_time() -> None:
    """감사는 요청 때 스캔으로 했다 — 그 사이 자세가 옮겨졌으면 결과를 적용하지 않는다."""
    from host.slam.scan_match import MatchResult

    controller = build(reloc_votes=1, min_match_frac=0.0)
    controller.pose = (2.0, 2.0, 0.0)
    asked = (1.0, 1.0, 0.0)
    far = MatchResult((3.0, 3.0, 0.0), 100, peers=0)
    points = np.zeros((100, 2))
    assert controller._apply_verify_result(far, points, 5000, asked) is False
    assert controller.pose == (2.0, 2.0, 0.0), "낡은 감사로 자세를 옮기지 않는다"


def _pending_reloc(controller, pose=(3.0, 3.0, 0.0), asked_ms=None, imu=None):
    from host.common.lidar_link import Scan
    from host.slam.scan_match import MatchResult

    controller._global_inflight = True
    if asked_ms is None:
        asked_ms = controller.wall_clock_ms()
    controller._global_req_context = (imu, controller._move_seq, controller._loc_epoch, asked_ms)
    controller._global_result = (
        "reloc",
        MatchResult(pose, 100, peers=0),
        np.zeros((100, 2)),
        Scan("l", "b", 1, 1, ((0.0, 1.0),)),
        controller.pose,
    )


def test_result_requested_before_trust_expiry_is_dropped() -> None:
    """만료 전에 요청한 결과가 만료 뒤 도착해도 신뢰를 되살리지 못한다 (Codex 검토 2 P1)."""
    controller = build(
        reloc_votes=1, min_match_frac=0.0, trust_expiry_ms=5000, wall_clock_ms=lambda: 1600
    )
    controller.observe_map_pose((1.0, 1.0, 0.0), 1000)
    _pending_reloc(controller, asked_ms=1500)
    controller._expire_trust(7000)  # 세대가 올라간다
    controller._poll_global(7000)
    assert controller.pose == (1.0, 1.0, 0.0)


def test_late_global_result_is_dropped() -> None:
    controller = build(
        reloc_votes=1,
        min_match_frac=0.0,
        global_result_max_age_ms=3000,
        wall_clock_ms=lambda: 4500,
    )
    controller.observe_map_pose((1.0, 1.0, 0.0), 1000)
    _pending_reloc(controller, asked_ms=1000)
    controller._poll_global(1001)  # 루프 시계가 멎어 있어도 실제로 묵은 결과는 버린다.
    assert controller.pose == (1.0, 1.0, 0.0), "3.5초 묵은 탐색 결과"


def test_stale_imu_is_not_used_as_anchor() -> None:
    """IMU 가 끊긴 동안 잡은 자세에 옛 IMU 를 묶지 않는다 — 재개 때 회전을 두 번 더하지 않게."""
    controller = build(imu_fresh_ms=300)
    controller.observe_telemetry(Reading(yaw=0.0), 1000)
    controller.observe_map_pose((1.0, 1.0, math.radians(20)), 3000)  # IMU 는 2초 묵음
    assert controller._imu_anchor is None
    controller.observe_telemetry(Reading(yaw=20.0), 3100)
    assert controller._consume_yaw_delta(3150) == 0.0, "재개 시점에 다시 묶는다"
    controller.observe_telemetry(Reading(yaw=25.0), 3200)
    assert controller._consume_yaw_delta(3250) == pytest.approx(math.radians(5))


def test_adopted_global_pose_aligns_steering_offset_to_request_imu() -> None:
    controller = build(reloc_votes=1, min_match_frac=0.0)
    controller.observe_telemetry(Reading(yaw=30.0), 1000)
    _pending_reloc(controller, pose=(3.0, 3.0, math.radians(10)), imu=math.radians(10))
    controller._poll_global(1500)
    assert controller.pose[:2] == (3.0, 3.0)
    assert controller._imu_anchor == pytest.approx(math.radians(10))
    # 조향 방위 = 지금 IMU(30°) − 오프셋(10°−10°=0) = 30° — 요청 이후 20° 회전이 들어 있다.
    assert controller._steering_yaw() == pytest.approx(math.radians(30))


def test_move_zero_counts_as_stop() -> None:
    controller = build()
    controller.note_sent([CommandEncoder().encode("MOVE", step=40, angle=0)], 1000)
    seq = controller._move_seq
    controller.note_sent([CommandEncoder().encode("MOVE", step=0, angle=0)], 2000)
    assert controller._move_seq == seq
    assert controller._stopped_since_ms == 2000 and controller._last_sent_moving is False


def test_fast_loop_does_not_expire_a_fresh_global_result(monkeypatch) -> None:
    from host.common.lidar_link import Scan

    wall = [1000]
    controller = build(reloc_votes=1, min_match_frac=0.0, wall_clock_ms=lambda: wall[0])
    monkeypatch.setattr(controller, "_ensure_global_worker", lambda: None)
    controller._scan_now_ms = 100_000
    scan = Scan("l", "b", 1, 1, ((0.0, 1.0),))
    assert controller._submit_global("reloc", np.zeros((100, 2)), scan)
    assert controller._global_req_context[-1] == 1000
    _pending_reloc(controller, asked_ms=controller._global_req_context[-1])
    wall[0] += 300
    controller._poll_global(120_000)
    assert controller.pose == (3.0, 3.0, 0.0)
    assert controller.pose_ms == 120_000  # 자세 신선도는 여전히 루프 시계를 쓴다.
    assert controller._pose_verified


def test_global_request_owns_readonly_map_and_metadata(monkeypatch) -> None:
    from host.common.lidar_link import Scan

    controller = build(loc_grid=open_room())
    monkeypatch.setattr(controller, "_ensure_global_worker", lambda: None)
    points = np.ones((60, 2))
    scan = Scan("l", "b", 1, 1, ((0.0, 1.0),))
    expected, meta = controller.match_grid.snapshot()
    assert controller._submit_global("reloc", points, scan)
    request = controller._global_req
    snapshot = request[4]
    controller.match_grid.cells[:] = 0.0
    controller.match_grid.meta.origin_x += 2.0
    controller.loc_grid = open_room()
    points[:] = 0.0
    assert np.array_equal(snapshot.cells, expected)
    assert snapshot.meta == meta
    assert np.all(request[1] == 1.0)
    with pytest.raises(ValueError):
        snapshot.cells[0, 0] = 5.0
    assert not controller._submit_global("verify", points, scan)
    assert controller._global_req is request


@pytest.mark.parametrize("kind", ["reloc", "verify"])
@pytest.mark.parametrize("diagnostics", [{"search_complete": False}, {"competing_peaks": 1}])
def test_unresolved_full_scan_result_cannot_vote_or_verify(kind, diagnostics) -> None:
    from host.common.lidar_link import Scan
    from host.slam.scan_match import MatchResult

    controller = build(reloc_votes=1, min_match_frac=0.0)
    controller.pose = (1.0, 1.0, 0.0)
    result = MatchResult(controller.pose, 60, peers=0, **diagnostics)
    points = np.ones((60, 2))
    if kind == "reloc":
        controller._apply_reloc_result(result, points, Scan("l", "b", 1, 1, ()), 1000)
    else:
        controller._apply_verify_result(result, points, 1000, controller.pose)
    assert not controller._global_votes
    assert not controller._pose_verified


def test_escape_skips_nearby_points_only_with_a_safe_body_connector() -> None:
    from host.behavior.planner import Plan

    grid = open_room()
    grid.cells[:, 40] = 5.0
    controller = build(grid=grid)
    controller.plan = Plan(
        "A",
        ((2.225, 2.025), (2.275, 2.025), (2.325, 2.025), (4.0, 2.025)),
        escape_end_index=2,
    )
    controller.waypoint_index = 1
    controller.pose = (2.29, 2.06, 0.0)  # 2.5cm 반경은 놓쳤지만 정상 도달 반경 안.
    assert controller._current_waypoint() == (4.0, 2.025)
    assert controller.waypoint_index == 3
    controller._follow()
    assert controller.commander.intent.fields["step"] > 0
    assert not controller._spinning


@pytest.mark.parametrize("escape_end", [-1, 1])
def test_waypoint_radius_cannot_cut_an_unsafe_corner(escape_end) -> None:
    from host.behavior.planner import Plan

    controller = build()
    controller.pose = (1.94, 2.025, 0.0)
    controller.plan = Plan("A", ((2.025, 2.025), (2.025, 2.225)), escape_end_index=escape_end)
    controller._body_blocked[41, 39] = True  # 다음 점으로 자르는 대각선만 막는다.
    assert controller._current_waypoint() == (2.025, 2.025)
    assert controller.waypoint_index == 0


def _margin_follower():
    from host.behavior.planner import Plan

    grid = open_room()
    grid.cells[:, 40] = 5.0
    controller = build(grid=grid)
    controller.zones = ZoneStore(("A",))
    controller.zones.place(2.375, 4.025)
    controller.plan = Plan("A", ((2.375, 1.025), (2.375, 4.025)))
    controller.phase = Phase.MOVING
    controller.waypoint_index = 1
    controller.note_sent([CommandEncoder().encode("MOVE", step=40, angle=0)], 1000)
    return controller


def test_repeated_tracking_margin_entries_keep_following_without_replans() -> None:
    controller = _margin_follower()
    plan = controller.plan
    for tick in range(20):
        now = 1100 + 100 * tick
        x = 2.285 if tick % 2 == 0 else 2.375
        controller.observe_map_pose((x, 1.5 + tick * 0.05, math.pi / 2), now)
        controller.observe_telemetry(Reading(), now)
        controller.steer(now)
        assert controller.phase is Phase.MOVING
        assert controller.commander.intent.type_ == "MOVE"
        assert controller.commander.intent.fields["step"] > 0
        assert controller.plan is plan
    assert controller.stats.lost == controller.stats.replans == 0


@pytest.mark.parametrize("hazard", ["body", "unknown", "dynamic", "off_path", "stale"])
def test_margin_following_preserves_stop_boundaries(hazard) -> None:
    controller = _margin_follower()
    x = 2.285
    if hazard == "body":
        x = 2.175
    elif hazard == "off_path":
        x = 2.225  # 몸체는 안전하지만 기존 선분에서 도달 반경보다 멀다.
    elif hazard == "unknown":
        controller.grid.cells[controller.grid.to_cell(x, 2.025)] = 0.0
        controller._rebuild_masks()
    elif hazard == "dynamic":
        controller._dynamic[controller.grid.to_cell(2.325, 3.025)] = True
    controller.observe_map_pose((x, 2.025, math.pi / 2), 1000 if hazard == "stale" else 2000)
    controller.observe_telemetry(Reading(), 2000)
    controller.steer(2000)
    assert controller.commander.intent.type_ == "STOP"
    assert controller.phase in (Phase.LOST, Phase.PLANNING)


def test_tracking_margin_does_not_disable_lidar_estop() -> None:
    from host.common.lidar_link import Scan

    controller = _margin_follower()
    controller.observe_map_pose((2.285, 2.025, math.pi / 2), 2000)
    assert controller.guard_scan(Scan("l", "b", 1, 2000, ((0.0, 0.10),))) is not None
    assert controller.phase is Phase.HALTED
    assert controller.stats.estops == 1


def test_obstacle_in_front_of_known_wall_is_not_hidden_by_tracking_inflation() -> None:
    from host.common.lidar_link import Scan

    grid = open_room()
    grid.cells[:, 60] = 5.0  # x=3.0 벽, 실제 반사보다 37.5cm 뒤.
    controller = build(grid=grid)
    controller.pose = (2.025, 2.025, 0.0)
    scan = Scan("l", "b", 1, 1000, ((0.0, 0.6),))
    controller._check_new_obstacle(scan)
    controller._check_new_obstacle(scan)
    assert controller.stats.replans == 1
    assert controller.take_new_obstacles()[0] == pytest.approx((2.625, 2.025))


@pytest.mark.parametrize("dynamic", [False, True])
def test_grazing_known_cell_is_not_a_new_obstacle(dynamic) -> None:
    from host.behavior.planner import detect_new_obstacle
    from host.common.lidar_link import Scan

    grid = open_room()
    pose = (2.025, 2.025, 0.0)
    angle = math.atan2(0.035, 0.075)
    # 빔은 이 셀의 모서리를 약 2cm만 통과한다. 5cm 샘플은 둘 다 놓친다.
    cell = grid.to_cell(2.09, 2.06)
    mask = np.zeros_like(grid.cells, dtype=bool)
    if dynamic:
        mask[cell] = True
    else:
        grid.cells[cell] = 5.0
    distance = 0.075
    for step in range(1, 31):
        sample = grid.to_cell(
            pose[0] + math.cos(angle) * step * 0.05, pose[1] + math.sin(angle) * step * 0.05
        )
        assert sample != cell  # 고정 간격 버그의 실제 반례.
    assert (
        grid.to_cell(pose[0] + math.cos(angle) * distance, pose[1] + math.sin(angle) * distance)
        == cell
    )
    assert (
        detect_new_obstacle(
            pose,
            ((angle, distance),),
            grid,
            mask,
            check_radius_m=1.5,
            margin_m=0.25,
            occ_thresh=1.0,
        )
        is None
    )
    controller = build(grid=grid)
    controller.pose = pose
    controller._dynamic[:] = mask
    for _ in range(3):
        controller._check_new_obstacle(Scan("l", "b", 1, 1000, ((angle, distance),)))
    assert controller.stats.replans == 0
    assert controller.take_new_obstacles() == ()


def test_hit_next_to_a_known_wall_is_not_a_new_obstacle() -> None:
    """벽을 비스듬히 스친 빔 — 벽에서 허용치(계획 여유 − 몸체 반경) 안의 반사는 새 물체가 아니다."""
    from host.behavior.planner import detect_new_obstacle

    grid = open_room()
    blocked = np.zeros(grid.cells.shape, dtype=bool)
    kwargs = {"check_radius_m": 3.5, "margin_m": 0.25, "occ_thresh": 1.0, "known_tolerance_m": 0.10}
    # 벽(y≈0~0.05)과 거의 나란히: (1.0, 0.15) 에서 1m 가서 y=0.10 에 닿는 빔 — 벽에서 5cm.
    grazing = ((math.atan2(0.10 - 0.15, 1.0), math.hypot(1.0, 0.05)),)
    assert detect_new_obstacle((1.0, 0.15, 0.0), grazing, grid, blocked, **kwargs) is None
    # 허용치를 넘는 거리(벽에서 약 20cm)의 반사는 새 물체다 — 경로와 몸체 여유가 겹칠 수 있다.
    beside = ((math.atan2(0.25 - 0.45, 1.0), math.hypot(1.0, 0.20)),)
    assert detect_new_obstacle((1.0, 0.45, 0.0), beside, grid, blocked, **kwargs) is not None
    # 방 한가운데의 짧은 반사는 그대로 새 물체다.
    hit = detect_new_obstacle((2.0, 2.5, 0.0), ((0.0, 0.6),), grid, blocked, **kwargs)
    assert hit == pytest.approx((2.6, 2.5))


def test_dynamic_marks_do_not_hide_objects_just_outside_them() -> None:
    """확인된 동적 표시 바로 바깥의 새 물체는 숨기지 않는다 (Codex 교차 검토)."""
    from host.behavior.planner import detect_new_obstacle, mark_obstacle

    grid = open_room()
    blocked = np.zeros(grid.cells.shape, dtype=bool)
    mark_obstacle(blocked, grid, (2.0, 2.0), 0.15)
    # 표시 가장자리에서 약 12cm 밖(중심에서 0.27m)의 반사
    hit = detect_new_obstacle(
        (2.0, 1.0, 0.0),
        ((math.atan2(1.0, 0.27), math.hypot(0.27, 1.0)),),
        grid,
        blocked,
        check_radius_m=3.5,
        margin_m=0.25,
        occ_thresh=1.0,
        known_tolerance_m=0.10,
    )
    assert hit is not None


# ── 신뢰 복원(E2, 2026-10-04) ─────────────────────────────────
# 집 지도는 스캔 하나로 구별이 안 되는 자리가 많아 전역 탐색 거절이 대부분 정당했다(재생).
# 그래서 «어디인가» 대신 «정지한 채 상실한 로봇이 제자리에서 다시 맞는가» 만 묻는다.
from host.common.config import ConfigError  # noqa: E402
from host.common.lidar_link import Scan  # noqa: E402
from host.slam.scan_match import MatchResult  # noqa: E402
from host.slam.settings import validate_section  # noqa: E402

POINTS = np.zeros((100, 2))
SCAN = Scan("l", "b", 1, 1, ((0.0, 1.0),))
HOME = (2.0, 2.0, 0.0)


def _lost_after_verified(**overrides):
    """확인된 자세 HOME 에서 IMU 0° 로 서 있다가 신뢰가 만료된 컨트롤러."""
    params = {
        "reloc_restore_enabled": True,
        "reloc_votes": 3,
        "min_match_frac": 0.5,
        "trust_expiry_ms": 5000,
        "imu_fresh_ms": 300,
    }
    params.update(overrides)
    controller = build(**params)
    controller.observe_telemetry(Reading(yaw=0.0), 1000)
    controller.observe_map_pose(HOME, 1000)
    controller._pose_verified = True
    controller._expire_trust(7000)
    return controller


def _restore_round(controller, now_ms, prior_pose=HOME, prior_score=95, global_score=100):
    """워커가 전역 결과와 창 안 결과를 함께 낸 것처럼 꾸며 루프에서 해석한다."""
    controller._global_inflight = True
    controller._global_req_context = (
        None,
        controller._move_seq,
        controller._loc_epoch,
        controller.wall_clock_ms(),
    )
    controller._global_result = (
        "reloc",
        MatchResult((4.5, 3.5, 2.0), global_score, peers=200),  # 모호한 엉뚱한 자리
        POINTS,
        SCAN,
        controller.pose,
    )
    controller._global_prior_result = MatchResult(prior_pose, prior_score)
    controller.observe_telemetry(Reading(yaw=1.0), now_ms)
    controller._poll_global(now_ms)


def test_restore_anchor_is_set_only_for_a_verified_pose_with_fresh_imu() -> None:
    controller = _lost_after_verified()
    assert controller._restore_anchor is not None
    assert controller._restore_anchor[0] == HOME
    unverified = build(reloc_restore_enabled=True, trust_expiry_ms=5000)
    unverified.observe_telemetry(Reading(yaw=0.0), 1000)
    unverified.observe_map_pose(HOME, 1000)
    unverified.pose_seeded = True  # 사람 시드는 «제자리» 근거가 아니다
    unverified._expire_trust(7000)
    assert unverified._restore_anchor is None


def test_restore_disabled_by_default() -> None:
    controller = build(trust_expiry_ms=5000)
    controller.observe_telemetry(Reading(yaw=0.0), 1000)
    controller.observe_map_pose(HOME, 1000)
    controller._pose_verified = True
    controller._expire_trust(7000)
    assert controller._restore_anchor is None


def test_restore_three_consistent_rounds_restore_trust_at_home() -> None:
    controller = _lost_after_verified()
    for n, now in enumerate((7100, 8100, 9100), start=1):
        _restore_round(controller, now)
        if n < 3:
            assert controller._pose_verified is False, "한두 표로는 되살리지 않는다"
    assert controller._pose_verified is True
    assert controller.pose == HOME, "엉뚱한 전역 최고점이 아니라 제자리"
    assert controller._restore_anchor is None


def test_restore_window_score_far_below_global_best_is_rejected() -> None:
    """창 안보다 다른 곳이 훨씬 잘 맞으면 — 들어서 옮겨졌을 수 있다."""
    controller = _lost_after_verified()
    for now in (7100, 8100, 9100, 10100):
        _restore_round(controller, now, prior_score=80, global_score=100)
    assert controller._pose_verified is False
    assert controller._restore_votes == []


def test_restore_weak_window_score_is_rejected_even_if_global_is_weaker() -> None:
    controller = _lost_after_verified()
    for now in (7100, 8100, 9100):
        _restore_round(controller, now, prior_score=40, global_score=40)
    assert controller._pose_verified is False


def test_restore_votes_must_agree() -> None:
    controller = _lost_after_verified()
    _restore_round(controller, 7100, prior_pose=HOME)
    _restore_round(controller, 8100, prior_pose=(2.2, 2.0, 0.0))
    _restore_round(controller, 9100, prior_pose=HOME)
    assert controller._pose_verified is False


def test_restore_move_command_after_loss_drops_the_anchor() -> None:
    controller = _lost_after_verified()
    controller.note_sent([CommandEncoder().encode("MOVE", step=40, angle=0)], 7050)
    controller.observe_telemetry(Reading(yaw=0.0), 7100)
    assert controller._restore_prior(7100) is None
    assert controller._restore_anchor is None


def test_restore_imu_turn_drops_the_anchor() -> None:
    controller = _lost_after_verified()
    controller.observe_telemetry(Reading(yaw=12.0), 7100)  # 사람이 들어 돌렸다
    assert controller._restore_prior(7100) is None


def test_restore_stale_imu_or_old_anchor_drops_it() -> None:
    controller = _lost_after_verified()
    assert controller._restore_prior(7500) is None, "IMU 가 0.3초 넘게 묵었다"
    controller = _lost_after_verified(reloc_restore_max_age_ms=10000)
    controller.observe_telemetry(Reading(yaw=0.0), 12000)
    assert controller._restore_prior(12000) is None, "기준이 10초 넘게 묵었다"


def test_restore_prior_yaw_follows_small_imu_rotation() -> None:
    controller = _lost_after_verified()
    controller.observe_telemetry(Reading(yaw=3.0), 7100)
    prior = controller._restore_prior(7100)
    assert prior is not None
    assert prior[2] == pytest.approx(math.radians(3.0))


def test_restore_submit_passes_prior_only_for_reloc() -> None:
    controller = _lost_after_verified()
    controller.observe_telemetry(Reading(yaw=0.0), 7100)
    controller._scan_now_ms = 7100
    controller._submit_global("reloc", POINTS, SCAN)
    assert controller._global_req_prior == HOME
    controller._global_req = None
    controller._global_inflight = False
    controller._global_result = None
    controller._submit_global("verify", POINTS, SCAN)
    assert controller._global_req_prior is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reloc_restore_enabled", "yes"),
        ("reloc_restore_score_ratio", 1.5),
        ("reloc_restore_radius_mm", 0),
        ("reloc_restore_yaw_deg", float("nan")),
    ],
)
def test_restore_config_rejects_bad_restore_values(key, value) -> None:
    from host.slam.settings import read_lidar_section

    section = dict(read_lidar_section())
    section[key] = value
    with pytest.raises(ConfigError):
        validate_section(section)


def test_restore_shipped_config_is_valid_and_enables_restore() -> None:
    from host.slam.settings import read_lidar_section

    section = read_lidar_section()
    validate_section(section)
    assert section["reloc_restore_enabled"] is True


def _room_scan(pose, beams=180):
    """open_room 안쪽 벽(셀 경계 0.05·5.95 / 0.05·4.95)까지의 광선 — 로봇 기준 (각, 거리)."""
    x, y, yaw = pose
    out = []
    for i in range(beams):
        a = 2 * math.pi * i / beams
        dx, dy = math.cos(yaw + a), math.sin(yaw + a)
        hits = []
        if dx > 1e-9:
            hits.append((5.95 - x) / dx)
        if dx < -1e-9:
            hits.append((0.05 - x) / dx)
        if dy > 1e-9:
            hits.append((4.95 - y) / dy)
        if dy < -1e-9:
            hits.append((0.05 - y) / dy)
        out.append((a, min(hits)))
    return tuple(out)


def test_restore_real_worker_picks_home_in_a_symmetric_room() -> None:
    """대칭 방 — 전역 탐색은 반대편 거울 자리와 구별 못 해 거절되지만, 복원은 제자리를 잡는다."""
    import time as _time

    from host.slam.scan_match import preprocess

    home = (1.5, 1.2, 0.3)
    controller = build(
        reloc_restore_enabled=True,
        reloc_votes=3,
        min_match_frac=0.5,
        trust_expiry_ms=5000,
        imu_fresh_ms=10_000,
        reloc_max_peers=0,
    )
    controller.observe_telemetry(Reading(yaw=0.0), 1000)
    controller.observe_map_pose(home, 1000)
    controller._pose_verified = True
    controller._expire_trust(7000)
    assert controller._restore_anchor is not None
    scan = Scan("l", "b", 1, 1, _room_scan(home))
    points = preprocess(scan.points, 0.12, 8.0)
    for round_ms in (7100, 8100, 9100):
        controller._scan_now_ms = round_ms
        assert controller._submit_global("reloc", points, scan)
        deadline = _time.monotonic() + 20
        while controller._global_result is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        controller._poll_global(round_ms)
    assert controller._pose_verified is True
    assert math.hypot(controller.pose[0] - home[0], controller.pose[1] - home[1]) <= 0.06
    assert abs(controller.pose[2] - home[2]) <= math.radians(2)


def test_restore_anchor_uses_move_count_at_pose_time() -> None:
    """마지막 자세 뒤 MOVE 가 나가고 만료됐으면 그 자세는 «제자리» 가 아니다 (Codex E2 P1)."""
    controller = build(reloc_restore_enabled=True, trust_expiry_ms=5000, imu_fresh_ms=300)
    controller.observe_telemetry(Reading(yaw=0.0), 1000)
    controller.observe_map_pose(HOME, 1000)
    controller._pose_verified = True
    controller.note_sent([CommandEncoder().encode("MOVE", step=40, angle=0)], 2000)
    controller._expire_trust(7000)
    controller.observe_telemetry(Reading(yaw=0.0), 7100)
    assert controller._restore_prior(7100) is None


def test_restore_rechecks_anchor_when_the_result_arrives() -> None:
    """두 표 뒤 결과 도착 전에 IMU 가 돌았다 — 세 번째 결과로 복원하지 않는다 (Codex E2 P1)."""
    controller = _lost_after_verified()
    _restore_round(controller, 7100)
    _restore_round(controller, 8100)
    controller._global_inflight = True
    controller._global_req_context = (
        None,
        controller._move_seq,
        controller._loc_epoch,
        controller.wall_clock_ms(),
    )
    controller._global_result = (
        "reloc",
        MatchResult((4.5, 3.5, 2.0), 100, peers=200),
        POINTS,
        SCAN,
        controller.pose,
    )
    controller._global_prior_result = MatchResult(HOME, 95)
    controller.observe_telemetry(Reading(yaw=15.0), 9100)
    controller._poll_global(9100)
    assert controller._pose_verified is False
    assert controller._restore_anchor is None


def test_restore_failure_breaks_the_vote_streak() -> None:
    """성공·성공·결과 없음·성공 은 연속 세 표가 아니다 (Codex E2 P2)."""
    controller = _lost_after_verified()
    _restore_round(controller, 7100)
    _restore_round(controller, 8100)
    controller._try_restore(None, None, POINTS, SCAN, 8600)
    assert controller._restore_votes == []
    _restore_round(controller, 9100)
    assert controller._pose_verified is False


def test_restore_anchor_dropped_when_robot_is_lifted() -> None:
    """같은 방위로 들어 옮겨도 몸체는 기운다 — pitch·roll 변화로 기준을 버린다."""
    controller = _lost_after_verified()
    controller.observe_telemetry(Reading(yaw=0.0, pitch=4.0, roll=-3.0), 7100)
    assert controller._restore_anchor is not None, "서 있는 동안의 작은 흔들림은 괜찮다"
    controller.observe_telemetry(Reading(yaw=0.0, pitch=15.0, roll=0.0), 7200)
    assert controller._restore_anchor is None
    controller.observe_telemetry(Reading(yaw=0.0), 7300)
    assert controller._restore_prior(7300) is None, "다시 내려놓아도 기준은 돌아오지 않는다"


def test_restore_needs_tilt_at_pose_time() -> None:
    controller = build(reloc_restore_enabled=True, trust_expiry_ms=5000)
    controller.observe_map_pose(HOME, 1000)  # 텔레메트리(IMU·기울기) 없이 잡은 자세
    controller._pose_verified = True
    controller._expire_trust(7000)
    assert controller._restore_anchor is None


def test_zone_hint_limits_global_search_to_that_zone() -> None:
    """대칭 방 — 전역 탐색은 거울 자리와 구별 못 하지만, 사람이 구역을 알려주면 그 안에서 잡는다."""
    import time as _time

    from host.slam.scan_match import global_match, preprocess

    home = (1.5, 1.2, 0.3)
    controller = build(reloc_votes=3, min_match_frac=0.4, reloc_max_peers=0)
    controller.observe_map_pose((4.0, 3.5, 0.0), 1000)  # 엉뚱한 자리를 믿고 있다
    controller._pose_verified = True
    scan = Scan("l", "b", 1, 1, _room_scan(home))
    points = preprocess(scan.points, 0.12, 8.0)
    free = global_match(
        controller.match_grid,
        points,
        lin_step_m=0.1,
        ang_step_rad=math.radians(15),
        occ_thresh=1.0,
        min_known_cells=50,
    )
    assert free is not None and free.peers > 0, "구역 없이는 거울 자리와 구별 못 한다"

    assert controller.hint_zone("A", 2000) is True  # 구역 A 앵커 (1.0, 1.0), 반경 1.5m
    assert controller._pose_verified is False and controller.pose_stale(2000)
    assert controller.hint_zone("Z", 2000) is False
    for round_ms in (2100, 3100, 4100):
        controller._scan_now_ms = round_ms
        assert controller._submit_global("reloc", points, scan)
        deadline = _time.monotonic() + 20
        while controller._global_result is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        controller._poll_global(round_ms)
    assert controller._pose_verified is True
    assert math.hypot(controller.pose[0] - home[0], controller.pose[1] - home[1]) <= 0.15
    assert abs((controller.pose[2] - home[2] + math.pi) % (2 * math.pi) - math.pi) <= math.radians(
        5
    )
    assert controller._zone_hint is None, "잡히면 구역 힌트는 끝난다"


def test_zone_hint_expires() -> None:
    controller = build(zone_hint_ms=60000)
    controller.hint_zone("B", 1000)
    assert controller._zone_filter(30000) is not None
    assert controller._zone_filter(62000) is None
    assert controller._zone_hint is None


def test_zone_map_contains_matches_zone_at() -> None:
    from host.behavior.zone_map import ZoneMap

    labels = np.zeros((4, 4), dtype=np.int32)
    labels[:2, :2] = 1
    labels[2:, 2:] = 2
    zone_map = ZoneMap(labels, 0.5, 0.0, 0.0, {1: "A", 2: "B"})
    xs = np.array([0.25, 1.25, 1.75, 5.0])
    ys = np.array([0.25, 1.25, 1.75, 5.0])
    assert zone_map.contains("A", xs, ys).tolist() == [True, False, False, False]
    assert zone_map.contains("B", xs, ys).tolist() == [False, True, True, False]
    assert zone_map.contains("Z", xs, ys).tolist() == [False] * 4
    for x, y, inside in zip(xs, ys, zone_map.contains("B", xs, ys), strict=True):
        assert (zone_map.zone_at(x, y) == "B") == inside


# ── 지도에서 찍은 곳으로 이동 (2026-10-04) ──────────────────────
def _verified_at(pose=(1.0, 1.0, 0.0)):
    controller = build()
    controller.observe_map_pose(pose, 1000)
    controller._pose_verified = True
    return controller


def test_goto_refuses_without_a_trusted_pose() -> None:
    controller = build()
    controller._own_localization = True
    ok, detail = controller.goto(3.0, 2.0)
    assert ok is False and "자기 위치" in detail
    assert controller.goal is None


def test_goto_refuses_unreachable_or_nonfinite_points() -> None:
    controller = _verified_at()
    assert controller.goto(float("nan"), 1.0)[0] is False
    ok, detail = controller.goto(50.0, 50.0)
    assert ok is False and "길이 없다" in detail
    assert controller.goal is None


def test_goto_plans_to_the_point_before_zones_and_holds_on_arrival() -> None:
    from host.behavior.patrol import GOAL_LABEL

    controller = _verified_at()
    controller.phase = Phase.PLANNING
    ok, _detail = controller.goto(3.0, 2.5)
    assert ok is True and controller.goal == (3.0, 2.5)
    controller._advance()
    assert controller.plan.label == GOAL_LABEL and controller.plan.reachable
    assert controller.phase is Phase.MOVING
    controller.observe_map_pose((3.0, 2.45, 0.0), 2000)  # 도착
    controller._advance()
    assert controller.holding_goal is True and controller.goal is None
    assert controller.visited == frozenset(), "찍은 곳은 구역 방문이 아니다"
    controller._advance()
    assert controller.plan.label is None, "도착 뒤엔 구역으로 새지 않고 선다"
    controller.cancel_goal("patrol_restart")
    controller._advance()
    assert controller.plan.label in {"A", "B", "C"}, "순찰을 다시 시작하면 구역 순찰로"


def test_goto_that_becomes_blocked_holds_instead_of_wandering() -> None:
    controller = _verified_at()
    controller.goto(3.0, 2.5)
    controller._goal = (50.0, 50.0)  # 가는 도중 길이 사라졌다
    controller.plan = __import__("host.behavior.planner", fromlist=["Plan"]).Plan("GOAL")
    controller.phase = Phase.PLANNING
    controller._advance()
    assert controller.holding_goal is True and controller.goal is None


def test_goal_hold_reason_separates_arrival_from_blockage() -> None:
    controller = _verified_at()
    controller.goto(3.0, 2.5)
    controller._goal = (50.0, 50.0)
    controller.plan = __import__("host.behavior.planner", fromlist=["Plan"]).Plan("GOAL")
    controller.phase = Phase.PLANNING
    controller._advance()
    assert controller.goal_hold_reason == "blocked"
    controller.cancel_goal("patrol_restart")
    assert controller.goal_hold_reason is None
    controller.goto(3.0, 2.5)
    controller._advance()
    controller.observe_map_pose((3.0, 2.45, 0.0), 2000)
    controller._advance()
    assert controller.goal_hold_reason == "reached"


def test_zone_restricted_global_match_never_returns_outside_the_zone() -> None:
    """정밀 탐색이 경계 밖으로 번져도 결과는 알려준 범위 안 (Codex 검토 G P1)."""
    from host.slam.scan_match import global_match, preprocess

    controller = build()
    home = (1.5, 1.2, 0.3)
    points = preprocess(_room_scan(home), 0.12, 8.0)
    # 참 자리 바로 옆까지만 허용 — 정밀 탐색은 참 자리(경계 밖)로 가고 싶어 한다.
    allowed = lambda xs, _ys: np.asarray(xs) <= 1.42  # noqa: E731
    result = global_match(
        controller.match_grid,
        points,
        lin_step_m=0.1,
        ang_step_rad=math.radians(15),
        occ_thresh=1.0,
        min_known_cells=50,
        allowed=allowed,
    )
    assert result is not None and result.pose[0] <= 1.42 + 1e-9
