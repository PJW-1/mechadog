"""오도메트리 · ODOM 링크 · odom_bridge 순수부 (WBS 5.4.3).

로봇·LiDAR·rclpy 없이 닫는다 — 보낸 전문과 IMU 표본, 시각만 넣는다.
"""

from __future__ import annotations

import json
import math
import runpy
from pathlib import Path

import pytest

import tools.patrol_run as patrol_run
from host.common.config import ConfigError
from host.common.odom_link import OdomDecoder, OdomEncoder, encode_odom, odom_of
from host.common.protocol import CommandEncoder, Verdict
from host.slam import settings
from host.slam.odometry import (
    CALIBRATION_STEP_MM,
    Odometry,
    OdomParams,
    hold_of_reading,
    odom_params_from_config,
)

BRIDGE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/ros2/odom_bridge.py"))
transform_of = BRIDGE["transform_of"]
laser_offset_from_env = BRIDGE["laser_offset_from_env"]

#: `mechdog-01` 실측값 (2026-09-11). 시험이 설정 파일을 바꿔도 흔들리지 않게 박는다.
PARAMS = OdomParams(
    forward_mm_per_sec=104.0,
    reverse_mm_per_sec=78.0,
    command_timeout_ms=600,
    imu_stale_ms=500,
)
PERIOD_MS = 100


class Rig:
    """실제 인코더로 만든 전문을 10Hz 로 넣는다. 텔레메트리도 10Hz."""

    def __init__(self, params: OdomParams = PARAMS) -> None:
        self.odom = Odometry(params)
        self.encoder = CommandEncoder(clock=lambda: 0)
        self.now = 1_000_000

    def run(
        self,
        seconds: float,
        *,
        move: tuple[float, float] | None,
        yaw_rate_deg: float = 0.0,
        yaw0: float = 10.0,
        imu: bool = True,
    ) -> None:
        ticks = round(seconds * 1000 / PERIOD_MS)
        for _ in range(ticks):
            if imu:
                self.odom.note_imu(yaw0 % 360.0, self.now, "boot-a")
            line = self.encoder.move(*move) if move is not None else self.encoder.stop()
            self.odom.note_sent([line], self.now)
            self.now += PERIOD_MS
            yaw0 += yaw_rate_deg * PERIOD_MS / 1000
        if imu:
            self.odom.note_imu(yaw0 % 360.0, self.now, "boot-a")
        self.yaw_deg = yaw0


def test_holding_stop_does_not_move() -> None:
    rig = Rig()
    rig.run(3.0, move=None)
    pose = rig.odom.pose(rig.now)
    assert pose.valid
    assert (pose.x_m, pose.y_m, pose.yaw_rad) == (0.0, 0.0, 0.0)


def test_straight_for_n_seconds_is_speed_times_time() -> None:
    rig = Rig()
    rig.run(0.1, move=None)  # 첫 IMU 표본 — 여기가 odom 원점이다
    rig.run(5.0, move=(CALIBRATION_STEP_MM, 0.0))
    pose = rig.odom.pose(rig.now)
    assert pose.valid
    assert pose.x_m == pytest.approx(0.104 * 5.0, abs=1e-9)
    assert pose.y_m == pytest.approx(0.0, abs=1e-9)


def test_step_scales_speed_from_the_calibration_step() -> None:
    """비례 가정 (모듈 머리말). 실측 때 step 60 의 절반이면 절반 속도다."""
    rig = Rig()
    rig.run(0.1, move=None)
    rig.run(2.0, move=(CALIBRATION_STEP_MM / 2, 0.0))
    assert rig.odom.pose(rig.now).x_m == pytest.approx(0.052 * 2.0, abs=1e-9)


def test_reverse_uses_the_reverse_speed() -> None:
    rig = Rig()
    rig.run(0.1, move=None)
    rig.run(2.0, move=(-CALIBRATION_STEP_MM, 0.0))
    assert rig.odom.pose(rig.now).x_m == pytest.approx(-0.078 * 2.0, abs=1e-9)


def test_imu_yaw_across_the_zero_boundary_is_a_small_turn() -> None:
    odom = Odometry(PARAMS)
    odom.note_imu(359.0, 0, "b")
    odom.note_imu(1.0, 100, "b")
    assert odom.pose(100).yaw_rad == pytest.approx(math.radians(2.0))
    odom.note_imu(358.0, 200, "b")
    assert odom.pose(200).yaw_rad == pytest.approx(math.radians(-1.0))


