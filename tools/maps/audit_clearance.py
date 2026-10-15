"""Measure map clearance without editing maps or weakening collision rules."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from host.behavior.planner import _collision_cells, body_collision_mask, inflate, plan_to
from host.slam.occupancy import OccupancyGrid
from host.slam.settings import load, plan_params_from_config


def rectangle_distance(start, end, boxes):
    """Exact segment distance to closed, axis-aligned cell rectangles."""
    a, b = np.asarray(start, float), np.asarray(end, float)
    lo, hi = boxes[:, :2], boxes[:, 2:]
    if not len(boxes):
        return np.empty(0)
    delta = b - a
    length2 = float(delta @ delta)
    endpoint = np.minimum(
        np.linalg.norm(np.maximum(np.maximum(lo - a, a - hi), 0), axis=1),
        np.linalg.norm(np.maximum(np.maximum(lo - b, b - hi), 0), axis=1),
    )
    if not length2:
        return endpoint
    corners = np.stack([lo, hi, np.c_[lo[:, 0], hi[:, 1]], np.c_[hi[:, 0], lo[:, 1]]], axis=1)
    fraction = np.clip(((corners - a) @ delta) / length2, 0, 1)
    corner_distance = np.linalg.norm(corners - (a + fraction[..., None] * delta), axis=2).min(1)
    entry, leave = np.zeros(len(boxes)), np.ones(len(boxes))
    possible = np.ones(len(boxes), bool)
    for axis in range(2):
        if abs(delta[axis]) < 1e-12:
            possible &= (a[axis] >= lo[:, axis]) & (a[axis] <= hi[:, axis])
        else:
            t0, t1 = (lo[:, axis] - a[axis]) / delta[axis], (hi[:, axis] - a[axis]) / delta[axis]
            entry = np.maximum(entry, np.minimum(t0, t1))
            leave = np.minimum(leave, np.maximum(t0, t1))
    result = np.minimum(endpoint, corner_distance)
    result[possible & (entry <= leave)] = 0
    return result


def mask_boxes(grid, mask):
    rr, cc = np.nonzero(mask)
    res, ox, oy = grid.meta.resolution, grid.meta.origin_x, grid.meta.origin_y
    lo = np.c_[ox + cc * res, oy + rr * res]
    return np.c_[lo, lo + res]


def clearance(grid, boxes, start, end):
    # Only nearby rectangles can improve the initial nearest rectangle bound.
    d = rectangle_distance(start, end, boxes)
    occupied = float(d.min()) if len(d) else math.inf
    x0, x1, y0, y1 = grid.extent
    edge = min(
        start[0] - x0,
        end[0] - x0,
        x1 - start[0],
        x1 - end[0],
        start[1] - y0,
        end[1] - y0,
        y1 - start[1],
        y1 - end[1],
    )
    return float(max(0, min(occupied, edge)))


def audit(folder, tracking_error):
    nav = OccupancyGrid.load(folder)
    loc = OccupancyGrid.load(folder, stem="slam_map_loc")
    params = plan_params_from_config(load(None))
    combined = _collision_cells(nav, loc, params, 0)
    body = body_collision_mask(nav, params, obstacle_grid=loc)
    blocked = inflate(nav, params, obstacle_grid=loc)
    occupied_boxes = mask_boxes(nav, combined >= params.occ_thresh)
    nonfree_boxes = mask_boxes(nav, combined > params.free_thresh)
    body_boxes = mask_boxes(nav, body)
    zones = json.loads((folder / "zones.json").read_text())
    points = {"S": [0.0, 0.0], **{k: [v["x"], v["y"]] for k, v in zones.items()}}
    point_rows, plans = {}, []
    for label, point in points.items():
        row, col = nav.to_cell(*point)
        unknown_distance = clearance(nav, nonfree_boxes, point, point)
        mask_distance = clearance(nav, body_boxes, point, point)
        point_rows[label] = {
            "point": point,
            "nav_value": float(nav.cells[row, col]),
            "body_blocked": bool(body[row, col]),
            "occupied_boundary_m": clearance(nav, occupied_boxes, point, point),
            "nonfree_boundary_m": unknown_distance,
            "physical_body_margin_m": unknown_distance - params.body_radius_m,
            "body_mask_boundary_m": mask_distance,
            "tracking_disk_safe": unknown_distance + 1e-9 >= params.body_radius_m + tracking_error
            and mask_distance + 1e-9 >= tracking_error,
        }
    for source, start in points.items():
        for target, end in points.items():
            if source == target:
                continue
            plan = plan_to(target, end, start, nav, blocked, params, body_blocked=body)
            if not plan.reachable:
                plans.append(
                    {"from": source, "to": target, "reachable": False, "reason": plan.fail_reason}
                )
                continue
            segments = list(zip(plan.waypoints[:-1], plan.waypoints[1:], strict=True)) or [
                (start, end)
            ]
            d_nonfree = min(clearance(nav, nonfree_boxes, a, b) for a, b in segments)
            d_body = min(clearance(nav, body_boxes, a, b) for a, b in segments)
            plans.append(
                {
                    "from": source,
                    "to": target,
                    "reachable": True,
                    "nonfree_boundary_m": d_nonfree,
                    "physical_body_margin_m": d_nonfree - params.body_radius_m,
                    "body_mask_boundary_m": d_body,
                    "tracking_disk_safe": d_nonfree + 1e-9 >= params.body_radius_m + tracking_error
                    and d_body + 1e-9 >= tracking_error,
                }
            )
    return {
        "folder": str(folder),
        "body_radius_m": params.body_radius_m,
        "tracking_error_m": tracking_error,
        "required_nonfree_distance_m": params.body_radius_m + tracking_error,
        "distance_definition": "Exact continuous distance to closed cell-square boundaries; map exterior unsafe. body_mask already includes body radius; require additional tracking error, not body radius again.",
        "points": point_rows,
        "plans": plans,
        "summary": {
            "points_safe": sum(x["tracking_disk_safe"] for x in point_rows.values()),
            "plans_safe": sum(x.get("tracking_disk_safe", False) for x in plans),
            "plan_count": len(plans),
        },
        "source_sha256": {
            f: hashlib.sha256((folder / f).read_bytes()).hexdigest()
            for f in [
                "slam_map.npy",
                "slam_map_loc.npy",
                "map_meta.json",
                "pose_frame.json",
                "zones.json",
            ]
        },
        "independent_physical_truth": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tracking-error", type=float, default=0.05)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if not math.isfinite(args.tracking_error) or args.tracking_error < 0:
        parser.error("tracking error must be finite and nonnegative")
    report = audit(args.maps, args.tracking_error)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps({"points": report["points"], "summary": report["summary"]}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
