"""Capture one stationary LiDAR occupancy view and a contemporaneous camera photo.

This is a receive-only observation at pose (0, 0, 0), not multi-pose SLAM.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.lidar_link import ScanDecoder, scan_of
from host.common.protocol import system_clock_ms
from host.slam import settings, viz
from host.slam.occupancy import OccupancyGrid
from host.slam.photo_map import PhotoRecorder
from host.slam.scan_match import integrate_scan, merge_batch, preprocess


def build_map(scans, config):
    """Median-fuse accepted stationary scans before one occupancy update."""
    lidar = config["lidar"]
    merged = merge_batch([scan.points for scan in scans])
    points = preprocess(merged, *settings.range_from_config(config))
    if len(scans) < 50 or len(points) < 270:
        raise ValueError(f"스캔 부족: {len(scans)}개, 유효 각도 {len(points)}/360개")
    grid = OccupancyGrid.blank(
        resolution=lidar["resolution_mm"] / 1000,
        span_cells=int(lidar["initial_span_cells"]),
    )
    integrate_scan(
        grid,
        (0.0, 0.0, 0.0),
        points,
        hit=float(lidar["hit_logodds"]),
        miss=float(lidar["miss_logodds"]),
        pad_cells=int(lidar["expand_pad_cells"]),
    )
    return grid, len(points)


def run(args) -> dict:
    output = Path(args.out)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"출력 폴더가 비어 있지 않음: {output}")
    if not 1 <= args.seconds <= 30:
        raise ValueError("측정 시간은 1~30초")
    config = settings.load(args.device)
    lidar = config["lidar"]
    decoder = ScanDecoder(float(lidar["mount_yaw_deg"]), int(lidar["angle_direction"]))
    recorder = PhotoRecorder(config, args.camera_url)
    scans = []
    counters = {"packets": 0, "accepted": 0, "wrong_device": 0, "rejected": 0}
    selected = None
    scan_ms = None
    # Bind before creating output files: a running map process must fail visibly.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("", int(lidar["scan_port"])))
        sock.settimeout(0.2)
        output.mkdir(parents=True, exist_ok=True)
        recorder.start()
        try:
            deadline = time.monotonic() + args.seconds
            with (output / "scans.jsonl").open("w", encoding="utf-8") as raw_file:
                while time.monotonic() < deadline:
                    try:
                        raw, _peer = sock.recvfrom(65536)
                    except TimeoutError:
                        continue
                    except ConnectionResetError:
                        continue  # Windows ICMP from a previous UDP send is not a scan.
                    counters["packets"] += 1
                    result = decoder.decode(raw)
                    scan = scan_of(result)
                    if scan is None:
                        counters["rejected"] += 1
                        continue
                    if scan.device_id != args.lidar_device:
                        counters["wrong_device"] += 1
                        continue
                    now = system_clock_ms()
                    counters["accepted"] += 1
                    scans.append(scan)
                    raw_file.write(raw.decode("utf-8", errors="replace").rstrip() + "\n")
                    candidate = recorder.sample(now)
                    if candidate is not None:
                        selected, scan_ms = candidate, now
            grid, bins = build_map(scans, config)
            if selected is None or scan_ms is None:
                raise ValueError("같은 측정 창의 신선한 카메라 JPEG가 없음")
            grid.save(output, stem="static_map")
            viz.save_png(grid, output / "static_map.png")
            recorder.capture(output, 1, (0.0, 0.0, 0.0), scan_ms, selected)
            recorder.save(output, grid.extent, has_png=True, image_name="static_map.png")
            summary = {
                "kind": "stationary_single_pose_snapshot_not_slam",
                "device_id": args.device,
                "lidar_device_id": args.lidar_device,
                "seconds": args.seconds,
                "scan_port": lidar["scan_port"],
                "mount_yaw_deg": lidar["mount_yaw_deg"],
                "angle_direction": lidar["angle_direction"],
                "packets": counters,
                "valid_angle_bins": bins,
                "camera_scan_skew_ms": abs(selected.received_ms - scan_ms),
                "camera_pose": [0.0, 0.0, 0.0],
                "camera_object_coordinates_calibrated": False,
            }
            (output / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return summary
        finally:
            recorder.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--lidar-device", required=True)
    parser.add_argument("--camera-url", required=True)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"측정 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
