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
from io import BytesIO
from pathlib import Path

import yaml
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.fsm import Behavior, Event, Fsm
from host.behavior.patrol import Phase
from host.behavior.planner import mark_obstacle
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
from tools.ops import dashboard_demo
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
    parser.add_argument(
        "--self-test", action="store_true", help="소켓 없는 센서/보행 모델로 A→B→C→D 자동 이동"
    )
    parser.add_argument("--max-ticks", type=int, default=4000)
    parser.add_argument(
        "--obstacle", action="append", default=[], help="SIM 물체 x,y,반경_m (여러 번 지정)"
    )
    parser.add_argument(
        "--remove-after-ms", type=int, default=0, help="SIM 물체를 치울 모의 시각 (0: 유지)"
    )
    parser.add_argument(
        "--insert-after-ms", type=int, default=0, help="주행 중 SIM 물체를 넣을 시각"
    )
    parser.add_argument("--auto-patrol", action="store_true", help="자동 구역 순찰 시작")
    parser.add_argument(
        "--patrol-cycles", type=int, default=0, help="자동 순찰 지정 바퀴 완료 후 결과 저장/종료"
    )
    parser.add_argument("--pause-after-event", default="", help="화면 캡처용 사건 뒤 SIM 정지")
    parser.add_argument("--scene3d-url", default="", help="외부 3D 보기 URL (SIM 시험용)")
    args = parser.parse_args(argv)
    if args.port in (5101, 5201, 8000) or not 1024 <= args.port <= 65535:
        parser.error("현장 포트 대신 1024~65535의 별도 포트를 고르세요")
    if not math.isfinite(args.speed) or args.speed <= 0:
        parser.error("배속은 양수여야 합니다")
    obstacles = [tuple(float(v) for v in item.split(",")) for item in args.obstacle]
    if any(
        len(item) != 3 or not all(math.isfinite(v) for v in item) or item[2] <= 0
        for item in obstacles
    ):
        parser.error("SIM 물체는 유한한 x,y,양수 반경_m 입니다")
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
                "local_navigation": controller.local_status,
                "blockage": controller.blockage_status,
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
    dashboard_demo.record_examples(state)
    synthetic_snapshots: dict[str, bytes] = {}

    def event_snapshot(entry: str) -> bytes | None:
        return synthetic_snapshots.get(entry) or dashboard_demo.snapshot(entry)

    app = create_app(
        state,
        commands,
        static_dir=DEFAULT_STATIC_DIR,
        scene3d_url=args.scene3d_url,
        policy=policy_view(config),
        map_view=view.get,
        nav_status=nav_status,
        planning=planning,
        event_snapshot=event_snapshot,
        simulated=True,
        voice_path="/api/demo-voice",
        extra_routes=dashboard_demo.voice_routes(),
    )

    sim = simulation.sim_params_from_config(config, settings.range_from_config(config)[1])
    hits = physical_hit_mask(
        controller.grid,
        controller.match_grid,
        controller.plan_params.occ_thresh,
        controller.plan_params.free_thresh,
    )
    rng = random.Random(7)
    base_hits = hits.copy()
    if not args.insert_after_ms:
        for x, y, radius in obstacles:
            mark_obstacle(hits, controller.grid, (x, y), radius)
    frozen = False
    period_ms = commander.period_ms
    send(commander.open_session())
    print(f"SIM only: http://127.0.0.1:{args.port}/glass-preview/?view=zones", flush=True)
    trace_path = args.output / "route-trace.jsonl"
    visits: list[str] = []
    navigation_events: list[dict[str, object]] = []
    visited_count = 0
    self_test_labels = list(controller.zones.labels)
    test_started = False
    tick_count = 0
    min_range = math.inf
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
                    if args.insert_after_ms and now_ms >= args.insert_after_ms:
                        for x, y, radius in obstacles:
                            mark_obstacle(hits, controller.grid, (x, y), radius)
                    if args.remove_after_ms and now_ms >= args.remove_after_ms:
                        hits = base_hits.copy()
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
                    tick_count += 1
                    if (args.auto_patrol or args.patrol_cycles) and tick_count == 1:
                        controller.start()
                        begin()
                    if args.self_test:
                        distances = [d for _, d in scan.points if d > 0]
                        if distances:
                            min_range = min(min_range, min(distances))
                        if controller.holding_goal and controller.goal_hold_reason == "reached":
                            visits.append(self_test_labels[len(visits)])
                            test_started = False
                        complete = len(visits) == len(self_test_labels)
                        if (
                            complete
                            or tick_count >= args.max_ticks
                            or controller.phase is Phase.HALTED
                        ):
                            result = {
                                "completed": complete,
                                "visited": visits,
                                "ticks": tick_count,
                                "simulated_ms": now_ms,
                                "min_scan_range_m": min_range,
                                "estops": controller.stats.estops,
                                "local_navigation": controller.local_status,
                                "limitation": "운동 모델 참 자세 주입, 실물 측위/보행/충돌 안전 검증 아님",
                            }
                            (args.output / "self-test.json").write_text(
                                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                            )
                            print(json.dumps(result, ensure_ascii=True), flush=True)
                            return 0 if complete else 1
                        if not test_started:
                            label = self_test_labels[len(visits)]
                            accepted, detail = goto(*controller.zones.xy(label))
                            if not accepted:
                                raise RuntimeError(f"self-test {label}: {detail}")
                            test_started = True
                    urgent = controller.guard_scan(scan)
                    if urgent:
                        send(urgent)
                    if behavior.state == "PATROL" and not frozen:
                        controller.step(now_ms)
                        if controller.stats.zones_visited > visited_count:
                            visited_count = controller.stats.zones_visited
                            if controller._inspection_zone is not None:
                                visits.append(controller._inspection_zone)
                        for event in controller.take_navigation_events():
                            entry = f"ah-sim-{event['judgement']['at_ms']}"
                            # 목업 증거는 물리 마스크의 합성 평면도다. 실물 카메라 사진으로 표시하지 않는다.
                            picture = (
                                Image.fromarray((~hits).astype("uint8") * 255)
                                .convert("RGB")
                                .resize((560, 480))
                            )
                            draw = ImageDraw.Draw(picture)
                            draw.text((12, 12), "SIM / SYNTHETIC OBSTACLE EVIDENCE", fill="#b91c1c")
                            buffer = BytesIO()
                            picture.save(buffer, format="JPEG")
                            synthetic_snapshots[entry] = buffer.getvalue()
                            if len(synthetic_snapshots) > 32:
                                del synthetic_snapshots[next(iter(synthetic_snapshots))]
                            event["judgement"]["camera_available"] = True
                            event["judgement"]["synthetic_evidence"] = True
                            navigation_events.append(event)
                            state.record_event(
                                {
                                    **event,
                                    "ts_ms": now_ms,
                                    "state": "PATROL",
                                    "escalation": "NORMAL",
                                    "mode": config["mission"]["mode"],
                                    "entry": entry,
                                    "snapshot": "snapshot.jpg",
                                    "simulated": True,
                                }
                            )
                            with (args.output / "navigation-events.jsonl").open(
                                "a", encoding="utf-8"
                            ) as stream:
                                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                            if event["event"] == args.pause_after_event:
                                frozen = True
                                commander.halt()
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
                            "device_id": "SIM-route",
                            "boot_id": "route-sim-boot",
                            "seq": now_ms // period_ms,
                            "ts_ms": now_ms,
                            "batt_v": 7.8,
                            "imu": {"pitch": 0.0, "roll": 0.0, "yaw": math.degrees(truth[2])},
                            "flags": {
                                "lowbatt": False,
                                "tipped": False,
                                "obstacle": False,
                                "link_ok": True,
                            },
                            "last_cmd_age_ms": 0,
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
                    if args.patrol_cycles and (
                        controller.stats.cycles >= args.patrol_cycles
                        or tick_count >= args.max_ticks
                        or controller.phase is Phase.HALTED
                    ):
                        result = {
                            "completed": controller.stats.cycles >= args.patrol_cycles,
                            "visited": visits,
                            "cycles": controller.stats.cycles,
                            "ticks": tick_count,
                            "simulated_ms": now_ms,
                            "estops": controller.stats.estops,
                            "events": navigation_events,
                            "blockage": controller.blockage_status,
                            "limitation": "합성 운동/센서 모델. 실물 보행 검증 아님",
                        }
                        (args.output / "patrol-test.json").write_text(
                            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        print(json.dumps(result, ensure_ascii=True), flush=True)
                        return 0 if result["completed"] else 1
                time.sleep(
                    max(0.0, period_ms / 1000 / args.speed - (time.perf_counter() - started))
                )
        except KeyboardInterrupt:
            stop_route()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