def test_absolute_imu_yaw_is_not_used() -> None:
    """부팅 옵셋이 137° 여도 odom 의 방위는 첫 표본에서 0 이다 (scan_match 버그 ⑥)."""
    odom = Odometry(PARAMS)
    odom.note_imu(137.0, 0, "b")
    assert odom.pose(0).yaw_rad == 0.0


def test_forward_while_turning_integrates_along_imu_heading() -> None:
    """좌선회 9 도/s 로 10초 = 90°. 원호 반경 r = v/ω 의 사분원 끝에 닿는다."""
    rig = Rig()
    rig.run(0.1, move=None)
    rig.run(10.0, move=(CALIBRATION_STEP_MM, 20.0), yaw_rate_deg=9.0)
    pose = rig.odom.pose(rig.now)
    radius = 0.104 / math.radians(9.0)
    assert pose.yaw_rad == pytest.approx(math.pi / 2, abs=1e-9)
    assert pose.x_m == pytest.approx(radius, rel=0.01)
    assert pose.y_m == pytest.approx(radius, rel=0.01)


def test_stale_imu_makes_the_pose_invalid() -> None:
    rig = Rig()
    rig.run(0.1, move=None)
    rig.run(2.0, move=(CALIBRATION_STEP_MM, 0.0), imu=False)
    pose = rig.odom.pose(rig.now)
    assert not pose.valid
    assert "IMU" in pose.reason


def test_no_imu_yet_is_invalid_and_commands_alone_do_not_move_it() -> None:
    rig = Rig()
    rig.run(2.0, move=(CALIBRATION_STEP_MM, 0.0), imu=False)
    assert not rig.odom.pose(rig.now).valid
    # 첫 표본이 오면 그 전 이동은 방위 기준이 없어 적분하지 않는다.
    rig.odom.note_imu(42.0, rig.now, "boot-a")
    pose = rig.odom.pose(rig.now)
    assert pose.valid
    assert (pose.x_m, pose.y_m) == (0.0, 0.0)


def test_motion_during_an_imu_gap_uses_the_measured_turn_when_imu_returns() -> None:
    """끊긴 동안의 이동은 방위를 알게 된 뒤 두 표본의 yaw 를 보간해 적분한다."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    for t in range(0, 2000, PERIOD_MS):
        odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 20.0)], t)
    assert not odom.pose(2000).valid
    odom.note_imu(90.0, 2000, "b")
    pose = odom.pose(2000)
    assert pose.valid
    # 2초 × 0.104 m/s 를 0→90° 로 고르게 돌며 갔다 — 사분원 끝.
    radius = 0.208 / (math.pi / 2)
    assert pose.x_m == pytest.approx(radius, rel=0.01)
    assert pose.y_m == pytest.approx(radius, rel=0.01)


def test_reboot_of_the_imu_is_not_read_as_a_turn() -> None:
    odom = Odometry(PARAMS)
    odom.note_imu(250.0, 0, "boot-a")
    odom.note_imu(0.0, 100, "boot-b")
    assert odom.pose(100).yaw_rad == 0.0


def test_no_motion_after_stop() -> None:
    rig = Rig()
    rig.run(0.1, move=None)
    rig.run(1.0, move=(CALIBRATION_STEP_MM, 0.0))
    moved = rig.odom.pose(rig.now).x_m
    rig.run(3.0, move=None)
    assert rig.odom.pose(rig.now).x_m == pytest.approx(moved)
    assert moved == pytest.approx(0.104)


def test_move_expires_after_the_command_timeout() -> None:
    """로봇은 `cmd_timeout_ms` 동안 명령이 없으면 스스로 멈춘다 (FR-1.3)."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    for t in range(100, 3001, 100):
        odom.note_imu(0.0, t, "b")
    assert odom.pose(3000).x_m == pytest.approx(0.104 * 0.6)


