"""Accumulate LiDAR observations only at measured, level, stationary poses.

The input pose is the LiDAR centre. This module never estimates motion from a
command or pretends an unknown pose is the previous pose. Moving scans are
counted but not committed to the permanent occupancy grid (ADR-7).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from host.common.lidar_link import Scan, ScanDecoder, scan_of
from host.slam import settings, viz
from host.slam.occupancy import OccupancyGrid
from host.slam.photo_map import PhotoRecorder
from host.slam.scan_match import integrate_scan, merge_batch, preprocess
from host.vision.stream_client import Frame

MAX_POSE_AGE_MS = 250
MAX_BATCH_AGE_MS = 500
MIN_ANGLE_BINS = 270
MAX_BATCH_PACKETS = 15


@dataclass(frozen=True, slots=True)
class LidarPose:
    received_ms: int
    x_m: float
    y_m: float
    yaw_rad: float
    stationary: bool
    level: bool

    def valid(self) -> bool:
        return all(math.isfinite(value) for value in (self.x_m, self.y_m, self.yaw_rad))


class ContinuousMap:
    """Map one accepted revolution at a time, with a fresh external pose."""

    def __init__(
        self,
        config: dict,
        lidar_device: str,
        photos: PhotoRecorder | None = None,
        *,
        source: str = "external_pose",
    ):
        lidar = config["lidar"]
        self.config = config
        self.lidar_device = lidar_device
        self.decoder = ScanDecoder(
            float(lidar.get("mount_yaw_deg", 0)), int(lidar.get("angle_direction", 1))
        )
        self.grid = OccupancyGrid.blank(
            resolution=float(lidar["resolution_mm"]) / 1000,
            span_cells=int(lidar["initial_span_cells"]),
        )
        self.photos = photos
        self.source = source
        self.track: list[tuple[float, float, float]] = []
        self.pose_track: list[LidarPose] = []
        self._batch: list[Scan] = []
        self._first_ms: int | None = None
        self._anchor_pose: LidarPose | None = None
        self._last_photo_ms: int | None = None
        self._last_photo_pose: tuple[float, float, float] | None = None
        self.counts = {
            "packets": 0,
            "mapped_revolutions": 0,
            "invalid_scan": 0,
            "wrong_device": 0,
            "missing_or_stale_pose": 0,
            "moving_or_tilted": 0,
            "sparse_batch": 0,
            "pose_changed_in_batch": 0,
        }

    def add(
        self,
        raw: bytes | str,
        received_ms: int,
        pose: LidarPose | None,
        frame: Frame | None = None,
        *,
        photo_dir: Path | None = None,
    ) -> bool:
        """Return true only when this packet completes a committed map update."""
        self.counts["packets"] += 1
        scan = scan_of(self.decoder.decode(raw))
        if scan is None:
            self.counts["invalid_scan"] += 1
            return False
        if scan.device_id != self.lidar_device:
            self.counts["wrong_device"] += 1
            return False
        if (
            pose is None
            or not pose.valid()
            or pose.received_ms > received_ms
            or received_ms - pose.received_ms > MAX_POSE_AGE_MS
        ):
            self._clear_batch()
            self.counts["missing_or_stale_pose"] += 1
            return False
        if (
            not self.pose_track
            or received_ms - self.pose_track[-1].received_ms >= 500
            or math.hypot(
                pose.x_m - self.pose_track[-1].x_m,
                pose.y_m - self.pose_track[-1].y_m,
            )
            >= 0.01
        ):
            self.pose_track.append(pose)
        if not pose.stationary or not pose.level:
            self._clear_batch()
            self.counts["moving_or_tilted"] += 1
            return False
        if self._first_ms is not None and received_ms - self._first_ms > MAX_BATCH_AGE_MS:
            self._clear_batch()
            self.counts["sparse_batch"] += 1
        if self._anchor_pose is not None and (
            math.hypot(pose.x_m - self._anchor_pose.x_m, pose.y_m - self._anchor_pose.y_m) > 0.02
            or abs(math.remainder(pose.yaw_rad - self._anchor_pose.yaw_rad, 2 * math.pi))
            > math.radians(2)
        ):
            self._clear_batch()
            self.counts["pose_changed_in_batch"] += 1
        if not self._batch:
            self._first_ms = received_ms
            self._anchor_pose = pose
        self._batch.append(scan)
        merged = merge_batch([item.points for item in self._batch])
        if len(merged) < MIN_ANGLE_BINS:
            if len(self._batch) >= MAX_BATCH_PACKETS:
                self._clear_batch()
                self.counts["sparse_batch"] += 1
            return False
        points = preprocess(merged, *settings.range_from_config(self.config))
        if len(points) < MIN_ANGLE_BINS:
            self._clear_batch()
            self.counts["sparse_batch"] += 1
            return False
        position = (pose.x_m, pose.y_m, pose.yaw_rad)
        integrate_scan(
            self.grid,
            position,
            points,
            hit=float(self.config["lidar"]["hit_logodds"]),
            miss=float(self.config["lidar"]["miss_logodds"]),
            pad_cells=int(self.config["lidar"]["expand_pad_cells"]),
        )
        self.track.append(position)
        self.counts["mapped_revolutions"] += 1
        if (
            self.photos is not None
            and photo_dir is not None
            and frame is not None
            and frame.looks_like_jpeg
            and abs(frame.received_ms - received_ms) <= 500
            and (
                self._last_photo_ms is None
                or (
                    received_ms - self._last_photo_ms >= 2000
                    and self._last_photo_pose is not None
                    and (
                        math.hypot(
                            pose.x_m - self._last_photo_pose[0],
                            pose.y_m - self._last_photo_pose[1],
                        )
                        >= 0.2
                        or abs(math.remainder(pose.yaw_rad - self._last_photo_pose[2], 2 * math.pi))
                        >= math.radians(15)
                    )
                )
            )
        ):
            self.photos.capture(photo_dir, len(self.track), position, received_ms, frame)
            self._last_photo_ms = received_ms
            self._last_photo_pose = position
        self._clear_batch()
        return True

    def _clear_batch(self) -> None:
        self._batch.clear()
        self._first_ms = None
        self._anchor_pose = None

    def save(self, directory: Path) -> None:
        import html
        import json

        directory.mkdir(parents=True, exist_ok=True)
        self.grid.save(directory, stem="slam_map")
        viz.save_png(self.grid, directory / "slam_map.png")
        (directory / "track.json").write_text(
            json.dumps(self.track, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (directory / "pose_track.json").write_text(
            json.dumps(
                [
                    {
                        "received_ms": item.received_ms,
                        "x_m": item.x_m,
                        "y_m": item.y_m,
                        "yaw_rad": item.yaw_rad,
                        "stationary": item.stationary,
                        "level": item.level,
                    }
                    for item in self.pose_track
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (directory / "summary.json").write_text(
            json.dumps(
                {
                    "kind": "pose_gated_lidar_map",
                    "source": self.source,
                    "pose_origin": "lidar_centre",
                    "counts": self.counts,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if self.photos is not None:
            self.photos.save(directory, self.grid.extent, has_png=True)
        left, right, bottom, top = self.grid.extent
        trail = " ".join(
            f"{100 * (x - left) / (right - left):.3f},{100 * (top - y) / (top - bottom):.3f}"
            for x, y in (
                [(item.x_m, item.y_m) for item in self.pose_track]
                or [(x, y) for x, y, _ in self.track]
            )
        )
        current = (
            (self.pose_track[-1].x_m, self.pose_track[-1].y_m)
            if self.pose_track
            else ((self.track[-1][0], self.track[-1][1]) if self.track else None)
        )
        marker = ""
        if current is not None:
            marker_x = 100 * (current[0] - left) / (right - left)
            marker_y = 100 * (top - current[1]) / (top - bottom)
            marker = f'<circle cx="{marker_x:.3f}" cy="{marker_y:.3f}" r="0.8" fill="#28e5a3"/>'
        status = html.escape(
            f"지도 반영 {self.counts['mapped_revolutions']}회 · "
            f"이동/기울기 제외 {self.counts['moving_or_tilted']}건 · "
            f"위치 없음/지연 {self.counts['missing_or_stale_pose']}건"
        )
        photo_link = '<p><a href="photo_map.html">촬영 위치와 사진</a></p>' if self.photos else ""
        page = (
            '<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="2"><title>LiDAR 누적 지도</title>'
            "<style>body{font:16px sans-serif;background:#181c24;color:white;"
            "max-width:1000px;margin:auto;padding:1rem}.map{position:relative;"
            f"width:min(90vw,850px);aspect-ratio:{self.grid.meta.width}/{self.grid.meta.height}"
            "}.map img,.map svg{position:absolute;inset:0;width:100%;height:100%}"
            "svg{overflow:visible}a{color:#8bd9ff}</style>"
            "<h1>LiDAR 누적 지도</h1><p>초록 선은 입력된 센서 위치 경로, 점은 최신 위치입니다. "
            "회색은 미관측 공간입니다. 표시된 경로의 정확도는 위치 입력에 따릅니다.</p>"
            f"<p>{status}</p>{photo_link}"
            '<div class="map"><img src="slam_map.png" alt="점유격자 지도">'
            '<svg viewBox="0 0 100 100" preserveAspectRatio="none">'
            f'<polyline points="{trail}" fill="none" stroke="#28e5a3" stroke-width="0.45"/>'
            f"{marker}"
            "</svg></div></html>"
        )
        (directory / "map_view.html").write_text(page, encoding="utf-8")
