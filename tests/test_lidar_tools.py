"""LiDAR 도구·설정·시각화 (WBS 6.2.1 커버리지 게이트 · FR-6 · FR-7).

**진입점도 시험 대상이다.** 저장소에 `test_mock_mechdog.py`·`test_teleop.py`·
`test_latency_probe.py`·`test_hardware_udp_tools.py`·`test_fetch_models.py` 가
이미 있으므로 `tools/` 를 시험하는 것이 이 저장소의 관례다. 그것을 따르지 않고
합쳤다가 커버리지가 **78.42%** 로 떨어져 CI 가 막았다 — 게이트가 제 일을 한
것이다.

여기서 보는 것은 **배선**이다. 알고리즘은 `test_lidar_mapping.py`·
`test_lidar_patrol.py`·`test_lidar_link.py` 가 이미 보고 있으므로, 이 파일은
*"인자를 이렇게 주면 이 함수가 이렇게 불린다"* 만 확인한다.

⚠️ **소켓을 여는 경로는 루프백만 쓴다.** 실제 상대가 필요한 코드는
`serve_real` 뿐이고 그것은 여기서 돌리지 않는다 — 하드웨어 없이 검증 가능한
범위가 CI 의 전제다 (`ci.yml` 주석).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import numpy as np
import pytest

import tools.lidar_slam as lidar_slam
import tools.mock_lidar as mock_lidar
import tools.patrol_run as patrol_run
import tools.zone_select as zone_select
from host.common.config import ConfigError
from host.common.lidar_link import ScanDecoder, encode_scan
from host.slam import settings, simulation, viz
from host.slam.occupancy import MapMeta, OccupancyGrid


@pytest.fixture(scope="module")
def lidar_config() -> dict:
    return settings.load(None)


def room(width: int = 140, height: int = 120) -> OccupancyGrid:
    """관측된 빈 방. 미관측으로 두면 `inflate` 가 전부 막는다."""
    grid = OccupancyGrid(MapMeta(0.05, 0.0, 0.0, width, height))
    grid.cells[:, :] = -3.0
    grid.cells[0, :] = 3.0
    grid.cells[-1, :] = 3.0
    grid.cells[:, 0] = 3.0
    grid.cells[:, -1] = 3.0
    return grid


def seed_maps(directory: Path) -> None:
    """지도와 구역을 심는다. 세 도구가 공통으로 요구하는 입력이다."""
    from host.behavior.zones import ZoneStore

    room().save(directory)
    store = ZoneStore(("A", "B", "C"))
    for point in ((1.5, 1.0), (4.5, 1.0), (4.5, 4.0)):
        store.place(*point)
    store.save(directory)


# ══════════════════════════════════════════════════════════════
#  설정 — host/slam/settings.py
# ══════════════════════════════════════════════════════════════


def test_config_has_the_lidar_section(lidar_config: dict) -> None:
    """`config.yaml` 의 `lidar:` 절이 정본이다 — 별 파일을 두지 않는다."""
    assert isinstance(lidar_config["lidar"], dict)
    assert lidar_config["lidar"]["scan_port"] > 0


def test_missing_required_key_is_refused() -> None:
    section = dict(settings.read_lidar_section())
    del section["scan_port"]
    with pytest.raises(ConfigError, match="scan_port"):
        settings.validate_section(section)


def test_inverted_range_is_refused() -> None:
    section = dict(settings.read_lidar_section())
    section["range_min_mm"], section["range_max_mm"] = 8000, 120
    with pytest.raises(ConfigError, match="range_min_mm"):
        settings.validate_section(section)


def test_inverted_logodds_thresholds_are_refused() -> None:
    section = dict(settings.read_lidar_section())
    section["free_logodds"] = 5.0
    with pytest.raises(ConfigError, match="free_logodds"):
        settings.validate_section(section)


def test_wrong_signed_weights_are_refused() -> None:
    """점유는 양수, 자유는 음수여야 한다. 뒤집으면 벽이 지워진다."""
    section = dict(settings.read_lidar_section())
    section["hit_logodds"] = -0.9
    with pytest.raises(ConfigError, match="hit_logodds"):
        settings.validate_section(section)


def test_nonpositive_resolution_is_refused() -> None:
    section = dict(settings.read_lidar_section())
    section["resolution_mm"] = 0
    with pytest.raises(ConfigError, match="resolution_mm"):
        settings.validate_section(section)


def test_simulation_runs_even_when_track_is_none(lidar_config: dict) -> None:
    """⚠️ Phase 1 표준 구성에는 LiDAR 가 없다 (CONTRIBUTING 1절).

    그래도 알고리즘 검증은 막지 않는다 — 막으면 Phase 1 기간 내내 개발을
    못 한다.
    """
    settings.require_lidar_track(lidar_config, simulation=True)


def test_real_mode_requires_the_lidar_track(lidar_config: dict) -> None:
    config = {**lidar_config, "localization": {**lidar_config["localization"], "track": "none"}}
    with pytest.raises(ConfigError, match="localization.track"):
        settings.require_lidar_track(config, simulation=False)

    config["localization"]["track"] = "lidar"
    settings.require_lidar_track(config, simulation=False)


def test_maps_dir_defaults_into_the_repo(lidar_config: dict) -> None:
    assert settings.maps_dir(lidar_config).name == "maps"


def test_maps_dir_honours_the_configured_path(lidar_config: dict) -> None:
    config = {**lidar_config, "lidar": {**lidar_config["lidar"], "maps_dir": "/tmp/elsewhere"}}
    assert settings.maps_dir(config) == Path("/tmp/elsewhere")


def test_parameter_builders_read_from_the_config(lidar_config: dict) -> None:
    plan = settings.plan_params_from_config(lidar_config)
    match = settings.match_params_from_config(lidar_config)
    low, high = settings.range_from_config(lidar_config)

    section = lidar_config["lidar"]
    expected = (section["robot_radius_mm"] + section["tracking_margin_mm"]) / 1000.0
    assert plan.clearance_m == pytest.approx(expected)
    assert match.min_known_cells == section["min_known_cells"]
    assert (low, high) == pytest.approx(
        (section["range_min_mm"] / 1000.0, section["range_max_mm"] / 1000.0)
    )


# ══════════════════════════════════════════════════════════════
#  시뮬레이션 — host/slam/simulation.py
# ══════════════════════════════════════════════════════════════


def test_move_with_zero_step_does_not_move() -> None:
    params = simulation.SimParams(200.0, 25.0, 0.0, 0.0, 90, 8.0)
    pose = (1.0, 2.0, 0.5)
    assert simulation.apply_move(pose, 0.0, 20.0, 0.1, params) == pose


def test_forward_move_advances_along_the_heading() -> None:
    params = simulation.SimParams(200.0, 25.0, 0.0, 0.0, 90, 8.0)
    x, y, yaw = simulation.apply_move((0.0, 0.0, 0.0), 100.0, 0.0, 1.0, params)
    assert x == pytest.approx(0.2, abs=1e-6), "명목 200mm/s x 1s"
    assert y == pytest.approx(0.0, abs=1e-6)
    assert yaw == pytest.approx(0.0)


def test_yaw_follows_the_angle_alone() -> None:
    """⚠️ **요는 `angle` 단독으로 결정된다 — `step` 의 부호를 곱하지 않는다.**

    예전 이 시험은 `move(-60,-20)` 이 반시계로 돈다고 단정했다(모델에 `sign(step)`
    이 곱해져 있었기 때문이다). 2026-09-11 실기가 반증했다 — 후진에서도 `angle`
    양수가 반시계이며, 벤더 API 원형이 `move(speed_x, angle_rate)` 인 것과 맞는다.
    """
    params = simulation.SimParams(200.0, 25.0, 0.0, 0.0, 90, 8.0)
    _, _, forward_yaw = simulation.apply_move((0.0, 0.0, 0.0), 60.0, 20.0, 1.0, params)
    _, _, reverse_same = simulation.apply_move((0.0, 0.0, 0.0), -60.0, 20.0, 1.0, params)
    _, _, reverse_opposite = simulation.apply_move((0.0, 0.0, 0.0), -60.0, -20.0, 1.0, params)
    assert forward_yaw > 0
    assert reverse_same == pytest.approx(forward_yaw), "같은 angle 은 걸음 방향과 무관하게 같은 요"
    assert reverse_opposite < 0, "후진에 음수 조향이면 시계 방향이다"


def test_waypoint_walk_turns_before_it_steps() -> None:
    """매핑은 사람이 옮기는 작업이라 제자리 회전 제약이 없다 (ADR-7)."""
    x, y, yaw = simulation.waypoint_walk((0.0, 0.0, 0.0), (0.0, 1.0), 0.1, 0.05)
    assert (x, y) == (0.0, 0.0), "먼저 방향만 맞춘다"
    assert yaw == pytest.approx(0.05)


def test_waypoint_walk_snaps_on_arrival() -> None:
    assert simulation.waypoint_walk((0.0, 0.0, 0.0), (0.02, 0.0), 0.1, 0.5)[:2] == (0.02, 0.0)


def test_ray_hit_misses_a_parallel_segment() -> None:
    assert simulation.ray_hit((0.0, 0.0), (1.0, 0.0), (0.0, 1.0, 5.0, 1.0)) is None


def test_ray_hit_ignores_segments_behind_the_ray() -> None:
    assert simulation.ray_hit((0.0, 0.0), (1.0, 0.0), (-1.0, -1.0, -1.0, 1.0)) is None


def test_scan_world_drops_beams_at_the_configured_rate() -> None:
    """유실률 1.0 이면 한 점도 남지 않는다 — 빔 유실 경로가 실제로 돈다."""
    import random

    params = simulation.SimParams(200.0, 25.0, 0.0, 1.0, 60, 8.0)
    assert (
        simulation.scan_world((3.0, 2.5, 0.0), simulation.DEFAULT_ROOM, params, random.Random(1))
        == []
    )


def test_sim_params_come_from_the_config(lidar_config: dict) -> None:
    params = simulation.sim_params_from_config(lidar_config, 8.0)
    assert params.beams == lidar_config["lidar"]["sim"]["beams"]
    assert params.range_max_m == 8.0


# ══════════════════════════════════════════════════════════════
#  시각화 — host/slam/viz.py
# ══════════════════════════════════════════════════════════════


def test_unknown_cells_render_grey_not_white() -> None:
    """⚠️ 미관측을 흰색으로 칠하면 *가 본 빈 공간*과 구별할 수 없다.

    구역을 클릭할 때 그 둘을 눈으로 갈라야 하고, 계획도 다르게 본다.
    """
    grid = OccupancyGrid(MapMeta(0.05, 0.0, 0.0, 3, 1))
    grid.cells[0, 0] = 5.0
    grid.cells[0, 1] = -5.0
    image = viz.to_image(grid)
    assert image[0, 0] < 0.1, "막힘은 검정"
    assert image[0, 2] == pytest.approx(0.5), "미관측은 중간 회색"
    assert image[0, 1] > 0.9, "빈곳은 흰색"


def test_save_png_writes_a_file(tmp_path: Path) -> None:
    path = viz.save_png(room(20, 20), tmp_path / "nested" / "map.png")
    assert path.is_file()
    assert path.stat().st_size > 0


def test_live_map_disabled_touches_no_backend() -> None:
    """`--plot` 없이 돌 때 matplotlib 를 아예 부르지 않아야 한다."""
    live = viz.LiveMap(room(10, 10), title="off", enabled=False)
    live.update((0.0, 0.0, 0.0), [(0.0, 0.0)])
    live.close()
    assert not live.enabled


# ══════════════════════════════════════════════════════════════
#  tools/lidar_slam.py — ① 매핑
# ══════════════════════════════════════════════════════════════


def test_slam_parser_defaults() -> None:
    args = lidar_slam.build_parser().parse_args(["--simulate"])
    assert args.simulate is True
    assert args.device is None
    assert args.steps > 0


def test_slam_main_needs_a_device_or_simulate(capsys: pytest.CaptureFixture[str]) -> None:
    assert lidar_slam.main([]) == 2
    assert "--simulate" in capsys.readouterr().err


def test_slam_simulation_writes_both_map_formats(tmp_path: Path, lidar_config: dict) -> None:
    """매핑 한 바퀴가 실제로 돌고 **네 파일**이 나온다."""
    args = lidar_slam.build_parser().parse_args(
        ["--simulate", "--steps", "3", "--seed", "1", "--out", str(tmp_path)]
    )
    assert lidar_slam.run(args, lidar_config) == 0
    for name in ("slam_map.npy", "map_meta.json", "slam_map.pgm", "slam_map.yaml"):
        assert (tmp_path / name).is_file(), name
    assert OccupancyGrid.load(tmp_path).known_cells() > 0


def test_collect_real_ignores_discarded_packets(lidar_config: dict) -> None:
    """⚠️ 폐기된 패킷은 배치에 세지 않는다 — 규칙 ③ 과 같은 판단이다."""

    class FakeSocket:
        def __init__(self) -> None:
            self.queue = [
                b"not json at all",
                encode_scan(
                    seq=1,
                    ts_ms=1,
                    device_id="lidar-a",
                    boot_id="b",
                    points_wire=[[0.0, 1000], [90.0, 1500]],
                ).encode("utf-8"),
            ]

        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            if not self.queue:
                raise TimeoutError
            return self.queue.pop(0), ("127.0.0.1", 1)

    del lidar_config
    batch = lidar_slam.collect_real(FakeSocket(), ScanDecoder(), 2)
    assert len(batch) == 1, "깨진 패킷은 세지 않았다"
    assert len(batch[0].points) == 2


def test_collect_real_ignores_another_lidar_device() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.queue = [
                encode_scan(
                    seq=1,
                    ts_ms=1,
                    device_id="lidar-other",
                    boot_id="a",
                    points_wire=[[0.0, 1000]],
                ).encode(),
                encode_scan(
                    seq=1,
                    ts_ms=2,
                    device_id="lidar-wanted",
                    boot_id="b",
                    points_wire=[[0.0, 1200]],
                ).encode(),
            ]

        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            if not self.queue:
                raise TimeoutError
            return self.queue.pop(0), ("127.0.0.1", 1)

    batch = lidar_slam.collect_real(
        FakeSocket(), ScanDecoder(), 1, expected_device_id="lidar-wanted"
    )
    assert len(batch) == 1
    assert batch[0].device_id == "lidar-wanted"


def test_collect_real_warns_on_unknown_type() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.sent = False

        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            if self.sent:
                raise TimeoutError
            self.sent = True
            payload = {
                "seq": 1,
                "ts": 1,
                "type": "SCAN_V9",
                "device_id": "a",
                "boot_id": "b",
                "points": [],
            }
            return json.dumps(payload).encode("utf-8"), ("127.0.0.1", 1)

    assert lidar_slam.collect_real(FakeSocket(), ScanDecoder(), 1) == []


def test_scan_socket_binds_and_closes(lidar_config: dict) -> None:
    del lidar_config
    sock = lidar_slam.open_scan_socket(0, 0.01)
    try:
        assert sock.getsockname()[1] > 0
        with pytest.raises((TimeoutError, OSError)):
            sock.recvfrom(64)
    finally:
        sock.close()


# ══════════════════════════════════════════════════════════════
#  tools/zone_select.py — ② 구역 지정
# ══════════════════════════════════════════════════════════════


def test_zone_parser_list_flag() -> None:
    assert zone_select.build_parser().parse_args(["--list"]).list is True


def test_place_snaps_off_a_wall(capsys: pytest.CaptureFixture[str]) -> None:
    from host.behavior.zones import ZoneStore

    grid = room(40, 40)
    store = ZoneStore(("A",))
    zone_select.place(store, grid, 0.025, 1.0, -1.0)  # 왼쪽 벽 위
    assert len(store) == 1
    assert "옮겼다" in capsys.readouterr().out


def test_place_refuses_outside_the_map(capsys: pytest.CaptureFixture[str]) -> None:
    from host.behavior.zones import ZoneStore

    store = ZoneStore(("A",))
    zone_select.place(store, room(20, 20), 99.0, 99.0, -1.0)
    assert len(store) == 0
    assert "범위 밖" in capsys.readouterr().out


def test_place_reports_when_labels_run_out(capsys: pytest.CaptureFixture[str]) -> None:
    from host.behavior.zones import ZoneStore

    grid = room(40, 40)
    store = ZoneStore(("A",))
    zone_select.place(store, grid, 1.0, 1.0, -1.0)
    zone_select.place(store, grid, 1.2, 1.2, -1.0)
    assert "zones.ids" in capsys.readouterr().out


def test_place_reports_when_nothing_free_is_near(capsys: pytest.CaptureFixture[str]) -> None:
    from host.behavior.zones import ZoneStore

    grid = OccupancyGrid(MapMeta(0.05, 0.0, 0.0, 20, 20))  # 전부 미관측
    grid.cells[10, 10] = 3.0
    store = ZoneStore(("A",))
    zone_select.place(store, grid, *grid.to_world(10, 10), -1.0)
    assert len(store) == 0
    assert "빈 셀이 없다" in capsys.readouterr().out


def test_zone_list_prints_saved_zones(
    tmp_path: Path, lidar_config: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_maps(tmp_path)
    args = zone_select.build_parser().parse_args(["--list", "--maps", str(tmp_path)])
    assert zone_select.run(args, lidar_config) == 0
    out = capsys.readouterr().out
    assert "A = " in out and "3 / 3" in out


def test_zone_list_says_when_empty(
    tmp_path: Path, lidar_config: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    args = zone_select.build_parser().parse_args(["--list", "--maps", str(tmp_path)])
    assert zone_select.run(args, lidar_config) == 0
    assert "저장된 구역이 없다" in capsys.readouterr().out


def test_zone_main_reports_a_missing_map(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert zone_select.main(["--maps", str(tmp_path)]) == 2
    assert "지도가 없다" in capsys.readouterr().err


# ══════════════════════════════════════════════════════════════
#  tools/patrol_run.py — ③ 순찰
# ══════════════════════════════════════════════════════════════


def test_patrol_parser_groups_simulation_options() -> None:
    args = patrol_run.build_parser().parse_args(["--simulate", "--cycles", "2", "--ticks", "50"])
    assert (args.simulate, args.cycles, args.ticks) == (True, 2, 50)


def test_patrol_main_needs_a_device_or_simulate(capsys: pytest.CaptureFixture[str]) -> None:
    assert patrol_run.main([]) == 2
    assert "--simulate" in capsys.readouterr().err


def test_patrol_main_reports_a_missing_map(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert patrol_run.main(["--simulate", "--maps", str(tmp_path)]) == 2
    assert "지도가 없다" in capsys.readouterr().err


def test_build_controller_refuses_without_zones(tmp_path: Path, lidar_config: dict) -> None:
    """구역 좌표 없이 순찰을 시작하면 갈 곳이 없다."""
    room().save(tmp_path)
    with pytest.raises(ConfigError, match="zone_select"):
        patrol_run.build_controller(lidar_config, tmp_path, seed=1)


def test_build_controller_uses_the_configured_command_rate(
    tmp_path: Path, lidar_config: dict
) -> None:
    """주기는 `network.cmd_rate_hz` 에서 온다 — 코드에 100ms 를 박지 않는다."""
    seed_maps(tmp_path)
    controller = patrol_run.build_controller(lidar_config, tmp_path, seed=1)
    expected = round(1000 / float(lidar_config["network"]["cmd_rate_hz"]))
    assert controller.commander.period_ms == expected
    assert len(controller.zones) == 3


def test_simulated_patrol_emits_only_valid_protocol_lines(
    tmp_path: Path, lidar_config: dict
) -> None:
    """⚠️ **로봇이 보는 것과 같은 검증을 통과해야 한다.**

    의도만 확인하면 "컨트롤러는 옳은데 나가는 전문은 틀린" 상태를 잡지 못한다.
    """
    from host.common.protocol import CommandDecoder, Verdict

    seed_maps(tmp_path)
    controller = patrol_run.build_controller(lidar_config, tmp_path, seed=5)
    emitted: list[str] = []
    real_tick = controller.commander.tick

    def spy(now_ms: int) -> list[str]:
        lines = real_tick(now_ms)
        emitted.extend(lines)
        return lines

    controller.commander.tick = spy  # type: ignore[method-assign]
    args = patrol_run.build_parser().parse_args(
        ["--simulate", "--cycles", "1", "--ticks", "60", "--maps", str(tmp_path)]
    )
    assert patrol_run.serve_simulated(args, lidar_config, controller) == 0
    assert emitted, "전문이 나가야 한다"

    decoder = CommandDecoder()
    for line in emitted:
        result = decoder.decode(line)
        assert result.accepted, f"로봇이 폐기한다: {line} — {result.reason}"
        assert result.verdict is not Verdict.CLAMP, f"클램핑되어 나갔다: {line}"
    assert controller.stats.scans > 0


def test_simulated_patrol_stops_on_the_tick_budget(tmp_path: Path, lidar_config: dict) -> None:
    """무한 루프 방지 장치가 실제로 동작한다."""
    seed_maps(tmp_path)
    controller = patrol_run.build_controller(lidar_config, tmp_path, seed=5)
    args = patrol_run.build_parser().parse_args(
        ["--simulate", "--cycles", "0", "--ticks", "5", "--maps", str(tmp_path)]
    )
    assert patrol_run.serve_simulated(args, lidar_config, controller) == 0
    assert controller.stats.cycles == 0


def test_simulated_patrol_verbose_prints_state(
    tmp_path: Path, lidar_config: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_maps(tmp_path)
    controller = patrol_run.build_controller(lidar_config, tmp_path, seed=5)
    args = patrol_run.build_parser().parse_args(
        ["--simulate", "--ticks", "3", "--verbose", "--maps", str(tmp_path)]
    )
    patrol_run.serve_simulated(args, lidar_config, controller)
    out = capsys.readouterr().out
    assert "STATE=" in out, "내려보내는 STATE 를 함께 찍어야 대조할 수 있다"


def test_send_is_a_noop_without_a_peer() -> None:
    """상대를 아직 모르면 조용히 넘어간다 — 예외로 루프를 죽이지 않는다."""
    patrol_run.send(None, None, ["{}"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        patrol_run.send(sock, None, ["{}"])
    finally:
        sock.close()


def test_send_delivers_to_a_loopback_listener() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(1.0)
        patrol_run.send(sender, listener.getsockname(), ['{"seq":1}'])
        payload, _ = listener.recvfrom(64)
        assert json.loads(payload)["seq"] == 1
    finally:
        listener.close()
        sender.close()


def test_patrol_shutdown_repeats_the_same_estop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lidar_config: dict
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.payloads: list[bytes] = []

        def sendto(self, payload: bytes, _peer: tuple[str, int]) -> None:
            self.payloads.append(payload)

    seed_maps(tmp_path)
    controller = patrol_run.build_controller(lidar_config, tmp_path, seed=1)
    sock = FakeSocket()
    monkeypatch.setattr(patrol_run.time, "sleep", lambda _seconds: None)
    patrol_run.stop_for_shutdown(controller, sock, ("127.0.0.1", 5001))  # type: ignore[arg-type]
    assert len(sock.payloads) == patrol_run.SHUTDOWN_ESTOP_REPEATS
    assert len(set(sock.payloads)) == 1
    assert json.loads(sock.payloads[0])["type"] == "ESTOP"


def test_open_socket_is_non_blocking() -> None:
    sock = patrol_run.open_socket(0)
    try:
        with pytest.raises((BlockingIOError, OSError)):
            sock.recvfrom(64)
    finally:
        sock.close()


def test_fake_reading_exposes_the_protocol_field_names() -> None:
    """규약의 이름을 그대로 쓴다 — `yaw` 가 아니라 `imu.yaw` 다."""
    reading = patrol_run._FakeReading(state="PATROL", imu={"yaw": 12.0})
    assert reading.state == "PATROL"
    assert reading.imu["yaw"] == 12.0
    assert reading.safety_latched is None


# ══════════════════════════════════════════════════════════════
#  tools/mock_lidar.py — 가상 중계 노드
# ══════════════════════════════════════════════════════════════


def test_boot_id_is_sixteen_hex_digits() -> None:
    """규약 권장 형식 — 난수 64비트의 16자리 hex."""
    import random

    boot_id = mock_lidar.new_boot_id(random.Random(1))
    assert len(boot_id) == 16
    int(boot_id, 16)


def test_boot_id_changes_between_boots() -> None:
    import random

    rng = random.Random(1)
    assert mock_lidar.new_boot_id(rng) != mock_lidar.new_boot_id(rng)


def test_mock_parser_fault_options() -> None:
    args = mock_lidar.build_parser().parse_args(["--drop-rate", "0.5", "--reboot-at", "3"])
    assert (args.drop_rate, args.reboot_at) == (0.5, 3)


def test_mock_lidar_sends_scans_the_host_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **목업이 내는 전문이 우리 디코더를 통과해야 한다.**

    목업이 규약과 어긋나면 시험이 목업만 검증하고 실기에서 처음 막힌다.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(1.0)
    port = listener.getsockname()[1]

    ticks = {"n": 0}

    def stop_after_three(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(mock_lidar.time, "sleep", stop_after_three)

    args = mock_lidar.build_parser().parse_args(
        ["--host", "127.0.0.1", "--port", str(port), "--walk", "--seed", "1", "--reboot-at", "2"]
    )
    try:
        assert mock_lidar.run(args) == 0
        decoder = ScanDecoder()
        payload, _ = listener.recvfrom(65536)
        result = decoder.decode(payload)
        assert result.accepted, result.reason
    finally:
        listener.close()


def test_mock_lidar_corrupt_rate_produces_discarded_packets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """깨진 패킷은 호스트가 폐기하고 **링크 카운터를 갱신하지 않는다** (규칙 ③)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(1.0)
    port = listener.getsockname()[1]

    def stop_now(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(mock_lidar.time, "sleep", stop_now)
    args = mock_lidar.build_parser().parse_args(
        ["--host", "127.0.0.1", "--port", str(port), "--corrupt-rate", "1.0", "--seed", "2"]
    )
    try:
        mock_lidar.run(args)
        payload, _ = listener.recvfrom(65536)
        result = ScanDecoder().decode(payload)
        assert not result.accepted
        assert not result.refreshes_link
    finally:
        listener.close()


def test_mock_lidar_drop_rate_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(0.2)
    port = listener.getsockname()[1]

    def stop_now(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(mock_lidar.time, "sleep", stop_now)
    args = mock_lidar.build_parser().parse_args(
        ["--host", "127.0.0.1", "--port", str(port), "--drop-rate", "1.0", "--seed", "3"]
    )
    try:
        mock_lidar.run(args)
        with pytest.raises((TimeoutError, OSError)):
            listener.recvfrom(64)
    finally:
        listener.close()


# ══════════════════════════════════════════════════════════════
#  경로 계획 보조 — 위 시험들이 지나지 않는 갈래
# ══════════════════════════════════════════════════════════════


def test_forward_fan_ignores_beams_behind_the_robot() -> None:
    import math

    from host.behavior.planner import min_forward_distance

    behind = ((math.pi, 0.2),)
    assert min_forward_distance(behind, math.radians(20)) is None
    ahead = ((0.0, 0.4), (math.radians(10), 0.3))
    assert min_forward_distance(ahead, math.radians(20)) == pytest.approx(0.3)


def test_astar_refuses_a_start_outside_the_grid() -> None:
    from host.behavior.planner import astar

    blocked = np.zeros((10, 10), dtype=bool)
    assert astar((-1, -1), (5, 5), blocked) is None


def test_plan_to_reports_an_unreachable_goal() -> None:
    from host.behavior.planner import PlanParams, plan_to

    grid = OccupancyGrid(MapMeta(0.05, 0.0, 0.0, 40, 40))  # 전부 미관측 = 통행 불가
    plan = plan_to(
        "A",
        (1.0, 1.0),
        (0.2, 0.2),
        grid,
        np.ones((40, 40), dtype=bool),
        PlanParams(1.0, -1.0, 0.15, 0.08),
    )
    assert not plan.reachable
    assert plan.label is None