def test_move_after_estop_is_not_motion_until_reset_safe() -> None:
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_sent([encoder.estop(), encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    odom.note_imu(0.0, 500, "b")
    assert odom.pose(500).x_m == 0.0
    odom.note_sent([encoder.reset_safe(), encoder.move(CALIBRATION_STEP_MM, 0.0)], 500)
    odom.note_imu(0.0, 1000, "b")
    assert odom.pose(1000).x_m == pytest.approx(0.052)


def test_move_while_the_robot_reports_a_latch_is_not_motion() -> None:
    """로봇이 `safety_latched=true` 라고 알려 오는 동안 보낸 `MOVE` 는 거리를 만들지 않는다."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_hold(True, 0)
    for t in range(0, 1000, 100):
        odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], t)
        odom.note_imu(0.0, t + 100, "b")
    assert odom.pose(1000).x_m == 0.0
    odom.note_hold(False, 1000)
    for t in range(1000, 2000, 100):
        odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], t)
        odom.note_imu(0.0, t + 100, "b")
    assert odom.pose(2000).x_m == pytest.approx(0.104)


def test_rejected_reset_safe_keeps_the_odometry_still() -> None:
    """호스트가 `RESET_SAFE` 를 보냈어도 로봇이 거부해 래치가 남아 있으면(저전압) 움직임이 아니다.

    명령만 보면 `RESET_SAFE` 뒤의 `MOVE` 는 이동이지만, 로봇이 계속 `safety_latched=true`
    라고 알려 오므로 텔레메트리가 이긴다 — 위치가 조용히 부풀지 않는다.
    """
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_hold(True, 0)
    odom.note_sent([encoder.estop()], 0)
    odom.note_sent([encoder.reset_safe(), encoder.move(CALIBRATION_STEP_MM, 0.0)], 100)
    for t in range(200, 1100, 100):
        odom.note_hold(True, t)
        odom.note_imu(0.0, t, "b")
        odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], t)
    assert odom.pose(1100).x_m == 0.0


def test_reported_hold_stops_a_move_already_in_progress() -> None:
    """이동 중 로봇이 스스로 멈췄다고 알려 오면 그 시각부터 거리를 세지 않는다."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    odom.note_hold(True, 300)
    odom.note_imu(0.0, 500, "b")
    assert odom.pose(500).x_m == pytest.approx(0.104 * 0.3)


def test_release_reported_by_the_robot_needs_a_new_move_to_count() -> None:
    """래치가 풀렸다는 보고만으로 움직이지 않는다 — 로봇은 다음에 받아들인 `MOVE` 부터 걷는다."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_hold(True, 0)
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    odom.note_hold(False, 200)
    odom.note_imu(0.0, 500, "b")
    assert odom.pose(500).x_m == 0.0
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 500)
    odom.note_imu(0.0, 1000, "b")
    assert odom.pose(1000).x_m == pytest.approx(0.052)


def test_old_firmware_without_the_flags_falls_back_to_the_command_estimate() -> None:
    """`None`(구형 펌웨어)은 아무것도 바꾸지 않는다 — `ESTOP`/`RESET_SAFE` 추정이 그대로 쓰인다."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_hold(None, 0)
    odom.note_sent([encoder.estop(), encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    odom.note_imu(0.0, 500, "b")
    assert odom.pose(500).x_m == 0.0
    odom.note_hold(None, 500)
    odom.note_sent([encoder.reset_safe(), encoder.move(CALIBRATION_STEP_MM, 0.0)], 500)
    odom.note_imu(0.0, 1000, "b")
    assert odom.pose(1000).x_m == pytest.approx(0.052)


def test_stale_false_report_does_not_override_a_fresh_host_estop() -> None:
    """`ESTOP` 직후 도착한 낡은 `safety_latched=false` 는 호스트의 래치 추정을 덮지 못한다.

    보고는 정지를 **넓히기만** 한다 — 어느 쪽이든 정지라 하면 정지다.
    """
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_hold(False, 0)
    odom.note_sent([encoder.estop()], 0)
    odom.note_hold(False, 50)  # ESTOP 이 닿기 전에 만들어진 보고
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 100)
    odom.note_imu(0.0, 500, "b")
    assert odom.pose(500).x_m == 0.0


def test_hold_reported_after_the_move_expired_does_not_stretch_it() -> None:
    """만료된 `MOVE` 뒤에 온 정지 보고는 이미 끝난 구간을 늘리지 않는다."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    odom.note_hold(True, 900)  # cmd_timeout 600ms 뒤
    odom.note_imu(0.0, 1000, "b")
    assert odom.pose(1000).x_m == pytest.approx(0.104 * 0.6)


def test_moves_sent_across_a_reboot_are_dropped() -> None:
    """텔레메트리 공백 뒤 `boot_id` 가 바뀌어 돌아오면 그 사이 보낸 `MOVE` 는 이동이 아니다.

    순찰기는 두절 뒤에도 최대 `link_loss_failsafe_ms` 동안 `MOVE` 를 계속 보내지만
    (patrol.py ②), 재부팅한 로봇은 SAFE 잠금으로 켜져 실행하지 않는다
    (`firmware_mechdog_motion/README.md` 안전 동작). 적분하면 수십 cm 가 조용히 붙는다.
    """
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "boot-a")
    odom.note_hold(False, 0)
    for t in range(0, 3000, 100):  # 3초 공백 동안 MOVE 만 나간다
        odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], t)
    odom.note_hold(True, 3000)  # 재부팅한 로봇의 첫 보고: 래치 상태
    odom.note_imu(0.0, 3000, "boot-b")
    assert odom.pose(3000).x_m == 0.0


def test_moves_across_an_imu_gap_without_a_reboot_still_count() -> None:
    """같은 `boot_id` 로 돌아온 공백은 재부팅이 아니다 — 그 사이 이동은 그대로 센다 (기존 동작)."""
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "boot-a")
    for t in range(0, 1000, 100):
        odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], t)
    odom.note_imu(0.0, 1000, "boot-a")
    assert odom.pose(1000).x_m == pytest.approx(0.104)


@pytest.mark.parametrize(
    ("safety_latched", "obstacle", "expected"),
    [
        (None, None, None),
        (False, None, False),
        (None, False, False),
        (True, None, True),
        (None, True, True),
        (False, True, True),
        (True, False, True),
        (False, False, False),
    ],
)
def test_hold_of_reading_combines_the_two_onboard_stops(
    safety_latched: bool | None, obstacle: bool | None, expected: bool | None
) -> None:
    """래치와 근거리 정지 둘 다 `MOVE` 를 막는다(펌웨어 `move_allowed()`). 둘 다 없으면 모른다."""
    assert hold_of_reading(safety_latched, obstacle) is expected


def test_non_motion_commands_do_not_change_motion() -> None:
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    odom.note_sent([encoder.state("PATROL"), encoder.led("green", 0.0)], 200)
    odom.note_imu(0.0, 500, "b")
    assert odom.pose(500).x_m == pytest.approx(0.052)


def test_pose_extrapolates_after_the_last_imu_within_the_limit() -> None:
    odom = Odometry(PARAMS)
    encoder = CommandEncoder(clock=lambda: 0)
    odom.note_imu(0.0, 0, "b")
    odom.note_sent([encoder.move(CALIBRATION_STEP_MM, 0.0)], 0)
    pose = odom.pose(300)
    assert pose.valid
    assert pose.stamp_ms == 300
    assert pose.x_m == pytest.approx(0.104 * 0.3)


# ── 설정 ─────────────────────────────────────────────────────


def config_with(calibration: object) -> dict:
    config = settings.load(None)
    config["gait_calibration"] = calibration
    return config


def test_missing_gait_calibration_refuses_to_build_odometry() -> None:
    """보행 실측 전인 기체는 만들지 않는다 — 다른 기체 값으로 채우지 않는다."""
    with pytest.raises(ConfigError, match="gait_calibration"):
        odom_params_from_config(config_with(None))


def test_missing_reverse_speed_is_refused_not_borrowed_from_forward() -> None:
    with pytest.raises(ConfigError, match="reverse_mm_per_sec"):
        odom_params_from_config(config_with({"forward_mm_per_sec": 104.0}))


def test_params_come_from_the_unit_profile_and_config() -> None:
    config = settings.load("mechdog-01")
    params = odom_params_from_config(config)
    assert params.forward_mm_per_sec == config["gait_calibration"]["forward_mm_per_sec"]
    assert params.reverse_mm_per_sec == config["gait_calibration"]["reverse_mm_per_sec"]
    assert params.command_timeout_ms == config["safety"]["cmd_timeout_ms"]
    assert params.imu_stale_ms == config["lidar"]["odom_imu_stale_ms"]


def test_unit_without_calibration_runs_without_odometry() -> None:
    odometry, encoder = patrol_run.open_odometry(config_with(None), "mechdog-uncalibrated")
    assert odometry is None
    assert (
        json.loads(encoder.encode(ts_ms=1, x_m=0, y_m=0, yaw_rad=0, valid=False))["valid"] is False
    )


def test_odom_port_must_differ_from_scan_port() -> None:
    section = dict(settings.read_lidar_section())
    section["odom_port"] = section["scan_port"]
    with pytest.raises(ConfigError, match="odom_port"):
        settings.validate_section(section)


def test_odom_port_must_differ_from_the_scan_forward_port() -> None:
    """컨테이너가 실제로 스캔을 받는 곳은 `scan_forward_port` 다 (WBS 5.4.4)."""
    section = dict(settings.read_lidar_section())
    section["odom_port"] = section["scan_forward_port"]
    with pytest.raises(ConfigError, match="odom_port"):
        settings.validate_section(section)


def test_odom_rate_and_imu_limit_must_be_positive() -> None:
    for key in ("odom_rate_hz", "odom_imu_stale_ms"):
        section = dict(settings.read_lidar_section())
        section[key] = 0
        with pytest.raises(ConfigError, match=key):
            settings.validate_section(section)


def test_odom_port_is_5204_and_free_of_the_other_links() -> None:
    config = settings.load(None)
    section = config["lidar"]
    assert section["odom_port"] == 5204
    assert section["odom_port"] not in {
        config["network"]["cmd_port"],
        config["network"]["telemetry_port"],
        section["scan_port"],
        5202,  # `lidar_live_map --pose-port` — ODOM 의 x_m·y_m 을 자세로 삼킨다
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("odom_port", 70000),
        ("odom_port", 0),
        ("odom_port", "5204"),
        ("odom_port", True),
        ("odom_host", ""),
        ("odom_host", 127),
    ],
)
def test_bad_odom_destination_is_refused_at_startup(key: str, value: object) -> None:
    """⚠️ 기동 뒤 `sendto` 에서 터지면 `send()` 가 삼켜 ODOM 이 조용히 안 나간다."""
    section = dict(settings.read_lidar_section())
    section[key] = value
    with pytest.raises(ConfigError, match=key):
        settings.validate_section(section)


def test_odom_peer_resolves_the_host_once_at_startup() -> None:
    """⚠️ 호스트명을 그대로 `sendto` 에 넘기면 10Hz 마다 DNS 조회가 순찰 루프를 막는다."""
    assert patrol_run.odom_peer_of({"odom_host": "localhost", "odom_port": 5204}) == (
        "127.0.0.1",
        5204,
    )


def test_send_reports_only_the_lines_that_left() -> None:
    """오도메트리는 **실제로 나간** 명령만 적분한다."""

    class FlakySocket:
        def __init__(self) -> None:
            self.calls = 0

        def sendto(self, *_: object) -> None:
            self.calls += 1
            if self.calls == 2:
                raise OSError("ICMP")

    sent = patrol_run.send(FlakySocket(), ("127.0.0.1", 5001), ["a", "b", "c"])  # type: ignore[arg-type]
    assert sent == ["a", "c"]
    assert patrol_run.send(FlakySocket(), None, ["a"]) == []  # type: ignore[arg-type]


# ── ODOM 전문 ────────────────────────────────────────────────


def odom_line(**overrides: object) -> str:
    fields = {
        "seq": 1,
        "ts_ms": 1000,
        "device_id": "mechdog-01",
        "boot_id": "a" * 16,
        "x_m": 1.25,
        "y_m": -0.5,
        "yaw_rad": 0.75,
        "valid": True,
    }
    fields.update(overrides)
    return encode_odom(**fields)  # type: ignore[arg-type]


def test_odom_round_trip() -> None:
    encoder = OdomEncoder("mechdog-01", "a" * 16)
    decoder = OdomDecoder()
    first = odom_of(
        decoder.decode(encoder.encode(ts_ms=5, x_m=1.0, y_m=2.0, yaw_rad=-0.5, valid=True))
    )
    second = odom_of(decoder.decode(encoder.encode(ts_ms=6, x_m=0, y_m=0, yaw_rad=0, valid=False)))
    assert first is not None and second is not None
    assert (first.seq, first.ts_ms, first.x_m, first.y_m, first.yaw_rad, first.valid) == (
        1,
        5,
        1.0,
        2.0,
        -0.5,
        True,
    )
    assert (second.seq, second.valid) == (2, False)
    assert json.loads(odom_line())["type"] == "ODOM"


@pytest.mark.parametrize(
    ("raw", "verdict"),
    [
        ("{not json", Verdict.DISCARD),
        ("[]", Verdict.DISCARD),
        ('{"type": []}', Verdict.DISCARD),
        ('{"type": "POSE2D", "seq": 1}', Verdict.DISCARD_WARN),
        (
            json.dumps({k: v for k, v in json.loads(odom_line()).items() if k != "valid"}),
            Verdict.DISCARD,
        ),
        (odom_line(seq=0), Verdict.DISCARD),
        (odom_line(seq=1.5), Verdict.DISCARD),
        (odom_line(ts_ms=-1), Verdict.DISCARD),
        (odom_line(device_id=""), Verdict.DISCARD),
        (odom_line().replace('"valid":true', '"valid":1'), Verdict.DISCARD),
        (odom_line().replace('"x_m":1.25', '"x_m":"1.25"'), Verdict.DISCARD),
        (odom_line().replace('"yaw_rad":0.75', '"yaw_rad":NaN'), Verdict.DISCARD),
        (odom_line().replace('"y_m":-0.5', '"y_m":true'), Verdict.DISCARD),
    ],
)
def test_malformed_odom_is_discarded(raw: str, verdict: Verdict) -> None:
    result = OdomDecoder().decode(raw)
    assert result.verdict is verdict, result.reason
    assert odom_of(result) is None


def test_odom_seq_reversal_is_discarded_and_new_session_accepted() -> None:
    decoder = OdomDecoder()
    assert decoder.decode(odom_line(seq=5)).accepted
    assert not decoder.decode(odom_line(seq=5)).accepted
    assert not decoder.decode(odom_line(seq=4)).accepted
    # 순찰기를 다시 켜면 새 boot_id 의 seq=1 이다.
    assert decoder.decode(odom_line(seq=1, boot_id="b" * 16)).accepted


# ── odom_bridge 순수부 ───────────────────────────────────────


def test_bridge_turns_a_valid_odom_into_a_planar_transform() -> None:
    odom = odom_of(OdomDecoder().decode(odom_line(yaw_rad=math.pi / 2)))
    x, y, z, qx, qy, qz, qw = transform_of(odom)
    assert (x, y, z, qx, qy) == (1.25, -0.5, 0.0, 0.0, 0.0)
    assert (qz, qw) == (pytest.approx(math.sqrt(0.5)), pytest.approx(math.sqrt(0.5)))


def test_bridge_publishes_nothing_for_invalid_or_discarded_odom() -> None:
    """무효·폐기 전문은 tf 가 되지 않는다 — 항등이나 직전 값으로 채우지 않는다."""
    decoder = OdomDecoder()
    assert transform_of(odom_of(decoder.decode(odom_line(valid=False)))) is None
    assert transform_of(odom_of(decoder.decode("{broken"))) is None


def test_laser_offset_needs_every_value_explicitly() -> None:
    assert laser_offset_from_env({})[0] is None
    offset, reason = laser_offset_from_env({"LASER_OFFSET_X_M": "0.05", "LASER_OFFSET_Y_M": "0"})
    assert offset is None
    assert "LASER_OFFSET_Z_M" in reason
    offset, _ = laser_offset_from_env(
        {"LASER_OFFSET_X_M": "0.05", "LASER_OFFSET_Y_M": "0", "LASER_OFFSET_Z_M": "abc"}
    )
    assert offset is None
    offset, _ = laser_offset_from_env(
        {"LASER_OFFSET_X_M": "0.05", "LASER_OFFSET_Y_M": "0", "LASER_OFFSET_Z_M": "nan"}
    )
    assert offset is None
    offset, reason = laser_offset_from_env(
        {"LASER_OFFSET_X_M": "0.05", "LASER_OFFSET_Y_M": "-0.01", "LASER_OFFSET_Z_M": "0.22"}
    )
    assert offset == (0.05, -0.01, 0.22)
    assert reason == ""


def test_unresolvable_destination_is_a_config_error_not_a_traceback() -> None:
    """이름을 못 풀면 `main()` 이 «설정 오류» 로 알리도록 `ConfigError` 로 올린다."""
    with pytest.raises(ConfigError, match="odom_host"):
        patrol_run.odom_peer_of({"odom_host": "mechdog.invalid", "odom_port": 5204})
    with pytest.raises(ConfigError, match="scan_forward_host"):
        patrol_run.forward_peer_of(
            {
                "scan_forward_enabled": True,
                "scan_forward_host": "mechdog.invalid",
                "scan_forward_port": 5203,
            }
        )


def test_malformed_forward_port_is_reported_as_itself() -> None:
    """`scan_forward_port: true` 가 `odom_port: 1` 과 «같다» 는 엉뚱한 메시지로 나오지 않는다."""
    section = dict(settings.read_lidar_section())
    section["scan_forward_port"] = True
    section["odom_port"] = 1
    with pytest.raises(ConfigError, match=r"^lidar\.scan_forward_port 는 1~65535"):
        settings.validate_section(section)
