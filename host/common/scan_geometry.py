"""LiDAR 스캔 점의 기하 계산 — 행동 계층과 LiDAR 수신(`host.telemetry.lidar_feed`)이 함께 쓴다."""

from __future__ import annotations

import math


def min_forward_distance(
    scan_points: tuple[tuple[float, float], ...],
    half_angle_rad: float,
) -> float | None:
    """전방 부채꼴 안의 최단 거리. 호스트측 위험 판단에 쓴다."""
    forward = [
        dist
        for angle, dist in scan_points
        if abs((angle + math.pi) % (2 * math.pi) - math.pi) <= half_angle_rad
    ]
    return min(forward) if forward else None
