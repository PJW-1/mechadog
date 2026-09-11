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

DRIVE = DriveParams(
    step_mm=60.0,
    turn_deg=20.0,
    reverse_mm=200.0,
    heading_tolerance_rad=math.radians(8),
    reverse_threshold_rad=math.radians(90),
    arrival_radius_m=0.3,
    waypoint_radius_m=0.1,
    lidar_estop_m=0.15,
    link_loss_ms=3000,
    cmd_timeout_ms=300,
    pose_timeout_ms=500,
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
        self.imu = fields.get("imu", {"pitch": 0.0, "roll": 0.0, "yaw": 0.0})
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
    grid = overrides.pop("grid", None) or open_room()
    zones = ZoneStore(("A", "B", "C"))
    zones.place(1.0, 1.0)
    zones.place(4.0, 1.0)
    zones.place(4.0, 3.5)
    return PatrolController(
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
#  호(arc) 조향 — DR-11
# ══════════════════════════════════════════════════════════════


def test_straight_when_aligned() -> None:
    steering = steering_for(0.0, DRIVE)
    assert steering.step_mm == DRIVE.step_mm
    assert steering.angle_deg == 0.0


def test_arc_steering_keeps_walking() -> None:
    """**제자리 회전이 없으므로 조향 중에도 걷는다** (DR-11).

    `step == 0` 이면서 `angle != 0` 인 명령은 로봇이 할 수 없는 동작이다 —
    합치기 전의 `TURN_LEFT` 가 정확히 그것을 뜻했다.
    """
    # 후진 임계(90도)를 넘는 값까지 함께 본다 — **조향 부호는 걸음의 방향과
    # 무관하게 항상 오차 부호를 따른다**(2026-09-11 실측). 예전에는 ±80 까지만
    # 봐서 전진 구간만 검증했고, 그래서 후진 구간의 뒤집힌 부호가 살아남았다.
    for error_deg in (15, 45, 80, 100, 170, -15, -45, -80, -100, -170):
        steering = steering_for(math.radians(error_deg), DRIVE)
        assert steering.step_mm != 0.0, error_deg
        assert steering.angle_deg != 0.0, error_deg
        assert math.copysign(1, steering.angle_deg) == math.copysign(1, error_deg), error_deg


def test_reverse_arc_keeps_the_steering_sign() -> None:
    """목표가 거의 뒤에 있으면 후진 호로 돌아선다. **조향 부호는 그대로다.**

    ⚠️ **이 시험은 틀린 부호를 굳혀 두고 있었다.** 예전 이름은
    `test_reverse_arc_flips_the_steering_sign` 이었고 근거는 요 변화가
    `step x angle` 을 따른다는 추정이었다. 2026-09-11 실기에서 `move(-60,+20)` 과
    `move(-60,-20)` 을 몰아 보니 **후진에서도 `angle` 양수가 반시계**였다 —
    `angle` 이 각속도 명령이라 걸음의 부호가 곱해지지 않는다.

    뒤집힌 부호는 로봇을 목표에서 **더 멀어지는 쪽으로** 후진시킨다. 그래서
    부호를 시험 이름에 적어 둔다.
    """
    steering = steering_for(math.radians(170), DRIVE)
    assert steering.step_mm < 0, "목표가 뒤에 있으면 후진한다"
    assert steering.angle_deg > 0, "왼쪽 뒤 목표 → 반시계(+) 로 돌아야 오차가 줄어든다"
    mirrored = steering_for(math.radians(-170), DRIVE)
    assert mirrored.step_mm < 0
    assert mirrored.angle_deg < 0


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
    controller.observe_telemetry(Reading(imu={"pitch": 1.0, "roll": 0.0, "yaw": 90.0}), 1000)
    assert controller.safety.yaw_deg == 90.0
    assert controller.safety.yaw_rad == pytest.approx(math.pi / 2)


def test_missing_imu_yaw_is_none_not_zero() -> None:
    """0 으로 때우면 로봇이 정북을 보고 있다고 믿고 정합 중심을 잘못 잡는다."""
    controller = build()
    controller.observe_telemetry(Reading(imu={"pitch": 1.0, "roll": 0.0}), 1000)
    assert controller.safety.yaw_rad is None


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
