"""호스트 런타임의 LiDAR 길 찾기 (`--lidar-device`) — 로봇·소켓 없이 닫는다.

순찰(`PATROL`)이 고정 보행 대신 `PatrolController.steer` 로 걷고, 측위 자세가 구역
점검에 닿고, 경보·점검에서 돌아오면 같은 목표로 다시 풀고, 경로 막힘은 가벼운 경고
한 건으로 끝나는지를 본다. 스캔 수신기의 전방 위험거리 ESTOP 은 길 찾기 상태를 만지지 않는다.
"""

from __future__ import annotations

import json
import random

import pytest
from conftest import FakeClock
from test_lidar_patrol import DRIVE, MATCH, PLAN, open_room
from test_runtime import DEVICE, FakeSocket, FakeVision, config, telemetry, vision_result

from host.behavior.commander import Commander
from host.behavior.fsm import Event
from host.behavior.patrol import PatrolController, Phase
from host.behavior.zones import ZoneStore
from host.common.config import ConfigError
from host.common.lidar_link import Scan, ScanDecoder
from host.common.protocol import TelemetryEncoder
from host.runtime import Runtime
from host.telemetry.lidar_feed import LidarFeed

__all__ = ["config"]  # 픽스처를 다시 쓴다 (`test_runtime.config`)

SCAN = Scan("lidar-01", "0" * 16, 1, 0, ())


def _navigator(commander: Commander) -> PatrolController:
    zones = ZoneStore(("A", "B", "C"))
    zones.place(1.0, 1.0)
    zones.place(4.0, 1.0)
    zones.place(4.0, 3.5)
    return PatrolController(
        commander=commander,
        grid=open_room(),
        zones=zones,
        drive=DRIVE,
        plan_params=PLAN,
        match_params=MATCH,
        range_m=(0.12, 8.0),
        new_obstacle_margin_m=0.25,
        new_obstacle_confirmations=2,
        new_obstacle_check_radius_m=1.5,
        obstacle_mark_radius_m=0.3,
        forward_fan_rad=0.35,
        random_after_first_cycle=False,
        rng=random.Random(7),
    )


def _runtime(config: dict, clock: FakeClock, **kwargs: object) -> tuple[Runtime, PatrolController]:
    runtime = Runtime(config, device_id=DEVICE, clock=clock, navigator_factory=_navigator, **kwargs)
    navigator = runtime._navigator
    assert navigator is not None
    assert navigator.commander is runtime.commander, "송신기는 하나여야 한다 (seq)"
    return runtime, navigator


def _fresh(navigator: PatrolController, now: int, pose: tuple[float, float, float]) -> None:
    navigator.pose = pose
    navigator._last_pose_ms = now


def _patrolling(config: dict, clock: FakeClock, **kwargs: object):
    runtime, navigator = _runtime(config, clock, **kwargs)
    runtime.attach_scans(lambda: None)  # 스캔은 없고, 측위 자세는 `_fresh` 로 준다
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime.ingest(telemetry(enc), clock.ms)
    assert runtime.start_patrol(clock.ms)
    _fresh(navigator, clock.ms, (2.0, 2.0, 0.0))
    runtime.tick(clock.ms)
    assert runtime.behavior.state == "PATROL"
    return runtime, navigator


# ── 순찰이 길 찾기로 걷는다 ─────────────────────────────────
def test_patrol_walks_by_the_navigator(config: dict, clock: FakeClock) -> None:
    runtime, navigator = _patrolling(config, clock)
    assert navigator.target is not None
    assert navigator.phase is Phase.MOVING
    assert runtime.commander.intent.type_ == "MOVE"


def test_stale_pose_stands_the_patrol_still(config: dict, clock: FakeClock) -> None:
    runtime, navigator = _patrolling(config, clock)
    clock.advance(DRIVE.pose_timeout_ms + 100)
    runtime.tick(clock.ms)
    assert runtime.commander.intent.type_ == "STOP"
    assert runtime.behavior.state == "PATROL", "측위 상실은 FSM 전이가 아니라 정지다"


def test_telemetry_reaches_the_navigator(config: dict, clock: FakeClock) -> None:
    runtime, navigator = _runtime(config, clock)
    enc = TelemetryEncoder(device_id=DEVICE, boot_id="boot-1")
    runtime.ingest(
        telemetry(
            enc, flags={"lowbatt": False, "tipped": False, "link_ok": True, "obstacle": True}
        ),
        clock.ms,
    )
    assert navigator.safety.obstacle_active


