"""Build a provenance-aware map bundle from explicitly supplied local evidence.

Private geometry belongs in the input JSON, never in this reusable tool.
The output must be new; existing source and destination bundles are protected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from host.behavior.planner import body_collision_mask, inflate, plan_to
from host.slam.occupancy import LOGODDS_MAX, LOGODDS_MIN, MapMeta, OccupancyGrid
from host.slam.settings import load, plan_params_from_config


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def reproject(source, target, frame):
    rows, cols = np.indices(target.cells.shape)
    x = target.meta.origin_x + (cols + 0.5) * target.meta.resolution
    y = target.meta.origin_y + (rows + 0.5) * target.meta.resolution
    a = math.radians(frame["rotate_deg"])
    px = math.cos(a) * x - math.sin(a) * y + frame["translate_x"]
    py = math.sin(a) * x + math.cos(a) * y + frame["translate_y"]
    r = np.floor((py - source.meta.origin_y) / source.meta.resolution).astype(int)
    c = np.floor((px - source.meta.origin_x) / source.meta.resolution).astype(int)
    valid = (r >= 0) & (r < source.cells.shape[0]) & (c >= 0) & (c < source.cells.shape[1])
    cells = np.zeros_like(target.cells)
    cells[valid] = source.cells[r[valid], c[valid]]
    return cells, px, py


def rectangle(px, py, box):
    return (px >= box[0]) & (py >= box[1]) & (px < box[2]) & (py < box[3])


def polygons_mask(polygons, grid, frame):
    canvas = Image.new("1", (grid.meta.width, grid.meta.height))
    draw = ImageDraw.Draw(canvas)
    a = math.radians(frame["rotate_deg"])
    ca, sa = math.cos(a), math.sin(a)
    for polygon in polygons:
        points = []
        for px, py in polygon:
            dx, dy = px - frame["translate_x"], py - frame["translate_y"]
            x, y = ca * dx + sa * dy, -sa * dx + ca * dy
            points.append(
                (
                    (x - grid.meta.origin_x) / grid.meta.resolution,
                    (y - grid.meta.origin_y) / grid.meta.resolution,
                )
            )
        draw.polygon(points, fill=1)
    return np.asarray(canvas, dtype=bool)


def input_hashes(spec, baseline):
    paths = [
        Path(spec["measured_map"]) / (spec["measured_stem"] + ".npy"),
        Path(spec["measured_map"]) / "map_meta.json",
    ]
    paths.extend(
        baseline / name
        for name in ("slam_map.npy", "slam_map_loc.npy", "map_meta.json", "pose_frame.json")
    )
    paths.append(Path(__file__).resolve().parents[2] / "config/config.yaml")
    for item in spec.get("recent_patches", []):
        directory = Path(item["maps"])
        paths.extend(
            directory / name
            for name in (
                item.get("stem", "slam_map_loc") + ".npy",
                "map_meta.json",
                "pose_frame.json",
            )
        )
    for item in spec.get("navigation_observation_keepouts", []):
        paths.extend(
            Path(item[key]) for key in ("mask", "endpoint_support", "pose_frame", "map_meta")
        )
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()
    }


def build(args):
    spec = read_json(args.evidence)
    destination = Path(args.output)
    if destination.exists():
        raise FileExistsError(f"Refusing to replace existing output: {destination}")
    baseline = Path(spec["baseline"])
    nav0 = OccupancyGrid.load(baseline)
    frame = read_json(baseline / "pose_frame.json")
    measured_source = OccupancyGrid.load(Path(spec["measured_map"]), stem=spec["measured_stem"])
    measured, px, py = reproject(measured_source, nav0, frame)
    config = load(None)
    params = plan_params_from_config(config)
    floor = np.zeros_like(measured, dtype=bool)
    zone_area = np.zeros_like(measured, dtype=np.uint8)
    # Explicit room boundaries take priority; circulation areas are assigned last.
    for item in spec["floors"]:
        mask = rectangle(px, py, item["rect"])
        floor |= mask
        zone_area[mask & (zone_area == 0)] = item["zone_index"]
    walls = polygons_mask(spec["wall_polygons"], nav0, frame)
    keepout = polygons_mask(spec["keepout_polygons"], nav0, frame)
    furniture = np.zeros_like(floor)
    for item in spec["furniture_keepouts"]:
        furniture |= rectangle(px, py, item["rect"])
    nav_measurement = measured.copy()
    patch = np.zeros_like(floor)
    occupied_to_free_conflicts = np.zeros_like(floor)
    free_to_occupied_conflicts = np.zeros_like(floor)
    for item in spec.get("recent_patches", []):
        recent = OccupancyGrid.load(Path(item["maps"]), stem=item.get("stem", "slam_map_loc"))
        if recent.meta.as_dict() != nav0.meta.as_dict():
            raise ValueError("Recent patch must share the target grid frame and metadata")
        recent_frame = read_json(Path(item["maps"]) / "pose_frame.json")
        if any(
            not math.isclose(recent_frame[k], frame[k], abs_tol=1e-9)
            for k in ("rotate_deg", "translate_x", "translate_y")
        ):
            raise ValueError("Recent patch pose frame differs from the target")
        roi = rectangle(px, py, item["plan_roi"])
        known = (recent.cells != 0) & roi
        occupied_to_free_conflicts |= (
            known & (measured >= params.occ_thresh) & (recent.cells <= params.free_thresh)
        )
        free_to_occupied_conflicts |= (
            known & (measured <= params.free_thresh) & (recent.cells >= params.occ_thresh)
        )
        measured[known] = recent.cells[known]
        if item.get("fill_navigation_unknown", False):
            observed_free = (recent.cells <= params.free_thresh) & roi
            unconfirmed = (nav_measurement > params.free_thresh) & (
                nav_measurement < params.occ_thresh
            )
            nav_measurement[observed_free & unconfirmed] = recent.cells[observed_free & unconfirmed]
        patch |= known
    free = (nav_measurement <= params.free_thresh) & floor
    occupied = nav_measurement >= params.occ_thresh
    vicinity = ndimage.binary_dilation(floor | walls, iterations=2)
    occupied &= vicinity
    # A drawing never overwrites actual ray evidence. Unseen boundaries remain blocked.
    supplemental_walls = walls & (nav_measurement == 0)
    furniture_border = furniture & ~ndimage.binary_erosion(furniture)
    nav = np.zeros_like(measured, dtype=np.float32)
    nav[free & ~furniture & ~keepout] = LOGODDS_MIN
    nav[occupied | supplemental_walls | furniture_border | keepout] = LOGODDS_MAX
    observation_keepouts = np.zeros_like(floor)
    for item in spec.get("navigation_observation_keepouts", []):
        requested = np.load(item["mask"]).astype(bool)
        support = np.load(item["endpoint_support"])
        if requested.shape != nav.shape or support.shape != nav.shape:
            raise ValueError("Observation keepout evidence must use the target grid")
        evidence_frame = read_json(item["pose_frame"])
        evidence_meta = read_json(item["map_meta"])
        if evidence_meta != nav0.meta.as_dict() or any(
            not math.isclose(evidence_frame[k], frame[k], abs_tol=1e-9)
            for k in ("rotate_deg", "translate_x", "translate_y")
        ):
            raise ValueError("Observation keepout evidence frame differs from target")
        minimum = int(item.get("minimum_distinct_endpoint_scans", 2))
        if minimum < 2 or np.any(requested & (support < minimum)):
            raise ValueError("Keepout requires repeated endpoint observations")
        # Conflicting hit/free evidence cannot open unknown space. Treat repeated
        # returns as conservative NAV keepouts, retaining both source layers.
        eligible = requested & (nav == 0) & floor & ~furniture & ~keepout
        observation_keepouts |= eligible
        nav[eligible] = LOGODDS_MAX
    loc = measured.copy()
    if not spec.get("retain_measured_returns_outside_navigation", False):
        loc[~vicinity] = 0
    # Synthetic geometry never becomes a sensor return. Pose origins still require floor.
    loc[(loc < 0) & ~(floor & ~furniture & ~keepout)] = 0
    grid = OccupancyGrid(MapMeta.of(nav0.meta.as_dict()), nav)
    loc_grid = OccupancyGrid(MapMeta.of(nav0.meta.as_dict()), loc)
    blocked = inflate(grid, params, obstacle_grid=loc_grid)
    body = body_collision_mask(grid, params, obstacle_grid=loc_grid)
    components, _ = ndimage.label(~blocked, structure=np.ones((3, 3)))
    start_cell = grid.to_cell(0, 0)
    start_component = components[start_cell]
    if args.diagnostics:
        diag = Path(args.diagnostics)
        diag.mkdir(parents=True, exist_ok=False)
        np.savez_compressed(
            diag / "candidate.npz",
            nav=nav,
            loc=loc,
            blocked=blocked,
            body=body,
            zone_area=zone_area,
            components=components,
            floor=floor,
            walls=walls,
            furniture=furniture,
            measured=measured,
            nav_measurement=nav_measurement,
        )
        (diag / "meta.json").write_text(json.dumps(grid.meta.as_dict()), encoding="utf-8")
        (diag / "counts.json").write_text(
            json.dumps(
                {
                    "start_component": int(start_component),
                    "zones": {
                        label: {
                            "free": int(((zone_area == i) & ~blocked).sum()),
                            "reachable": int(
                                ((zone_area == i) & (components == start_component)).sum()
                            ),
                        }
                        for i, label in enumerate(spec["zone_ids"], 1)
                    },
                }
            ),
            encoding="utf-8",
        )
    if not start_component:
        raise ValueError("Source origin is blocked; refusing a fabricated free start")
    distances = ndimage.distance_transform_edt(~body) * grid.meta.resolution
    zone_anchors = {}
    for index, label in enumerate(spec["zone_ids"], start=1):
        room = spec["anchor_rooms"][label]
        inset = float(spec.get("anchor_inset_m", 0))
        primary_room = rectangle(
            px, py, [room[0] + inset, room[1] + inset, room[2] - inset, room[3] - inset]
        )
        candidates = (
            (zone_area == index) & primary_room & (components == start_component) & ~blocked
        )
        rr, cc = np.nonzero(candidates)
        if not len(rr):
            raise ValueError(f"No reachable free anchor in {label}")
        centre_r, centre_c = np.mean(rr), np.mean(cc)
        priority = (
            distances[rr, cc] - 0.12 * np.hypot(rr - centre_r, cc - centre_c) * grid.meta.resolution
        )
        selected = int(np.argmax(priority))
        x, y = grid.to_world(int(rr[selected]), int(cc[selected]))
        plan = plan_to(label, (x, y), (0, 0), grid, blocked, params, body_blocked=body)
        if not plan.reachable or plan.goal_moved_m > 1e-8:
            raise ValueError(f"Invalid exact anchor in {label}: {plan.fail_reason}")
        zone_anchors[label] = {"x": x, "y": y}
    labels = np.where(nav <= params.free_thresh, zone_area, 0).astype(np.uint8)
    if np.any((nav <= params.free_thresh) & (labels == 0)):
        raise ValueError("Free floor has unassigned zone labels")
    destination.mkdir(parents=True)
    grid.save(destination)
    np.save(destination / "slam_map_loc.npy", loc)
    np.save(destination / "zone_labels.npy", labels)
    shutil.copy2(baseline / "pose_frame.json", destination / "pose_frame.json")
    (destination / "zones.json").write_text(
        json.dumps(zone_anchors, indent=2) + "\n", encoding="utf-8"
    )
    zone_plan = {
        "meta": grid.meta.as_dict(),
        "pose_frame": frame,
        "note": "Explicit room and doorway boundaries; all observed free floor is labelled.",
        "zones": [
            {
                "id": label,
                "index": i,
                "label": label,
                "patrol": zone_anchors[label],
                "plan": {
                    "x": math.cos(math.radians(frame["rotate_deg"])) * zone_anchors[label]["x"]
                    - math.sin(math.radians(frame["rotate_deg"])) * zone_anchors[label]["y"]
                    + frame["translate_x"],
                    "y": math.sin(math.radians(frame["rotate_deg"])) * zone_anchors[label]["x"]
                    + math.cos(math.radians(frame["rotate_deg"])) * zone_anchors[label]["y"]
                    + frame["translate_y"],
                },
            }
            for i, label in enumerate(spec["zone_ids"], 1)
        ],
    }
    (destination / "zones_plan.json").write_text(
        json.dumps(zone_plan, indent=2) + "\n", encoding="utf-8"
    )
    masks = {
        "measured_occupied": occupied,
        "observed_free": free,
        "supplemental_unseen_plan_walls": supplemental_walls,
        "provisional_furniture": furniture,
        "keepout": keepout,
        "recent_patch": patch,
        "registered_return_conflict_keepouts": observation_keepouts,
        "localization_occupied_to_free_conflicts": occupied_to_free_conflicts,
        "localization_free_to_occupied_conflicts": free_to_occupied_conflicts,
    }
    np.savez_compressed(destination / "source_masks.npz", **masks)
    report = {
        "spec": spec,
        "meta": grid.meta.as_dict(),
        "counts": {k: int(v.sum()) for k, v in masks.items()},
        "nav_free": int((nav <= params.free_thresh).sum()),
        "nav_occupied": int((nav >= params.occ_thresh).sum()),
        "loc_occupied": int((loc >= params.occ_thresh).sum()),
        "free_label_coverage": 1.0,
        "navigation_ready": False,
        "zone_clearance_from_body_mask_m": {
            k: float(distances[grid.to_cell(v["x"], v["y"])]) for k, v in zone_anchors.items()
        },
        "input_spec_sha256": hashlib.sha256(Path(args.evidence).read_bytes()).hexdigest(),
        "input_sha256": input_hashes(spec, baseline),
    }
    (destination / "build_manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rgb = np.full((*nav.shape, 3), 135, np.uint8)
    palette = {1: (188, 211, 223), 2: (219, 230, 216), 3: (234, 218, 192), 4: (216, 207, 228)}
    for i, colour in palette.items():
        rgb[labels == i] = colour
    rgb[nav >= params.occ_thresh] = (35, 39, 45)
    image = Image.fromarray(rgb[::-1]).resize(
        (nav.shape[1] * 5, nav.shape[0] * 5), Image.Resampling.NEAREST
    )
    draw = ImageDraw.Draw(image)
    for label, point in {"S": {"x": 0, "y": 0}, **zone_anchors}.items():
        r, c = grid.to_cell(point["x"], point["y"])
        x, y = (c + 0.5) * 5, (nav.shape[0] - r - 0.5) * 5
        draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill="white", outline="black", width=2)
        draw.text((x + 12, y - 7), label, fill="black")
    image.save(destination / "zone_map.png")
    print(json.dumps({k: v for k, v in report.items() if k != "spec"}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--diagnostics")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
