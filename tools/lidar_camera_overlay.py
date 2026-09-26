"""Project a saved stationary 2D LiDAR scan onto a calibrated camera frame.

Read-only sensor input. This is an alignment diagnostic, not a depth image or
3D reconstruction. Calibration JSON must contain every field explicitly.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.lidar_link import ScanDecoder, scan_of
from host.slam.camera_lidar_geometry import CameraGeometry
from host.slam.scan_match import merge_batch


def project_saved_scans(lines, calibration: dict) -> dict:
    """Return projected scan-plane points and explicit rejection counts."""
    geometry = CameraGeometry(**calibration["camera"])
    lidar_height = float(calibration["lidar_height_m"])
    decoder = ScanDecoder(float(calibration["mount_yaw_deg"]), int(calibration["angle_direction"]))
    scans = []
    rejected = 0
    for raw in lines:
        scan = scan_of(decoder.decode(raw))
        if scan is None:
            rejected += 1
        else:
            scans.append(scan.points)
    if not scans:
        raise ValueError("유효한 저장 스캔이 없음")
    projected = []
    merged = merge_batch(scans)
    for angle, distance in merged:
        x = distance * math.cos(angle)
        y = distance * math.sin(angle)
        pixel = geometry.lidar_hit_pixel(x, y, lidar_height_m=lidar_height)
        if pixel is not None:
            projected.append(
                {
                    "u_px": round(pixel[0], 2),
                    "v_px": round(pixel[1], 2),
                    "forward_m": round(x, 3),
                    "left_m": round(y, 3),
                    "range_m": round(distance, 3),
                }
            )
    return {
        "schema": "lidar_camera_overlay_v1",
        "scan_packets": len(scans),
        "rejected_packets": rejected,
        "merged_angle_bins": len(merged),
        "visible_scan_points": len(projected),
        "points": projected,
        "interpretation": "Only returns at the assumed horizontal LiDAR scan height; alignment is unverified.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photo", type=Path, required=True)
    parser.add_argument("--scans", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    args = parser.parse_args(argv)
    try:
        if args.out.exists() and any(args.out.iterdir()):
            raise ValueError("출력 폴더가 비어 있지 않음")
        calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
        image = cv2.imdecode(np.fromfile(args.photo, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("사진을 해독할 수 없음")
        camera = calibration["camera"]
        if image.shape[:2] != (camera["height_px"], camera["width_px"]):
            raise ValueError("보정 영상 크기와 사진 크기가 다름")
        with args.scans.open(encoding="utf-8") as stream:
            report = project_saved_scans(stream, calibration)
        for point in report["points"]:
            u, v = point["u_px"], point["v_px"]
            cv2.circle(image, (round(u), round(v)), 2, (50, 75, 255), -1)
        args.out.mkdir(parents=True, exist_ok=True)
        encoded, png = cv2.imencode(".png", image)
        if not encoded:
            raise ValueError("투영 PNG를 인코딩할 수 없음")
        (args.out / "lidar_scan_plane_overlay.png").write_bytes(png.tobytes())
        (args.out / "projection.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "points"}, ensure_ascii=False
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"카메라·라이다 투영 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
