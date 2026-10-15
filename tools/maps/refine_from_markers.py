"""Offline localization-map refinement using measured stationary marker poses."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from host.common.lidar_link import ScanDecoder, scan_of
from host.slam.continuous_map import LidarPose
from host.slam.map_refinement import MapRefinement
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import MatchResult, global_match, match, preprocess
from host.slam.settings import load, match_params_from_config
from host.telemetry.lidar_feed import RevolutionAssembler
from tools.ops.refinement_prepare import file_hash, load_grid

REPORT_NAME = "refinement_report.json"
CIRCULAR_WARNING = (
    "Circular validation: scans used to build this map are reused for evaluation; "
    "these scores do not establish independent localization accuracy."
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def parse_intervals(value: Any) -> list[dict[str, Any]]:
    """Intervals are inclusive epoch milliseconds; poses are LiDAR-centre m/rad."""
    if isinstance(value, dict):
        if value.get("truth_source") == "localization_candidate":
            raise ValueError("Detected localization candidates are not measured ground truth")
        if value.get("frame_id", "patrol") != "patrol":
            raise ValueError("Ground truth must use the patrol frame")
        if value.get("yaw_unit", "radians") != "radians":
            raise ValueError("Ground truth yaw must be radians")
        if value.get("pose_origin", "lidar_centre") != "lidar_centre":
            raise ValueError("Ground truth must locate the lidar_centre")
        value = value.get("intervals")
    if not isinstance(value, list) or not value:
        raise ValueError("A nonempty intervals list is required")
    intervals = []
    previous_end = -1
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each interval must be an object")
        start, end = item.get("start_ms"), item.get("end_ms")
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("start_ms/end_ms must be ordered nonnegative integer epoch ms")
        if start <= previous_end:
            raise ValueError("Intervals must be ordered and nonoverlapping (inclusive endpoints)")
        if not all(finite_number(item.get(key)) for key in ("x", "y", "yaw")):
            raise ValueError("x, y and yaw must be finite numbers in metres/radians")
        intervals.append({key: item[key] for key in ("start_ms", "end_ms", "x", "y", "yaw")})
        previous_end = end
    return intervals


def parse_rectangles(value: Any) -> list[list[float]]:
    if isinstance(value, dict):
        if value.get("frame_id", "patrol") != "patrol":
            raise ValueError("Rectangles must use the patrol frame")
        value = value.get("allowed_rectangles_m")
    if not isinstance(value, list) or not value:
        raise ValueError("A nonempty allowed rectangle list is required")
    result = []
    for rectangle in value:
        if (
            not isinstance(rectangle, (list, tuple))
            or len(rectangle) != 4
            or not all(finite_number(v) for v in rectangle)
            or rectangle[0] >= rectangle[2]
            or rectangle[1] >= rectangle[3]
        ):
            raise ValueError("Rectangle must be finite [min_x, min_y, max_x, max_y]")
        result.append([float(v) for v in rectangle])
    return result


def rectangle_mask(grid: OccupancyGrid, rectangles: list[list[float]]) -> np.ndarray:
    """Only cells wholly inside a rectangle may change, including their edges."""
    rectangles = parse_rectangles(rectangles)
    meta = grid.meta
    xs = meta.origin_x + np.arange(meta.width) * meta.resolution
    ys = meta.origin_y + np.arange(meta.height) * meta.resolution
    mask = np.zeros(grid.cells.shape, dtype=bool)
    for x0, y0, x1, y1 in rectangles:
        columns = (xs >= x0 - 1e-9) & (xs + meta.resolution <= x1 + 1e-9)
        rows = (ys >= y0 - 1e-9) & (ys + meta.resolution <= y1 + 1e-9)
        mask |= rows[:, None] & columns[None, :]
    return mask


def merge_refined(
    original: OccupancyGrid, updated: OccupancyGrid, mask: np.ndarray
) -> OccupancyGrid:
    """Crop an expanded accumulator by world coordinates, retaining original metadata."""
    resolution = original.meta.resolution
    if not math.isclose(resolution, updated.meta.resolution, rel_tol=0, abs_tol=1e-12):
        raise ValueError("Refined grid resolution changed")
    offsets = (
        (original.meta.origin_y - updated.meta.origin_y) / resolution,
        (original.meta.origin_x - updated.meta.origin_x) / resolution,
    )
    row, col = (round(offset) for offset in offsets)
    if any(abs(v - round(v)) > 1e-6 for v in offsets):
        raise ValueError("Refined grid is not aligned with original cells")
    height, width = original.cells.shape
    if row < 0 or col < 0 or row + height > updated.meta.height or col + width > updated.meta.width:
        raise ValueError("Refined grid does not cover original extent")
    if mask.shape != original.cells.shape or mask.dtype != bool:
        raise ValueError("Invalid refinement mask")
    cells = original.cells.copy()
    cells[mask] = updated.cells[row : row + height, col : col + width][mask]
    return OccupancyGrid(replace(original.meta), cells)


def iter_scans(session: Path) -> Iterator[tuple[int, str]]:
    previous = -1
    with (session / "events.jsonl").open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError(f"Event {line_number} is not an object")
            if event.get("kind") != "scan":
                continue
            stamp, raw = event.get("t"), event.get("raw")
            if type(stamp) is not int or stamp < 0 or not isinstance(raw, str):
                raise ValueError(f"Scan event {line_number} needs integer epoch ms and raw string")
            if stamp < previous:
                raise ValueError(f"Scan timestamps reversed at event {line_number}")
            previous = stamp
            yield stamp, raw


def selected_scans(
    session: Path, intervals: list[dict[str, Any]]
) -> Iterator[tuple[int, int, str]]:
    index = 0
    for stamp, raw in iter_scans(session):
        while index < len(intervals) and stamp > intervals[index]["end_ms"]:
            index += 1
        if index < len(intervals) and stamp >= intervals[index]["start_ms"]:
            yield index, stamp, raw


def packet_id(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tree_hashes(directory: Path) -> dict[str, str]:
    paths = sorted(directory.rglob("*"))
    if any(path.is_symlink() or path.is_junction() for path in [directory, *paths]):
        raise ValueError("Map bundle must not contain links or junctions")
    return {
        path.relative_to(directory).as_posix(): file_hash(path) for path in paths if path.is_file()
    }


def new_output(output: Path, *sources: Path) -> None:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Refusing existing output: {output}")
    resolved = output.resolve()
    if any(
        resolved == source.resolve() or resolved.is_relative_to(source.resolve())
        for source in sources
    ):
        raise ValueError("Output must be outside all input folders")


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def fit_metrics(
    result: MatchResult | None, truth: dict[str, Any], points: int, *, global_search: bool = False
) -> dict[str, Any]:
    if result is None or result.skipped:
        return {"status": "no_match", "xy_error_m": None, "yaw_error_deg": None, "peers": None}
    return {
        "status": "matched",
        "pose": list(result.pose),
        "xy_error_m": math.hypot(result.pose[0] - truth["x"], result.pose[1] - truth["y"]),
        "yaw_error_deg": abs(math.degrees(math.remainder(result.pose[2] - truth["yaw"], math.tau))),
        "score_fraction": result.score / max(1, points),
        "peers": result.peers if global_search else None,
        "competing_peaks": None,
        "search_complete": None,
        "peer_diagnostic": "legacy_global_peers" if global_search else "not_computed_locally",
    }


def validate_maps(
    session: Path,
    baseline: OccupancyGrid,
    candidate: OccupancyGrid,
    truth_path: Path,
    config: dict[str, Any],
    lidar_device: str,
    *,
    max_revolutions: int = 3,
    training_packet_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Compare the same complete revolutions in both maps, without fitting truth."""
    if max_revolutions < 1:
        raise ValueError("max_revolutions must be positive")
    intervals = parse_intervals(read_json(truth_path))
    if baseline.meta != candidate.meta:
        raise ValueError("Validation maps must share metadata")
    evaluation_hashes = {
        "events": file_hash(session / "events.jsonl"),
        "truth": file_hash(truth_path),
    }
    lidar = config["lidar"]
    params = replace(match_params_from_config(config), search_lin_m=0.7, search_lin_step_m=0.05)
    decoder = ScanDecoder(
        float(lidar.get("mount_yaw_deg", 0)), int(lidar.get("angle_direction", 1))
    )
    assembler = RevolutionAssembler()
    rows = []
    counts: Counter[int] = Counter()
    previous_index = None
    overlaps = False
    for index, stamp, raw in selected_scans(session, intervals):
        if training_packet_ids is not None and packet_id(raw) in training_packet_ids:
            overlaps = True
        if index != previous_index:
            assembler = RevolutionAssembler()
            previous_index = index
        scan = scan_of(decoder.decode(raw))
        if scan is None or scan.device_id != lidar_device:
            assembler = RevolutionAssembler()
            continue
        revolution = assembler.add(scan, stamp)
        if revolution is None or counts[index] >= max_revolutions:
            continue
        points = preprocess(
            revolution.points, lidar["range_min_mm"] / 1000, lidar["range_max_mm"] / 1000
        )
        if len(points) < 270:
            continue
        counts[index] += 1
        truth = intervals[index]
        pose = (truth["x"], truth["y"], truth["yaw"])
        for label, grid in (("original", baseline), ("refined", candidate)):
            fits = {
                "local_truth": match(grid, points, pose, params),
                "local_offset_x_0.5m": match(
                    grid, points, (pose[0] + 0.5, pose[1], pose[2]), params
                ),
                "global": global_match(
                    grid,
                    points,
                    lin_step_m=0.1,
                    ang_step_rad=math.radians(15),
                    occ_thresh=params.occ_thresh,
                    min_known_cells=params.min_known_cells,
                    free_thresh=float(lidar.get("free_logodds", -0.5)),
                    sigma_m=params.sigma_m,
                ),
            }
            for mode, fit in fits.items():
                rows.append(
                    {
                        "interval": index,
                        "t": stamp,
                        "map": label,
                        "mode": mode,
                        "points": len(points),
                        **fit_metrics(fit, truth, len(points), global_search=mode == "global"),
                    }
                )
    warnings = ["Match scores and peers are diagnostics, not a deployment approval."]
    if overlaps:
        warnings.append(CIRCULAR_WARNING)
    elif training_packet_ids is None:
        warnings.append(
            "Training provenance unavailable; independence is unverified. " + CIRCULAR_WARNING
        )
    else:
        warnings.append(
            "No identical training packets found; independent measurement is still required."
        )
    missing = [i for i in range(len(intervals)) if not counts[i]]
    if missing:
        warnings.append(f"No complete usable evaluation revolutions for intervals: {missing}")
    if evaluation_hashes != {
        "events": file_hash(session / "events.jsonl"),
        "truth": file_hash(truth_path),
    }:
        raise RuntimeError("Evaluation inputs changed during validation")
    return {
        "intervals": intervals,
        "input_sha256": evaluation_hashes,
        "rows": rows,
        "revolutions_per_interval": [counts[i] for i in range(len(intervals))],
        "training_overlap": overlaps if training_packet_ids is not None else None,
        "independent_accuracy_verified": False,
        "warnings": warnings,
        "global_full_scan_ambiguity": False,
        "max_revolutions_per_interval": max_revolutions,
    }


