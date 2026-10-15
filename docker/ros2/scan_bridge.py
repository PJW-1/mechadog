"""LD19 SCAN UDP -> ROS2 LaserScan. Only the link decoder is shared with the host."""

from __future__ import annotations

import math
import os
import socket
import time
from collections.abc import Mapping

from host.common.lidar_link import ScanDecoder, scan_of


class Revolution:
    """Collect one rotation from independently valid UDP sectors."""

    def __init__(self, bins: int, min_m: float, max_m: float, angle_direction: int = 1) -> None:
        if type(angle_direction) is not int or angle_direction not in (-1, 1):
            raise ValueError("angle_direction must be -1 or 1")
        self.bins = bins
        self.min_m = min_m
        self.max_m = max_m
        self.angle_direction = angle_direction
        self.ranges = [math.inf] * bins
        self.last_angle: float | None = None
        self.has_points = False

    def add(self, points: tuple[tuple[float, float], ...]) -> list[list[float]]:
        completed = []
        for angle, distance in points:
            wrapped = self.last_angle is not None and (
                (self.angle_direction == 1 and angle < self.last_angle - math.pi)
                or (self.angle_direction == -1 and angle > self.last_angle + math.pi)
            )
            if wrapped and self.has_points:
                completed.append(self.flush())
            self.last_angle = angle
            if self.min_m <= distance <= self.max_m:
                index = min(int(angle * self.bins / (2 * math.pi)), self.bins - 1)
                self.ranges[index] = min(self.ranges[index], distance)
                self.has_points = True
        return completed

    def flush(self) -> list[float]:
        ranges = self.ranges
        self.ranges = [math.inf] * self.bins
        self.last_angle = None
        self.has_points = False
        return ranges


MOUNT_ENV = ("LIDAR_MOUNT_YAW_DEG", "LIDAR_ANGLE_DIRECTION")


def mount_from_env(env: Mapping[str, str]) -> tuple[float, int, str]:
    """장착 보정 `(yaw, direction)` 과 경고. 미설정·빈 값은 «보정 없음»(0, +1)으로 둔다.

    기본값은 배치가 없는 목업용이고 실물 정본이 아니다 — `config.yaml` 의
    `lidar.mount_yaw_deg`(270) · `lidar.angle_direction`(-1) 을 넘겨야 한다. 안 넘기면
    조용히 돌아간 지도가 나온다(2026-09-29 실측: 같은 물체가 40° 대 229°).
    `odom_bridge` 가 `LASER_OFFSET_*` 에 하는 것과 같이 경고한다.
    """
    missing = [name for name in MOUNT_ENV if not env.get(name, "").strip()]
    yaw = float(env.get("LIDAR_MOUNT_YAW_DEG", "").strip() or "0")
    direction = int(env.get("LIDAR_ANGLE_DIRECTION", "").strip() or "1")
    if not missing:
        return yaw, direction, ""
    return (
        yaw,
        direction,
        f"{missing} 미설정 — 장착 보정 없이 발행한다 "
        "(config.yaml lidar.mount_yaw_deg · angle_direction 을 넘긴다)",
    )


def main() -> None:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import LaserScan

    class Bridge(Node):
        def __init__(self) -> None:
            super().__init__("mechdog_scan_bridge")
            # 5201 은 중계 노드가 직접 보내는 포트다. Windows 순찰기가 그 포트의
            # 유일한 수신자로 남고 여기로는 복사본을 전달하므로 기본값이 다르다
            # (config.yaml lidar.scan_forward_port · WBS 5.4.4).
            self.port = int(os.getenv("LIDAR_SCAN_PORT", "5203"))
            self.expected_device = os.getenv("LIDAR_DEVICE_ID", "")
            yaw, direction, mount_warning = mount_from_env(os.environ)
            self.rotation = Revolution(
                int(os.getenv("LIDAR_ANGLE_BINS", "450")),
                float(os.getenv("LIDAR_RANGE_MIN_M", "0.12")),
                float(os.getenv("LIDAR_RANGE_MAX_M", "8.0")),
                direction,
            )
            self.decoder = ScanDecoder(yaw, direction)
            self.publisher = self.create_publisher(LaserScan, "/scan", 10)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.setblocking(False)
            self.last_packet = time.monotonic()
            self.last_publish: float | None = None
            self.scan_started_stamp = None
            self.create_timer(0.01, self.poll)
            if mount_warning:
                self.get_logger().warning(mount_warning)
            self.get_logger().info(f"SCAN UDP :{self.port} -> /scan")

        def publish(self, ranges: list[float]) -> None:
            now = time.monotonic()
            msg = LaserScan()
            msg.header.stamp = self.scan_started_stamp
            msg.header.frame_id = "laser"
            msg.angle_min = 0.0
            msg.angle_increment = 2 * math.pi / self.rotation.bins
            msg.angle_max = 2 * math.pi - msg.angle_increment
            msg.time_increment = 0.0
            msg.scan_time = now - self.last_publish if self.last_publish is not None else 0.1
            msg.range_min = self.rotation.min_m
            msg.range_max = self.rotation.max_m
            msg.ranges = ranges
            self.publisher.publish(msg)
            self.last_publish = now

        def poll(self) -> None:
            for _ in range(64):
                try:
                    raw, _ = self.sock.recvfrom(65535)
                except BlockingIOError:
                    break
                result = self.decoder.decode(raw)
                scan = scan_of(result)
                if scan is None or (
                    self.expected_device and scan.device_id != self.expected_device
                ):
                    continue
                if scan.points:
                    self.last_packet = time.monotonic()
                    if not self.rotation.has_points:
                        self.scan_started_stamp = self.get_clock().now().to_msg()
                for ranges in self.rotation.add(scan.points):
                    self.publish(ranges)
                    self.scan_started_stamp = self.get_clock().now().to_msg()
            if self.rotation.has_points and time.monotonic() - self.last_packet > 0.3:
                self.publish(self.rotation.flush())
                self.scan_started_stamp = None

        def destroy_node(self) -> bool:
            self.sock.close()
            return super().destroy_node()

    rclpy.init()
    bridge = Bridge()
    try:
        rclpy.spin(bridge)
    finally:
        bridge.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
