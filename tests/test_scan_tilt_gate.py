"""AP: 기울어진 반사를 자유 공간/측위 근거로 승격하지 않는다."""

import json
import math
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from test_lidar_patrol import Reading, build

from host.behavior.patrol import Phase
from host.behavior.scan_gate import ScanGate
from host.common.config import ConfigError
from host.common.lidar_link import Scan
from host.slam.scan_match import MatchResult
from host.slam.settings import read_lidar_section, validate_section
from host.telemetry.lidar_feed import RevolutionAssembler


def scan(seq=10, *, received=None, started=None, distance=3.0):
    return Scan(
        "lidar-a",
        "boot-a",
        seq,
        123,
        tuple((math.radians(a), distance) for a in range(-180, 180, 5)),
        received_ms=received,
        started_ms=started,
    )


def pose(pitch=0, roll=0, dur=500):
    return {"type": "POSE", "pitch": pitch, "roll": roll, "dur": dur}


@pytest.mark.parametrize("pitch,roll", [(6.01, 0), (-15, 0), (0, -6.01), (0, 8)])
def test_fresh_tilt_is_rejected(pitch, roll):
    gate = ScanGate()
    gate.observe_imu(SimpleNamespace(pitch=pitch, roll=roll), 1000)
    assert gate.check(scan(), 1250) == "tilted"
    assert gate.status["imu_fresh"]


def test_stale_missing_and_future_imu_fall_back_to_pose():
    gate = ScanGate()
    gate.observe_imu(Reading(pitch=-15), 1000)
    assert gate.check(scan(), 1251) is None
    assert not gate.status["imu_fresh"]
    gate.note_pose(pose(-15), 1260)
    assert gate.check(scan(), 2000) == "pose_tilt"
    assert ScanGate().check(scan(), 100) is None
    future = ScanGate()
    future.observe_imu(Reading(pitch=15), 200)
    assert future.check(scan(received=100), 300) is None


def test_pose_is_held_until_neutral_settles_and_entire_scan_is_after_it():
    gate = ScanGate(settle_ms=250)
    gate.note_pose(pose(-15), 1000)
    gate.observe_imu(Reading(), 3000)
    assert gate.check(scan(), 3000) == "pose_tilt"  # 신선한 수평 IMU도 명령 관문을 우회 못함
    gate.note_pose(pose(), 3100)
    assert gate.check(scan(), 3800) == "pose_settling"
    assert gate.check(scan(received=4000, started=3800), 4100) == "pose_settling"
    assert gate.check(scan(received=4000, started=3900), 4100) is None


def test_sensor_offset_and_command_roll_correction_are_distinct():
    gate = ScanGate(imu_roll_offset_deg=-4, pose_roll_offset_deg=4, settle_ms=0)
    gate.observe_imu(Reading(roll=-4), 1000)
    gate.note_pose(pose(roll=4, dur=100), 500)
    assert gate.check(scan(), 1000) is None
    gate.observe_imu(Reading(roll=3), 1010)
    assert gate.check(scan(), 1010) == "tilted"


def test_delayed_processing_uses_acquisition_time_and_history():
    gate = ScanGate()
    gate.observe_imu(Reading(pitch=-15), 1000)
    gate.observe_imu(Reading(), 1400)
    assert gate.check(scan(received=1100), 1500) == "tilted"
    assert gate.check(scan(received=1500, started=1400), 9000) is None


def test_queued_scan_before_pose_cannot_confirm_return():
    gate = ScanGate()
    gate.note_pose(pose(-15), 2000)
    assert gate.check(scan(received=1900), 2100) == "before_pose"


def test_assembled_scan_preserves_host_interval():
    assembler = RevolutionAssembler()
    assert assembler.add(replace(scan(), points=scan().points[:36]), 1000) is None
    merged = assembler.add(replace(scan(11), points=scan().points[36:]), 1100)
    assert merged is not None
    assert (merged.started_ms, merged.received_ms) == (1000, 1100)


def test_rejected_scan_cannot_update_pose_vote_audit_or_map_then_level_recovers():
    c = build(ready=False, live_map_write=True, map_hit_logodds=1, map_miss_logodds=-1)
    c.observe_map_pose((2.0, 2.0, 0), 900)
    c._pose_verified = True
    c.verify_interval_ms = 1
    c._global_votes.append(((2.0, 2.0, 0), 0))
    before = c.grid.cells.copy()
    c.observe_telemetry(Reading(pitch=-15), 1000)
    with patch("host.behavior.patrol.match") as matcher, patch.object(c, "_verify_pose") as audit:
        c.observe_scan(scan(), 1100)
        matcher.assert_not_called()
        audit.assert_not_called()
    assert c.pose_ms == 900
    assert not c._global_votes
    assert np.array_equal(before, c.grid.cells)
    assert c._live_clear.counts is None
    assert c.local_status["scan_rejected"] == "tilted"
    c.observe_telemetry(Reading(), 1500)
    c.verify_interval_ms = 0
    with patch("host.behavior.patrol.match", return_value=MatchResult(c.pose, 72, False)):
        c.observe_scan(scan(11), 1500)
    assert c.pose_ms == 1500
    assert c.local_status["scan_rejected"] is None


