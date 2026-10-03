"""slam_toolbox 가 만든 세션 지도를 운용 지도 좌표계에 정렬하고 **빠진 벽·물체만** 보탠다.

    python -m tools.lidar.merge_slam_map --maps <운용 지도 폴더> --slam C:/dev/out/slam_maps/s0_patrol_1/slam_map.yaml \
        --session C:/dev/out/s0_patrol_1 [--out slam_map_merged.npy] [--apply]

왜 «보태기만» 하나 — 운용 지도는 평면도 벽 + 이틀 전 42정지 실측 + 라이다가 밑으로 지나는
가구의 **테두리**(계획 안전용)로 만들어졌다. 세션 지도의 빈 공간을 그대로 덮으면 그 테두리가
지워져 경로 계획이 책상 밑으로 로봇을 보낸다. 그래서 세션 지도의 **점유 셀**만 가져오고,
운용 지도에 이미 벽인 곳은 건드리지 않는다.

정렬: 세션 지도의 점유 셀 중심을 «한 장의 스캔» 으로 보고, 세션 첫 측위 자세(순찰 좌표)를
초기값으로 운용 지도에 우도장 정합(거친 격자 → 세밀)한다. 정렬 점수(세션 벽이 운용 벽
근처에 얹힌 비율)가 낮으면 반영하지 않는다 — 틀린 정렬로 보태면 벽이 두 겹이 된다.

기본은 비교용 파일(`--out`)만 쓴다. `--apply` 를 주면 `slam_map.npy` 를 교체한다(원본은
`slam_map.orig.npy` 로 한 번만 백업). 로봇·소켓을 만지지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.slam.occupancy import OccupancyGrid  # noqa: E402
from host.slam.scan_match import MatchParams, match, rotate  # noqa: E402


def occupied_points(grid: OccupancyGrid, occ_thresh: float) -> np.ndarray:
    rows, cols = np.nonzero(grid.cells > occ_thresh)
    res = grid.meta.resolution
    return np.column_stack(
        [grid.meta.origin_x + (cols + 0.5) * res, grid.meta.origin_y + (rows + 0.5) * res]
    )


def first_fix(session: Path) -> tuple[float, float, float] | None:
    """세션 첫 측위 자세 — 세션 지도 원점(odom 원점)이 운용 지도 어디쯤인지의 초기값."""
    with (session / "events.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("kind") == "localization" and event.get("updated"):
                x, y, yaw = event["pose"]
                return float(x), float(y), float(yaw)
    return None


def align(
    target: OccupancyGrid,
    points: np.ndarray,
    guess: tuple[float, float, float],
    occ_thresh: float,
    sigma_m: float,
    window_m: float = 0.4,
    window_deg: float = 15.0,
) -> tuple[tuple[float, float, float], float]:
    """초기값 주변의 **좁은 창** 안에서만 정렬한다. (변환, 정렬률) 을 돌려준다.

    창을 넓히면(±1.2m) 지도 어딘가의 벽 조각에 우연히 얹혀 1.7m 어긋난 변환을 낸다 —
    실제로 s0_patrol_1 에서 일어났고, 그 정렬로 보탠 지도는 S0 에서 가짜 자리를 만들었다.
    초기값(세션 첫 측위 = 사용자 배치 자리)은 ±수십 cm 안에 있으므로 좁은 창이 정답을 놓치지 않는다.
    """
    p = MatchParams(window_m, 0.02, math.radians(window_deg), math.radians(1), occ_thresh, 1, sigma_m=sigma_m)
    result = match(target, points, guess, p)
    return result.pose, result.score / max(1, len(points))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--maps", required=True, type=Path)
    parser.add_argument("--slam", required=True, type=Path, help="세션 지도 YAML (map_server 형식)")
    parser.add_argument("--session", type=Path, help="초기 정렬값을 뽑을 세션 폴더 (첫 측위 자세)")
    parser.add_argument("--guess", help="초기 정렬값 'x,y,yaw_deg' (세션 대신 직접)")
    parser.add_argument("--occ-thresh", type=float, default=1.0)
    parser.add_argument("--sigma-mm", type=float, default=100)
    parser.add_argument("--min-align", type=float, default=0.5, help="이 정렬률 미만이면 반영하지 않는다")
    parser.add_argument("--min-overlap", type=float, default=0.4, help="세션 벽이 기존 벽·그 이웃에 얹힌 비율 하한")
    parser.add_argument(
        "--verify", type=Path,
        help="회귀 검증용 세션 폴더 — 이 세션의 첫 측위 바퀴들로, 합친 지도에서 전역 정합이 "
             "기록 자세를 유지하는지 확인한다 (지도가 가짜 자리를 만들지 않았는지의 검증)",
    )
    parser.add_argument("--verify-max-err", type=float, default=0.3)
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--out", type=Path, help="비교용 출력 .npy (기본 <maps>/slam_map_merged.npy)")
    parser.add_argument("--apply", action="store_true", help="운용 slam_map.npy 를 교체한다 (원본 1회 백업)")
    args = parser.parse_args()

    target = OccupancyGrid.load(args.maps)
    session_grid = OccupancyGrid.load_ros2(args.slam)
    points = occupied_points(session_grid, args.occ_thresh)
    if args.guess:
        gx, gy, gyaw = (float(v) for v in args.guess.split(","))
        guess = (gx, gy, math.radians(gyaw))
    else:
        guess = first_fix(args.session) if args.session else None
        if guess is None:
            raise SystemExit("--session 의 첫 측위가 없다 — --guess 로 초기값을 직접 준다")
    print(f"세션 지도 점유 셀 {len(points)}개 · 초기값 ({guess[0]:.2f},{guess[1]:.2f},{math.degrees(guess[2]):.0f}°)")

    pose, align_frac = align(target, points, guess, args.occ_thresh, args.sigma_mm / 1000.0)
    moved = math.hypot(pose[0] - guess[0], pose[1] - guess[1])
    print(f"정렬 → ({pose[0]:.2f},{pose[1]:.2f},{math.degrees(pose[2]):.1f}°) 정렬률 {align_frac:.2f} (초기값에서 {moved:.2f}m)")

    world = rotate(points, pose[2]) + np.array(pose[:2])
    res = target.meta.resolution
    cols = np.floor((world[:, 0] - target.meta.origin_x) / res).astype(int)
    rows = np.floor((world[:, 1] - target.meta.origin_y) / res).astype(int)
    inside = (rows >= 0) & (rows < target.cells.shape[0]) & (cols >= 0) & (cols < target.cells.shape[1])
    rows, cols = rows[inside], cols[inside]
    already = target.cells[rows, cols] > args.occ_thresh
    # 운용 지도의 벽 1셀 이웃도 «이미 있음» 으로 본다 — 정렬 오차 한 칸으로 벽이 두 겹 되지 않게.
    near_wall = np.zeros(len(rows), dtype=bool)
    for d_r in (-1, 0, 1):
        for d_c in (-1, 0, 1):
            r2, c2 = np.clip(rows + d_r, 0, target.cells.shape[0] - 1), np.clip(cols + d_c, 0, target.cells.shape[1] - 1)
            near_wall |= target.cells[r2, c2] > args.occ_thresh
    add = ~near_wall
    # 추가 셀이 덩어리를 이루는지 본다 — 스캔 노이즈의 흩어진 점 하나가 지도에 들어가면
    # 그 자체가 «가짜 벽» 이 되어 정합 가짜 자리를 만든다. 벽·다리·가구 테두리는
    # 선·면으로 뭉쳐서 나오므로 10cm 안에 점유 이웃이 3개 미만인 추가 셀은 버린다.
    if add.any():
        add_rows, add_cols = rows[add], cols[add]
        occ_or_add = (target.cells > args.occ_thresh).copy()
        occ_or_add[add_rows, add_cols] = True
        neighbors = np.zeros(len(add_rows), dtype=int)
        for d_r in range(-2, 3):
            for d_c in range(-2, 3):
                if (d_r == 0 and d_c == 0) or d_r * d_r + d_c * d_c > 4:
                    continue
                r2 = np.clip(add_rows + d_r, 0, target.cells.shape[0] - 1)
                c2 = np.clip(add_cols + d_c, 0, target.cells.shape[1] - 1)
                neighbors += occ_or_add[r2, c2]
        idx = np.nonzero(add)[0]
        add[idx[neighbors < 3]] = False
    overlap = float(near_wall.sum() / max(1, len(rows)))
    # 정렬 오차 지표 — 추가 셀이 «확실히 빈 바닥»(수많은 빔이 통과해 확정된 셀) 위에
    # 떨어지면 그 셀은 실제 장애물이 아니라 정렬이 어긋난 흔적일 가능성이 크다.
    # 이 지도는 측위(랜드마크)용이라 그대로 두되, 항법 지도에는 쓰지 않는다.
    from host.common.config import load_config  # 지연 임포트 — 지도만 정렬할 땐 불필요
    free_logodds = float(load_config(args.device)["lidar"]["free_logodds"])
    on_free = int((target.cells[rows[add], cols[add]] <= free_logodds).sum()) if add.any() else 0
    report = {
        "slam": str(args.slam),
        "transform": {"x": round(pose[0], 3), "y": round(pose[1], 3), "yaw_deg": round(math.degrees(pose[2]), 2)},
        "align_frac": round(align_frac, 3),
        "overlap_frac": round(overlap, 3),
        "session_occupied": int(len(points)),
        "already_in_map_or_adjacent": int(near_wall.sum()),
        "added": int(add.sum()),
        "added_on_confirmed_free": on_free,
        "applied": False,
    }
    if align_frac < args.min_align or overlap < args.min_overlap:
        print(
            f"정렬률 {align_frac:.2f} · 벽 겹침 {overlap:.2f} — 기준(정렬 {args.min_align}, 겹침 {args.min_overlap}) "
            "미달이라 반영하지 않는다 (틀린 정렬로 보태면 벽이 두 겹이 되고 가짜 자리가 생긴다 — 실측 확인)"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    merged = target.cells.copy()
    merged[rows[add], cols[add]] = np.maximum(merged[rows[add], cols[add]], 3.0)

    if args.verify is not None:
        # 회귀 검증 — 합친 지도에서도 알려진 자리(세션 첫 측위들의 중앙값)가
        # 전역 정합의 **유일한** 답으로 남는지 확인한다. s0_patrol_1 사례에서 정렬이
        # 1.7m 어긋난 채 보태져 (1.2,-2.0) 가짜 자리가 진짜 자리보다 점수가 높아졌다.
        from tools.lidar.localization_bench import iter_revolutions  # 지연 임포트 — 지도만 합칠 땐 불필요
        from host.common.config import load_config

        lidar = load_config(args.device)["lidar"]
        revs: list[tuple] = []
        for _t, rev, recorded in iter_revolutions(
            args.verify / "events.jsonl",
            float(lidar.get("mount_yaw_deg", 0.0)),
            int(lidar.get("angle_direction", 1)),
        ):
            if recorded is not None:
                revs.append((rev, recorded))
            if len(revs) >= 20:
                break
        if not revs:
            print("회귀 검증 건너뜀 — 검증 세션에 측위된 바퀴가 없다")
        else:
            probe = OccupancyGrid(target.meta, merged.copy())
            errors = []
            from host.slam.scan_match import global_match, preprocess
            rng = (float(lidar["range_min_mm"]) / 1000.0, float(lidar["range_max_mm"]) / 1000.0)
            anchor = np.median(np.array([p for _r, p in revs]), axis=0)
            for rev, _p in revs[:5]:
                pts = preprocess(rev.points, *rng)
                found = global_match(
                    probe, pts, lin_step_m=0.10, ang_step_rad=math.radians(10),
                    occ_thresh=args.occ_thresh, min_known_cells=50,
                    free_thresh=float(lidar["free_logodds"]), sigma_m=args.sigma_mm / 1000.0,
                )
                if found is None:
                    errors.append(float("inf"))
                    continue
                errors.append(math.hypot(found.pose[0] - anchor[0], found.pose[1] - anchor[1]))
            worst = max(errors)
            report["verify_errors_m"] = [round(e, 2) for e in errors]
            if worst > args.verify_max_err:
                print(
                    f"회귀 검증 실패 — 알려진 자리에서 전역 정합 오차 최대 {worst:.2f}m "
                    f"(>{args.verify_max_err}m). 합친 지도가 가짜 자리를 만들었다. 저장하지 않는다."
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
            print(f"회귀 검증 통과 — 알려진 자리 오차 {errors} (최대 {worst:.2f}m)")

    out = args.out or (args.maps / "slam_map_merged.npy")
    np.save(out, merged.astype(np.float32))
    report["out"] = str(out)
    if args.apply:
        source = args.maps / "slam_map.npy"
        backup = args.maps / "slam_map.orig.npy"
        if source.exists() and not backup.exists():
            shutil.copy2(source, backup)
        np.save(source, merged.astype(np.float32))
        report["applied"] = True
    (out.with_suffix(".merge.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
