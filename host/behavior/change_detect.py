"""구역 기준 저장과 변화 분류 (WBS 3.6.1 · 3.6.2 · FR-8.1/8.2/8.3).

구역마다 기준을 남긴다 — 스냅샷 한 장과 «무엇이 몇 개, 대략 어디(격자 칸)» 목록. 다음
방문의 검출 목록을 이 기준과 견주는 것이 변화 감지다.

    기준 등록                         다음 방문
    chair ×1  (가운데)      →        chair ×1  (가운데)   변화 없음
    bottle ×1 (오른쪽 위)            —                    반출
    —                                backpack ×1 (왼쪽)   반입

- 픽셀 차분을 쓰지 않는다 (FR-8.2) — 비교 입력은 객체 목록뿐이고 스냅샷은 사람이 보는 용도다.
- 위치는 성긴 격자(`baseline_grid`, 기본 3×3) 칸으로 뭉갠다 — 로봇이 방문마다 조금씩 다르게
  서도 같은 칸에 잡히게. 격자를 잘게 나누면 같은 물건이 반출·반입으로 동시에 보고된다.
- `person` 은 기준에 넣지 않는다 — 사람은 사람 게이트(FR-3)가 맡는다 (ADR-41 결정 5).

비교는 `classify_changes()`, 확정은 `ChangeConfirmer` 다. 경보로 가는 것은 확정된 변화뿐이다
(FR-8.4 · ADR-41).
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from host.common.config import repo_path
from host.vision.detector import Detection

__all__ = [
    "PERSON_LABEL",
    "ChangeKind",
    "Change",
    "ObjectEntry",
    "ZoneBaseline",
    "summarize_detections",
    "classify_changes",
    "ChangeConfirmer",
    "BaselineStore",
]

#: 기준 목록에서 제외하는 라벨.
PERSON_LABEL = "person"

_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class ObjectEntry:
    """*"무엇이 · 몇 개 · 대략 어디"* 한 줄.

    `cell` 은 `(열, 행)` 이며 왼쪽 위가 `(0, 0)` 이다. 픽셀이 아니라 칸이다.
    """

    label: str
    count: int
    cell: tuple[int, int]

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "count": self.count, "cell": list(self.cell)}


@dataclass(frozen=True, slots=True)
class ZoneBaseline:
    """한 구역의 기준. 이것이 다음 사이클 비교의 왼쪽 항이다."""

    zone_id: str
    captured_ms: int
    frame_size: tuple[int, int]
    grid: tuple[int, int]
    objects: tuple[ObjectEntry, ...]
    snapshot: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "zone_id": self.zone_id,
            "captured_ms": self.captured_ms,
            "frame_size": list(self.frame_size),
            "grid": list(self.grid),
            "objects": [entry.as_dict() for entry in self.objects],
            "snapshot": self.snapshot,
        }


def summarize_detections(
    detections: Iterable[Detection],
    *,
    frame_size: tuple[int, int],
    grid: tuple[int, int],
) -> tuple[ObjectEntry, ...]:
    """검출 목록을 (라벨, 칸)별 개수로 뭉개 정렬해 돌려준다. `person` 은 뺀다."""
    width, height = frame_size
    columns, rows = grid
    if width <= 0 or height <= 0:
        raise ValueError("frame_size 는 양수여야 함")
    if columns <= 0 or rows <= 0:
        raise ValueError("grid 는 1 이상이어야 함")

    tally: Counter[tuple[str, int, int]] = Counter()
    for detection in detections:
        if detection.label == PERSON_LABEL:
            continue  # 사람은 놓인 물건이 아니다
        x1, y1, x2, y2 = detection.box
        centre_x = (x1 + x2) / 2.0
        centre_y = (y1 + y2) / 2.0
        # 화면 밖으로 살짝 나간 박스도 가장자리 칸으로 받는다. 버리면 반출로 보인다.
        column = min(max(int(centre_x * columns / width), 0), columns - 1)
        row = min(max(int(centre_y * rows / height), 0), rows - 1)
        tally[(detection.label, column, row)] += 1

    return tuple(
        ObjectEntry(label=label, count=count, cell=(column, row))
        for (label, column, row), count in sorted(tally.items())
    )


class ChangeKind(StrEnum):
    """FR-8.3 의 세 분류. 표가 정본이며 여기 이름이 그것과 1:1 이다."""

    REMOVED = "removed"  # 물체 반출 — 기준에 있던 객체가 사라짐
    ADDED = "added"  # 물체 반입 — 기준에 없던 객체가 추가됨
    PERSON = "person"  # 인원 출현 — 기준에 없던 person 검출


@dataclass(frozen=True, slots=True)
class Change:
    """변화 한 건. `count` 는 **몇 개가** 늘거나 줄었는지다."""

    kind: ChangeKind
    label: str
    count: int
    cell: tuple[int, int] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "label": self.label,
            "count": self.count,
            "cell": list(self.cell) if self.cell is not None else None,
        }


def classify_changes(
    baseline: ZoneBaseline,
    detections: Iterable[Detection],
    *,
    frame_size: tuple[int, int],
    watch_classes: Iterable[str],
) -> tuple[Change, ...]:
    """기준과 현재를 목록으로 견주어 FR-8.3 의 세 분류를 낸다 (이미지는 입력이 아니다).

    - 현재 프레임은 설정이 아니라 기준이 떠진 격자로 뭉갠다 — 양쪽이 같은 격자라야 비교가 된다.
    - 감시 목록(`vision.coco.change_watch_classes`) 밖의 라벨은 무시한다.
    - `person` 은 «있는가» 한 건으로만 낸다(`count` 는 인원 수).
    """
    watched = frozenset(watch_classes)
    # 두 번 훑으므로 제너레이터를 먼저 굳힌다.
    frame = tuple(detections)
    # 기준이 떠진 격자로 현재를 뭉갠다.
    current = summarize_detections(frame, frame_size=frame_size, grid=baseline.grid)

    before: Counter[tuple[str, int, int]] = Counter()
    for entry in baseline.objects:
        if entry.label in watched:
            before[(entry.label, entry.cell[0], entry.cell[1])] += entry.count
    after: Counter[tuple[str, int, int]] = Counter()
    for entry in current:
        if entry.label in watched:
            after[(entry.label, entry.cell[0], entry.cell[1])] += entry.count

    changes: list[Change] = []
    for key in sorted(set(before) | set(after)):
        label, column, row = key
        delta = after[key] - before[key]
        if delta == 0:
            continue
        kind = ChangeKind.ADDED if delta > 0 else ChangeKind.REMOVED
        changes.append(Change(kind=kind, label=label, count=abs(delta), cell=(column, row)))

    people = sum(1 for detection in frame if detection.label == PERSON_LABEL)
    if people:
        changes.append(Change(kind=ChangeKind.PERSON, label=PERSON_LABEL, count=people, cell=None))
    return tuple(changes)


class ChangeConfirmer:
    """같은 변화가 `confirm_cycles` 번 연속 방문에서 보일 때만 확정한다 (WBS 3.6.3 · ADR-41).

    사이클 하나는 구역 방문 하나다 — 런타임이 방문마다 한 번만 `observe` 한다. 구역마다
    따로 세고, 방문 사이에 누적을 유지하며, 확정한 변화는 사라질 때까지 다시 확정하지
    않는다. `person` 도 지름길 없이 똑같이 센다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        section = config.get("change_detect")
        if not isinstance(section, Mapping):
            raise ValueError("change_detect 설정이 필요함")
        cycles = section.get("confirm_cycles", 2)
        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 1:
            raise ValueError("change_detect.confirm_cycles 는 1 이상 정수여야 함")
        self._cycles = cycles
        #: 구역 → (변화 키 → 연속 관찰 횟수)
        self._streaks: dict[str, dict[tuple[Any, ...], int]] = {}
        #: 구역 → 이미 확정해서 다시 올리지 않는 변화 키
        self._confirmed: dict[str, set[tuple[Any, ...]]] = {}

    @property
    def confirm_cycles(self) -> int:
        return self._cycles

    @staticmethod
    def _key(change: Change) -> tuple[Any, ...]:
        return (change.kind, change.label, change.count, change.cell)

    def observe(self, zone_id: str, changes: Iterable[Change]) -> tuple[Change, ...]:
        """한 방문의 관찰을 넣고 이번에 새로 확정된 것만 돌려준다.

        이번에 안 보인 변화는 횟수와 확정 기록이 지워져, 다시 나타나면 처음부터 센다.
        """
        observed = tuple(changes)
        seen = {self._key(change): change for change in observed}
        streaks = self._streaks.setdefault(zone_id, {})
        confirmed = self._confirmed.setdefault(zone_id, set())

        # 이번에 안 보인 것은 연속이 끊겼다 — 확정 기록도 같이 지운다.
        for key in list(streaks):
            if key not in seen:
                del streaks[key]
        confirmed &= set(seen)

        newly: list[Change] = []
        for key, change in seen.items():
            streaks[key] = streaks.get(key, 0) + 1
            if streaks[key] >= self._cycles and key not in confirmed:
                confirmed.add(key)
                newly.append(change)
        return tuple(newly)

    def forget(self, zone_id: str) -> None:
        """구역의 누적을 버린다. 기준을 다시 뜰 때 부른다 (ADR-41 결정 2·6)."""
        self._streaks.pop(zone_id, None)
        self._confirmed.pop(zone_id, None)


