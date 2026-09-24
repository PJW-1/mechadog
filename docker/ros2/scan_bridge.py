"""LD19 SCAN UDP -> ROS2 LaserScan. Only the link decoder is shared with the host."""

from __future__ import annotations

import math
import os
import socket
import time

from host.common.lidar_link import ScanDecoder, scan_of


class Revolution:
    """Collect one rotation from independently valid UDP sectors."""

    def __init__(self, bins: int, min_m: float, max_m: float) -> None:
        self.bins = bins
        self.min_m = min_m
        self.max_m = max_m
        self.ranges = [math.inf] * bins
        self.last_angle: float | None = None
        self.has_points = False

    def add(self, points: tuple[tuple[float, float], ...]) -> list[list[float]]:
        completed = []
        for angle, distance in points:
            if (
                self.last_angle is not None
                and angle < self.last_angle - math.pi
                and self.has_points
            ):
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


def main() -> None:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import LaserScan

    class Bridge(Node):
        def __init__(self) -> None:
            super().__init__("mechdog_scan_bridge")
            self.port = int(os.getenv("LIDAR_SCAN_PORT", "5201"))
            self.expected_device = os.getenv("LIDAR_DEVICE_ID", "")
            self.rotation = Revolution(
                int(os.getenv("LIDAR_ANGLE_BINS", "450")),
                float(os.getenv("LIDAR_RANGE_MIN_M", "0.12")),
                float(os.getenv("LIDAR_RANGE_MAX_M", "8.0")),
            )
            self.decoder = ScanDecoder()
            self.publisher = self.create_publisher(LaserScan, "/scan", 10)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.setblocking(False)
            self.last_packet = time.monotonic()
            self.last_publish: float | None = None
            self.scan_started_stamp = None
            self.create_timer(0.01, self.poll)
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
