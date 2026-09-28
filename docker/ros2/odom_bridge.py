"""ODOM UDP -> tf `odom -> base_link` (+ 명시된 `base_link -> laser`). WBS 5.4.3.

Only the link decoder is shared with the host (`host/common/odom_link.py`).

- ROS 시각은 **수신 시각**이다. 전문의 `ts` 는 호스트 시계라 컨테이너 시계와 같다는
  보장이 없다 — SCAN 브리지가 중계 노드 `ts` 를 쓰지 않는 것과 같은 이유다.
- `valid=false` 이거나 전문이 끊기면 **tf 를 내지 않는다.** 마지막 값을 되풀이하거나
  항등 변환으로 채우면 `slam_toolbox` 가 정지해 있다고 믿는다 (WBS 5.4.3 DoD).
- `base_link -> laser` 는 **`LASER_OFFSET_X_M`·`_Y_M`·`_Z_M` 이 모두 있을 때만** 낸다.
  마스트 실측 전에는 없고, 없으면 경고만 한다. 회전은 **항상 0** 이다 — 장착 방향은
  `scan_bridge` 의 디코더(`LIDAR_MOUNT_YAW_DEG`·`LIDAR_ANGLE_DIRECTION`)가 이미 적용해
  `/scan` 이 로봇 기준 각도로 나간다. 여기서 또 돌리면 두 번 돌아간다
  (`lidar_link._valid_point` «보정은 한 곳에서만»).
"""

from __future__ import annotations

import math
import os
import socket
import time
from collections.abc import Mapping

from host.common.odom_link import Odom, OdomDecoder, odom_of

LASER_OFFSET_ENV = ("LASER_OFFSET_X_M", "LASER_OFFSET_Y_M", "LASER_OFFSET_Z_M")


def transform_of(
    odom: Odom | None,
) -> tuple[float, float, float, float, float, float, float] | None:
    """전문 → `(x, y, z, qx, qy, qz, qw)`. 무효·폐기면 `None` (tf 를 내지 않는다)."""
    if odom is None or not odom.valid:
        return None
    half = odom.yaw_rad / 2.0
    return (odom.x_m, odom.y_m, 0.0, 0.0, 0.0, math.sin(half), math.cos(half))


def laser_offset_from_env(env: Mapping[str, str]) -> tuple[tuple[float, float, float] | None, str]:
    """`base_link -> laser` 이동량과 사유. 하나라도 없거나 수가 아니면 `None`."""
    missing = [name for name in LASER_OFFSET_ENV if not env.get(name, "").strip()]
    if missing:
        return None, f"{missing} 미설정 — base_link->laser 를 발행하지 않는다 (마스트 실측 후 설정)"
    try:
        values = tuple(float(env[name]) for name in LASER_OFFSET_ENV)
    except ValueError:
        return (
            None,
            f"{list(LASER_OFFSET_ENV)} 중 수가 아닌 값 — base_link->laser 를 발행하지 않는다",
        )
    if not all(math.isfinite(value) for value in values):
        return None, "LASER_OFFSET_* 가 유한한 수가 아님 — base_link->laser 를 발행하지 않는다"
    x, y, z = values
    return (x, y, z), ""


def main() -> None:
    import rclpy
    from geometry_msgs.msg import TransformStamped
    from rclpy.node import Node
    from tf2_ros import StaticTransformBroadcaster, TransformBroadcaster

    class Bridge(Node):
        def __init__(self) -> None:
            super().__init__("mechdog_odom_bridge")
            self.port = int(os.getenv("ODOM_PORT", "5204"))
            self.expected_device = os.getenv("ODOM_DEVICE_ID", "")
            self.stall_s = float(os.getenv("ODOM_STALL_S", "0.5"))
            self.decoder = OdomDecoder()
            self.broadcaster = TransformBroadcaster(self)
            self.static_broadcaster = StaticTransformBroadcaster(self)
            offset, reason = laser_offset_from_env(os.environ)
            if offset is None:
                self.get_logger().warning(reason)
            else:
                self.static_broadcaster.sendTransform(
                    self.stamped("base_link", "laser", (*offset, 0.0, 0.0, 0.0, 1.0))
                )
                self.get_logger().info(f"base_link->laser {offset}")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.setblocking(False)
            self.last_published: float | None = None
            self.stalled = True
            self.create_timer(0.01, self.poll)
            self.get_logger().info(f"ODOM UDP :{self.port} -> tf odom->base_link")

        def stamped(self, parent: str, child: str, values: tuple[float, ...]) -> TransformStamped:
            msg = TransformStamped()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = parent
            msg.child_frame_id = child
            t, r = msg.transform.translation, msg.transform.rotation
            t.x, t.y, t.z, r.x, r.y, r.z, r.w = values
            return msg

        def poll(self) -> None:
            for _ in range(64):
                try:
                    raw, _ = self.sock.recvfrom(65535)
                except BlockingIOError:
                    break
                result = self.decoder.decode(raw)
                if result.warns:
                    self.get_logger().warning(f"ODOM 폐기: {result.reason}")
                odom = odom_of(result)
                if (
                    odom is not None
                    and self.expected_device
                    and odom.device_id != self.expected_device
                ):
                    continue
                values = transform_of(odom)
                if values is None:
                    continue
                self.broadcaster.sendTransform(self.stamped("odom", "base_link", values))
                self.last_published = time.monotonic()
                if self.stalled:
                    self.stalled = False
                    self.get_logger().info("odom->base_link 발행 시작")
            idle = (
                self.last_published is None or time.monotonic() - self.last_published > self.stall_s
            )
            if idle and not self.stalled:
                # 엣지에서 한 번만 남긴다 (ENGINEERING_GUIDE 1.3).
                self.stalled = True
                self.get_logger().warning("유효한 ODOM 이 끊겼다 — odom->base_link 발행 중단")

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