def refine(
    session: Path,
    map_directory: Path,
    truth_path: Path,
    rectangles_path: Path,
    output: Path,
    config: dict[str, Any],
    lidar_device: str,
    *,
    validate: bool = False,
    max_revolutions: int = 3,
    validation_session: Path | None = None,
    validation_truth: Path | None = None,
) -> dict[str, Any]:
    if (validation_session is None) != (validation_truth is None):
        raise ValueError("Separate validation needs both session and truth")
    if validation_session is not None and not validate:
        raise ValueError("Separate validation inputs require validation mode")
    new_output(
        output, map_directory, session, *([validation_session] if validation_session else [])
    )
    intervals = parse_intervals(read_json(truth_path))
    rectangles = parse_rectangles(read_json(rectangles_path))
    source_hashes = tree_hashes(map_directory)
    input_hashes = {
        "events": file_hash(session / "events.jsonl"),
        "truth": file_hash(truth_path),
        "rectangles": file_hash(rectangles_path),
    }
    original = load_grid(map_directory, "slam_map_loc")
    load_grid(map_directory, "slam_map")
    mask = rectangle_mask(original, rectangles)
    if not mask.any():
        raise ValueError("No original grid cells wholly inside allowed rectangles")
    current = original
    accumulator = None
    previous_index = None
    details = [{**item, "packets": 0, "accepted_revolutions": 0} for item in intervals]
    metadata = []
    packet_ids: set[str] = set()
    lidar = config["lidar"]
    decoder = ScanDecoder(
        float(lidar.get("mount_yaw_deg", 0)), int(lidar.get("angle_direction", 1))
    )
    rejected: Counter[str] = Counter()
    for index, stamp, raw in selected_scans(session, intervals):
        if index != previous_index:
            if accumulator is not None:
                current = merge_refined(original, accumulator.snapshot(), mask)
                metadata.append(accumulator.metadata())
            # A fresh accumulator discards unfinished batches at every window boundary.
            # Do NOT pass allowed_rectangles: that paints the exterior as occupied.
            accumulator = MapRefinement(config, lidar_device, current, "patrol")
            previous_index = index
        assert accumulator is not None
        details[index]["packets"] += 1
        scan = scan_of(decoder.decode(raw))
        if scan is None or scan.device_id != lidar_device:
            rejected["invalid_or_wrong_device"] += 1
            # Let MapRefinement clear its own partial batch on a rejected packet.
            accumulator.submit(
                raw,
                stamp,
                None,
                pose_frame_id="patrol",
                localization_valid=False,
                laser_extrinsics_verified=True,
                stationary_verified=True,
                level_verified=True,
            )
            continue
        truth = intervals[index]
        result = accumulator.submit(
            raw,
            stamp,
            LidarPose(stamp, truth["x"], truth["y"], truth["yaw"], True, True),
            pose_frame_id="patrol",
            localization_valid=True,
            laser_extrinsics_verified=True,
            stationary_verified=True,
            level_verified=True,
        )
        packet_ids.add(packet_id(raw))
        details[index]["accepted_revolutions"] += int(result.accepted)
    if accumulator is not None:
        current = merge_refined(original, accumulator.snapshot(), mask)
        metadata.append(accumulator.metadata())
    accepted = sum(item["accepted_revolutions"] for item in details)
    if not accepted:
        raise ValueError("No complete verified revolutions accepted; no output created")
    warnings = [
        "Stationarity, levelness, LiDAR extrinsics and measured patrol-frame poses must be verified by the operator.",
        "Only slam_map_loc.npy is updated; copied previews and prior reports are not regenerated.",
    ]
    missing = [i for i, item in enumerate(details) if not item["accepted_revolutions"]]
    if missing:
        warnings.append(f"No accepted revolutions for intervals: {missing}")
    report = {
        "schema_version": 1,
        "kind": "marker_localization_map_refinement",
        "frame_id": "patrol",
        "pose_origin": "lidar_centre",
        "yaw_unit": "radians",
        "navigation_ready": False,
        "accepted_revolutions": accepted,
        "changed_cells": int(np.count_nonzero(current.cells != original.cells)),
        "cells_sign_changed": int(
            np.count_nonzero(np.sign(current.cells) != np.sign(original.cells))
        ),
        "allowed_rectangles_m": rectangles,
        "allowed_cells": int(mask.sum()),
        "mask_policy": "whole cells inside any rectangle; exterior and original extent preserved",
        "intervals": details,
        "accumulators": metadata,
        "input_rejected": dict(rejected),
        "map_meta": original.meta.as_dict(),
        "source_sha256": source_hashes,
        "input_sha256": input_hashes,
        "training_packet_sha256": sorted(packet_ids),
        "lidar_config": config["lidar"],
        "lidar_device": lidar_device,
        "warnings": warnings,
    }
    if validate:
        report["validation"] = validate_maps(
            validation_session or session,
            original,
            current,
            validation_truth or truth_path,
            config,
            lidar_device,
            max_revolutions=max_revolutions,
            training_packet_ids=packet_ids,
        )
        report["warnings"].extend(report["validation"]["warnings"])
    if source_hashes != tree_hashes(map_directory) or input_hashes != {
        "events": file_hash(session / "events.jsonl"),
        "truth": file_hash(truth_path),
        "rectangles": file_hash(rectangles_path),
    }:
        raise RuntimeError("Inputs changed during refinement; no output created")
    shutil.copytree(map_directory, output)
    np.save(output / "slam_map_loc.npy", current.cells, allow_pickle=False)
    (output / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return report


def print_validation(report: dict[str, Any]) -> None:
    print("interval | map | mode | xy error cm | yaw error deg | peers | competing peaks")
    for row in report["rows"]:
        xy = "n/a" if row["xy_error_m"] is None else f"{row['xy_error_m'] * 100:.2f}"
        yaw = "n/a" if row["yaw_error_deg"] is None else f"{row['yaw_error_deg']:.2f}"
        print(
            f"{row['interval']} | {row['map']} | {row['mode']} | {xy} | {yaw} | {row['peers']} | {row.get('competing_peaks', 'n/a')}"
        )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("refine", help="Create a new map bundle from measured marker poses")
    check = commands.add_parser("validate", help="Compare saved maps on the same selected scans")
    for command in (build, check):
        command.add_argument("--session", type=Path, required=True)
        command.add_argument("--truth", type=Path, required=True)
        command.add_argument("--device", default="mechdog-02")
        command.add_argument("--lidar-device", required=True)
        command.add_argument("--max-revolutions", type=int, default=3)
        command.add_argument("--output", type=Path, required=True)
    build.add_argument("--maps", type=Path, required=True)
    build.add_argument("--rectangles", type=Path, required=True)
    build.add_argument("--validate", action="store_true")
    build.add_argument("--validation-session", type=Path)
    build.add_argument("--validation-truth", type=Path)
    check.add_argument("--baseline", type=Path, required=True)
    check.add_argument("--candidate", type=Path, required=True)
    detect = commands.add_parser("detect", help="Propose stationary windows from saved pose logs")
    detect.add_argument("--input", type=Path, required=True)
    detect.add_argument("--output", type=Path, required=True)
    line = commands.add_parser(
        "line", help="Generate equally spaced truth from measured line geometry"
    )
    line.add_argument("--intervals", type=Path, required=True)
    line.add_argument("--origin", type=float, nargs=2, required=True, metavar=("X", "Y"))
    line.add_argument("--direction-deg", type=float, required=True)
    line.add_argument("--yaw-deg", type=float, required=True)
    line.add_argument("--spacing-m", type=float, default=0.30)
    line.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "refine":
            report = refine(
                args.session,
                args.maps,
                args.truth,
                args.rectangles,
                args.output,
                load(args.device),
                args.lidar_device,
                validate=args.validate,
                max_revolutions=args.max_revolutions,
                validation_session=args.validation_session,
                validation_truth=args.validation_truth,
            )
            if "validation" in report:
                print_validation(report["validation"])
            print(
                json.dumps(
                    {
                        key: report[key]
                        for key in ("accepted_revolutions", "changed_cells", "cells_sign_changed")
                    }
                )
            )
        elif args.command == "validate":
            new_output(args.output, args.baseline, args.candidate, args.session)
            provenance = args.candidate / REPORT_NAME
            training = (
                set(read_json(provenance)["training_packet_sha256"])
                if provenance.is_file()
                else None
            )
            report = validate_maps(
                args.session,
                load_grid(args.baseline, "slam_map_loc"),
                load_grid(args.candidate, "slam_map_loc"),
                args.truth,
                load(args.device),
                args.lidar_device,
                max_revolutions=args.max_revolutions,
                training_packet_ids=training,
            )
            write_json_new(args.output, report)
            print_validation(report)
        else:
            from tools.maps.marker_observations import detect_stationary, generate_line_truth

            if args.command == "detect":
                intervals = detect_stationary(args.input)
                source = "localization_candidate"
            else:
                value = read_json(args.intervals)
                intervals = generate_line_truth(
                    value["intervals"] if isinstance(value, dict) else value,
                    origin=tuple(args.origin),
                    direction_deg=args.direction_deg,
                    yaw_deg=args.yaw_deg,
                    spacing_m=args.spacing_m,
                )
                source = "measured_line_geometry"
            write_json_new(
                args.output,
                {
                    "frame_id": "patrol",
                    "pose_origin": "lidar_centre",
                    "yaw_unit": "radians",
                    "truth_source": source,
                    "intervals": intervals,
                },
            )
            print(f"Wrote {len(intervals)} intervals ({source})")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
