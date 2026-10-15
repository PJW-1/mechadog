"""Export a saved LiDAR map as truthful sim input without commanding hardware."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.slam import settings
from host.slam.scene_bundle import export_scene_bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--map", type=Path, required=True, help="slam_map.npy가 있는 폴더")
    parser.add_argument("--out", type=Path, required=True, help="scene.json 경로")
    parser.add_argument("--stem", choices=("slam_map", "static_map"), default="slam_map")
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("기존 산출물을 덮어쓰지 않음")
    try:
        bundle = export_scene_bundle(args.map, args.out, settings.load(args.device), stem=args.stem)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(
        f"{args.out}: 점유 구간 {len(bundle['geometry']['occupied_runs_row_start_end'])}, "
        f"카메라 관측 {len(bundle['camera_observations'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
