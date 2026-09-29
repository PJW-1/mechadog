"""LiDAR 점유격자를 Isaac 이 읽는 정적 충돌 USD 로 세운다.

**실측 지도를 스케일 정본으로 삼는 조각이다.** 시뮬레이터 안의 집을 눈대중으로
짓고 그 지도를 로봇에 주면, 로봇은 실제 벽과 다른 지도를 들고 측위한다. 반대로
간다 — 실제 LiDAR 로 뜬 지도를 시뮬에 **벽으로 세운다.** 그러면 시뮬과 실기가
같은 좌표계를 쓰고, `zones.json` 앵커 하나가 양쪽에서 같은 자리를 가리킨다.

```
실제 집 + 실제 LiDAR → slam_toolbox → 점유격자 ─┬─→ 로봇 자율주행 (실기)
                                                └─→ 이 도구 → Isaac USD (시뮬)
```

⚠️ **이 벽은 LiDAR 가 본 한 장의 수평면을 위아래로 늘린 것이다.** LD19 는
장착 높이(실측 225mm)의 평면 하나만 본다. 그 위의 선반, 그 아래의 문턱은
지도에 없고 따라서 여기 세우는 벽에도 없다. `--height` 는 충돌을 성립시키려고
주는 값이지 **측정한 벽 높이가 아니다.**

⚠️ **미지는 자유가 아니다.** 기본값 `--unknown open` 은 미지 칸에 형상을 두지
않는데, 이건 *"관측하지 않았다"* 를 그린 것이지 *"비어 있다"* 가 아니다. 시뮬에서
경로계획을 시험할 때 로봇이 한 번도 보지 않은 공간을 뚫고 지나가면 안 되므로,
그 시험에는 `--unknown wall` 을 쓴다. 어느 쪽이든 두 수를 모두 출력한다.

칸 하나에 프림 하나를 두지 않고 **직사각형으로 합친다.** 09-29 에 여러 시점을
동시에 렌더하다 GPU 가 `device lost` 로 죽은 적이 있어서, 프림 수는 적을수록
좋다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.slam.occupancy import OccupancyGrid  # noqa: E402

# 점유·자유의 경계. `load_ros2` 가 점유를 `LOGODDS_MAX`, 자유를 `LOGODDS_MIN`,
# 미지를 정확히 0 으로 넣으므로 0 을 기준으로 세 갈래가 갈린다.
OCCUPIED_ABOVE: float = 0.0
FREE_BELOW: float = 0.0


@dataclass(frozen=True, slots=True)
class Box:
    """세울 상자 하나. 단위는 m 이고 좌표는 **지도 좌표계 그대로**다."""

    x: float
    y: float
    size_x: float
    size_y: float
    kind: str  # "wall" | "unknown"


def merge_rectangles(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """참인 칸을 직사각형으로 묶는다. `(row0, row1, col0, col1)` 반열린 구간.

    가로로 이어진 구간을 잡고, **같은 열 범위가 계속 참인 동안 아래로 늘린다.**
    최소 개수를 보장하는 분할은 아니지만 방 모양의 벽에서는 칸 수의 수십분의
    일로 줄어들고, 무엇보다 **겹치지 않고 빠짐이 없다** — 그 두 성질만 있으면
    충돌 형상으로 충분하다.
    """
    remaining = mask.copy()
    height, width = remaining.shape
    rectangles: list[tuple[int, int, int, int]] = []
    for row in range(height):
        col = 0
        while col < width:
            if not remaining[row, col]:
                col += 1
                continue
            col_end = col
            while col_end < width and remaining[row, col_end]:
                col_end += 1
            row_end = row + 1
            while row_end < height and remaining[row_end, col:col_end].all():
                row_end += 1
            rectangles.append((row, row_end, col, col_end))
            remaining[row:row_end, col:col_end] = False
            col = col_end
    return rectangles


def boxes_from_grid(grid: OccupancyGrid, *, include_unknown: bool) -> list[Box]:
    """격자를 상자 목록으로 바꾼다.

    `to_world` 가 칸의 **중심**을 주므로 직사각형의 중심도 양 끝 칸 중심의
    중간으로 잡는다. 모서리로 잡으면 반 칸(기본 2.5cm)씩 치우친다.
    """
    res = grid.meta.resolution
    cells = grid.cells
    groups: list[tuple[np.ndarray, str]] = [(cells > OCCUPIED_ABOVE, "wall")]
    if include_unknown:
        groups.append((cells == 0.0, "unknown"))

    boxes: list[Box] = []
    for mask, kind in groups:
        for row0, row1, col0, col1 in merge_rectangles(mask):
            x0, y0 = grid.to_world(row0, col0)
            x1, y1 = grid.to_world(row1 - 1, col1 - 1)
            boxes.append(
                Box(
                    x=(x0 + x1) / 2.0,
                    y=(y0 + y1) / 2.0,
                    size_x=(col1 - col0) * res,
                    size_y=(row1 - row0) * res,
                    kind=kind,
                )
            )
    return boxes


def _cube(name: str, box: Box, height: float, color: tuple[float, float, float]) -> str:
    """`size = 1` 인 단위 상자를 실치수로 늘린다 — `scale` 이 곧 전체 길이다."""
    return f"""
        def Cube "{name}" (
            prepend apiSchemas = ["PhysicsCollisionAPI"]
        )
        {{
            double size = 1
            double3 xformOp:translate = ({box.x:.4f}, {box.y:.4f}, {height / 2:.4f})
            float3 xformOp:scale = ({box.size_x:.4f}, {box.size_y:.4f}, {height:.4f})
            uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
            color3f[] primvars:displayColor = [({color[0]}, {color[1]}, {color[2]})]
        }}
