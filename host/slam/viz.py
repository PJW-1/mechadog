"""지도·순찰 시각화 (개발 도구).

⚠️ **matplotlib 는 선택 의존성이다.** `requirements.txt` 에 없으므로 import 를
함수 안에서 한다. 모듈 최상단에서 import 하면 `host.slam` 을 쓰는 모든
코드가 matplotlib 를 요구하게 되고, **CI 러너와 대시보드 프로세스까지** 그것을
깔아야 한다. 지도를 그리는 것은 사람이 보기 위한 일이고 순찰 자체와 무관하다.

관제 화면의 정본은 대시보드(`4.5`·`4.6`)다. 여기 있는 것은 그것이 붙기 전까지
알고리즘을 눈으로 확인하는 수단이며, **운용 UI 가 아니다.**
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from host.slam.occupancy import LOGODDS_MAX, LOGODDS_MIN, OccupancyGrid


def to_image(grid: OccupancyGrid) -> np.ndarray:
    """로그오즈를 흑백으로. 막힘=검정 · 빈곳=흰색 · **미관측=회색**.

    미관측을 흰색으로 칠하지 않는 것이 요점이다 — 구역을 클릭할 때 "가 본 빈
    공간"과 "아직 모르는 곳"을 구별할 수 있어야 한다. 계획도 그 둘을 다르게
    본다 (`planner.inflate`).
    """
    scale = max(abs(LOGODDS_MIN), LOGODDS_MAX) * 2
    return np.clip(0.5 - grid.cells / scale, 0.0, 1.0)


def save_png(grid: OccupancyGrid, path: Path) -> Path:
    """사람이 보는 지도. `.npy` 가 정본이고 이것은 사본이다."""
    import matplotlib

    matplotlib.use("Agg")  # 저장만 한다 — 창을 열려고 하면 헤드리스에서 죽는다
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    plt.imsave(path, to_image(grid), cmap="gray", vmin=0, vmax=1, origin="lower")
    return path


class LiveMap:
    """SLAM 진행을 실시간으로 보여준다. 창이 없으면 조용히 아무것도 하지 않는다."""

    def __init__(self, grid: OccupancyGrid, *, title: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self._grid = grid
        self._plt: Any = None
        if not enabled:
            return
        import matplotlib.pyplot as plt

        self._plt = plt
        plt.ion()
        self._fig, self._ax = plt.subplots(figsize=(6, 6))
        self._image = self._ax.imshow(
            to_image(grid),
            cmap="gray",
            vmin=0,
            vmax=1,
            origin="lower",
            extent=grid.extent,
        )
        (self._path_line,) = self._ax.plot([], [], "b-", linewidth=1)
        (self._robot,) = self._ax.plot([], [], "ro", markersize=7)
        self._heading = self._ax.annotate(
            "",
            xy=(0, 0),
            xytext=(0, 0),
            arrowprops={"arrowstyle": "->", "color": "red"},
        )
        self._ax.set_title(title)

    def update(self, pose: tuple[float, float, float], trail: list[tuple[float, float]]) -> None:
        if not self.enabled:
            return
        import math

        self._image.set_data(to_image(self._grid))
        self._image.set_extent(self._grid.extent)
        left, right, bottom, top = self._grid.extent
        self._ax.set_xlim(left, right)
        self._ax.set_ylim(bottom, top)
        self._path_line.set_data([p[0] for p in trail], [p[1] for p in trail])
        self._robot.set_data([pose[0]], [pose[1]])
        self._heading.set_position((pose[0], pose[1]))
        self._heading.xy = (
            pose[0] + 0.3 * math.cos(pose[2]),
            pose[1] + 0.3 * math.sin(pose[2]),
        )
        self._fig.canvas.draw_idle()
        self._plt.pause(0.001)

    def close(self) -> None:
        if self.enabled and self._plt is not None:
            self._plt.close(self._fig)


class PatrolView:
    """순찰 진행 — 경로 · 구역 · 신규 장애물 · 내려보내는 `STATE`."""

    def __init__(
        self, grid: OccupancyGrid, zone_points: np.ndarray, labels: tuple[str, ...]
    ) -> None:
        import matplotlib.pyplot as plt

        self._plt = plt
        self._grid = grid
        plt.ion()
        self._fig, self._ax = plt.subplots(figsize=(6, 6))
        self._ax.imshow(
            to_image(grid),
            cmap="gray",
            vmin=0,
            vmax=1,
            origin="lower",
            extent=grid.extent,
        )
        for (x, y), label in zip(zone_points, labels, strict=True):
            self._ax.plot(x, y, "gs", markersize=8)
            self._ax.annotate(
                label,
                (x, y),
                color="green",
                fontweight="bold",
                xytext=(4, 4),
                textcoords="offset points",
            )
        (self._path,) = self._ax.plot([], [], "b--", linewidth=1)
        (self._robot,) = self._ax.plot([], [], "ro", markersize=8)
        (self._obstacles,) = self._ax.plot([], [], "rx", markersize=9, markeredgewidth=2)
        self._heading = self._ax.annotate(
            "",
            xy=(0, 0),
            xytext=(0, 0),
            arrowprops={"arrowstyle": "->", "color": "red"},
        )

    def update(
        self,
        pose: tuple[float, float, float],
        waypoints: tuple[tuple[float, float], ...],
        obstacles: tuple[tuple[float, float], ...],
        *,
        fsm_state: str,
        target: str | None,
    ) -> None:
        import math

        self._path.set_data([p[0] for p in waypoints], [p[1] for p in waypoints])
        self._robot.set_data([pose[0]], [pose[1]])
        self._obstacles.set_data([p[0] for p in obstacles], [p[1] for p in obstacles])
        self._heading.set_position((pose[0], pose[1]))
        self._heading.xy = (
            pose[0] + 0.3 * math.cos(pose[2]),
            pose[1] + 0.3 * math.sin(pose[2]),
        )
        # **내려보내는 `STATE` 를 제목에 찍는다** — 텔레메트리로 되돌아오는
        # 값과 대조할 것이 화면에 있어야 인식 불일치가 보인다 (PROTOCOL 2절).
        self._ax.set_title(f"Patrol — STATE={fsm_state} / target={target or '-'}")
        self._fig.canvas.draw_idle()
        self._plt.pause(0.001)

    def close(self) -> None:
        self._plt.close(self._fig)
