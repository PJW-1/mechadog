"""Prepare candidate calibration routes from saved maps; never opens a socket.

Maps and home coordinates are local inputs, not repository fixtures. An offline
path is evidence about this saved grid, never authorization to drive a robot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from host.behavior.refinement_mission import build_blocked, mission_params_from_config, safe_plan
from host.slam import settings
from host.slam.occupancy import MapMeta, OccupancyGrid


def read_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_grid(directory: Path, stem: str) -> OccupancyGrid:
    if Path(stem).name != stem or not stem:
        raise ValueError("Map stem must be a filename without directories")
    meta = MapMeta.of(read_mapping(directory / "map_meta.json"))
    cells = np.load(directory / f"{stem}.npy", allow_pickle=False)
    if (
        cells.ndim != 2
        or cells.shape != (meta.height, meta.width)
        or not np.issubdtype(cells.dtype, np.floating)
        or not np.isfinite(cells).all()
        or not all(math.isfinite(v) for v in (meta.resolution, meta.origin_x, meta.origin_y))
        or meta.resolution <= 0
    ):
        raise ValueError("Invalid map shape, log odds or metadata")
    return OccupancyGrid(meta, cells.copy())


def scope_mask(grid: OccupancyGrid, structure: dict[str, Any]) -> np.ndarray:
    """Outside requested indoor floors is forbidden, including the entire entry."""
    allowed = np.zeros(grid.cells.shape, dtype=bool)
    res = grid.meta.resolution
    # Require the complete cell, not just its centre, to lie in the requested union.
    xs = grid.meta.origin_x + np.arange(grid.meta.width) * res
    ys = grid.meta.origin_y + np.arange(grid.meta.height) * res
    xx, yy = np.meshgrid(xs, ys)
    floors = structure.get("floors", [])
    if not isinstance(floors, list):
        raise ValueError("Structure must contain a floors list")
    for floor in floors:
        if floor.get("patrol_scope") != "requested" or floor.get("model_include") is not True:
            continue
        a, b, c, d = (float(v) for v in floor["bounds_xy_m"])
        if not all(math.isfinite(v) for v in (a, b, c, d)) or a >= c or b >= d:
            raise ValueError("Invalid requested floor bounds")
        allowed |= (
            (xx >= a - 1e-9) & (xx + res <= c + 1e-9) & (yy >= b - 1e-9) & (yy + res <= d + 1e-9)
        )
    if not allowed.any():
        raise ValueError("No requested indoor floor scope")
    return ~allowed


def prepare(
    map_directory: Path,
    stem: str,
    structure_path: Path,
    route_path: Path,
    output: Path,
) -> dict[str, Any]:
    inputs = [
        map_directory / f"{stem}.npy",
        map_directory / "map_meta.json",
        structure_path,
        route_path,
    ]
    if output.resolve() == map_directory.resolve():
        raise ValueError("Output must be separate from the original map")
    destinations = {
        output / name
        for name in (
            "route_report.json",
            "mission.json",
            "mission_primary.json",
            "scope.json",
            "blocked.npy",
            "no_go.npy",
        )
    }
    if {path.resolve() for path in destinations} & {path.resolve() for path in inputs}:
        raise ValueError("Output would overwrite an input file")
    hashes = {str(path.resolve()): file_hash(path) for path in inputs}
    started = time.perf_counter()
    grid = load_grid(map_directory, stem)
    route_spec = read_mapping(route_path)
    structure = read_mapping(structure_path)
    no_go = scope_mask(grid, structure)
    params = mission_params_from_config(settings.load(None))
    blocked = build_blocked(grid, params, no_go)
    anchors = {
        str(key): (float(value[0]), float(value[1]))
        for key, value in route_spec["anchors_m"].items()
    }
    if not all(math.isfinite(v) for xy in anchors.values() for v in xy):
        raise ValueError("Nonfinite observation target")
    sequence = [str(item) for item in route_spec["sequence"]]
    if len(sequence) < 2 or any(key not in anchors for key in sequence):
        raise ValueError("Route needs at least two known targets")
    legs: list[dict[str, Any]] = []
    for index, (start_id, goal_id) in enumerate(zip(sequence, sequence[1:], strict=False)):
        start, goal = anchors[start_id], anchors[goal_id]
        t0 = time.perf_counter()
        path = safe_plan(grid, blocked, start, goal)
        points = list(path) if path is not None else []
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
        endpoints_free = []
        for point in (start, goal):
            cell = grid.to_cell(*point)
            endpoints_free.append(grid.inside(*cell) and not bool(blocked[cell]))
        legs.append(
            {
                "id": f"leg-{index + 1:02d}",
                "start_id": start_id,
                "goal_id": goal_id,
                "start_m": start,
                "goal_m": goal,
                "accepted_on_saved_grid": path is not None,
                "reason": "collision_checked_grid_route"
                if path is not None
                else (
                    "endpoint_blocked_or_unknown"
                    if not all(endpoints_free)
                    else "no_clear_observed_connection"
                ),
                "waypoints_m": points,
                "length_m": length,
                "planning_ms": (time.perf_counter() - t0) * 1000,
            }
        )
    first_return = next((i for i, key in enumerate(sequence[1:], 1) if key == sequence[0]), None)
    primary_legs = legs[:first_return] if first_return is not None else []
    primary_valid = bool(primary_legs) and all(
        leg["accepted_on_saved_grid"] for leg in primary_legs
    )
    report: dict[str, Any] = {
        "schema": 1,
        "mode": "offline_candidate_preparation",
        "commands_enabled": False,
        "physical_commands_sent": 0,
        "navigation_ready": False,
        "frame_status": "candidate_registration_not_verified",
        "map_frame_id": "house_candidate",
        "scene_revision": route_spec.get("scene_revision"),
        "source_sha256": hashes,
        "map_meta": grid.meta.as_dict(),
        "clearance_m": params.clearance_m,
        "grid_geometry_padding_m": grid.meta.resolution * math.sqrt(2) / 2,
        "unknown_cells_are_obstacles": True,
        "unknown_boundary_is_inflated": True,
        "scope": "requested indoor floors only",
        "excluded": [
            "room1",
            "balcony",
            "entire entry",
            "bathroom pending door/threshold confirmation",
        ],
        "anchors_m": anchors,
        "sequence": sequence,
        "legs": legs,
        "accepted_legs": sum(leg["accepted_on_saved_grid"] for leg in legs),
        "total_legs": len(legs),
        "all_legs_valid": all(leg["accepted_on_saved_grid"] for leg in legs),
        "primary_loop": {
            "sequence": sequence[: first_return + 1] if first_return is not None else [],
            "accepted_on_saved_grid": primary_valid,
            "length_m": sum(leg["length_m"] for leg in primary_legs),
            "file": "mission_primary.json" if primary_valid else None,
        },
        "accepted_length_m": sum(leg["length_m"] for leg in legs if leg["accepted_on_saved_grid"]),
        "prepare_ms": (time.perf_counter() - started) * 1000,
        "grid_sha256": hashlib.sha256(grid.cells.tobytes()).hexdigest(),
        "unverified_gates": [
            "continuous real localization in a registered map frame",
            "base_link to laser measured extrinsics",
            "timestamp and scan freshness",
            "observed stationary and level state",
            "whole robot footprint and in-place-turn driver integration",
        ],
        "observation_policy": {
            "stop_settle_ms": params.settle_ms,
            "minimum_angle_bins": 270,
            "pose_max_age_ms": 250,
            "revolution_max_age_ms": 500,
            "moving_scans": "avoidance only; no permanent map update",
            "map_update": "copy to candidate revision; stop and invalidate route before replanning",
            "sim_geometry": "propose supported geometry changes for review; never infer furniture identity from 2D rays",
        },
    }
    for path in inputs:
        if hashes[str(path.resolve())] != file_hash(path):
            raise RuntimeError("Input changed during preparation; do not use this report")
    output.mkdir(parents=True, exist_ok=True)
    (output / "route_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    np.save(output / "blocked.npy", blocked)
    np.save(output / "no_go.npy", no_go)
    mission = {
        "schema": 1,
        "mode": "offline_only",
        "map_frame_id": "house_candidate",
        "frame_verified": False,
        "source_sha256": hashes,
        "stops": [
            {
                "id": f"{i:02d}-{key}",
                "source_id": key,
                "x_m": anchors[key][0],
                "y_m": anchors[key][1],
            }
            for i, key in enumerate(sequence)
        ],
        "clearance_m": params.clearance_m,
        "physical_commands_sent": 0,
    }
    (output / "mission.json").write_text(
        json.dumps(mission, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    primary = dict(mission)
    primary["stops"] = (
        mission["stops"][: first_return + 1] if primary_valid and first_return is not None else []
    )
    primary["candidate_loop_available"] = primary_valid
    (output / "mission_primary.json").write_text(
        json.dumps(primary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    scope = {
        "schema": 1,
        "frame_id": "house_candidate",
        "frame_verified": False,
        "rectangles_m": [],
        "allowed_rectangles_m": [
            floor["bounds_xy_m"]
            for floor in structure["floors"]
            if floor.get("patrol_scope") == "requested" and floor.get("model_include") is True
        ],
        "source_sha256": file_hash(structure_path),
        "policy": "Everything outside requested floor union remains occupied even after grid expansion",
    }
    (output / "scope.json").write_text(
        json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-dir", required=True, type=Path)
    parser.add_argument("--stem", default="slam_map")
    parser.add_argument("--structure", required=True, type=Path)
    parser.add_argument("--route-spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.map_dir, args.stem, args.structure, args.route_spec, args.out)
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "accepted_legs": result["accepted_legs"],
                "total_legs": result["total_legs"],
                "commands_enabled": False,
                "prepare_ms": result["prepare_ms"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