# ── 측위 자세 → 구역 점검 ─────────────────────────────────
def test_matched_pose_reaches_the_zone_inspector(
    config: dict, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, navigator = _runtime(config, clock)
    runtime.attach_scans(lambda: SCAN)

    def matched(_scan: Scan, now_ms: int) -> None:
        _fresh(navigator, now_ms, (1.5, 1.0, 0.2))

    monkeypatch.setattr(navigator, "observe_scan", matched)
    runtime.tick(clock.ms)
    assert runtime._zone_inspector._fresh_pose(clock.ms) == (1.5, 1.0, 0.2)


def test_failed_match_does_not_refresh_the_zone_pose(
    config: dict, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, navigator = _runtime(config, clock)
    runtime.attach_scans(lambda: SCAN)
    monkeypatch.setattr(navigator, "observe_scan", lambda _scan, _now: None)
    runtime.tick(clock.ms)
    assert runtime._zone_inspector._fresh_pose(clock.ms) is None


# ── 돌아오면 같은 목표로 다시 푼다 ─────────────────────────
def test_reentering_patrol_replans_to_the_same_unvisited_zone(
    config: dict, clock: FakeClock
) -> None:
    """C→D 로 가던 중 B 에서 쓰러짐을 처리했다면, 확인 뒤에도 D 로 간다 (시연 시나리오)."""
    runtime, navigator = _patrolling(config, clock)
    target = navigator.target
    assert target is not None
    assert runtime._apply(Event.SCAN_DUE, clock.ms)
    _fresh(navigator, clock.ms, (3.0, 3.0, 0.0))  # 떠나 있는 동안 자리가 바뀌었다
    assert runtime._apply(Event.SCAN_DONE, clock.ms)
    runtime.tick(clock.ms)
    assert navigator.target == target
    assert navigator.plan.reachable
    assert target not in navigator.visited


def test_patrol_reentry_resumes_the_navigator_on_the_loop_tick(
    config: dict, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """대시보드 스레드의 전이(`apply_external`)는 길 찾기 상태를 만지지 않는다.

    진입 훅은 표시만 하고, 다시 푸는 것은 다음 틱의 순찰 시퀀스(루프 스레드)다. 훅에서
    바로 풀면 루프가 웨이포인트를 따라가는 중에 경로가 비워진다.
    """
    runtime, _navigator = _patrolling(config, clock)
    resumed: list[int] = []
    real = PatrolController.resume
    monkeypatch.setattr(
        PatrolController, "resume", lambda self: (resumed.append(clock.ms), real(self))[1]
    )
    assert runtime._apply(Event.SCAN_DUE, clock.ms)
    assert runtime.apply_external(Event.SCAN_DONE)
    assert runtime.behavior.state == "PATROL"
    assert resumed == [], "전이한 스레드에서 풀지 않는다"
    runtime.tick(clock.ms)
    assert resumed == [clock.ms]
    runtime.tick(clock.ms + 100)
    assert resumed == [clock.ms], "한 번 진입에 한 번만 푼다"


# ── 경로 막힘 = 가벼운 경고 하나 ───────────────────────────
def test_path_blocked_is_recorded_once_without_escalating(
    config: dict, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    vision = FakeVision()
    vision.result = vision_result(1, clock.ms, present=False, hits=0, last_seen_ms=None)
    runtime, navigator = _patrolling(config, clock, vision=vision)
    records: list[tuple] = []
    monkeypatch.setattr(runtime, "_record_scene", lambda *args: records.append(args))
    level = runtime.escalation.level
    navigator._new_obstacles.append((2.5, 2.0))
    target = navigator.target
    runtime.tick(clock.ms)
    runtime.tick(clock.advance(100))
    assert len(records) == 1
    event_type, result, judgement = records[0]
    assert event_type == "path_blocked"
    assert result is vision.result
    assert judgement == {"x": 2.5, "y": 2.0, "target": target, "source": "lidar"}
    assert runtime.behavior.state == "PATROL"
    assert runtime.escalation.level == level


def test_path_blocked_without_a_frame_still_announces(
    config: dict, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import host.runtime as runtime_module

    said: list[str] = []
    monkeypatch.setattr(runtime_module, "describe", lambda kind, _j: f"{kind} 문장")
    runtime, navigator = _patrolling(config, clock, announcer=said.append)
    navigator._new_obstacles.append((2.5, 2.0))
    runtime.tick(clock.ms)
    assert said == ["path_blocked 문장"]
    assert runtime.behavior.state == "PATROL"


def test_obstacle_outside_patrol_is_not_a_path_block(
    config: dict, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """추적·경보 중 앞에 선 사람은 «경로 막힘» 이 아니다."""
    runtime, navigator = _patrolling(config, clock)
    records: list[tuple] = []
    monkeypatch.setattr(runtime, "_record_scene", lambda *args: records.append(args))
    assert runtime._apply(Event.SCAN_DUE, clock.ms)
    navigator._new_obstacles.append((2.5, 2.0))
    runtime.tick(clock.ms)
    assert records == []


# ── 스캔 수신기의 전방 위험거리 ─────────────────────────────
def _raw(device_id: str, dist_mm: int, seq: int = 1) -> bytes:
    return json.dumps(
        {
            "seq": seq,
            "ts": 1,
            "type": "SCAN",
            "device_id": device_id,
            "boot_id": "b",
            "points": [[0.0, dist_mm]],
        }
    ).encode("utf-8")


def _feed(armed: bool, on_danger) -> LidarFeed:
    return LidarFeed(
        lidar_device="lidar-01",
        decoder=ScanDecoder(),
        forward_fan_rad=0.35,
        estop_m=0.1,
        armed=lambda: armed,
        on_danger=on_danger,
    )


def test_close_scan_sends_estop_without_touching_the_navigator(
    config: dict, clock: FakeClock
) -> None:
    runtime, navigator = _patrolling(config, clock)
    sock = FakeSocket(clock)
    runtime.begin(sock)
    phase, target = navigator.phase, navigator.target
    feed = _feed(True, runtime.send_emergency_stop)
    feed.handle(_raw("lidar-01", 50))
    assert "ESTOP" in sock.types()
    assert (navigator.phase, navigator.target, navigator.stats.estops) == (phase, target, 0)
    assert feed.take() is not None
    assert feed.take() is None, "한 번 꺼내면 비워진다"


def test_far_or_disarmed_scan_does_not_stop() -> None:
    calls: list[int] = []
    _feed(True, lambda: calls.append(1)).handle(_raw("lidar-01", 2000))
    disarmed = _feed(False, lambda: calls.append(1))
    disarmed.handle(_raw("lidar-01", 50))
    assert calls == []
    assert disarmed.take() is not None, "걷지 않을 때도 측위용 스캔은 남긴다"


def test_foreign_lidar_scan_is_dropped() -> None:
    calls: list[int] = []
    feed = _feed(True, lambda: calls.append(1))
    feed.handle(_raw("lidar-99", 50))
    assert calls == []
    assert feed.take() is None


# ── `--lidar-device` 가 없으면 아무것도 바뀌지 않는다 ─────────
def _cli(config: dict, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict:
    import host.runtime as runtime_module

    captured: dict = {"feeds": []}

    class CliRuntime:
        telemetry_port = 5101

        def __init__(self, _cfg, **kwargs) -> None:
            captured.update(kwargs)

        def attach_scans(self, take) -> None:
            captured["take"] = take

        def send_emergency_stop(self) -> str:
            return ""

        def serve(self, _sock, *, duration_s=None) -> None:  # noqa: ARG002
            captured["served"] = True

    class CliSocket:
        def close(self) -> None:
            pass

    class CliFeed:
        def __init__(self) -> None:
            self.events: list[str] = []

        def take(self) -> None:
            return None

        def start(self) -> None:
            self.events.append("start")

        def stop(self) -> None:
            self.events.append("stop")

    def open_feed(_cfg, lidar_device, **_kw):
        feed = CliFeed()
        captured["feeds"].append((lidar_device, feed))
        return feed

    monkeypatch.setattr(runtime_module, "load_config", lambda _device: config)
    monkeypatch.setattr(runtime_module, "setup_logging", lambda *_args, **_kw: None)
    monkeypatch.setattr(runtime_module, "build_worker", lambda _cfg: FakeVision())
    monkeypatch.setattr(runtime_module, "EventBlackbox", lambda _cfg: object())
    monkeypatch.setattr(runtime_module, "Runtime", CliRuntime)
    monkeypatch.setattr(runtime_module, "open_socket", lambda _port: CliSocket())
    monkeypatch.setattr(runtime_module, "open_lidar_feed", open_feed)
    monkeypatch.setattr(
        runtime_module, "load_patrol_map", lambda _cfg, _maps: (open_room(), ZoneStore(("A",)))
    )
    captured["code"] = runtime_module.main(["--device", DEVICE, "--duration", "0", *argv])
    return captured


def test_cli_without_lidar_device_keeps_the_fixed_patrol(
    config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _cli(config, monkeypatch, [])
    assert captured["code"] == 0
    assert captured["navigator_factory"] is None
    assert captured["feeds"] == []
    assert "take" not in captured


def test_cli_lidar_device_wires_navigator_and_feed(
    config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _cli(config, monkeypatch, ["--lidar-device", "lidar-01"])
    assert captured["code"] == 0
    assert captured["navigator_factory"] is not None
    [(lidar_device, feed)] = captured["feeds"]
    assert lidar_device == "lidar-01"
    assert captured["take"] == feed.take
    assert feed.events == ["start", "stop"], "종료 때 닫는다"


def test_cli_lidar_device_without_zones_refuses_to_start(
    config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    import host.runtime as runtime_module

    def no_zones(_cfg, _maps):
        raise ConfigError("구역 좌표가 없다")

    monkeypatch.setattr(runtime_module, "load_config", lambda _device: config)
    monkeypatch.setattr(runtime_module, "load_patrol_map", no_zones)
    assert runtime_module.main(["--device", DEVICE, "--lidar-device", "lidar-01"]) == 2


def test_cli_lidar_device_with_a_broken_map_refuses_to_start(
    config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """깨진 지도·구역 파일도 traceback 이 아니라 기동 거부(rc 2)다."""
    import host.runtime as runtime_module

    def broken(_cfg, _maps):
        raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(runtime_module, "load_config", lambda _device: config)
    monkeypatch.setattr(runtime_module, "load_patrol_map", broken)
    assert runtime_module.main(["--device", DEVICE, "--lidar-device", "lidar-01"]) == 2
