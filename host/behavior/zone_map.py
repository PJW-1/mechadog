"""구역 영역 지도 — «지금 어느 방인가» 를 로봇 자신이 안다.

`zones.json` 의 앵커는 **점**이라 도착 판정(반경 0.3m)에만 쓰인다. 방 전체를 구역으로
다루려면 셀마다 소유 구역을 적은 라벨 격자가 필요하다 — `zone_labels.npy`(점유 격자와
같은 해상도·원점) + `zones_plan.json`(라벨 번호 ↔ 구역 id). 대시보드가 같은 파일로
구역을 칠하므로 로봇과 화면이 **같은 정의**를 쓴다.

조회는 실공간 좌표로 한다 — 운용 격자는 걸으며 자라 원점이 움직이지만 이 격자는 고정이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LABELS_FILENAME = "zone_labels.npy"
PLAN_FILENAME = "zones_plan.json"


@dataclass(frozen=True, slots=True)
class ZoneMap:
    labels: np.ndarray
    resolution: float
    origin_x: float
    origin_y: float
    #: 라벨 번호(1부터) → 구역 id. 0 은 미도달·미관측.
    names: dict[int, str]

    @classmethod
    def load(cls, directory: Path) -> ZoneMap | None:
        """두 파일이 다 있을 때만 돌려준다 — 없으면 구역 인지 없이 돈다."""
        labels_path = directory / LABELS_FILENAME
        plan_path = directory / PLAN_FILENAME
        if not labels_path.is_file() or not plan_path.is_file():
            return None
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        meta = plan["meta"]
        labels = np.load(labels_path)
        if labels.shape != (int(meta["height"]), int(meta["width"])):
            raise ValueError(
                f"zone_labels 모양 {labels.shape} 이 zones_plan meta "
                f"({meta['height']}, {meta['width']}) 와 다름"
            )
        return cls(
            labels=labels,
            resolution=float(meta["resolution"]),
            origin_x=float(meta["origin_x"]),
            origin_y=float(meta["origin_y"]),
            names={int(zone["index"]): str(zone["id"]) for zone in plan["zones"]},
        )

    def contains(self, zone: str, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """실공간 좌표 배열 중 `zone` 에 속한 것 (격자 밖·미도달은 False)."""
        index = next((k for k, name in self.names.items() if name == zone), None)
        cols = np.floor((np.asarray(xs) - self.origin_x) / self.resolution).astype(np.int64)
        rows = np.floor((np.asarray(ys) - self.origin_y) / self.resolution).astype(np.int64)
        height, width = self.labels.shape
        inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
        out = np.zeros(cols.shape, dtype=bool)
        if index is not None:
            out[inside] = self.labels[rows[inside], cols[inside]] == index
        return out

    def zone_at(self, x: float, y: float) -> str | None:
        """실공간 `(x, y)` 가 속한 구역 id. 미도달 셀·격자 밖이면 `None`."""
        col = int(np.floor((x - self.origin_x) / self.resolution))
        row = int(np.floor((y - self.origin_y) / self.resolution))
        rows, cols = self.labels.shape
        if not (0 <= row < rows and 0 <= col < cols):
            return None
        return self.names.get(int(self.labels[row, col]))
