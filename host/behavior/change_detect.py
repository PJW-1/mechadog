"""구역별 기준 스냅샷·객체 목록 저장 (WBS 3.6.1 · FR-8.1).

구역에 도착하면 그 자리의 **기준**을 남긴다 — 스냅샷 한 장과 *"무엇이 몇 개,
대략 어디에 있었는가"*. 다음 사이클에 같은 구역을 다시 보고 이 기준과 견주는
것이 변화 감지다(`3.6.2`).

⚠️ **픽셀 차분을 쓰지 않는다** (FR-8.2 필수 제약). 그래서 여기서 남기는 것은
이미지가 아니라 **객체 목록**이다 — 스냅샷은 사람이 나중에 눈으로 확인하려고
같이 저장할 뿐, 비교의 입력이 아니다. 로봇이 매번 정확히 같은 자리에 서지
못하므로 픽셀은 항상 다르다. 같은 이유로 **좌표를 그대로 저장하지 않는다.**

    기준 등록                         다음 사이클
    chair ×1  (가운데)      →        chair ×1  (가운데)   변화 없음
    bottle ×1 (오른쪽 위)            —                    반출
    —                                backpack ×1 (왼쪽)   반입

⚠️ **위치는 칸으로 뭉갠다.** FR-8.1 이 요구하는 것은 *"대략 위치"* 다. 픽셀
좌표를 그대로 두면 로봇이 몇 cm 만 달리 서도 전부 다른 값이 되어 비교가
성립하지 않는다. 격자를 성기게 잡는 것이 목적이며 **정밀도가 아니라 재현성을
사는 것**이다.

⚠️ **격자를 잘게 나누면 안 된다.** 3×3 을 6×6 으로 바꾸면 칸이 작아져서 같은
물건이 사이클마다 다른 칸에 잡힌다 — 반출과 반입이 동시에 보고된다. 칸 수는
`config` 에 두되 기본을 성기게 잡는 이유가 이것이다.

⚠️ **`person` 은 기준에 넣지 않는다.** 사람은 지나다니는 것이지 구역에 놓인
물건이 아니다. 기준에 사람이 섞이면 그 사람이 자리를 뜬 것만으로 *"반출"* 이
된다. 사람 출현은 FR-8.4 에서 **즉시** 다루는 별개 경로다.

저장 형식은 JSON 한 벌 + 스냅샷 한 장이며 구역 ID 로 찾는다. 비교(`3.6.2`)와
오검출 억제(`3.6.3`) 는 이 파일에 이어 붙는다 — 지금은 기준을 남기는 데까지다.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from host.common.config import repo_path
from host.vision.detector import Detection

__all__ = [
    "PERSON_LABEL",
    "ObjectEntry",
    "ZoneBaseline",
    "summarize_detections",
    "BaselineStore",
]

#: 기준 목록에서 제외하는 라벨. 위 docstring 참조.
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
    """검출 목록을 *(라벨, 칸)* 별 개수로 뭉갠다.

    같은 라벨이 같은 칸에 둘 있으면 한 줄에 `count=2` 로 모인다. 결과는 **정렬해
    돌려준다** — 검출 순서가 바뀌었다고 기준이 달라 보이면 안 된다.
    """
    width, height = frame_size
    columns, rows = grid
    if width <= 0 or height <= 0:
        raise ValueError("frame_size 는 양수여야 함")
    if columns <= 0 or rows <= 0:
        raise ValueError("grid 는 1 이상이어야 함")

    tally: Counter[tuple[str, int, int]] = Counter()
    for detection in detections:
        if detection.label == PERSON_LABEL:
            continue  # 사람은 놓인 물건이 아니다 — 위 docstring 참조.
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


class BaselineStore:
    """구역 기준을 디스크에 남기고 구역 ID 로 되찾는다.

    한 구역에 기준은 하나다. 다시 등록하면 **덮어쓴다** — 기준이 여러 벌이면
    어느 것과 비교해야 하는지 정할 수 없다.
    """

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

    def _paths(self, zone_id: str) -> tuple[Path, Path]:
        safe = zone_id.strip()
        if not safe or any(character in safe for character in '/\\:*?"<>|' + "\0"):
            raise ValueError(f"구역 ID 로 쓸 수 없는 값: {zone_id!r}")
        return self._dir / f"{safe}.json", self._dir / f"{safe}.jpg"

    def register(
        self,
        zone_id: str,
        detections: Iterable[Detection],
        *,
        frame_size: tuple[int, int],
        now_ms: int,
        jpeg: bytes | None = None,
    ) -> ZoneBaseline:
        """구역 도착 시 기준을 남긴다. **JPEG 는 재인코딩하지 않는다.**"""
        if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
            raise ValueError("now_ms 는 0 이상의 정수여야 함")
        meta_path, image_path = self._paths(zone_id)
        objects = summarize_detections(detections, frame_size=frame_size, grid=self._grid)

        snapshot = None
        if jpeg:
            image_path.write_bytes(jpeg)
            snapshot = image_path.name
        elif image_path.exists():
            # 새 기준에 그림이 없는데 옛 그림을 남겨두면 서로 다른 시점이 한 벌로
            # 보인다. 사람이 그것을 근거로 판단하게 두지 않는다.
            image_path.unlink()

        baseline = ZoneBaseline(
            zone_id=zone_id,
            captured_ms=now_ms,
            frame_size=(int(frame_size[0]), int(frame_size[1])),
            grid=self._grid,
            objects=objects,
            snapshot=snapshot,
        )
        payload = json.dumps(baseline.as_dict(), ensure_ascii=False, indent=2)
        meta_path.write_text(payload + "\n", encoding="utf-8")
        return baseline

    def load(self, zone_id: str) -> ZoneBaseline | None:
        """기준이 없으면 `None`. **없는 것과 비어 있는 것은 다르다** — 물건이
        하나도 없는 구역의 기준은 `objects` 가 빈 튜플이지 `None` 이 아니다.
        """
        meta_path, _ = self._paths(zone_id)
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