"""


def build_usda(
    grid: OccupancyGrid,
    boxes: list[Box],
    *,
    height: float,
    lidar_plane_m: float,
) -> str:
    """USD 본문을 만든다. **`metersPerUnit = 1` 이라 지도의 m 가 그대로 m 다.**"""
    walls = [b for b in boxes if b.kind == "wall"]
    unknown = [b for b in boxes if b.kind == "unknown"]
    meta = grid.meta
    body = "".join(
        _cube(f"wall_{i:04d}", b, height, (0.55, 0.55, 0.58)) for i, b in enumerate(walls)
    ) + "".join(
        _cube(f"unknown_{i:04d}", b, height, (0.80, 0.70, 0.35)) for i, b in enumerate(unknown)
    )
    return f"""#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Z"
    doc = \"\"\"LiDAR 점유격자에서 세운 정적 충돌 장면.
좌표는 지도 좌표계 그대로다 — origin ({meta.origin_x:.4f}, {meta.origin_y:.4f}) m,
해상도 {meta.resolution:.4f} m/셀, {meta.width} x {meta.height} 셀.
벽 높이 {height} m 는 충돌용 임의값이며 측정값이 아니다.
LiDAR 는 높이 {lidar_plane_m} m 의 수평면 하나만 보았다.\"\"\"
)

def Xform "World"
{{
    def PhysicsScene "PhysicsScene"
    {{
        vector3f physics:gravity = (0, 0, -9.8)
    }}

    def Xform "Map"
    {{{body}    }}
}}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("map_dir", type=Path, help="지도가 있는 폴더")
    parser.add_argument("--stem", default="slam_map", help="지도 파일 이름 (확장자 제외)")
    parser.add_argument("--out", type=Path, required=True, help="쓸 .usda 경로")
    parser.add_argument(
        "--height", type=float, default=1.0, help="벽 높이 m (충돌용 임의값, 측정값 아님)"
    )
    parser.add_argument(
        "--unknown",
        choices=("open", "wall"),
        default="open",
        help="미지 칸을 비워 둘지 막을지. 경로계획 시험에는 wall 을 쓴다",
    )
    parser.add_argument(
        "--lidar-plane-m",
        type=float,
        default=0.225,
        help="LiDAR 수평면 높이 m (기록용 · 실측 장착 높이)",
    )
    args = parser.parse_args(argv)

    grid = OccupancyGrid.load(args.map_dir, stem=args.stem)
    boxes = boxes_from_grid(grid, include_unknown=args.unknown == "wall")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        build_usda(grid, boxes, height=args.height, lidar_plane_m=args.lidar_plane_m),
        encoding="utf-8",
    )

    cells = grid.cells
    stats = {
        "map_dir": str(args.map_dir),
        "stem": args.stem,
        "usd": str(args.out),
        "resolution_m": grid.meta.resolution,
        "origin_xy_m": [grid.meta.origin_x, grid.meta.origin_y],
        "grid_cells": [grid.meta.width, grid.meta.height],
        "occupied_cells": int((cells > OCCUPIED_ABOVE).sum()),
        "free_cells": int((cells < FREE_BELOW).sum()),
        "unknown_cells": int((cells == 0.0).sum()),
        "wall_prims": sum(1 for b in boxes if b.kind == "wall"),
        "unknown_prims": sum(1 for b in boxes if b.kind == "unknown"),
        "wall_height_m": args.height,
        "wall_height_measured": False,
        "lidar_plane_m": args.lidar_plane_m,
        "unknown_mode": args.unknown,
    }
    args.out.with_suffix(".json").write_text(
        json.dumps(stats, indent=4, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(stats, indent=4, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
