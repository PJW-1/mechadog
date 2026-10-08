"""지도 점 위치 힌트 — 합성 지도에서 측위 관문과 스레드 예약을 확인한다."""

from __future__ import annotations

import math
import threading

import numpy as np
import pytest
from conftest import FakeClock
from test_lidar_patrol import POINTS, SCAN, _lost_after_verified, _room_scan, build
from test_runtime import DEVICE, config
from test_runtime_lidar import _runtime

from host.behavior.patrol import controller_from_config
from host.common.lidar_link import Scan
from host.runtime import Runtime
from host.slam.scan_match import MatchResult, global_match, preprocess

__all__ = ["config"]


def test_point_hint_is_a_disk_including_its_boundary() -> None:
    controller = build(point_hint_radius_m=0.5)
    assert controller.hint_point(2.0, 2.0, 1000)
    allowed = controller.localization.zone_filter(1000)
    assert allowed is not None
    xs = np.array([2.0, 2.0, 2.49, 2.4, 2.51])
    ys = np.array([2.0, 2.5, 2.0, 2.4, 2.0])
    assert allowed(xs, ys).tolist() == [True, True, True, False, False]


def test_point_hint_discards_pose_votes_restore_and_inflight_epoch() -> None:
    controller = _lost_after_verified()
    assert controller.localization.restore_anchor is not None
    controller.observe_map_pose((2.0, 2.0, 0.7), 7100)
    controller.localization.verified = True
    controller.pose_seeded = True
    controller.localization.global_votes.append((2.0, 2.0, 0.7))
    controller.localization.restore_votes.append((2.0, 2.0, 0.7))
    old_epoch = controller.localization.loc_epoch
    controller.commander.drive(step=50.0, angle=0.0)
    assert controller.hint_point(3.0, 2.5, 7200)
    assert controller.pose == pytest.approx((3.0, 2.5, 0.7))
    assert controller.pose_stale(7200)
    assert not controller.pose_verified and not controller.pose_seeded
    assert controller.localization.global_votes == controller.localization.restore_votes == []
    assert controller.localization.restore_anchor is None
    assert controller.localization.loc_epoch == old_epoch + 1
    assert controller.localization.reloc_next_ms == 0
    assert controller.commander.intent.type_ == "STOP"


def test_point_and_zone_hints_replace_each_other() -> None:
    controller = build()
    assert controller.hint_zone("A", 1000)
    assert controller.hint_point(2.0, 2.0, 1100)
    assert controller.localization.zone_hint is None
    assert controller.localization.point_hint == (2.0, 2.0, 1100)
    assert not controller.hint_zone("missing", 1200)
    assert controller.localization.point_hint == (2.0, 2.0, 1100)
    assert controller.hint_zone("B", 1300)
    assert controller.localization.point_hint is None
    assert controller.localization.zone_hint == ("B", 1300)


def test_point_hint_rejects_global_result_submitted_before_the_hint() -> None:
    controller = build(reloc_votes=1)
    controller.localization.worker.inflight = True
    controller.localization.worker.context = (
        None,
        controller.localization.move_seq,
        controller.localization.loc_epoch,
        controller.wall_clock_ms(),
    )
    controller.localization.worker.result = (
        "reloc",
        MatchResult((4.0, 3.5, 0.0), 100),
        POINTS,
        SCAN,
        controller.pose,
    )
    assert controller.hint_point(2.0, 2.0, 1000)
    controller.localization.poll_global(1100)
    assert controller.pose[:2] == (2.0, 2.0)
    assert not controller.pose_verified
    assert controller.localization.point_hint == (2.0, 2.0, 1000)
    assert not controller.localization.worker.inflight


def test_point_hint_expires_without_verifying_display_pose() -> None:
    controller = build(zone_hint_ms=1000)
    assert controller.hint_point(2.0, 2.0, 1000)
    assert controller.localization.zone_filter(2000) is not None
    assert controller.localization.zone_filter(2001) is None
    assert controller.localization.point_hint is None
    assert not controller.pose_verified
    assert controller.pose_stale(2001)


def test_point_hint_still_requires_normal_votes_and_rejects_ambiguity() -> None:
    controller = build(reloc_votes=3, reloc_max_peers=60)
    assert controller.hint_point(2.0, 2.0, 1000)
    good = MatchResult((2.0, 2.0, 0.0), 100, peers=0)
    controller.localization.apply_reloc_result(good, POINTS, SCAN, 1100)
    assert not controller.pose_verified
    controller.localization.apply_reloc_result(
        MatchResult((2.0, 2.0, 0.0), 100, peers=61), POINTS, SCAN, 1200
    )
    assert controller.localization.global_votes == []
    assert controller.localization.point_hint is not None
    for now_ms in (1300, 1400):
        controller.localization.apply_reloc_result(good, POINTS, SCAN, now_ms)
        assert not controller.pose_verified
    controller.localization.apply_reloc_result(good, POINTS, SCAN, 1500)
    assert controller.pose_verified
    assert controller.localization.point_hint is None


def test_point_hint_filters_actual_global_candidates_in_symmetric_room() -> None:
    home = (1.5, 1.2, 0.3)
    controller = build()
    scan = Scan("synthetic", "test", 1, 1000, _room_scan(home))
    points = preprocess(scan.points, 0.12, 8.0)
    free = global_match(
        controller.match_grid,
        points,
        lin_step_m=0.1,
        ang_step_rad=math.radians(15),
        occ_thresh=1.0,
        min_known_cells=50,
    )
    assert free is not None and free.peers > 0
    assert controller.hint_point(*home[:2], 1000)
    narrowed = global_match(
        controller.match_grid,
        points,
        lin_step_m=0.1,
        ang_step_rad=math.radians(15),
        occ_thresh=1.0,
        min_known_cells=50,
        allowed=controller.localization.zone_filter(1000),
    )
    assert narrowed is not None
    assert math.dist(narrowed.pose[:2], home[:2]) <= 0.15
    assert narrowed.peers < free.peers
    assert not controller.pose_verified, "탐색 결과만으로 표결 관문을 건너뛰지 않는다"


