"""Read-only local fit comparison; recorded poses are not independent truth."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from itertools import islice
from pathlib import Path

import numpy as np

from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import match, preprocess
from host.slam.settings import load, match_params_from_config
from tools.lidar.global_reloc_replay import iter_samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--sessions", nargs="+", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--every", type=int, default=100)
    parser.add_argument(
        "--max-samples",
        type=int,
        default=30,
        help="selected scans per session; 0 scans the complete log",
    )
    args = parser.parse_args()
    if args.every < 1 or args.max_samples < 0:
        parser.error("--every must be positive and --max-samples nonnegative")
    if args.output.exists():
        raise FileExistsError(f"Refusing to replace existing output: {args.output}")
    config = load("mechdog-02")
    params = match_params_from_config(config)
    lidar = config["lidar"]
    grids = {}
    for label, folder in [("baseline", args.baseline), ("candidate", args.candidate)]:
        stem = "slam_map_loc" if (folder / "slam_map_loc.npy").exists() else "slam_map"
        grids[label] = OccupancyGrid.load(folder, stem=stem)
    rows = []
    by_session = []
    for session in args.sessions:
        stats = Counter()
        before = len(rows)
        count = 0
        selected = iter_samples(session, args.every, stats)
        if args.max_samples:
            selected = islice(selected, args.max_samples)
        for index, time_ms, scan, reference in selected:
            count += 1
            if scan is None or reference is None:
                continue
            points = preprocess(
                scan.points, lidar["range_min_mm"] / 1000, lidar["range_max_mm"] / 1000
            )
            row = {
                "session": session.name,
                "sample_index": index,
                "time_ms": time_ms,
                "recorded_reference": reference,
                "independent_truth": False,
                "points": len(points),
            }
            for label, grid in grids.items():
                fit = match(grid, points, reference, params)
                row[label] = {
                    "frac": fit.score / max(1, len(points)),
                    "skipped": fit.skipped,
                    "pose": fit.pose,
                    "peers": fit.competing_peaks,
                    "reference_shift_m": math.hypot(
                        fit.pose[0] - reference[0], fit.pose[1] - reference[1]
                    ),
                }
            rows.append(row)
        subset = rows[before:]
        by_session.append(
            {
                "session": session.name,
                "stats": dict(stats),
                "processed_selected": count,
                "assessed": len(subset),
                **{
                    label + "_median_frac": float(np.median([x[label]["frac"] for x in subset]))
                    if subset
                    else None
                    for label in grids
                },
            }
        )
    result = {
        "reference_warning": "Recorded updated non-LOST pose; algorithm fit consistency, not ruler truth.",
        "sampling": {
            "every": args.every,
            "selected_scans_limit_per_session": args.max_samples,
            "complete_logs": args.max_samples == 0,
        },
        "sessions": by_session,
        "samples": rows,
        "summary": {
            "assessed": len(rows),
            "improved": sum(x["candidate"]["frac"] > x["baseline"]["frac"] + 1e-9 for x in rows),
            "regressed": sum(x["candidate"]["frac"] < x["baseline"]["frac"] - 1e-9 for x in rows),
            **{
                label + "_median_frac": float(np.median([x[label]["frac"] for x in rows]))
                if rows
                else None
                for label in grids
            },
        },
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
