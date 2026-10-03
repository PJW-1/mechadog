"""측위·지도 품질 오프라인 벤치 — 기록 세션 + 사용자가 그은 실제 경로로 로봇 없이 평가한다.

    python -m tools.lidar.localization_bench --session C:/dev/out/s0_patrol_3 \
        --maps <maps_dir> [--truth <truth_paths.jsonl>] [--audit-every 400] [--replay] [--out bench.json]

무엇을 재나:

| 지표 | 뜻 | 좋아지려면 |
| :--- | :--- | :--- |
| `frac` p10/p50 | 기록된 자세에서 한 바퀴 스캔이 벽에 얹힌 비율 | 지도가 센서 세계와 같을수록 ↑ |
| 전역 감사 불일치 | N바퀴마다 지도 전역 탐색 → 기록 자세와 0.5m 넘게 어긋난 비율·거리 | 오정합이 없을수록 ↓ |
| 모호 비율 | 전역 탐색의 동급 후보가 `reloc_max_peers` 를 넘은 비율 | 지도 변별력이 클수록 ↓ |
| 정답 오차 | 기록 궤적 ↔ 사용자가 그은 실제 경로의 최단거리 p50/p90/max | 측위가 맞을수록 ↓ |
| 나쁜 구간 | 정답 오차가 임계를 넘은 연속 구간(시각·구역) | **그 구간의 지도를 다시 스캔하라는 뜻** |

기본은 **기록 자세**를 평가한다(빠름 — 바퀴당 점수 1번). `--replay` 는 컨트롤러로 세션을
처음부터 다시 추적한다 — 정합 코드를 바꾼 뒤 회귀를 보는 용도다(느림, 바퀴당 수십 ms).

실제 경로는 대시보드 `map2d.html` 이 plan 좌표로 저장하므로 `maps/pose_frame.json` 의
역변환으로 순찰 좌표에 맞춘다. 로봇을 움직이지 않는다 — 소켓을 열지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.commander import Commander  # noqa: E402
from host.behavior.patrol import controller_from_config, load_patrol_map  # noqa: E402
from host.behavior.zone_map import ZoneMap  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.lidar_link import Scan, points_from_wire  # noqa: E402
from host.common.protocol import CommandEncoder  # noqa: E402
from host.slam.occupancy import OccupancyGrid  # noqa: E402
from host.slam.scan_match import global_match, preprocess, rotate  # noqa: E402
from host.telemetry.lidar_feed import RevolutionAssembler  # noqa: E402

Pose = tuple[float, float, float]


def iter_revolutions(events: Path, mount_yaw_deg: float, angle_direction: int):
    """세션의 원시 스캔 패킷을 한 바퀴로 조립해 `(t_ms, Scan, 기록 자세|None)` 을 낸다.

    기록 자세는 그 바퀴 직후의 `localization` 이벤트다 — 런타임이 그 바퀴로 낸 답이다.
    """
    assembler = RevolutionAssembler()
    pending: tuple[int, Scan] | None = None
    with events.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            kind = event.get("kind")
            if kind == "scan":
                raw = json.loads(event["raw"])
                points = points_from_wire(raw["points"], mount_yaw_deg, angle_direction)
                revolution = assembler.add(
                    Scan("lidar", "boot", raw["seq"], raw["ts"], points), event["t"]
                )
                if revolution is not None:
                    if pending is not None:
                        yield pending[0], pending[1], None
                    pending = (event["t"], revolution)
            elif kind == "localization" and pending is not None:
                pose = event.get("pose")
                yield pending[0], pending[1], (
                    None if pose is None else (float(pose[0]), float(pose[1]), float(pose[2]))
                )
                pending = None
    if pending is not None:
        yield pending[0], pending[1], None


def load_truth(path: Path, pose_frame: dict[str, Any]) -> list[tuple[str, np.ndarray]]:
    """plan 좌표의 실제 경로들을 순찰 좌표 폴리라인으로 바꾼다 (`pose_frame` 역변환)."""
    theta = math.radians(float(pose_frame["rotate_deg"]))
    t_x, t_y = float(pose_frame["translate_x"]), float(pose_frame["translate_y"])
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    paths = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            pts = []
            for p in record.get("points", []):
                d_x, d_y = float(p["x"]) - t_x, float(p["y"]) - t_y
                # plan = R(θ)·patrol + t  →  patrol = R(−θ)·(plan − t)
                pts.append((cos_t * d_x + sin_t * d_y, -sin_t * d_x + cos_t * d_y))
            if len(pts) >= 2:
                paths.append((str(record.get("label", "")), np.array(pts)))
    return paths


def distance_to_polylines(x: float, y: float, paths: list[tuple[str, np.ndarray]]) -> float:
    """점에서 모든 실제 경로 선분까지의 최단거리."""
    best = math.inf
    p = np.array([x, y])
    for _, pts in paths:
        a, b = pts[:-1], pts[1:]
        ab = b - a
        length_sq = np.maximum((ab * ab).sum(axis=1), 1e-12)
        t = np.clip(((p - a) * ab).sum(axis=1) / length_sq, 0.0, 1.0)
        proj = a + ab * t[:, None]
        best = min(best, float(np.sqrt(((proj - p) ** 2).sum(axis=1)).min()))
    return best


def bad_segments(
    samples: list[tuple[int, float, str | None]], threshold_m: float
) -> list[dict[str, Any]]:
    """정답 오차가 임계를 넘는 연속 구간 — 지도를 다시 봐야 할 자리다."""
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for t_ms, err, zone in samples:
        if err > threshold_m:
            if current is None:
                current = {"start_ms": t_ms, "end_ms": t_ms, "max_err_m": err, "zones": set()}
            current["end_ms"] = t_ms
            current["max_err_m"] = max(current["max_err_m"], err)
            if zone:
                current["zones"].add(zone)
        elif current is not None:
            segments.append(current)
            current = None
    if current is not None:
        segments.append(current)
    for seg in segments:
        seg["zones"] = sorted(seg["zones"])
        seg["duration_s"] = round((seg["end_ms"] - seg["start_ms"]) / 1000, 1)
        seg["max_err_m"] = round(seg["max_err_m"], 2)
    return segments


def percentiles(values: list[float], *qs: int) -> dict[str, float]:
    if not values:
        return {f"p{q}": math.nan for q in qs}
    arr = np.array(values)
    return {f"p{q}": round(float(np.percentile(arr, q)), 3) for q in qs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, type=Path, help="events.jsonl 이 있는 세션 폴더")
    parser.add_argument("--maps", required=True, type=Path, help="slam_map.npy · zones.json · pose_frame.json 폴더")
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--truth", type=Path, help="map2d 가 저장한 truth_paths.jsonl (plan 좌표)")
    parser.add_argument("--audit-every", type=int, default=400, help="몇 바퀴마다 지도 전역 감사를 돌릴지 (0=안 함)")
    parser.add_argument("--replay", action="store_true", help="기록 자세 대신 컨트롤러로 처음부터 다시 추적한다")
    parser.add_argument("--bad-threshold-m", type=float, default=0.5)
    parser.add_argument("--max-revs", type=int, default=0, help="앞에서부터 이만큼만 평가 (0=전부)")
    parser.add_argument("--out", type=Path, help="결과 JSON 저장 경로")
    args = parser.parse_args()

    config = load_config(args.device)
    lidar = config["lidar"]
    mount_yaw, direction = float(lidar.get("mount_yaw_deg", 0.0)), int(lidar.get("angle_direction", 1))
    range_m = (float(lidar["range_min_mm"]) / 1000, float(lidar["range_max_mm"]) / 1000)
    occ_thresh = float(lidar["occupied_logodds"])
    max_peers = int(lidar.get("reloc_max_peers", 60))
    sigma_m = float(lidar.get("match_sigma_mm", 0)) / 1000.0

    grid = OccupancyGrid.load(args.maps)
    # 측위 전용 지도가 있으면 점수·감사는 그 지도로 — 런타임의 정합이 보는 것과 같다.
    loc_grid = grid
    if (args.maps / "slam_map_loc.npy").is_file():
        loc_grid = OccupancyGrid.load(args.maps, stem="slam_map_loc")
    field = loc_grid.likelihood_field(occ_thresh, sigma_m) if sigma_m > 0 else None
    zone_map = ZoneMap.load(args.maps)
    truth: list[tuple[str, np.ndarray]] = []
    if args.truth is not None and args.truth.is_file():
        frame_path = args.maps / "pose_frame.json"
        truth = load_truth(args.truth, json.loads(frame_path.read_text(encoding="utf-8")))

    controller = None
    if args.replay:
        patrol_map = load_patrol_map(config, args.maps)
        controller = controller_from_config(config, Commander(CommandEncoder()), *patrol_map, random.Random(0))
        controller.zone_map = zone_map
        controller.loc_grid = loc_grid
        # 리플레이는 읽기 전용이다 — 지도 적분으로 평가 대상을 바꾸지 않는다.
        controller.map_hit_logodds = 0.0

    fracs: list[float] = []
    stale_revs = 0
    audits: list[dict[str, Any]] = []
    truth_samples: list[tuple[int, float, str | None]] = []
    zone_counts: dict[str, int] = {}
    evaluated = 0
    started = time.time()

    for index, (t_ms, revolution, recorded) in enumerate(
        iter_revolutions(args.session / "events.jsonl", mount_yaw, direction)
    ):
        if args.max_revs and index >= args.max_revs:
            break
        points = preprocess(revolution.points, *range_m)
        if not points.size:
            continue
        if controller is not None:
            controller.observe_scan(revolution, t_ms)
            if controller.pose_stale(t_ms):
                stale_revs += 1
                continue
            pose: Pose = controller.pose
        else:
            if recorded is None:
                continue
            pose = recorded
        evaluated += 1
        world = rotate(points, pose[2]) + np.array(pose[:2])
        frac = (loc_grid.score_field(world, field) if field is not None else loc_grid.score(world, occ_thresh)) / len(points)
        fracs.append(frac)
        zone = zone_map.zone_at(pose[0], pose[1]) if zone_map else None
        if zone:
            zone_counts[zone] = zone_counts.get(zone, 0) + 1
        if truth:
            truth_samples.append((t_ms, distance_to_polylines(pose[0], pose[1], truth), zone))
        if args.audit_every and index % args.audit_every == 0:
            result = global_match(
                loc_grid, points,
                lin_step_m=float(lidar.get("global_match_step_mm", 100)) / 1000,
                ang_step_rad=math.radians(float(lidar.get("global_match_angle_deg", 15))),
                occ_thresh=occ_thresh, min_known_cells=int(lidar["min_known_cells"]),
                free_thresh=float(lidar["free_logodds"]), sigma_m=sigma_m,
            )
            entry: dict[str, Any] = {"t_ms": t_ms, "pose": [round(v, 2) for v in pose], "frac_at_pose": round(frac, 3)}
            if result is None:
                entry["global"] = None
            else:
                entry.update(
                    global_pose=[round(v, 2) for v in result.pose],
                    global_frac=round(result.score / len(points), 3),
                    peers=result.peers,
                    ambiguous=result.peers > max_peers,
                    disagreement_m=round(math.hypot(result.pose[0] - pose[0], result.pose[1] - pose[1]), 2),
                )
            audits.append(entry)
            print(f"  audit @{index}: {entry}", file=sys.stderr)

    clear_audits = [a for a in audits if "global_pose" in a and not a.get("ambiguous")]
    disagreements = [a["disagreement_m"] for a in clear_audits]
    report: dict[str, Any] = {
        "session": str(args.session),
        "maps": str(args.maps),
        "mode": "replay" if args.replay else "recorded",
        "revolutions_evaluated": evaluated,
        "stale_revolutions": stale_revs,
        "frac": percentiles(fracs, 10, 50, 90),
        "frac_below_0.45": round(float(np.mean(np.array(fracs) < 0.45)), 3) if fracs else math.nan,
        "audits": len(audits),
        "audits_ambiguous": sum(1 for a in audits if a.get("ambiguous")),
        "audits_disagree_over_0.5m": sum(1 for d in disagreements if d > 0.5),
        "audit_disagreement_m": percentiles(disagreements, 50, 90),
        "zone_share": {k: round(v / evaluated, 3) for k, v in sorted(zone_counts.items())} if evaluated else {},
        "truth_paths": len(truth),
        "truth_error_m": percentiles([e for _, e, _ in truth_samples], 50, 90, 100),
        "bad_segments": bad_segments(truth_samples, args.bad_threshold_m),
        "elapsed_s": round(time.time() - started, 1),
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