def test_tilted_scan_retains_stop_distance_but_cannot_clear_or_drive():
    c = build()
    c.observe_map_pose((2, 2, 0), 1000)
    c.phase = Phase.MOVING
    c.observe_telemetry(Reading(pitch=-15), 1100)
    c.observe_obstacle_scan(scan(distance=0.1), 1100)
    assert c._local_scan.distance() == 0.1
    assert not c._local_scan.complete
    assert c._local_scan.gap(0.15) is None
    assert c.guard_scan(scan(distance=0.1)) is not None
    c.phase = Phase.MOVING
    c.observe_obstacle_scan(scan(11), 1200)
    c.steer(1200)
    assert c.commander.intent.type_ == "STOP"
    assert c.local_status["clear_allowed"] is False


def test_tilted_free_rays_do_not_erase_blockage_or_accumulate_live_clear():
    c = build(ready=False)
    c.observe_map_pose((2, 2, 0), 1000)
    c._pose_verified = True
    c.observe_obstacle_scan(scan(1), 1000)
    item = c._blockages.remember(c.grid, [(2.5, 2)], 1000)
    assert item is not None
    counts = c._live_clear.counts.copy()
    seen = c._live_clear.seen_ms.copy()
    points = dict(item.points)
    c.observe_telemetry(Reading(pitch=-15), 1100)
    c.observe_obstacle_scan(scan(2), 1100)
    assert c._blockages.items[0].points == points
    assert np.array_equal(c._live_clear.counts, counts)
    assert np.array_equal(c._live_clear.seen_ms, seen)
    c.observe_telemetry(Reading(), 1500)
    c.observe_map_pose(c.pose, 1500)
    c.observe_obstacle_scan(scan(3), 1500)
    assert not c._blockages.items


def test_pose_invalidates_inflight_epoch_and_waits_for_first_return_scan():
    c = build()
    c.observe_map_pose((2, 2, 0), 1000)
    epoch = c._loc_epoch
    c.note_sent([json.dumps(pose(-15))], 1100)
    assert c._loc_epoch > epoch
    assert not c._local_scan.clear_allowed
    c.note_sent([json.dumps(pose())], 2000)
    c.observe_obstacle_scan(scan(10, received=3300, started=3200), 4000)
    assert not c._local_scan.clear_allowed
    c.observe_obstacle_scan(scan(11, received=3500, started=3300), 4000)
    assert c._local_scan.clear_allowed


def test_pre_tilt_global_audit_result_is_discarded_after_return():
    c = build(wall_clock_ms=lambda: 3000)
    c.observe_map_pose((2, 2, 0), 1000)
    points = np.array([[1.0, 0.0]])
    c.global_worker.inflight = True
    c.global_worker.context = (None, c._move_seq, c._loc_epoch, 1000)
    c.global_worker.result = ("verify", MatchResult(c.pose, 1), points, scan(), c.pose)
    c.note_sent([json.dumps(pose(-15))], 1100)
    c.note_sent([json.dumps(pose(dur=100))], 1200)
    with patch.object(c, "_apply_verify_result") as adopt:
        c._poll_global(3000)
        adopt.assert_not_called()
    assert not c.global_worker.inflight


@pytest.mark.parametrize(
    "key,value",
    [
        ("scan_tilt_max_deg", float("nan")),
        ("scan_tilt_imu_max_age_ms", 0),
        ("scan_tilt_settle_ms", -1),
        ("scan_tilt_settle_ms", 1.5),
        ("scan_tilt_roll_offset_deg", 46),
        ("scan_tilt_max_deg", True),
    ],
)
def test_bad_gate_settings_are_rejected(key, value):
    config = read_lidar_section()
    config[key] = value
    with pytest.raises(ConfigError):
        validate_section(config)


def test_walking_sway_uses_wider_limit_but_scan_pose_still_rejected():
    # lap_1 실기: 보행 p50 6.3°·p90 10.7° — 6° 로 거르면 추적이 굶는다(리뷰 지적).
    gate = ScanGate()
    gate.observe_imu(SimpleNamespace(pitch=9.0, roll=0.0), 1_000)
    gate.note_move(900)
    assert gate.check(scan(received=1_050, started=950), 1_050) is None
    assert gate.status["tilt_limit_deg"] == 12.0
    gate.observe_imu(SimpleNamespace(pitch=13.0, roll=0.0), 1_100)
    assert gate.check(scan(received=1_150, started=1_060), 1_150) == "tilted"
    # 보행이 멈추면 다시 정지 기준(6°)
    gate.observe_imu(SimpleNamespace(pitch=9.0, roll=0.0), 2_000)
    assert gate.check(scan(received=2_050, started=1_960), 2_050) == "tilted"
