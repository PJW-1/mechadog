"""Offline map-refinement mission decisions; callers own I/O and accepted map updates.

This layer reuses the existing A* and occupancy coordinate conversions. It never
opens a socket, assumes stationarity, accepts a candidate pose as localization,
or creates a new gait/rotation command model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import numpy as np

from host.behavior.planner import astar, detect_new_obstacle, dilate, mark_obstacle
from host.common.lidar_link import Scan
from host.common.units import wrap_pi
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.scan_match import Pose
from host.slam.settings import plan_params_from_config

Point = tuple[float, float]
Action = Literal["stop", "move", "observe", "finished", "halted"]
PoseStatus = Literal["localized", "candidate", "lost"]


@dataclass(frozen=True, slots=True)
class MissionStop:
    id: str
    x_m: float
    y_m: float

    @property
    def point(self) -> Point:
        return self.x_m, self.y_m


@dataclass(frozen=True, slots=True)
class MissionParams:
    """Defaults are preparation/test values, not validated real gait tuning.

    Deployment parameters come from ``mission_params_from_config``. Arrival
    radius and heading gate are mission-preparation defaults awaiting integration.
    ``obstacle_radius_m`` is TOTAL body-clearance radius, already inflated.
    """

    clearance_m: float = 0.25
    occ_thresh: float = 1.0
    free_thresh: float = -1.0
    settle_ms: int = 750
    pose_timeout_ms: int = 500
    scan_timeout_ms: int = 500
    telemetry_timeout_ms: int = 1000
    arrival_radius_m: float = 0.08
    heading_limit_rad: float = math.radians(45)
    forward_fan_rad: float = math.radians(35)
    immediate_stop_m: float = 0.30
    new_obstacle_check_m: float = 0.80
    new_obstacle_margin_m: float = 0.10
    obstacle_radius_m: float = 0.30
    obstacle_same_hit_m: float = 0.12
    new_obstacle_confirmations: int = 2

    def __post_init__(self) -> None:
        distances = (
            self.clearance_m,
            self.arrival_radius_m,
            self.heading_limit_rad,
            self.forward_fan_rad,
            self.immediate_stop_m,
            self.new_obstacle_check_m,
            self.new_obstacle_margin_m,
            self.obstacle_radius_m,
            self.obstacle_same_hit_m,
        )
        if any(not math.isfinite(value) or value <= 0 for value in distances):
            raise ValueError("Mission distances and angles must be finite and positive")
        if not all(math.isfinite(value) for value in (self.free_thresh, self.occ_thresh)):
            raise ValueError("Occupancy thresholds must be finite")
        if self.free_thresh >= self.occ_thresh:
            raise ValueError("free_thresh must be below occ_thresh")
        if self.new_obstacle_confirmations < 2:
            raise ValueError("Dynamic obstacles require at least two distinct confirmations")
        if (
            self.settle_ms < 750
            or min(self.pose_timeout_ms, self.scan_timeout_ms, self.telemetry_timeout_ms) <= 0
        ):
            raise ValueError("Settle must be at least 750 ms and freshness limits positive")


def mission_params_from_config(config: Mapping[str, Any]) -> MissionParams:
    """Read canonical safety values without modifying config or inventing a gait."""
    plan = plan_params_from_config(config)
    lidar, localization, safety = config["lidar"], config["localization"], config["safety"]
    return MissionParams(
        clearance_m=plan.clearance_m,
        occ_thresh=plan.occ_thresh,
        free_thresh=plan.free_thresh,
        settle_ms=int(localization["settle_delay_ms"]),
        pose_timeout_ms=int(localization["pose_timeout_ms"]),
        scan_timeout_ms=int(lidar["scan_stall_timeout_ms"]),
        telemetry_timeout_ms=int(safety["link_loss_failsafe_ms"]),
        immediate_stop_m=float(lidar["estop_distance_mm"]) / 1000.0,
        forward_fan_rad=math.radians(float(lidar["forward_fan_deg"])),
        new_obstacle_check_m=float(lidar["new_obstacle_check_radius_mm"]) / 1000.0,
        # 파일 전용 정제 도구의 이전 설정 호환. AG 실시간 정책과 반경을 이중 팽창하지 않는다.
        new_obstacle_margin_m=float(lidar.get("new_obstacle_margin_mm", 100)) / 1000.0,
        obstacle_radius_m=float(lidar.get("obstacle_mark_radius_mm", lidar["robot_radius_mm"]))
        / 1000.0,
        new_obstacle_confirmations=int(lidar.get("new_obstacle_confirmations", 2)),
    )


@dataclass(frozen=True, slots=True)
class ObservationFrame:
    """Caller-verified observations in ``pose_frame_id`` metric coordinates.

    ``pose`` is base_link / robot-centre XY and body yaw. ``scan_pose`` is the
    actual laser-centre XY and the SAME body yaw in that frame. Scan polar angles
    are body-oriented, with the mounting yaw already applied exactly once by the
    caller. This mission applies only the remaining sensor translation, through
    the scan origin; it does not apply mounting yaw again.
    """

    pose: Pose | None
    pose_status: PoseStatus
    pose_received_ms: int | None
    scan: Scan | None
    scan_received_ms: int | None
    telemetry_received_ms: int | None
    stationary: bool | None
    level: bool | None
    safety_latched: bool = False
    obstacle_active: bool = False
    pose_frame_id: str = "map"
    sensor_extrinsics_verified: bool = False
    scan_pose: Pose | None = None
    scan_pose_received_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Directive:
    action: Action
    reason: str
    stop_id: str | None
    map_revision: str
    waypoint: Point | None = None
    path: tuple[Point, ...] = ()


def build_blocked(
    grid: OccupancyGrid, params: MissionParams, no_go: np.ndarray | None = None
) -> np.ndarray:
    """Dilate occupied, unknown, no-go and the map boundary together.

    Unlike the legacy inflate helper, unknown also receives body clearance. An
    edge is blocked before dilation so outside-map space cannot be traversed by
    a robot whose centre happens to lie in the last observed cell.
    """
    if not math.isfinite(grid.meta.resolution) or grid.meta.resolution <= 0:
        raise ValueError("Grid resolution must be finite and positive")
    if grid.cells.ndim != 2 or not grid.cells.size:
        raise ValueError("Grid must be a nonempty 2D array")
    base = (grid.cells > params.free_thresh) | ~np.isfinite(grid.cells)
    base |= grid.cells >= params.occ_thresh
    if no_go is not None:
        if no_go.shape != grid.cells.shape or no_go.dtype != np.dtype(bool):
            raise ValueError("no_go must be a bool mask with the grid shape")
        base |= no_go
    base[0, :] = True
    base[-1, :] = True
    base[:, 0] = True
    base[:, -1] = True
    # Occupied/unknown cells are areas, not infinitesimal centre points. Include
    # the half-diagonal before discretising the dilation radius; otherwise a
    # diagonal 5-cell stencil can allow less than 25 cm to a cell's actual area.
    radius = int(math.ceil(params.clearance_m / grid.meta.resolution + math.sqrt(2) / 2))
    return dilate(base, radius)


def safe_plan(
    grid: OccupancyGrid, blocked: np.ndarray, start: Point, goal: Point
) -> tuple[Point, ...] | None:
    """Return raw A* cell centres: no Douglas-Peucker corner shortcut.

    The existing A* allows a blocked start; this mission explicitly rejects it.
    """
    if blocked.shape != grid.cells.shape or blocked.dtype != np.dtype(bool):
        raise ValueError("blocked must be a bool mask with the grid shape")
    if any(not math.isfinite(value) for value in (*start, *goal)):
        return None
    start_cell, goal_cell = grid.to_cell(*start), grid.to_cell(*goal)
    if not grid.inside(*start_cell) or not grid.inside(*goal_cell):
        return None
    if blocked[start_cell] or blocked[goal_cell]:
        return None
    cells = astar(start_cell, goal_cell, blocked)
    return None if cells is None else tuple(grid.to_world(*cell) for cell in cells)


@dataclass
class RefinementMission:
    grid: OccupancyGrid
    map_revision: str
    stops: tuple[MissionStop, ...]
    params: MissionParams = field(default_factory=MissionParams)
    no_go: np.ndarray | None = None
    map_frame_id: str = "map"
    stop_index: int = field(init=False, default=0)
    dynamic_obstacles: list[Point] = field(init=False, default_factory=list)
    _static: np.ndarray = field(init=False)
    _dynamic: np.ndarray = field(init=False)
    _path: tuple[Point, ...] = field(init=False, default=())
    _waypoint_index: int = field(init=False, default=0)
    _map_signature: tuple[float, float, float, int, int] = field(init=False)
    _map_changed: bool = field(init=False, default=False)
    _settle_started_ms: int | None = field(init=False, default=None)
    _arrived_ms: int | None = field(init=False, default=None)
    _observing: bool = field(init=False, default=False)
    _halt_reason: str = field(init=False, default="")
    _pending_hit: Point | None = field(init=False, default=None)
    _pending_count: int = field(init=False, default=0)
    _last_hit_packet: tuple[str, str, int] | None = field(init=False, default=None)
    _hit_sequences: dict[tuple[str, str], int] = field(init=False, default_factory=dict)
    _last_now_ms: int | None = field(init=False, default=None)
    _consumed_observation_ids: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        if not self.map_revision or not self.map_frame_id:
            raise ValueError("Map revision and frame ID are required")
        if any(not stop.id or not all(math.isfinite(v) for v in stop.point) for stop in self.stops):
            raise ValueError("Stops need an ID and finite metric coordinates")
        # Own an immutable-by-contract snapshot. Callers must publish updates
        # through update_map, never mutate mission.grid or mission.no_go directly.
        self.grid = OccupancyGrid(MapMeta.of(self.grid.meta.as_dict()), self.grid.cells.copy())
        self.no_go = None if self.no_go is None else self.no_go.copy()
        self._refresh_masks()

    @property
    def blocked(self) -> np.ndarray:
        # Canonical obstacle_mark_radius includes robot radius + tracking margin.
        # Dynamic marks already have TOTAL clearance; do not dilate it twice.
        return cast(np.ndarray, self._static | self._dynamic)

    @property
    def current_stop(self) -> MissionStop | None:
        return self.stops[self.stop_index] if self.stop_index < len(self.stops) else None

    def _signature(self) -> tuple[float, float, float, int, int]:
        return (
            self.grid.meta.resolution,
            self.grid.meta.origin_x,
            self.grid.meta.origin_y,
            int(self.grid.cells.shape[0]),
            int(self.grid.cells.shape[1]),
        )

    def _refresh_masks(self) -> None:
        self._static = build_blocked(self.grid, self.params, self.no_go)
        self._dynamic = np.zeros(self.grid.cells.shape, dtype=bool)
        for point in self.dynamic_obstacles:
            mark_obstacle(
                self._dynamic,
                self.grid,
                point,
                self._dynamic_radius(self.grid),
            )
        self._map_signature = self._signature()

    def _dynamic_radius(self, grid: OccupancyGrid) -> float:
        return (
            max(self.params.clearance_m, self.params.obstacle_radius_m)
            + grid.meta.resolution * math.sqrt(2) / 2
        )

    def _invalidate_route(self) -> None:
        self._path = ()
        self._waypoint_index = 0
        self._arrived_ms = None
        self._settle_started_ms = None
        self._observing = False

    def update_map(
        self, grid: OccupancyGrid, revision: str, no_go: np.ndarray | None = None
    ) -> None:
        """Caller supplies an accepted snapshot; retain dynamic hits in world XY."""
        if not revision:
            raise ValueError("Map revision is required")
        if no_go is None and self.no_go is not None:
            if (
                grid.cells.shape != self.no_go.shape
                or (
                    grid.meta.resolution,
                    grid.meta.origin_x,
                    grid.meta.origin_y,
                )
                != self._map_signature[:3]
            ):
                raise ValueError(
                    "Expanded/rebased grid requires an explicit reprojected no-go mask"
                )
            no_go = self.no_go
        # Validate and project before changing any active snapshot. A rejected
        # mask must not leave the previous mission with half-updated coordinates.
        owned_grid = OccupancyGrid(MapMeta.of(grid.meta.as_dict()), grid.cells.copy())
        owned_no_go = None if no_go is None else no_go.copy()
        static = build_blocked(owned_grid, self.params, owned_no_go)
        dynamic = np.zeros(owned_grid.cells.shape, dtype=bool)
        for point in self.dynamic_obstacles:
            mark_obstacle(
                dynamic,
                owned_grid,
                point,
                self._dynamic_radius(owned_grid),
            )
        self.grid, self.map_revision, self.no_go = owned_grid, revision, owned_no_go
        self._static, self._dynamic = static, dynamic
        self._map_signature = self._signature()
        self._invalidate_route()
        self._pending_hit = None
        self._pending_count = 0
        self._map_changed = True

    def accept_observation(
        self,
        stop_id: str,
        grid: OccupancyGrid,
        revision: str,
        no_go: np.ndarray | None = None,
        *,
        observation_id: str | None = None,
    ) -> None:
        """Advance only after the caller accepts an observation and its map update.

        Map-content revision may stay unchanged in saturated cells. In that case
        a fresh caller-issued committed observation ID is mandatory. An ID is
        evidence identity, not an invented map revision; it can be consumed once.
        """
        stop = self.current_stop
        if not self._observing or stop is None or stop.id != stop_id:
            raise ValueError("No matching settled observation is awaiting acceptance")
        if observation_id is not None:
            if not observation_id.strip():
                raise ValueError("Accepted observation ID must be nonempty")
            if observation_id in self._consumed_observation_ids:
                raise ValueError("Accepted observation ID has already been consumed")
        if revision == self.map_revision and observation_id is None:
            raise ValueError("Unchanged map revision requires a fresh accepted observation ID")
        self.update_map(grid, revision, no_go)
        if observation_id is not None:
            self._consumed_observation_ids.add(observation_id)
        self.stop_index += 1

    def _directive(self, action: Action, reason: str, waypoint: Point | None = None) -> Directive:
        if action in ("stop", "halted"):
            self._observing = False
        stop = self.current_stop
        return Directive(
            action,
            reason,
            None if stop is None else stop.id,
            self.map_revision,
            waypoint,
            self._path,
        )

    @staticmethod
    def _fresh(now_ms: int, received_ms: int | None, limit_ms: int) -> bool:
        return received_ms is not None and 0 <= now_ms - received_ms <= limit_ms

    def _input_problem(self, now_ms: int, frame: ObservationFrame) -> str | None:
        if frame.safety_latched:
            return "onboard_safety_latched"
        if frame.pose_status != "localized" or frame.pose is None:
            return "pose_lost" if frame.pose_status == "lost" else "pose_candidate_unverified"
        if not all(math.isfinite(value) for value in frame.pose):
            return "pose_invalid"
        if frame.pose_frame_id != self.map_frame_id:
            return "pose_frame_mismatch"
        if not frame.sensor_extrinsics_verified:
            return "sensor_extrinsics_unverified"
        if frame.scan_pose is None:
            return "scan_pose_missing"
        if not all(math.isfinite(value) for value in frame.scan_pose):
            return "scan_pose_invalid"
        if not self._fresh(now_ms, frame.scan_pose_received_ms, 250):
            return "scan_pose_stale"
        if not self._fresh(now_ms, frame.pose_received_ms, self.params.pose_timeout_ms):
            return "pose_stale"
        if frame.scan is None or not frame.scan.points:
            return "scan_missing"
        if not self._fresh(now_ms, frame.scan_received_ms, self.params.scan_timeout_ms):
            return "scan_stale"
        if any(
            not math.isfinite(a) or not math.isfinite(d) or d <= 0 for a, d in frame.scan.points
        ):
            return "scan_invalid"
        if not self._fresh(now_ms, frame.telemetry_received_ms, self.params.telemetry_timeout_ms):
            return "telemetry_stale"
        if frame.obstacle_active:
            return "onboard_obstacle_stop"
        return None

    def _observe_hit(self, hit: Point, scan: Scan) -> bool:
        packet = scan.device_id, scan.boot_id, scan.seq
        key = scan.device_id, scan.boot_id
        if packet == self._last_hit_packet or scan.seq <= self._hit_sequences.get(key, -1):
            return False
        self._last_hit_packet = packet
        self._hit_sequences[key] = scan.seq
        if (
            self._pending_hit is not None
            and math.dist(hit, self._pending_hit) <= self.params.obstacle_same_hit_m
        ):
            self._pending_count += 1
        else:
            self._pending_hit, self._pending_count = hit, 1
        if self._pending_count < self.params.new_obstacle_confirmations:
            return False
        self.dynamic_obstacles.append(hit)
        self._pending_hit, self._pending_count = None, 0
        self._refresh_masks()
        self._invalidate_route()
        return True

    def step(self, now_ms: int, frame: ObservationFrame) -> Directive:
        """One nonblocking decision. Time and all real observations are caller-owned."""
        # A physical safety latch takes precedence over all clock/map refresh
        # bookkeeping. Once reported, a later clear packet cannot resume this
        # mission implicitly; the caller must establish a new approved mission.
        if frame.safety_latched is True:
            self._halt_reason = "onboard_safety_latched"
        if self._halt_reason:
            return self._directive("halted", self._halt_reason)
        if self._last_now_ms is not None and now_ms < self._last_now_ms:
            self._invalidate_route()
            return self._directive("stop", "clock_reversed")
        self._last_now_ms = now_ms
        if self._signature() != self._map_signature:
            # Expansion without a corresponding no-go snapshot cannot silently
            # erase excluded zones. Require the caller to supply it explicitly.
            self._invalidate_route()
            return self._directive("stop", "map_geometry_changed_update_required")
        if self._map_changed:
            self._map_changed = False
            return self._directive("stop", "map_revision_changed_route_invalidated")
        problem = self._input_problem(now_ms, frame)
        if problem is not None:
            self._settle_started_ms = None
            self._observing = False
            self._pending_hit, self._pending_count = None, 0
            if problem == "onboard_safety_latched":
                self._halt_reason = problem
                return self._directive("halted", problem)
            return self._directive("stop", problem)
        assert frame.pose is not None and frame.scan is not None and frame.scan_pose is not None
        pose = frame.pose
        blocked = self.blocked
        start_cell = self.grid.to_cell(pose[0], pose[1])
        if not self.grid.inside(*start_cell) or blocked[start_cell]:
            self._invalidate_route()
            return self._directive("stop", "blocked_or_outside_start")
        stop = self.current_stop
        if stop is None:
            return self._directive("finished", "ordered_stops_completed")
        goal_cell = self.grid.to_cell(*stop.point)
        if not self.grid.inside(*goal_cell) or blocked[goal_cell]:
            self._invalidate_route()
            return self._directive("stop", "blocked_or_outside_goal")

        forward = tuple(
            (angle, distance)
            for angle, distance in frame.scan.points
            if abs(wrap_pi(angle)) <= self.params.forward_fan_rad
        )
        if not forward:
            return self._directive("stop", "forward_scan_missing")
        near = min(distance for _, distance in forward) <= self.params.immediate_stop_m
        hit = detect_new_obstacle(
            frame.scan_pose,
            forward,
            self.grid,
            self._static,
            check_radius_m=self.params.new_obstacle_check_m,
            margin_m=self.params.new_obstacle_margin_m,
            occ_thresh=self.params.occ_thresh,
        )
        if hit is not None:
            cell = self.grid.to_cell(*hit)
            already_marked = self.grid.inside(*cell) and bool(self._dynamic[cell])
            if not already_marked:
                confirmed = self._observe_hit(hit, frame.scan)
                self._settle_started_ms = None
                return self._directive(
                    "stop",
                    "dynamic_obstacle_confirmed_replan"
                    if confirmed
                    else "forward_obstacle_immediate_stop"
                    if near
                    else "obstacle_confirmation_pending",
                )
        else:
            self._pending_hit, self._pending_count = None, 0
        if near:
            self._settle_started_ms = None
            return self._directive("stop", "forward_obstacle_immediate_stop")

        if math.dist(pose[:2], stop.point) <= self.params.arrival_radius_m:
            if self._arrived_ms is None:
                self._arrived_ms = now_ms
                return self._directive("stop", "arrived_stop_before_settle")
            if frame.stationary is not True or frame.level is not True:
                self._settle_started_ms = None
                self._observing = False
                return self._directive("stop", "stationary_level_confirmation_required")
            if (
                frame.telemetry_received_ms is None
                or frame.telemetry_received_ms <= self._arrived_ms
            ):
                return self._directive("stop", "post_stop_telemetry_required")
            if self._settle_started_ms is None:
                self._settle_started_ms = now_ms
            if now_ms - self._settle_started_ms < self.params.settle_ms:
                return self._directive("stop", "settling")
            if frame.telemetry_received_ms < self._settle_started_ms + self.params.settle_ms:
                return self._directive("stop", "settle_confirmation_telemetry_required")
            self._observing = True
            return self._directive("observe", "settled_awaiting_accepted_map_update")
        if self._arrived_ms is not None or self._observing:
            self._invalidate_route()
        if not self._path:
            path = safe_plan(self.grid, blocked, pose[:2], stop.point)
            if path is None:
                return self._directive("stop", "route_blocked_no_unknown_sweep")
            self._path = path
            self._waypoint_index = min(1, len(path) - 1)
            return self._directive("stop", "route_planned")

        # Advance by actual cell membership, never a broad radius that can skip
        # corner waypoints and connect two individually safe path points blindly.
        cells = [self.grid.to_cell(*point) for point in self._path]
        while self._waypoint_index < len(cells) - 1 and start_cell == cells[self._waypoint_index]:
            self._waypoint_index += 1
        previous_cell = cells[max(0, self._waypoint_index - 1)]
        if start_cell not in (previous_cell, cells[self._waypoint_index]):
            self._invalidate_route()
            return self._directive("stop", "pose_left_planned_cells_replan")
        waypoint = self._path[self._waypoint_index]
        heading = math.atan2(waypoint[1] - pose[1], waypoint[0] - pose[0])
        if abs(wrap_pi(heading - pose[2])) > self.params.heading_limit_rad:
            return self._directive("stop", "pending_inplace_turn_driver", waypoint)
        return self._directive("move", "validated_cell_waypoint", waypoint)
