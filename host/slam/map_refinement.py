"""Refine a copied occupancy map from verified stationary LiDAR observations.

This is an offline/PC composition of ``ContinuousMap``. It neither activates a
map nor changes the SIM or a robot. Poses are measured at the LiDAR centre in the
declared map frame; a STOP command does not establish stationarity or levelness.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

from host.common.lidar_link import ScanDecoder, scan_of
from host.common.protocol import DecodeResult
from host.slam.continuous_map import MAX_POSE_AGE_MS, ContinuousMap, LidarPose
from host.slam.occupancy import LOGODDS_MAX, MapMeta, OccupancyGrid

Rectangle = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """``accepted`` means a complete revolution was committed, not a packet."""

    accepted: bool
    reason: str
    revision: str
    changed_cells: int
    observation_id: str | None = None


class _RefinementDecoder(ScanDecoder):
    """Keep the existing sequence gate and discard a batch across device boots."""

    def __init__(
        self,
        mount_yaw_deg: float,
        angle_direction: int,
        clear_batch: Callable[[], None],
    ) -> None:
        super().__init__(mount_yaw_deg, angle_direction)
        self.last_result: DecodeResult | None = None
        self._session: tuple[str, str] | None = None
        self._clear_batch = clear_batch

    def decode(self, raw: str | bytes) -> DecodeResult:
        result = super().decode(raw)
        self.last_result = result
        scan = scan_of(result)
        if scan is not None:
            session = (scan.device_id, scan.boot_id)
            if self._session is not None and session != self._session:
                self._clear_batch()
            self._session = session
        return result


def _copy_grid(grid: OccupancyGrid) -> OccupancyGrid:
    return OccupancyGrid(MapMeta.of(grid.meta.as_dict()), grid.cells.copy())


def _validated_rectangles(rectangles: Iterable[Rectangle], role: str) -> tuple[Rectangle, ...]:
    checked: list[Rectangle] = []
    for rectangle in rectangles:
        if len(rectangle) != 4 or not all(math.isfinite(value) for value in rectangle):
            raise ValueError(f"{role} rectangle must contain four finite coordinates")
        min_x, min_y, max_x, max_y = rectangle
        if min_x >= max_x or min_y >= max_y:
            raise ValueError(f"{role} rectangle must have positive width and height")
        checked.append((float(min_x), float(min_y), float(max_x), float(max_y)))
    return tuple(checked)


class MapRefinement:
    """Accumulate verified stationary scans without exposing a mutable map.

    ``no_go_rectangles`` contain ``(min_x, min_y, max_x, max_y)`` in map metres.
    Every overlapping cell is occupied. When ``allowed_rectangles`` is provided,
    a cell must fit wholly within an allowed rectangle to remain eligible. This
    conservative union never clears a partly excluded cell at room boundaries.
    The scope and no-go rectangles are applied again after
    every commit, including grid expansion, so free-space rays cannot erase them.
    ``revision`` identifies map content, frame and exclusions, not packet count.
    """

    def __init__(
        self,
        config: dict[str, Any],
        lidar_device: str,
        seed_grid: OccupancyGrid,
        frame_id: str,
        *,
        no_go_rectangles: Iterable[Rectangle] = (),
        allowed_rectangles: Iterable[Rectangle] | None = None,
    ) -> None:
        if not lidar_device or not frame_id:
            raise ValueError("lidar_device and frame_id must be nonempty")
        if (
            seed_grid.cells.ndim != 2
            or not np.issubdtype(seed_grid.cells.dtype, np.floating)
            or not np.isfinite(seed_grid.cells).all()
            or not math.isfinite(seed_grid.meta.resolution)
            or seed_grid.meta.resolution <= 0
            or not math.isfinite(seed_grid.meta.origin_x)
            or not math.isfinite(seed_grid.meta.origin_y)
            or seed_grid.cells.size == 0
        ):
            raise ValueError("seed_grid must be a finite floating occupancy grid")
        self.frame_id = frame_id
        self.lidar_device = lidar_device
        self._no_go = _validated_rectangles(no_go_rectangles, "no-go")
        self._allowed = (
            None
            if allowed_rectangles is None
            else _validated_rectangles(allowed_rectangles, "allowed")
        )
        mapping_config = copy.deepcopy(config)
        lidar = mapping_config["lidar"]
        resolution_m = float(lidar["resolution_mm"]) / 1000
        if not math.isclose(seed_grid.meta.resolution, resolution_m, abs_tol=1e-12):
            raise ValueError("seed_grid resolution must match lidar.resolution_mm")
        # ContinuousMap's existing constructor has no return annotation.
        self._mapper = ContinuousMap(  # type: ignore[no-untyped-call]
            mapping_config, lidar_device, source="verified_stationary_refinement"
        )
        self._mapper.grid = _copy_grid(seed_grid)
        self._decoder = _RefinementDecoder(
            float(lidar.get("mount_yaw_deg", 0)),
            int(lidar.get("angle_direction", 1)),
            self._mapper._clear_batch,
        )
        self._mapper.decoder = self._decoder
        self._received_ms: int | None = None
        self._submitted = 0
        self._rejected: dict[str, int] = {}
        self._apply_no_go()
        self._revision = self._checksum()

    @property
    def revision(self) -> str:
        return self._revision

    def snapshot(self) -> OccupancyGrid:
        """Return a detached grid; modifying it cannot change this accumulator."""
        return _copy_grid(self._mapper.grid)

    def metadata(self) -> dict[str, Any]:
        """Return serializable detached provenance and gate counters."""
        return {
            "schema_version": 1,
            "kind": "verified_stationary_map_refinement",
            "frame_id": self.frame_id,
            "pose_origin": "lidar_centre",
            "lidar_device": self.lidar_device,
            "revision": self._revision,
            "grid": self._mapper.grid.meta.as_dict(),
            "no_go_rectangles": [list(rectangle) for rectangle in self._no_go],
            "allowed_rectangles_m": (
                None if self._allowed is None else [list(rectangle) for rectangle in self._allowed]
            ),
            "counts": {
                "submitted": self._submitted,
                "mapped_revolutions": self._mapper.counts["mapped_revolutions"],
                "rejected": dict(self._rejected),
                "continuous_map": dict(self._mapper.counts),
            },
            "activation": "none",
            "sim_automatic_overwrite": False,
            "requirements": {
                "localization_valid": True,
                "laser_extrinsics_verified": True,
                "stationary_verified": True,
                "level_verified": True,
                "max_pose_age_ms": MAX_POSE_AGE_MS,
                "pose_origin": "lidar_centre",
            },
        }

    def submit(
        self,
        raw: bytes | str,
        received_ms: int,
        pose: LidarPose | None,
        *,
        pose_frame_id: str,
        localization_valid: bool,
        laser_extrinsics_verified: bool,
        stationary_verified: bool,
        level_verified: bool,
    ) -> UpdateResult:
        """Commit only complete, correctly located and verified revolutions.

        ``received_ms`` and ``pose.received_ms`` use the same host monotonic
        clock. Verification flags come from the upstream observation gate; this
        module cannot infer them from a command or a pre-existing map.
        """
        self._submitted += 1
        reason = self._gate_reason(
            received_ms,
            pose,
            pose_frame_id,
            localization_valid,
            laser_extrinsics_verified,
            stationary_verified,
            level_verified,
        )
        if reason:
            # Consume the existing decoder sequence gate even for rejected poses.
            # A previously rejected scan cannot be replayed with a newer pose.
            self._decoder.decode(raw)
            self._mapper._clear_batch()
            self._rejected[reason] = self._rejected.get(reason, 0) + 1
            return UpdateResult(False, reason, self._revision, 0)
        self._received_ms = received_ms
        before = self.snapshot()
        counters = dict(self._mapper.counts)
        accepted = self._mapper.add(raw, received_ms, pose)
        if not accepted:
            reason = "awaiting_revolution"
            for name in (
                "invalid_scan",
                "wrong_device",
                "missing_or_stale_pose",
                "moving_or_tilted",
                "sparse_batch",
                "pose_changed_in_batch",
            ):
                if self._mapper.counts[name] > counters[name]:
                    reason = name
                    break
            if reason != "awaiting_revolution":
                self._mapper._clear_batch()
                self._rejected[reason] = self._rejected.get(reason, 0) + 1
            return UpdateResult(False, reason, self._revision, 0)
        self._apply_no_go()
        changed = self._changed_cells(before)
        self._revision = self._checksum()
        decoded = self._decoder.last_result
        committed_scan = scan_of(decoded) if decoded is not None else None
        assert committed_scan is not None
        observation_id = hashlib.sha256(
            json.dumps(
                [committed_scan.device_id, committed_scan.boot_id, committed_scan.seq],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return UpdateResult(True, "committed", self._revision, changed, observation_id)

    def _gate_reason(
        self,
        received_ms: int,
        pose: LidarPose | None,
        pose_frame_id: str,
        localization_valid: bool,
        laser_extrinsics_verified: bool,
        stationary_verified: bool,
        level_verified: bool,
    ) -> str | None:
        if type(received_ms) is not int or received_ms < 0:
            return "invalid_receive_time"
        if self._received_ms is not None and received_ms < self._received_ms:
            return "receive_time_reversed"
        if pose_frame_id != self.frame_id:
            return "frame_mismatch"
        if localization_valid is not True:
            return "localization_unverified"
        if laser_extrinsics_verified is not True:
            return "laser_extrinsics_unverified"
        if stationary_verified is not True:
            return "stationary_unverified"
        if level_verified is not True:
            return "level_unverified"
        if pose is None:
            return "missing_pose"
        if not pose.valid() or type(pose.received_ms) is not int or pose.received_ms < 0:
            return "invalid_pose"
        if pose.received_ms > received_ms:
            return "future_pose"
        if received_ms - pose.received_ms > MAX_POSE_AGE_MS:
            return "stale_pose"
        if pose.stationary is not True:
            return "moving_pose"
        if pose.level is not True:
            return "tilted_pose"
        return None

    def _apply_no_go(self) -> None:
        grid = self._mapper.grid
        meta = grid.meta
        height, width = grid.cells.shape
        if self._allowed is not None:
            allowed = np.zeros(grid.cells.shape, dtype=bool)
            xs = meta.origin_x + np.arange(width) * meta.resolution
            ys = meta.origin_y + np.arange(height) * meta.resolution
            for min_x, min_y, max_x, max_y in self._allowed:
                columns = (xs >= min_x - 1e-9) & (xs + meta.resolution <= max_x + 1e-9)
                rows = (ys >= min_y - 1e-9) & (ys + meta.resolution <= max_y + 1e-9)
                allowed |= rows[:, None] & columns[None, :]
            grid.cells[~allowed] = LOGODDS_MAX
        for min_x, min_y, max_x, max_y in self._no_go:
            col0 = max(0, math.floor((min_x - meta.origin_x) / meta.resolution))
            row0 = max(0, math.floor((min_y - meta.origin_y) / meta.resolution))
            col1 = min(width, math.ceil((max_x - meta.origin_x) / meta.resolution))
            row1 = min(height, math.ceil((max_y - meta.origin_y) / meta.resolution))
            if row0 < row1 and col0 < col1:
                grid.cells[row0:row1, col0:col1] = LOGODDS_MAX

    def _changed_cells(self, before: OccupancyGrid) -> int:
        current = self._mapper.grid
        baseline = np.zeros_like(current.cells)
        row = round((before.meta.origin_y - current.meta.origin_y) / current.meta.resolution)
        col = round((before.meta.origin_x - current.meta.origin_x) / current.meta.resolution)
        height, width = before.cells.shape
        baseline[row : row + height, col : col + width] = before.cells
        return int(np.count_nonzero(current.cells != baseline))

    def _checksum(self) -> str:
        grid = self._mapper.grid
        payload = {
            "frame_id": self.frame_id,
            "no_go_rectangles": self._no_go,
            "allowed_rectangles": self._allowed,
            "grid": grid.meta.as_dict(),
            "dtype": grid.cells.dtype.str,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
        digest.update(grid.cells.tobytes(order="C"))
        return digest.hexdigest()
