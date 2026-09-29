"""Export measured 2D geometry and camera observations for a later sim viewer.

No wall height, furniture shape, camera depth, or robot pose is inferred here.
Unknown cells remain unknown so a renderer cannot silently treat them as floor.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from host.slam.occupancy import OccupancyGrid


def _runs(cells: np.ndarray, threshold: float, *, occupied: bool) -> list[list[int]]:
    """Return row/start/end runs, with end exclusive."""
    rows: list[list[int]] = []
    mask = cells >= threshold if occupied else cells <= threshold
    for row, values in enumerate(mask):
        starts = np.flatnonzero(values & ~np.r_[False, values[:-1]])
        ends = np.flatnonzero(values & ~np.r_[values[1:], False]) + 1
        rows.extend([row, int(start), int(end)] for start, end in zip(starts, ends, strict=True))
    return rows


def build_scene_bundle(
    grid: OccupancyGrid,
    *,
    occupied_logodds: float,
    free_logodds: float,
    source: str,
    photos: list[dict[str, Any]] | None = None,
    track: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Preserve measured coordinates; attach photos only as camera observations."""
    if not (math.isfinite(free_logodds) and math.isfinite(occupied_logodds)):
        raise ValueError("점유 경계값은 유한해야 함")
    if free_logodds >= 0 or occupied_logodds <= 0:
        raise ValueError("빈 공간 경계는 음수, 장애물 경계는 양수여야 함")
    if not math.isfinite(grid.meta.resolution) or grid.meta.resolution <= 0:
        raise ValueError("지도 해상도는 양수여야 함")
    if not np.isfinite(grid.cells).all():
        raise ValueError("지도에 유한하지 않은 셀이 있음")
    observations = []
    for photo in photos or []:
        pose = [photo[key] for key in ("x_m", "y_m", "yaw_rad")]
        if not all(type(value) in (int, float) and math.isfinite(value) for value in pose):
            raise ValueError("카메라 위치가 유효하지 않음")
        observations.append(
            {
                "pose_lidar_centre": pose,
                "frame_received_ms": photo["frame_received_ms"],
                "photo": photo["photo"],
                "projection": "unavailable_until_camera_calibration_and_depth",
            }
        )
    path = track or []
    for pose in path:
        if len(pose) != 3 or not all(math.isfinite(value) for value in pose):
            raise ValueError("경로 좌표가 유효하지 않음")
    occupied_runs = _runs(grid.cells, occupied_logodds, occupied=True)
    free_runs = _runs(grid.cells, free_logodds, occupied=False)
    return {
        "schema": "mechadog_scene_bundle_v1",
        "source": source,
        "coordinate_frame": "lidar_centre_xy_metres",
        "geometry": {
            "dimension": "2d_scan_plane_only",
            "height_m": None,
            "resolution_m": grid.meta.resolution,
            "origin_xy_m": [grid.meta.origin_x, grid.meta.origin_y],
            "width_cells": grid.meta.width,
            "height_cells": grid.meta.height,
            "occupied_runs_row_start_end": occupied_runs,
            "free_runs_row_start_end": free_runs,
            "unknown_cells": "all cells outside occupied/free runs",
        },
        "camera_observations": observations,
        "lidar_centre_track": path,
        "navigation_ready": False,
        "missing_for_reality_link": [
            "measured_pose_and_tf",
            "camera_intrinsics_and_extrinsics",
            "multi_view_depth_or_reconstruction",
            "physical_navigation_validation",
        ],
    }


def export_scene_bundle(
    map_directory: Path, output: Path, config: dict[str, Any], *, stem: str = "slam_map"
) -> dict[str, Any]:
    """Read an existing saved map; never opens a sensor or command socket."""
    if stem not in {"slam_map", "static_map"}:
        raise ValueError("지원하는 지도 이름은 slam_map 또는 static_map")
    grid = OccupancyGrid.load(map_directory, stem=stem)
    photo_file = map_directory / "photo_poses.json"
    track_file = map_directory / "track.json"
    summary_file = map_directory / "summary.json"
    photos = (
        json.loads(photo_file.read_text(encoding="utf-8"))["records"] if photo_file.exists() else []
    )
    track = json.loads(track_file.read_text(encoding="utf-8")) if track_file.exists() else []
    summary = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.exists() else {}
    lidar = config["lidar"]
    bundle = build_scene_bundle(
        grid,
        occupied_logodds=float(lidar["occupied_logodds"]),
        free_logodds=float(lidar["free_logodds"]),
        source=summary.get("source", summary.get("kind", "saved_map")),
        photos=photos,
        track=track,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return bundle
