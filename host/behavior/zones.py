"""순찰 구역 앵커와 순찰 스케줄러 (WBS 3.9.1 · 3.9.2 · FR-7).

**두 작업이 한 파일에 있는 것은 작업 사전이 그렇게 정했기 때문이다** —
`3.9.1`(구역 앵커 정의·관리)과 `3.9.2`(순차·랜덤 순찰 스케줄러)의 산출물이
둘 다 `behavior/zones.py` 다. 실제로 붙어 있는 것이 맞다: 다음 구역을 고르는
일은 **구역 목록과 도착 판정 반경을 함께 봐야** 하고, 그 둘이 여기 있다.

구역의 **정본은 `config.yaml` 의 `zones.ids`** 다 (`[A, B, C]`). 여기서 만드는
`zones.json` 은 그 라벨에 **좌표를 붙인 것**이며 라벨 목록을 새로 정하지 않는다.

⚠️ 이 구분이 중요한 이유 — `zones.marker_map` 은 ArUco ID 를 같은 라벨에 묶고
(FR-8 변화 감지), 대시보드도 같은 라벨로 구역을 표시한다. 좌표 파일이 제멋대로
`D`·`E` 를 만들면 **한쪽에만 있는 구역**이 생겨서 마커로 식별한 구역과 좌표로
이동한 구역이 다른 것을 가리킨다. 그래서 설정에 없는 라벨은 만들지 않고,
이미 있는 파일에서 발견하면 경고와 함께 무시한다.

구역 "식별"(ArUco)과 구역 "이동"(측위)은 다른 일이다 — 설정 파일이 그렇게
적어 두었고, 이 파일은 후자만 담당한다.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from host.behavior.planner import Plan, PlanParams, Point, plan_to
from host.common.logging_setup import event_logger
from host.slam.occupancy import OccupancyGrid

LOG = event_logger("mechadog.behavior.zones")

ZONES_FILENAME = "zones.json"


@dataclass(frozen=True, slots=True)
class Zone:
    """구역 하나. 좌표는 실공간 m 다."""

    label: str
    x: float
    y: float

    @property
    def xy(self) -> tuple[float, float]:
        return self.x, self.y


class ZoneStore:
    """구역 목록. **설정의 `zones.ids` 순서를 순찰 순서로 쓴다.**

    첫 사이클이 사용자 지정 순서를 따르는데(FR-7.3), 그 "사용자 지정"의 정본이
    설정 파일이다. 클릭한 순서를 따로 저장하면 두 개의 순서가 생긴다.
    """

    def __init__(self, allowed_labels: Sequence[str]) -> None:
        self._allowed = tuple(allowed_labels)
        self._zones: dict[str, Zone] = {}

    # ── 조회 ──────────────────────────────────────────────────
    @property
    def allowed_labels(self) -> tuple[str, ...]:
        return self._allowed

    @property
    def labels(self) -> tuple[str, ...]:
        """좌표가 붙은 구역을 **설정 순서로** 돌려준다."""
        return tuple(label for label in self._allowed if label in self._zones)

    def __len__(self) -> int:
        return len(self._zones)

    def __contains__(self, label: object) -> bool:
        return label in self._zones

    def __iter__(self) -> Iterable[Zone]:
        return iter(self.as_tuple())

    def as_tuple(self) -> tuple[Zone, ...]:
        return tuple(self._zones[label] for label in self.labels)

    def get(self, label: str) -> Zone:
        return self._zones[label]

    def xy(self, label: str) -> tuple[float, float]:
        return self._zones[label].xy

    @property
    def next_label(self) -> str | None:
        """아직 좌표가 없는 첫 라벨. 없으면 `None` (설정된 구역을 다 채웠다)."""
        return next((label for label in self._allowed if label not in self._zones), None)

    # ── 편집 ──────────────────────────────────────────────────
    def place(self, x: float, y: float) -> Zone | None:
        """다음 라벨에 좌표를 붙인다. 라벨이 남아 있지 않으면 `None`."""
        label = self.next_label
        if label is None:
            LOG.warning(
                "zone_labels_exhausted",
                allowed=list(self._allowed),
                hint="config.yaml 의 zones.ids 를 늘려야 한다",
            )
            return None
        zone = Zone(label, float(x), float(y))
        self._zones[label] = zone
        return zone

    def undo(self) -> Zone | None:
        """가장 마지막으로 붙인 좌표를 뗀다."""
        placed = self.labels
        if not placed:
            return None
        return self._zones.pop(placed[-1])

    def clear(self) -> None:
        self._zones.clear()

    # ── 파일 ──────────────────────────────────────────────────
    @classmethod
    def load(cls, directory: Path, allowed_labels: Sequence[str]) -> ZoneStore:
        """`zones.json` 을 읽는다. 없으면 빈 저장소를 돌려준다.

        설정에 없는 라벨은 **버리고 경고한다.** 조용히 받아들이면 대시보드와
        마커 매핑에서 한쪽에만 있는 구역이 되어, 그 불일치가 실기 시험에서야
        드러난다.
        """
        store = cls(allowed_labels)
        path = directory / ZONES_FILENAME
        if not path.is_file():
            return store
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            LOG.warning("zones_file_malformed", path=str(path))
            return store
        unknown: list[str] = []
        for label, value in raw.items():
            if label not in store.allowed_labels:
                unknown.append(label)
                continue
            if not isinstance(value, Mapping) or "x" not in value or "y" not in value:
                LOG.warning("zone_entry_malformed", label=label)
                continue
            store._zones[label] = Zone(label, float(value["x"]), float(value["y"]))
        if unknown:
            LOG.warning(
                "zone_labels_not_in_config",
                ignored=sorted(unknown),
                allowed=list(store.allowed_labels),
            )
        return store

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ZONES_FILENAME
        payload: dict[str, Any] = {
            zone.label: {"x": round(zone.x, 3), "y": round(zone.y, 3)} for zone in self.as_tuple()
        }
        path.write_text(
            json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        LOG.info("zones_saved", path=str(path), count=len(payload))
        return path


def nearest_free_cell(
    grid: OccupancyGrid,
    row: int,
    col: int,
    *,
    free_thresh: float,
    max_radius_cells: int,
) -> tuple[int, int] | None:
    """막힌 셀을 클릭했을 때 가장 가까운 **확실히 빈** 셀을 찾는다.

    `free_thresh` 이하만 받아들인다 — 미관측(값 0) 셀을 허용하면 지도 밖 빈
    공간에 구역이 놓이고, A* 는 거기까지 갈 경로를 못 찾는다. 클릭을 그냥
    거절하지 않는 이유는 벽에서 몇 cm 안쪽을 노린 클릭이 대부분이기 때문이다.
    """
    if grid.inside(row, col) and grid.cells[row, col] <= free_thresh:
        return row, col
    for radius in range(1, max_radius_cells + 1):
        for d_row in range(-radius, radius + 1):
            for d_col in range(-radius, radius + 1):
                if max(abs(d_row), abs(d_col)) != radius:
                    continue  # 껍질만 훑는다 — 안쪽은 이미 봤다
                r, c = row + d_row, col + d_col
                if grid.inside(r, c) and grid.cells[r, c] <= free_thresh:
                    return r, c
    return None


def zone_arrays(store: ZoneStore) -> tuple[np.ndarray, tuple[str, ...]]:
    """시각화용. 좌표 배열과 라벨을 같은 순서로 돌려준다."""
    labels = store.labels
    if not labels:
        return np.zeros((0, 2)), ()
    return np.array([store.xy(label) for label in labels], dtype=np.float64), labels


# ══════════════════════════════════════════════════════════════
#  순찰 스케줄러 — WBS 3.9.2 · FR-7.2/7.3/7.4
# ══════════════════════════════════════════════════════════════


def select_next(
    *,
    cycle: int,
    visited: frozenset[str],
    start: Point,
    candidates: dict[str, Point],
    order: tuple[str, ...],
    grid: OccupancyGrid,
    blocked: np.ndarray,
    params: PlanParams,
    random_after_first_cycle: bool,
    rng: random.Random | None = None,
) -> Plan:
    """다음 순찰 구역을 고른다 (FR-7.3).

    | 상황 | 규칙 | 근거 |
    | :--- | :--- | :--- |
    | 사이클 0 | 설정 순서 그대로 | 첫 사이클은 사용자 지정 순서다 |
    | 사이클 1+ 첫 구역 | 무작위 | `zones.random_after_first_cycle` |
    | 그 뒤 | **실제 경로가 가장 짧은** 미방문 구역 | 직선거리는 벽을 무시한다 |

    `random_after_first_cycle` 이 거짓이면 모든 사이클이 설정 순서를 따른다 —
    설정 항목이 있으니 코드가 그것을 실제로 지켜야 한다. 무작위 순찰은
    예측 가능한 순회를 막는 보안 목적이므로 끌 수 있어야 옳다.
    """
    remaining = [label for label in order if label not in visited and label in candidates]
    if not remaining:
        return Plan(None)

    # ⚠️ **막힌 구역은 그 구역만 건너뛴다.** 첫 후보만 풀고 빈 계획을 돌려주면
    # 호출부(`patrol._replan`)가 *방문한 구역이 있다* 는 이유로 사이클을 끝내서,
    # 뒤에 남은 갈 수 있는 구역까지 그 사이클에서 빠진다.
    sequential = cycle == 0 or not random_after_first_cycle
    if sequential:
        return _first_reachable(remaining, candidates, start, grid, blocked, params)

    if not visited:
        chooser = rng if rng is not None else random
        label = chooser.choice(remaining)
        rest = [other for other in remaining if other != label]
        return _first_reachable([label, *rest], candidates, start, grid, blocked, params)

    best = Plan(None)
    for label in remaining:
        plan = plan_to(label, candidates[label], start, grid, blocked, params)
        if plan.reachable and (not best.reachable or plan.length_m < best.length_m):
            best = plan
    return best


def _first_reachable(
    labels: Sequence[str],
    candidates: dict[str, Point],
    start: Point,
    grid: OccupancyGrid,
    blocked: np.ndarray,
    params: PlanParams,
) -> Plan:
    """순서대로 풀어 **처음 도달 가능한** 구역의 계획. 막힌 구역은 남긴 채 넘어간다."""
    for label in labels:
        plan = plan_to(label, candidates[label], start, grid, blocked, params)
        if plan.reachable:
            return plan
        LOG.warning("zone_skipped_unreachable", label=label)
    return Plan(None)
