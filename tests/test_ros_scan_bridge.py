"""One rotation assembled from the real UDP decoder's sector format."""

import math
import runpy
from pathlib import Path

from host.common.lidar_link import ScanDecoder, encode_scan, scan_of

Revolution = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "docker/ros2/scan_bridge.py")
)["Revolution"]


def test_sectors_become_fixed_bins_with_missing_beams() -> None:
    decoder = ScanDecoder()
    rotation = Revolution(4, 0.12, 8.0)
    sectors = (
        [[0.0, 1000], [90.0, 2000]],
        [[270.0, 3000]],
        [[0.0, 4000]],
    )
    completed = []
    for seq, points in enumerate(sectors, 1):
        raw = encode_scan(
            seq=seq,
            ts_ms=seq * 100,
            device_id="lidar-test",
            boot_id="a" * 16,
            points_wire=points,
        )
        scan = scan_of(decoder.decode(raw))
        assert scan is not None
        completed.extend(rotation.add(scan.points))
    assert completed == [[1.0, 2.0, math.inf, 3.0]]
    assert rotation.flush() == [4.0, math.inf, math.inf, math.inf]