class BaselineStore:
    """구역 기준을 디스크에 남기고 구역 ID 로 되찾는다. 구역당 기준은 하나이며 재등록은 덮어쓴다."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        section = config.get("change_detect")
        if not isinstance(section, Mapping):
            raise ValueError("change_detect 설정이 필요함")
        directory = section.get("snapshot_dir")
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("change_detect.snapshot_dir 는 비어 있지 않은 문자열이어야 함")
        grid = section.get("baseline_grid", [3, 3])
        if (
            not isinstance(grid, Sequence)
            or isinstance(grid, (str, bytes))
            or len(grid) != 2
            or not all(isinstance(value, int) and value >= 1 for value in grid)
        ):
            raise ValueError("change_detect.baseline_grid 는 1 이상 정수 두 개여야 함")
        self._grid = (int(grid[0]), int(grid[1]))
        self._dir = repo_path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def grid(self) -> tuple[int, int]:
        return self._grid

    def _meta_path(self, zone_id: str) -> Path:
        safe = zone_id.strip()
        if not safe or any(character in safe for character in '/\\:*?"<>|' + "\0"):
            raise ValueError(f"구역 ID 로 쓸 수 없는 값: {zone_id!r}")
        return self._dir / f"{safe}.json"

    def _snapshot_of(self, meta_path: Path) -> Path | None:
        """지금 JSON 이 가리키는 그림. 없거나 읽지 못하면 `None`.

        ⚠️ 파일 이름만 받는다 — JSON 이 `../` 를 품고 있어도 폴더 밖을 지우지 않는다.
        """
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        name = data.get("snapshot") if isinstance(data, dict) else None
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".jpg"):
            return None
        return self._dir / name

    def register(
        self,
        zone_id: str,
        detections: Iterable[Detection],
        *,
        frame_size: tuple[int, int],
        now_ms: int,
        jpeg: bytes | None = None,
    ) -> ZoneBaseline:
        """기준을 남긴다. JPEG 는 재인코딩하지 않는다.

        ⚠️ 새 그림(시각이 붙은 이름) → JSON 원자 교체 → 옛 그림 삭제 순서를 지킨다 — 중간에
        끊겨도 목록과 그림이 다른 시점의 것으로 한 벌이 되지 않는다(새 그림이 고아로 남을 뿐).
        """
        if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
            raise ValueError("now_ms 는 0 이상의 정수여야 함")
        meta_path = self._meta_path(zone_id)
        objects = summarize_detections(detections, frame_size=frame_size, grid=self._grid)
        previous = self._snapshot_of(meta_path)

        snapshot = None
        if jpeg:
            image_path = meta_path.with_name(f"{meta_path.stem}_{now_ms}.jpg")
            image_path.write_bytes(jpeg)
            snapshot = image_path.name

        baseline = ZoneBaseline(
            zone_id=zone_id,
            captured_ms=now_ms,
            frame_size=(int(frame_size[0]), int(frame_size[1])),
            grid=self._grid,
            objects=objects,
            snapshot=snapshot,
        )
        payload = json.dumps(baseline.as_dict(), ensure_ascii=False, indent=2)
        # 다 쓴 뒤 한 번에 바꿔 끼운다 — 잘린 기준이 남지 않게.
        partial = meta_path.with_name(meta_path.name + ".tmp")
        partial.write_text(payload + "\n", encoding="utf-8")
        partial.replace(meta_path)
        # 새 기준에 그림이 없어도 옛 그림은 지운다. 못 지우면 고아로 둔다(기준은 이미 바뀌었다).
        if previous is not None and previous.name != snapshot:
            with contextlib.suppress(OSError):
                previous.unlink(missing_ok=True)
        return baseline

    def clear(self, zone_id: str) -> bool:
        """구역의 기준을 지운다 — 그 구역을 다음에 볼 때 새로 뜬다 (WBS 3.6.5 · 관리자 재등록).

        기준이 있었으면 `True`. JSON 을 먼저 지운다 — 끊겨도 없는 그림을 가리키는 기준이 남지 않는다.
        """
        meta_path = self._meta_path(zone_id)
        snapshot = self._snapshot_of(meta_path)
        existed = meta_path.exists()
        meta_path.unlink(missing_ok=True)
        if snapshot is not None:
            with contextlib.suppress(OSError):
                snapshot.unlink(missing_ok=True)
        return existed

    def load(self, zone_id: str) -> ZoneBaseline | None:
        """기준이 없으면 `None`. 물건이 없는 구역의 기준은 `objects` 가 빈 튜플이다."""
        meta_path = self._meta_path(zone_id)
        if not meta_path.exists():
            return None
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if data.get("schema") != _SCHEMA:
            raise ValueError(f"기준 스키마가 다르다: {data.get('schema')!r}")
        objects = tuple(
            ObjectEntry(
                label=str(entry["label"]),
                count=int(entry["count"]),
                cell=(int(entry["cell"][0]), int(entry["cell"][1])),
            )
            for entry in data["objects"]
        )
        return ZoneBaseline(
            zone_id=str(data["zone_id"]),
            captured_ms=int(data["captured_ms"]),
            frame_size=(int(data["frame_size"][0]), int(data["frame_size"][1])),
            grid=(int(data["grid"][0]), int(data["grid"][1])),
            objects=objects,
            snapshot=data.get("snapshot"),
        )

    def zone_ids(self) -> tuple[str, ...]:
        return tuple(sorted(path.stem for path in self._dir.glob("*.json")))
