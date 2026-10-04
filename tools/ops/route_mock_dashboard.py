"""동선 편집·주행 UI 확인용 로컬 SIM. 로봇/센서 소켓을 열지 않는다.

python tools/ops/route_mock_dashboard.py --maps <지도> --output <새 작업폴더>
    --start x,y,yaw_deg --port 8016

원본 지도를 복사한 뒤 실제 계획 API·PatrolController·Commander를 사용한다.
처음에는 운동 모델의 참 자세를 주입한다. 위치 힌트를 주면 참 자세 주입을 끊고
합성 스캔을 기존 정합·표결 경로로 처리한다. 실물 측위/보행 정확도 검증은 아니다.
개인 지도·좌표·화면은 Git 제외된 로컬 output만 사용해야 한다.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.fsm import Behavior, Event, Fsm
from host.behavior.patrol import Phase
from host.behavior.routes import route_digest
from host.behavior.zone_map import ZoneMap
from host.common.config import DEFAULT_CONFIG, DEFAULT_DEVICES_DIR, load_config
from host.common.console import survive_encoding_errors
from host.common.lidar_link import Scan, points_from_wire
from host.dashboard.commands import CommandService
from host.dashboard.live_map import MapView, PoseFrame, render
from host.dashboard.planning import PlanError, PlanningService
from host.dashboard.server import DEFAULT_STATIC_DIR, create_app, serving
from host.dashboard.state import DashboardState
from host.runtime import policy_view
from host.slam import settings, simulation
from tools.ops.patrol_mock import physical_hit_mask, raycast_scan
from tools.ops.patrol_run import _FakeReading, build_controller


def prepare(source: Path, output: Path, device: str) -> tuple[Path, Path, Path]:
    """지도 복사본과 서버 저장 설정을 새 폴더에 격리한다."""
    output.mkdir(parents=True, exist_ok=False)
    maps = output / "maps"
    maps.mkdir()
    for name in (
        "slam_map.npy",
        "slam_map_loc.npy",
        "map_meta.json",
        "slam_map.pgm",
        "slam_map.yaml",
        "pose_frame.json",
        "zones.json",
        "zones_plan.json",
        "zone_labels.npy",
    ):
        if (source / name).is_file():
            shutil.copyfile(source / name, maps / name)
    devices = output / "devices"
    devices.mkdir()
    shutil.copyfile(DEFAULT_DEVICES_DIR / f"{device}.yaml", devices / f"{device}.yaml")
    config = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    config["lidar"]["maps_dir"] = str(maps.resolve())
    config_path = output / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return maps, devices, config_path


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--start", required=True, help="SIM 시작 자세 x,y,yaw_deg")
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.port in (5101, 5201, 8000) or not 1024 <= args.port <= 65535:
        parser.error("현장 포트 대신 1024~65535의 별도 포트를 고르세요")
    if not math.isfinite(args.speed) or args.speed <= 0:
        parser.error("배속은 양수여야 합니다")
    start = tuple(float(value) for value in args.start.split(","))
    if len(start) != 3 or not all(math.isfinite(value) for value in start):
        parser.error("시작 자세는 유한한 x,y,yaw_deg입니다")
    maps, devices, config_path = prepare(args.maps, args.output, args.device)
    config = load_config(args.device, config_path=config_path, devices_dir=devices)
    planning = PlanningService(config, args.device, config_path=config_path, devices_dir=devices)
    controller = build_controller(config, maps, 7)
    controller.zone_map = ZoneMap.load(maps)
    controller._rebuild_masks()
    controller.pose_seeded = True
    commander = controller.commander
    behavior = Behavior(commander, Fsm(initial="IDLE"))
    lock = threading.RLock()
    now_ms = 0
    truth = (start[0], start[1], math.radians(start[2]))
    localize_from_scans = False
    feedback: dict[str, object] = {}
    state = DashboardState("SIM-route", stale_after_ms=3000)
    view = MapView(
        lambda: render(
            controller.grid,
            zones=controller.zones,
            zone_map=controller.zone_map,
            frame=PoseFrame.load(maps),
            occ_thresh=controller.plan_params.occ_thresh,
            free_thresh=controller.plan_params.free_thresh,
        )
    )

    def send(line: str) -> None:
        controller.note_sent([line], now_ms)

    def apply(event: Event) -> bool:
        with lock:
            accepted = behavior.event(event, now_ms)
            if accepted and behavior.state != "PATROL":
                controller.cancel_route("sim_operator_stop")
                controller.cancel_goal("sim_operator_stop")
                controller.phase = Phase.IDLE
                commander.halt()
            return accepted

    def begin() -> None:
        if behavior.state == "IDLE":
            behavior.event(Event.START_PATROL, now_ms)

    def goto(x: float, y: float) -> tuple[bool, str]:
        with lock:
            accepted, detail = controller.goto(x, y)
            feedback.update(accepted=accepted, detail=detail)
            if accepted:
                begin()
            return accepted, detail

    def start_route(route_id: str, expected_digest: str | None = None) -> tuple[bool, str]:
        with lock:
            try:
                route = planning.route_for_start(route_id)
            except PlanError as exc:
                return False, str(exc)
            if expected_digest is not None and route_digest(route) != expected_digest:
                return False, "동선이 바뀌었습니다. 저장본을 다시 확인하세요"
            accepted, detail = controller.start_route(route, now_ms)
            feedback.update(accepted=accepted, detail=detail)
            if accepted:
                begin()
            return accepted, detail

    def stop_route() -> tuple[bool, str]:
        with lock:
            controller.cancel_route("operator_stop")
            controller.cancel_goal("operator_stop")
            controller.phase = Phase.IDLE
            send(commander.stop_now())
            behavior.event(Event.MANUAL_ON, now_ms)
            behavior.event(Event.MANUAL_OFF, now_ms)
            return True, "SIM 동선을 정지했습니다"

    def locate_point(x: float, y: float) -> tuple[bool, str]:
        nonlocal localize_from_scans
        with lock:
            accepted, detail = controller.validate_hint_point(x, y)
            if not accepted:
                return False, detail
            send(commander.stop_now())
            if not controller.hint_point(x, y, now_ms):
                return False, "위치 힌트를 적용하지 못했습니다"
            localize_from_scans = True
            return True, "알려준 점 주변을 합성 스캔으로 다시 확인합니다"

    def locate_zone(zone: str) -> tuple[bool, str]:
        nonlocal localize_from_scans
        with lock:
            if zone not in controller.zones.labels:
                return False, "알려줄 구역이 없습니다"
            send(commander.stop_now())
            if not controller.hint_zone(zone, now_ms):
                return False, "구역 힌트를 적용하지 못했습니다"
            localize_from_scans = True
            return True, "알려준 구역을 합성 스캔으로 다시 확인합니다"

    def nav_status() -> dict[str, object]:
        with lock:
            controller._zone_filter(now_ms)
            point_hint = controller._point_hint
            return {
                "available": True,
                "frame": "patrol",
                "simulated": True,
                "simulation": (
                    "합성 스캔 정합 · 실물 측위 검증 아님"
                    if localize_from_scans
                    else "운동 모델의 참 자세 주입 · 실물 측위 검증 아님"
                ),
                "pose": [*controller.pose[:2], math.degrees(controller.pose[2])],
                "verified": controller.pose_verified,
                "seeded": controller.pose_seeded,
                "stale": controller.pose_stale(now_ms),
                "phase": controller.phase.value,
                "target": controller.target,
                "zone": controller.current_zone,
                "zone_hint": controller._zone_hint[0] if controller._zone_hint else None,
                "point_hint": (
                    None
                    if point_hint is None
                    else {
                        "x": point_hint[0],
                        "y": point_hint[1],
                        "radius": controller.point_hint_radius_m,
                    }
                ),
                "goal": controller.goal,
                "holding_goal": controller.holding_goal,
                "goal_hold_reason": controller.goal_hold_reason,
                "goal_feedback": dict(feedback),
                "route_feedback": dict(feedback),
                "route": controller.route_status(),
                "path": list(controller.plan.waypoints),
                "fsm": behavior.state,
            }

    commands = CommandService(
        behavior,
        commander,
        send,
        apply_event=apply,
        goto_point=goto,
        locate_point=locate_point,
        locate_zone=locate_zone,
        start_route=start_route,
        stop_route=stop_route,
    )
    app = create_app(
        state,
        commands,
        static_dir=DEFAULT_STATIC_DIR,
        policy=policy_view(config),
        map_view=view.get,
        nav_status=nav_status,
        planning=planning,
    )
    sim = simulation.sim_params_from_config(config, settings.range_from_config(config)[1])
    hits = physical_hit_mask(
        controller.grid,
        controller.match_grid,
        controller.plan_params.occ_thresh,
        controller.plan_params.free_thresh,
    )
    rng = random.Random(7)
    period_ms = commander.period_ms
    send(commander.open_session())
    print(f"SIM only: http://127.0.0.1:{args.port}/glass-preview/?view=zones", flush=True)
    trace_path = args.output / "route-trace.jsonl"
    with trace_path.open("w", encoding="utf-8") as trace, serving(app, args.port):
        try:
            while True:
                started = time.perf_counter()
                with lock:
                    now_ms += period_ms
                    controller.observe_telemetry(
                        _FakeReading(
                            state=behavior.state,
                            safety_latched=False,
                            obstacle=False,
                            dist_cm=100,
                            yaw=math.degrees(truth[2]),
                            last_cmd_age_ms=0,
                        ),
                        now_ms,
                    )
                    scan = Scan(
                        "route-sim",
                        "0" * 16,
                        now_ms // period_ms,
                        now_ms,
                        points_from_wire(
                            raycast_scan(hits, controller.match_grid.meta, truth, sim, rng)
                        ),
                    )
                    if localize_from_scans:
                        controller.observe_scan(scan, now_ms)
                    else:
                        controller.observe_map_pose(truth, now_ms)
                        controller.observe_obstacle_scan(scan, now_ms)
                    urgent = controller.guard_scan(scan)
                    if urgent:
                        send(urgent)
                    if behavior.state == "PATROL":
                        controller.step(now_ms)
                    else:
                        commander.halt()
                    emitted = commander.tick(now_ms)
                    controller.note_sent(emitted, now_ms)
                    for line in emitted:
                        message = json.loads(line)
                        if message["type"] == "MOVE":
                            truth = simulation.apply_move(
                                truth, message["step"], message["angle"], period_ms / 1000, sim
                            )
                    state.publish(
                        telemetry={
                            "state": behavior.state,
                            "safety_latched": False,
                            "obstacle": False,
                            "dist_cm": 100,
                            "yaw": math.degrees(truth[2]),
                        },
                        state=behavior.state,
                        escalation="NORMAL",
                        mode=config["mission"]["mode"],
                        received_at=state.now(),
                    )
                    trace.write(
                        json.dumps(
                            {
                                "at_ms": now_ms,
                                **nav_status(),
                                "emitted": [json.loads(line) for line in emitted],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    trace.flush()
                time.sleep(
                    max(0.0, period_ms / 1000 / args.speed - (time.perf_counter() - started))
                )
        except KeyboardInterrupt:
            stop_route()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
