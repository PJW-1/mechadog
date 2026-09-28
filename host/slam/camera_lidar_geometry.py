"""Measured camera/LiDAR geometry in a robot-centred coordinate frame.

Robot X points forward, Y left, Z up. Image X points right, Y down. Input
frames must already be upright. A floor intersection uses the *assumption*
that the selected image pixel lies on the floor; a LiDAR return lies only on
the measured scan plane. Neither operation invents a 3D surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CameraGeometry:
    width_px: int
    height_px: int
    fx_px: float
    fy_px: float
    cx_px: float
    cy_px: float
    height_m: float
    forward_m: float
    left_m: float
    pitch_down_deg: float
    yaw_left_deg: float

    def __post_init__(self) -> None:
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("영상 크기는 양수여야 함")
        values = (
            self.fx_px,
            self.fy_px,
            self.cx_px,
            self.cy_px,
            self.height_m,
            self.forward_m,
            self.left_m,
            self.pitch_down_deg,
            self.yaw_left_deg,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("카메라 보정값은 유한해야 함")
        if self.fx_px <= 0 or self.fy_px <= 0 or self.height_m <= 0:
            raise ValueError("초점거리와 렌즈 높이는 양수여야 함")
        if not (0 <= self.cx_px < self.width_px and 0 <= self.cy_px < self.height_px):
            raise ValueError("주점은 영상 안에 있어야 함")
        if abs(self.pitch_down_deg) >= 89:
            raise ValueError("카메라 피치는 수직이 아니어야 함")

    def _basis(self) -> tuple[tuple[float, float, float], ...]:
        yaw = math.radians(self.yaw_left_deg)
        pitch = math.radians(self.pitch_down_deg)
        forward_xy = (math.cos(yaw), math.sin(yaw))
        forward = (
            math.cos(pitch) * forward_xy[0],
            math.cos(pitch) * forward_xy[1],
            -math.sin(pitch),
        )
        right = (math.sin(yaw), -math.cos(yaw), 0.0)
        down = (
            -math.sin(pitch) * forward_xy[0],
            -math.sin(pitch) * forward_xy[1],
            -math.cos(pitch),
        )
        return forward, right, down

    def _ray(self, u_px: float, v_px: float) -> tuple[float, float, float]:
        if not all(math.isfinite(value) for value in (u_px, v_px)):
            raise ValueError("영상 좌표는 유한해야 함")
        if not (0 <= u_px < self.width_px and 0 <= v_px < self.height_px):
            raise ValueError("영상 밖 좌표")
        forward, right, down = self._basis()
        x = (u_px - self.cx_px) / self.fx_px
        y = (v_px - self.cy_px) / self.fy_px
        return tuple(forward[i] + x * right[i] + y * down[i] for i in range(3))

    def floor_xy(self, u_px: float, v_px: float) -> tuple[float, float] | None:
        """Find the floor point for a pixel *known to depict the floor*.

        Return None for a ray on/above the horizon. A random object pixel
        must not be called its floor position.
        """
        ray = self._ray(u_px, v_px)
        if ray[2] >= -1e-9:
            return None
        travel = -self.height_m / ray[2]
        return self.forward_m + travel * ray[0], self.left_m + travel * ray[1]

    def project_xyz(self, x_m: float, y_m: float, z_m: float) -> tuple[float, float] | None:
        """Project a measured 3D point, returning None when behind/out of frame."""
        if not all(math.isfinite(value) for value in (x_m, y_m, z_m)):
            raise ValueError("3D 좌표는 유한해야 함")
        vector = (x_m - self.forward_m, y_m - self.left_m, z_m - self.height_m)
        forward, right, down = self._basis()
        depth = sum(vector[i] * forward[i] for i in range(3))
        if depth <= 0:
            return None
        u = self.cx_px + self.fx_px * sum(vector[i] * right[i] for i in range(3)) / depth
        v = self.cy_px + self.fy_px * sum(vector[i] * down[i] for i in range(3)) / depth
        return (u, v) if 0 <= u < self.width_px and 0 <= v < self.height_px else None

    def lidar_hit_pixel(
        self, x_m: float, y_m: float, *, lidar_height_m: float
    ) -> tuple[float, float] | None:
        """Project one robot-frame LD19 return at its measured scan height."""
        if not math.isfinite(lidar_height_m) or lidar_height_m < 0:
            raise ValueError("라이다 스캔면 높이는 0 이상이어야 함")
        return self.project_xyz(x_m, y_m, lidar_height_m)

    def apparent_height(
        self, foot_u_px: float, foot_v_px: float, top_u_px: float, top_v_px: float
    ) -> tuple[float, float] | None:
        """Estimate vertical height from visible floor contact and top pixels.

        Returns (height_m, horizontal_mismatch_m). This is only credible when
        both pixels belong to the same upright object, its foot touches the
        floor, and horizontal mismatch is small. The caller must verify those
        conditions rather than silently accepting every bbox.
        """
        foot = self.floor_xy(foot_u_px, foot_v_px)
        if foot is None:
            return None
        ray = self._ray(top_u_px, top_v_px)
        horizontal_norm = ray[0] ** 2 + ray[1] ** 2
        if horizontal_norm <= 1e-12:
            return None
        offset = (foot[0] - self.forward_m, foot[1] - self.left_m)
        travel = (offset[0] * ray[0] + offset[1] * ray[1]) / horizontal_norm
        if travel <= 0:
            return None
        mismatch = math.hypot(offset[0] - travel * ray[0], offset[1] - travel * ray[1])
        height = self.height_m + travel * ray[2]
        return (height, mismatch) if height >= 0 else None