@pytest.mark.parametrize("verified_by", ["relocalize", "audit"])
def test_any_verified_pose_clears_hint(verified_by: str) -> None:
    controller = build()
    assert controller.hint_point(2.0, 2.0, 1000)
    if verified_by == "relocalize":
        controller.reloc_votes = 1
        controller.localization.apply_reloc_result(
            MatchResult((2.0, 2.0, 0.0), 100), POINTS, SCAN, 1100
        )
    else:
        controller.localization.mark_verified()
    assert controller.pose_verified
    assert controller.localization.zone_hint is controller.localization.point_hint is None


@pytest.mark.parametrize(
    "x,y,reason",
    [
        (float("nan"), 2.0, "유한"),
        (2.0, float("inf"), "유한"),
        (float("-inf"), 2.0, "유한"),
        (-0.01, 2.0, "지도 밖"),
        (6.0, 2.0, "지도 밖"),
        (2.0, 5.0, "지도 밖"),
        (0.025, 2.0, "장애물"),
    ],
)
def test_runtime_refuses_invalid_points_without_overwriting_hint(
    config: dict,
    clock: FakeClock,
    x: float,
    y: float,
    reason: str,
) -> None:
    runtime, navigator = _runtime(config, clock)
    assert runtime.ask_locate_zone("A")[0]
    accepted, detail = runtime.ask_locate_point(x, y)
    assert not accepted and reason in detail
    runtime.tick(clock.ms)
    assert navigator.localization.zone_hint == ("A", clock.ms)
    assert navigator.localization.point_hint is None


def test_runtime_point_is_reserved_from_server_thread_and_applied_on_next_tick(
    config: dict,
    clock: FakeClock,
) -> None:
    runtime, navigator = _runtime(config, clock)
    navigator.observe_map_pose((1.0, 1.0, 0.7), clock.ms)
    navigator.localization.verified = True
    result = []
    server = threading.Thread(target=lambda: result.append(runtime.ask_locate_point(2.0, 2.0)))
    server.start()
    server.join(timeout=2)
    assert not server.is_alive() and result[0][0]
    assert navigator.pose == pytest.approx((1.0, 1.0, 0.7)) and navigator.pose_verified
    runtime.tick(clock.ms)
    assert navigator.localization.point_hint == (2.0, 2.0, clock.ms)
    assert navigator.pose == pytest.approx((2.0, 2.0, 0.7))
    assert not navigator.pose_verified
    assert runtime.nav_status()["point_hint"] == {"x": 2.0, "y": 2.0, "radius": 0.6}
    assert runtime.nav_status()["zone_hint"] is None


@pytest.mark.parametrize("last", ["point", "zone"])
def test_runtime_latest_location_request_wins(config: dict, clock: FakeClock, last: str) -> None:
    runtime, navigator = _runtime(config, clock)
    if last == "point":
        assert runtime.ask_locate_zone("A")[0]
        assert runtime.ask_locate_point(2.0, 2.0)[0]
    else:
        assert runtime.ask_locate_point(2.0, 2.0)[0]
        assert runtime.ask_locate_zone("A")[0]
    runtime.tick(clock.ms)
    assert (navigator.localization.point_hint is not None) == (last == "point")
    assert (navigator.localization.zone_hint is not None) == (last == "zone")


def test_runtime_hint_snapshot_expires_without_scans(config: dict, clock: FakeClock) -> None:
    runtime, navigator = _runtime(config, clock)
    navigator.zone_hint_ms = 1000
    assert runtime.ask_locate_point(2.0, 2.0)[0]
    runtime.tick(clock.ms)
    assert runtime.nav_status()["point_hint"] is not None
    clock.advance(1001)
    runtime.tick(clock.ms)
    assert runtime.nav_status()["point_hint"] is None
    assert navigator.localization.point_hint is None


def test_runtime_revalidates_reserved_point_if_map_changes(config: dict, clock: FakeClock) -> None:
    runtime, navigator = _runtime(config, clock)
    assert runtime.ask_locate_point(2.0, 2.0)[0]
    navigator.grid.cells[navigator.grid.to_cell(2.0, 2.0)] = 3.0
    runtime.tick(clock.ms)
    assert navigator.localization.point_hint is None


def test_unknown_point_is_only_a_hint_and_does_not_grant_pose_trust(
    config: dict,
    clock: FakeClock,
) -> None:
    runtime, navigator = _runtime(config, clock)
    navigator.grid.cells[navigator.grid.to_cell(2.0, 2.0)] = 0.0
    assert runtime.ask_locate_point(2.0, 2.0)[0]
    runtime.tick(clock.ms)
    assert navigator.localization.point_hint is not None
    assert not navigator.pose_verified and navigator.pose_stale(clock.ms)


def test_runtime_point_hint_without_lidar_is_refused(config: dict, clock: FakeClock) -> None:
    runtime = Runtime(config, device_id=DEVICE, clock=clock)
    accepted, detail = runtime.ask_locate_point(2.0, 2.0)
    assert not accepted and "LiDAR" in detail


@pytest.mark.parametrize("radius", [0.6, 0.25])
def test_configured_point_radius_reaches_actual_controller(config: dict, radius: float) -> None:
    base = build()
    settings = dict(config)
    settings["lidar"] = dict(config["lidar"], point_hint_radius_m=radius)
    controller = controller_from_config(settings, base.commander, base.grid, base.zones, None)
    assert controller.point_hint_radius_m == radius
