"""막힘 정책 오프라인 근거와 합성 목업 지도. 로봇 소켓을 사용하지 않는다."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.planner import PlanParams, body_collision_mask, inflate, plan_to, segment_clear
from host.behavior.zone_map import ZoneMap
from host.behavior.zones import ZoneStore
from host.common.config import load_base_config
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.settings import plan_params_from_config


def ad_regression(maps: Path, output: Path) -> None:
    files = [
        p
        for p in maps.iterdir()
        if p.name in {"slam_map.npy", "slam_map_loc.npy", "map_meta.json", "zones.json"}
    ]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    grid = OccupancyGrid.load(maps)
    loc = OccupancyGrid.load(maps, stem="slam_map_loc")
    zones = ZoneStore.load(maps, ("A", "B", "C", "D"))
    start = (-1.415, -0.275)
    params = [
        PlanParams(1, -1, 0.25, 0.08, hard_thresh=4, soft_clearance_m=0.15),
        plan_params_from_config(load_base_config()),
    ]
    cases = []
    for name, param in zip(
        ("이전 여유 0.25m + 막힘 정책 경계 연결", "실시간 회피·막힘 정책 현재 설정"),
        params,
        strict=True,
    ):
        blocked = inflate(grid, param, obstacle_grid=loc if param.clearance_m == 0.25 else None)
        body = body_collision_mask(
            grid, param, obstacle_grid=loc if param.clearance_m == 0.25 else None
        )
        for label in zones.labels:
            plan = plan_to(label, zones.xy(label), start, grid, blocked, param, body_blocked=body)
            assert plan.reachable, (name, label, plan.fail_reason)
            assert plan.waypoints[0] == start
            for i, (a, b) in enumerate(zip(plan.waypoints, plan.waypoints[1:], strict=False)):
                assert segment_clear(grid, body if i < plan.escape_end_index else blocked, a, b)
            cases.append(
                {
                    "configuration": name,
                    "zone": label,
                    "reachable": True,
                    "length_m": plan.length_m,
                    "escape_m": plan.start_moved_m,
                    "escape_end_index": plan.escape_end_index,
                }
            )
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    assert before == after
    (output / "ad-regression.json").write_text(
        json.dumps(
            {"start": start, "cases": cases, "source_sha256_unchanged": before},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def synthetic_maps(output: Path) -> None:
    maps = output / "synthetic-maps"
    maps.mkdir(exist_ok=True)
    grid = OccupancyGrid(MapMeta(0.05, 0, 0, 140, 120), np.full((120, 140), -5, dtype=np.float32))
    grid.cells[:2] = grid.cells[-2:] = 5
    grid.cells[:, :2] = grid.cells[:, -2:] = 5
    grid.save(maps)
    zones = ZoneStore(("A", "B", "C", "D"))
    for xy in ((5, 3), (1.5, 4.5), (5, 4.5), (1.5, 1.5)):
        zones.place(*xy)
    zones.save(maps)
    labels = np.zeros(grid.cells.shape, dtype=np.int16)
    labels[:60, :70] = 4
    labels[60:, :70] = 2
    labels[:60, 70:] = 1
    labels[60:, 70:] = 3
    np.save(maps / "zone_labels.npy", labels)
    plan = {
        "meta": grid.meta.__dict__,
        "zones": [{"index": i, "id": z} for i, z in enumerate(zones.labels, 1)],
    }
    (maps / "zones_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    assert ZoneMap.load(maps) is not None
    corridor = output / "synthetic-corridor"
    corridor.mkdir(exist_ok=True)
    grid.cells[2:50, 60] = 5
    grid.cells[70:118, 60] = 5
    grid.save(corridor)
    zones.save(corridor)
    np.save(corridor / "zone_labels.npy", labels)
    (corridor / "zones_plan.json").write_text(json.dumps(plan), encoding="utf-8")


def render_traces(output: Path, traces: list[Path]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = "Malgun Gothic"
    fig, axes = plt.subplots(1, len(traces), figsize=(6 * len(traces), 6), squeeze=False)
    summary = []
    for ax, trace in zip(axes[0], traces, strict=True):
        rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        events_path = trace.parent / "navigation-events.jsonl"
        if events_path.exists() and not (trace.parent / "self-test.json").exists():
            events = [
                json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            if events:
                rows = [r for r in rows if r["at_ms"] <= events[0]["judgement"]["at_ms"]]
        grid = OccupancyGrid.load(trace.parent / "maps")
        ax.imshow(
            grid.cells >= 1,
            origin="lower",
            cmap="Greys",
            vmin=0,
            vmax=1,
            extent=(
                grid.meta.origin_x,
                grid.meta.origin_x + grid.meta.width * grid.meta.resolution,
                grid.meta.origin_y,
                grid.meta.origin_y + grid.meta.height * grid.meta.resolution,
            ),
            alpha=0.65,
        )
        poses = np.asarray([r["pose"][:2] for r in rows])
        ax.plot(poses[:, 0], poses[:, 1], color="#2563eb", label="SIM 주행 기록")
        ax.scatter(*poses[0], color="#16a34a", label="출발", zorder=5)
        ax.scatter(*poses[-1], color="#d97706", label="종료/정지", zorder=5)
        most = max(
            rows,
            key=lambda r: sum(len(b["points"]) for b in r.get("blockage", {}).get("obstacles", [])),
        )
        points = [p for b in most.get("blockage", {}).get("obstacles", []) for p in b["points"]]
        if points:
            xs, ys = zip(*points, strict=True)
            ax.scatter(xs, ys, s=10, color="#dc2626", label="기억한 장애물")
        for zone in ZoneStore.load(trace.parent / "maps", ("A", "B", "C", "D")).labels:
            zones = ZoneStore.load(trace.parent / "maps", ("A", "B", "C", "D"))
            x, y = zones.xy(zone)
            skipped = zone in rows[-1].get("blockage", {}).get("skipped_zones", [])
            ax.text(x, y, zone, color="#6b7280" if skipped else "black", fontsize=13)
        ax.set(title=trace.parent.name, xlabel="순찰 x (m)", ylabel="순찰 y (m)", aspect="equal")
        ax.legend(fontsize=8, loc="lower right")
        ax.text(
            0.02,
            0.98,
            f"기록 {rows[0]['at_ms'] / 1000:.1f}~{rows[-1]['at_ms'] / 1000:.1f}s",
            transform=ax.transAxes,
            va="top",
        )
        summary.append(
            {
                "trace": str(trace),
                "rows": len(rows),
                "end_ms": rows[-1]["at_ms"],
                "end_blockage": rows[-1].get("blockage"),
            }
        )
    fig.suptitle("막힘 정책 합성 주행 기록 — 관제 브라우저 캡처가 아님", fontsize=14)
    fig.tight_layout()
    fig.savefig(output / "mock-traces.png", dpi=140)
    plt.close(fig)
    (output / "mock-traces.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out/ah"))
    parser.add_argument("--ad-maps", type=Path)
    parser.add_argument("--trace", type=Path, action="append", default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    synthetic_maps(args.output)
    if args.ad_maps is not None:
        ad_regression(args.ad_maps, args.output)
    if args.trace:
        render_traces(args.output, args.trace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
