"""관제 웹의 실제 집 지도 — 항법 지도·구역 영역을 그림으로, 좌표 변환을 행렬로 내보낸다.

로봇은 **순찰 좌표**(출발 자리 원점, 출발 방향 +x)로 생각하고, 사람은 평면도처럼 놓인
지도를 본다. 둘을 잇는 것이 지도 폴더의 `pose_frame.json`(회전 → 평행이동)이다. 서버가
그림을 표시 좌표로 그려 주고, 픽셀 ↔ 순찰 좌표 아핀 행렬을 함께 준다 — 화면은 행렬만
곱하면 되므로 좌표계 규칙을 두 군데에 적지 않는다.

픽셀 좌표 `(u, v)` 는 그림 왼쪽 위 모서리가 (0, 0), 오른쪽·아래로 커지는 **연속** 좌표다
(픽셀 중심은 +0.5).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import cv2
import numpy as np

from host.behavior.zone_map import ZoneMap
from host.behavior.zones import ZoneStore
from host.slam.occupancy import OccupancyGrid

#: 구역 칠 색 (B, G, R) — 상태색(초록·노랑·빨강)과 겹치지 않는 차분한 색. 순서대로 돌려 쓴다.
_ZONE_COLORS = ((214, 170, 120), (140, 190, 140), (120, 160, 220), (190, 140, 200), (160, 200, 200))


@dataclass(frozen=True, slots=True)
class PoseFrame:
    """순찰 → 표시: 반시계 `rotate_deg` 회전 뒤 `(tx, ty)` 평행이동."""

    rotate_deg: float = 0.0
    tx: float = 0.0
    ty: float = 0.0

    @classmethod
    def load(cls, directory: Path | None) -> PoseFrame:
        path = None if directory is None else directory / "pose_frame.json"
        if path is None or not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            float(data.get("rotate_deg", 0.0)),
            float(data.get("translate_x", 0.0)),
            float(data.get("translate_y", 0.0)),
        )

    def matrix(self) -> np.ndarray:
        r = math.radians(self.rotate_deg)
        c, s = math.cos(r), math.sin(r)
        return np.array([[c, -s, self.tx], [s, c, self.ty], [0.0, 0.0, 1.0]])


def _affine3(m: np.ndarray) -> np.ndarray:
    out = np.eye(3)
    out[:2, :] = m[:2, :]
    return out


def render(
    grid: OccupancyGrid,
    *,
    zones: ZoneStore,
    zone_map: ZoneMap | None,
    frame: PoseFrame,
    occ_thresh: float,
    free_thresh: float,
    scale: int = 12,
) -> tuple[bytes, dict[str, Any]]:
    """지도 PNG 와 메타(크기·행렬·구역)를 돌려준다. 그림 한 칸 = 지도 한 칸을 `scale` 배로."""
    meta = grid.meta
    res = meta.resolution
    to_display = frame.matrix()
    x0, y0 = meta.origin_x, meta.origin_y
    # 관측된 칸의 경계상자(+여백 0.3m)만 그린다 — 지도 격자는 둘레가 넉넉해 집이 작게 보인다.
    rows_k, cols_k = np.nonzero(np.abs(grid.cells) > 0.01)
    margin = 0.3
    if rows_k.size:
        bx0 = float(x0 + cols_k.min() * res - margin)
        bx1 = float(x0 + (cols_k.max() + 1) * res + margin)
        by0 = float(y0 + rows_k.min() * res - margin)
        by1 = float(y0 + (rows_k.max() + 1) * res + margin)
    else:
        bx0, by0 = x0, y0
        bx1, by1 = x0 + meta.width * res, y0 + meta.height * res
    corners = to_display @ np.array([[bx0, bx1, bx0, bx1], [by0, by0, by1, by1], [1, 1, 1, 1]])
    dx0, dx1 = float(corners[0].min()), float(corners[0].max())
    dy0, dy1 = float(corners[1].min()), float(corners[1].max())
    width = max(1, round((dx1 - dx0) / res))
    height = max(1, round((dy1 - dy0) / res))
    # 표시 좌표 → 픽셀: u = (xd − dx0)/res, v = (dy1 − yd)/res (위가 북쪽).
    display_to_px = np.array([[1 / res, 0, -dx0 / res], [0, -1 / res, dy1 / res], [0, 0, 1]])
    patrol_to_px = display_to_px @ to_display
    px_to_patrol = np.linalg.inv(patrol_to_px)

    # 픽셀 중심마다 순찰 좌표로 되돌려 지도 칸을 읽는다 (회전이 90° 배수가 아니어도 된다).
    us, vs = np.meshgrid(np.arange(width) + 0.5, np.arange(height) + 0.5)
    px = np.stack([us.ravel(), vs.ravel(), np.ones(us.size)])
    xp, yp, _ = px_to_patrol @ px
    cols = np.floor((xp - x0) / res).astype(np.int64)
    rows = np.floor((yp - y0) / res).astype(np.int64)
    inside = (rows >= 0) & (rows < meta.height) & (cols >= 0) & (cols < meta.width)
    cells = np.zeros(xp.shape)
    cells[inside] = grid.cells[rows[inside], cols[inside]]
    image = np.full((xp.size, 3), 205, dtype=np.uint8)  # 미관측 — 회색
    free = inside & (cells <= free_thresh)
    occupied = inside & (cells >= occ_thresh)
    image[free] = (250, 250, 250)
    labels = list(zones.labels)
    if zone_map is not None:
        for index, label in enumerate(labels):
            mine = free & zone_map.contains(label, xp, yp)
            color = np.array(_ZONE_COLORS[index % len(_ZONE_COLORS)], dtype=np.float64)
            image[mine] = (0.72 * 250 + 0.28 * color).astype(np.uint8)
    image[occupied] = (40, 40, 40)
    picture = image.reshape(height, width, 3)
    if scale > 1:
        picture = cv2.resize(
            picture, (width * scale, height * scale), interpolation=cv2.INTER_NEAREST
        )
        # Blend only zone tint; preserve exact occupancy cell edges.
        free_mask = cv2.resize(
            free.reshape(height, width).astype(np.uint8),
            (width * scale, height * scale),
            interpolation=cv2.INTER_NEAREST,
        ).astype(bool)
        zone_tint = image.copy()
        zone_tint[~free] = (250, 250, 250)
        tint = cv2.resize(
            zone_tint.reshape(height, width, 3),
            (width * scale, height * scale),
            interpolation=cv2.INTER_LINEAR,
        )
        picture[free_mask] = tint[free_mask]
        scaler = np.diag([scale, scale, 1.0])
        patrol_to_px = scaler @ patrol_to_px
        px_to_patrol = np.linalg.inv(patrol_to_px)
    ok, png = cv2.imencode(".png", picture)
    if not ok:
        raise RuntimeError("지도 PNG 인코딩 실패")
    zone_list = []
    for index, label in enumerate(labels):
        zx, zy = zones.xy(label)
        zone_list.append(
            {
                "id": label,
                "x": round(zx, 3),
                "y": round(zy, 3),
                "color": "#{:02x}{:02x}{:02x}".format(
                    *reversed(_ZONE_COLORS[index % len(_ZONE_COLORS)])
                ),
            }
        )
    return png.tobytes(), {
        "width": int(picture.shape[1]),
        "height": int(picture.shape[0]),
        "resolution_m": res / scale,
        "frame": "patrol",
        "patrol_to_px": [[round(v, 6) for v in row] for row in _affine3(patrol_to_px)[:2]],
        "px_to_patrol": [[round(v, 6) for v in row] for row in _affine3(px_to_patrol)[:2]],
        "zones": zone_list,
        "zone_areas": zone_map is not None,
    }


class MapView:
    """한 번 그려 두고 같은 그림을 돌려준다 — 항법 지도는 운행 중 바뀌지 않는다."""

    def __init__(self, renderer: Callable[[], tuple[bytes, dict[str, Any]]]) -> None:
        self._lock = Lock()
        self._renderer = renderer
        self._cached: tuple[bytes, dict[str, Any]] | None = None

    def get(self) -> tuple[bytes, dict[str, Any]]:
        with self._lock:
            if self._cached is None:
                self._cached = self._renderer()
            return self._cached
