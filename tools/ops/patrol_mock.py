"""실제 결과물 지도 위의 순찰 목업 — 코드와 지도 산출물의 종단 대응 검증.

`patrol_run --simulate` 와의 차이 — 그 쪽은 합성 방(`simulation.DEFAULT_ROOM`)에서
돌고 매 틱 `controller.pose = true_pose` 로 **자세를 강제 주입**해 측위를 시험하지
않는다. 이 목업은:

    ① 지도는 실제 산출물 — `slam_map.npy`(항법)와 `slam_map_loc.npy`(측위)를
      `patrol_run.build_controller` 가 만드는 배선 그대로 읽는다.
    ② 스캔은 «진실 지도»(기본: 측위 지도 — 가구 다리까지 있는 물리에 가까운 것)의
      점유 셀을 레이캐스트해서 만든다. 합성 선분이 아니다.
    ③ 자세는 시드 한 번(`pose_seeded`) 이후 **컨트롤러가 `observe_scan` 정합으로
      추정**한다. 매 틱 추정 자세와 참 자세의 오차를 모은다.

    python tools/ops/patrol_mock.py --maps <지도 폴더> --cycles 1
    python tools/ops/patrol_mock.py --maps <지도 폴더> --mode inject   # 계획만 분리 검증
    python tools/ops/patrol_mock.py --maps <지도 폴더> --goto 1.0,-0.5 # 좌표 지정 주행 모의
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.zone_map import ZoneMap
from host.behavior.zones import ZoneStore
from host.common.console import survive_encoding_errors
from host.common.lidar_link import Scan, points_from_wire
from host.common.logging_setup import event_logger, setup_logging
from host.common.units import ms_to_s
from host.slam import settings, simulation
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.settings import range_from_config
from tools.ops.patrol_run import (
    SIM_IMU_DRIFT_DEG_PER_TICK,
    SIM_IMU_OFFSET_DEG,
    _FakeReading,
    build_controller,
    describe,
)

LOG = event_logger("mechadog.tools.patrol_mock")

#: 레이캐스트 보간 — 셀 대각선을 건너뛰지 않게 해상도의 절반보다 촘촘히 걷는다.
RAY_STEP_FRACTION = 0.4


def hit_mask(grid: OccupancyGrid, occ_thresh: float) -> np.ndarray:
    """«벽이 있다» 마스크 — 레이캐스트가 부딪히는 면."""
    return grid.cells >= occ_thresh


def raycast_scan(
    hit: np.ndarray,
    meta: MapMeta,
    pose: tuple[float, float, float],
    params: simulation.SimParams,
    rng: random.Random,
) -> list[list[float]]:
    """점유 격자를 레이캐스트해 전선 형식(`[angle_deg, dist_mm]`)의 스캔을 만든다."""
    x, y, yaw = pose
    res = meta.resolution
    step_m = res * RAY_STEP_FRACTION
    height, width = hit.shape
    points: list[list[float]] = []
    for index in range(params.beams):
        angle = index / params.beams * 2 * math.pi
        direction = (math.cos(angle + yaw), math.sin(angle + yaw))
        distance = step_m
        nearest: float | None = None
        while distance < params.range_max_m:
            col = int(math.floor((x + distance * direction[0] - meta.origin_x) / res))
            row = int(math.floor((y + distance * direction[1] - meta.origin_y) / res))
            if not (0 <= row < height and 0 <= col < width):
                break  # 지도 밖 — 실기면 미반사. 닿은 것이 없다.
            if hit[row, col]:
                nearest = distance
                break
            distance += step_m
        if nearest is None or rng.random() < params.dropout_rate:
            continue
        noisy_mm = nearest * 1000.0 + rng.gauss(0.0, params.noise_mm)
        if noisy_mm <= 0:
            continue
        points.append([round(math.degrees(angle), 2), int(round(noisy_mm))])
    return points


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(description="실제 지도 위의 순찰 목업")
    parser.add_argument("--maps", required=True, help="지도·구역 폴더 (slam_map.npy 등)")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--ticks", type=int, default=6000, help="최대 틱 (무한 루프 방지)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--start", default="0,0,90", help="'x,y,yaw_deg' 시작 자세")
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="시드를 주지 않는다 — 임의 배치 후 스스로 위치를 찾는지 본다 (전역 재측위)",
    )
    parser.add_argument(
        "--mode",
        choices=("localize", "inject"),
        default="localize",
        help="localize: 정합 추정(기본) / inject: 참 자세 강제 주입(계획만 검증)",
    )
    parser.add_argument(
        "--truth",
        choices=("phys", "loc", "nav"),
        default="phys",
        help=(
            "레이캐스트할 «진실» 지도 — "
            "phys(기본): 측위 지도에서 '로봇이 실제로 지나간 확정 빈 바닥'과 모순되는 "
            "셀을 뺀 물리적 일관 지도 / loc: 병합 원본 / nav: 항법 지도"
        ),
    )
    parser.add_argument(
        "--goto",
        default=None,
        help="'x,y' 좌표 하나를 찍어 구역 순찰 대신 그곳으로 간다 (스냅 동작 확인용)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    maps = Path(args.maps)
    config = settings.load(None)
    setup_logging(config, device_id="patrol-mock", console=args.verbose)
    range_m = range_from_config(config)
    sim_params = simulation.sim_params_from_config(config, range_m[1])
    rng = random.Random(args.seed)

    # ── 생산과 같은 배선으로 컨트롤러 조립 (slam_map + slam_map_loc 자동 로드) ──
    controller = build_controller(config, maps, args.seed)
    controller.zone_map = ZoneMap.load(maps)  # host/runtime.py 와 같은 추가 배선
    plan_params = controller.plan_params

    # ── «진실» 지도 — 레이캐스트 대상. ──
    # phys(기본): 측위 지도의 점유 셀 중 항법 지도가 «확실히 빈 바닥»(≤free_thresh)
    # 으로 확정한 셀 — 즉 로봇 몸이 실제로 지나간 자리 — 를 뺀다. 그 셀에 물체가
    # 있었다면 로봇이 거기 설 수 없으므로 정렬 오차로 찍힌 셀이다. 남는 것은
    # 벽 + 실물과 모순 없는 가구 다리로, 물리적으로 성립하는 세계다.
    if args.truth == "loc":
        truth_hit = hit_mask(controller.match_grid, plan_params.occ_thresh)
        truth_meta = controller.match_grid.meta
    elif args.truth == "nav":
        truth_hit = hit_mask(controller.grid, plan_params.occ_thresh)
        truth_meta = controller.grid.meta
    else:
        free_confirmed = controller.grid.cells <= plan_params.free_thresh
        truth_hit = hit_mask(controller.match_grid, plan_params.occ_thresh) & ~free_confirmed
        truth_meta = controller.match_grid.meta
    print(
        f"[Mock] truth={args.truth} "
        f"hit_cells={int(truth_hit.sum())} · loc_grid={'있음' if controller.loc_grid is not None else '없음'} "
        f"· match_grid_is={'loc' if controller.loc_grid is not None else 'nav'}"
    )

    # ── 시작 자세 — 사람이 놓은 자리를 알려주는 것과 같다 (--pose-seed) ──
    x_s, y_s, yaw_s = (float(part) for part in args.start.split(","))
    true_pose = (x_s, y_s, math.radians(yaw_s))
    if args.no_seed:
        # 참 자세만 놓고 컨트롤러에는 알리지 않는다 — 전역 재측위가 스스로 찾아야 한다.
        print(f"[Mock] 시드 없음 — 참 pose=({x_s:.2f}, {y_s:.2f}, {yaw_s:.0f}°)")
    else:
        controller.pose = true_pose
        controller.pose_seeded = True
        print(f"[Mock] 시드 pose=({x_s:.2f}, {y_s:.2f}, {yaw_s:.0f}°)")

    # ── 좌표 지정 모드: 찍은 좌표를 단일 구역 G로 등록한다 — 순찰 컨트롤러의
    #    스냅·주행·도착 판정과 정확히 같은 경로를 탄다 (미래 /api/command/goto 와 동일). ──
    if args.goto:
        gx, gy = (float(part) for part in args.goto.split(","))
        zones = ZoneStore(("G",))
        zones.place(gx, gy)
        controller.zones = zones
        print(f"[Mock] goto 요청 ({gx:.2f}, {gy:.2f}) — plan_to가 스냅을 판단한다")

    lines = [controller.commander.open_session()]
    controller.start()
    now_ms = 0
    controller.note_sent(lines, now_ms)
    period_s = ms_to_s(controller.commander.period_ms)

    pose_errors: list[float] = []
    snaps: list[str] = []
    last_plan_label: str | None = None
    for tick in range(1, args.ticks + 1):
        if args.cycles > 0 and controller.stats.cycles >= args.cycles:
            break
        scan = Scan(
            "lidar-mock",
            "0" * 16,
            tick,
            now_ms,
            points_from_wire(raycast_scan(truth_hit, truth_meta, true_pose, sim_params, rng)),
        )
        imu_yaw = (
            math.degrees(true_pose[2]) + SIM_IMU_OFFSET_DEG + SIM_IMU_DRIFT_DEG_PER_TICK * tick
        )
        controller.observe_telemetry(
            _FakeReading(
                state="PATROL",
                safety_latched=False,
                obstacle=False,
                dist_cm=100,
                yaw=imu_yaw,
                last_cmd_age_ms=20,
            ),
            now_ms,
        )
        urgent = controller.guard_scan(scan)
        if urgent:
            lines.append(urgent)
            controller.note_sent([urgent], now_ms)
        controller.observe_scan(scan, now_ms)
        lines.extend(controller.step(now_ms))
        emitted = controller.commander.tick(now_ms)
        lines.extend(emitted)
        controller.note_sent(emitted, now_ms)

        for line in emitted:
            message = json.loads(line)
            if message["type"] == "MOVE":
                true_pose = simulation.apply_move(
                    true_pose, message["step"], message["angle"], period_s, sim_params
                )
        if args.mode == "inject":
            controller.pose = true_pose
            controller._last_pose_ms = now_ms

        # ── 계획이 바뀌면 스냅 결과를 기록한다 ──
        plan = controller.plan
        if plan.label != last_plan_label:
            last_plan_label = plan.label
            eff = getattr(plan, "effective", None)
            req = getattr(plan, "requested", None)
            moved = getattr(plan, "goal_moved_m", None)
            if req is not None and eff is not None and (moved or 0) > 0.01:
                snaps.append(
                    f"{plan.label}: 요청({req[0]:.2f},{req[1]:.2f})→유효({eff[0]:.2f},{eff[1]:.2f}) "
                    f"스냅 {moved:.2f}m"
                )

        # 추정 오차 — 정합이 끊긴 틱(자세 갱신 없음)은 제외하지 않고 찍는다
        if controller.pose_ms is not None:
            ex, ey = controller.pose[0] - true_pose[0], controller.pose[1] - true_pose[1]
            pose_errors.append(math.hypot(ex, ey))

        if args.verbose and tick % 10 == 0:
            err = f" err={pose_errors[-1]:.2f}m" if pose_errors else ""
            print(f"tick={tick:4d} {describe(controller)} zone={controller.current_zone}{err}")

        now_ms += controller.commander.period_ms

    stats = controller.stats
    arr = np.array(pose_errors)
    print(
        f"[Mock] 사이클 {stats.cycles} · 구역방문 {stats.zones_visited} · 재계획 {stats.replans} "
        f"· E-STOP {stats.estops} · 측위상실 {stats.lost} · 스캔 {stats.scans} · 전문 {len(lines)}"
    )
    if arr.size:
        print(
            f"[Mock] 추정 오차 p50={np.percentile(arr, 50):.3f}m · p90={np.percentile(arr, 90):.3f}m "
            f"· max={arr.max():.3f}m ({args.mode} 모드)"
        )
    if snaps:
        print(f"[Mock] 목표 스냅 {len(snaps)}건:")
        for snap in snaps:
            print(f"  - {snap}")
    if controller.pose_verified:
        print("[Mock] 자세가 지도 전역 탐색으로 검증됨 (pose_verified)")
    else:
        print("[Mock] pose_verified 없음 — 시드 신뢰로만 주행")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
