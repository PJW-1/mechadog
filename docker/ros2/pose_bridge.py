"""tf ``map -> base_link`` -> Windows 순찰기 UDP 반환 브리지 (WBS 5.4.4)."""

from __future__ import annotations

import math
import os
import secrets
import socket
import time

from host.common.map_pose_link import MapPoseEncoder


def yaw_of_quaternion(x: float, y: float, z: float, w: float) -> float:
    """정규화된 ROS 쿼터니언의 평면 yaw를 돌려준다."""
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def transform_age_s(now_ns: int, sec: int, nanosec: int) -> float:
    """ROS 시각 기준 tf 나이. 미래 시각은 0으로 접는다."""
    stamp_ns = sec * 1_000_000_000 + nanosec
    return max(0.0, (now_ns - stamp_ns) / 1_000_000_000)


def main() -> None:
    import rclpy
    from rclpy.duration import Duration
    from rclpy.node import Node
    from rclpy.time import Time
    from tf2_ros import Buffer, TransformException, TransformListener

    class Bridge(Node):
        def __init__(self) -> None:
            super().__init__("mechdog_map_pose_bridge")
            self.host = os.getenv("MAP_POSE_HOST", "host.docker.internal")
            self.port = int(os.getenv("MAP_POSE_PORT", "5205"))
            device_id = os.getenv("MAP_POSE_DEVICE_ID", "").strip()
            if not device_id:
                raise RuntimeError("MAP_POSE_DEVICE_ID 가 필요하다 — 다른 기체 자세 혼입 방지")
            self.stall_s = float(os.getenv("MAP_POSE_STALL_S", "0.5"))
            self.encoder = MapPoseEncoder(device_id, secrets.token_hex(8))
            self.peer = (socket.gethostbyname(self.host), self.port)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.buffer = Buffer()
            self.listener = TransformListener(self.buffer, self)
            self.failed = False
            self.create_timer(0.1, self.publish_pose)
            self.get_logger().info(f"tf map->base_link -> UDP {self.peer}")

        def publish_pose(self) -> None:
            now = self.get_clock().now()
            valid = False
            x_m = y_m = yaw_rad = 0.0
            reason = ""
            try:
                transform = self.buffer.lookup_transform(
                    "map", "base_link", Time(), timeout=Duration(seconds=0.02)
                )
                stamp = transform.header.stamp
                age_s = transform_age_s(now.nanoseconds, stamp.sec, stamp.nanosec)
                if age_s <= self.stall_s:
                    t = transform.transform.translation
                    q = transform.transform.rotation
                    x_m, y_m = float(t.x), float(t.y)
                    yaw_rad = yaw_of_quaternion(q.x, q.y, q.z, q.w)
                    valid = all(math.isfinite(v) for v in (x_m, y_m, yaw_rad))
                else:
                    reason = f"tf 오래됨: {age_s:.3f}s"
            except TransformException as exc:
                reason = str(exc)
            line = self.encoder.encode(
                ts_ms=time.monotonic_ns() // 1_000_000,
                x_m=x_m,
                y_m=y_m,
                yaw_rad=yaw_rad,
                valid=valid,
            )
            try:
                self.sock.sendto(line.encode("utf-8"), self.peer)
            except OSError as exc:
                if not self.failed:
                    self.get_logger().warning(f"MAP_POSE 송신 실패: {exc}")
                self.failed = True
                return
            if self.failed:
                self.get_logger().info("MAP_POSE 송신 복구")
            self.failed = False
            if reason:
                self.get_logger().debug(reason)

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
