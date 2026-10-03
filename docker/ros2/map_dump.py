"""`/map` 한 장을 받아 PGM + YAML 로 쓴다 — 컨테이너 안에서 돈다 (`nav2_map_server` 없이).

    python3 /opt/mechdog/docker/ros2/map_dump.py /tmp/out/replay_map

slam_toolbox 가 발행하는 `nav_msgs/OccupancyGrid` 를 map_server 형식(점유 100 → 0,
빈 0 → 254, 미지 −1 → 205)으로 저장한다 — `host/slam/occupancy.py` 의 `load_ros2` 가 읽는다.
한 장 받으면 끝난다. 로봇·소켓을 만지지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import rclpy
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy


class MapDump(Node):
    def __init__(self, stem: Path) -> None:
        super().__init__("map_dump")
        self.stem = stem
        qos = QoSProfile(depth=1)
        qos.reliability = ReliabilityPolicy.RELIABLE
        qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.sub = self.create_subscription(OccupancyGrid, "/map", self.on_map, qos)
        self.done = False

    def on_map(self, msg: OccupancyGrid) -> None:
        if self.done:
            return
        self.done = True
        width, height = msg.info.width, msg.info.height
        rows = []
        for r in range(height):
            row = bytearray(width)
            base = r * width
            for c in range(width):
                v = msg.data[base + c]
                row[c] = 205 if v < 0 else (0 if v >= 65 else (254 if v <= 25 else 205))
            rows.append(bytes(row))
        # PGM 은 위가 첫 줄 — OccupancyGrid 는 아래(y 작은 쪽)가 첫 줄이라 뒤집는다.
        pgm = self.stem.with_suffix(".pgm")
        with pgm.open("wb") as handle:
            handle.write(f"P5\n{width} {height}\n255\n".encode())
            for row in reversed(rows):
                handle.write(row)
        self.stem.with_suffix(".yaml").write_text(
            "\n".join(
                [
                    f"image: {pgm.name}",
                    f"resolution: {msg.info.resolution}",
                    f"origin: [{msg.info.origin.position.x},{msg.info.origin.position.y},0]",
                    "negate: 0",
                    "occupied_thresh: 0.65",
                    "free_thresh: 0.196",
                    "",
                ]
            )
        )
        self.get_logger().info(f"saved {pgm} {width}x{height}")


def main() -> int:
    stem = Path(sys.argv[1])
    stem.parent.mkdir(parents=True, exist_ok=True)
    rclpy.init()
    node = MapDump(stem)
    while rclpy.ok() and not node.done:
        rclpy.spin_once(node, timeout_sec=1.0)
    node.destroy_node()
    rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
