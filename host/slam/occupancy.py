"""점유격자 — 로그오즈 누적 · 자동 확장 · 저장 (FR-6 · Phase 2).

지도는 **로그오즈 격자** 하나다. 셀마다 "여기가 막혀 있다"는 확신의 누적값을
들고 있고, 광선이 지나가면 내려가고 끝점에 닿으면 올라간다. 확률로 저장하지
않는 이유는 갱신이 곱셈이 되어 부동소수 아래로 눌리기 때문이다 — 로그오즈에서는
더하기이고 상·하한만 두면 된다.

**공간 크기를 미리 입력받지 않는다.** 사용자가 로봇을 끌고 다니는 동안 스캔이
격자 경계에 닿으면 그 방향으로 늘린다(`expand`). 시연 공간의 치수를 모르는
상태에서 매핑을 시작해야 하기 때문이다.

⚠️ **저장 산출물은 `maps/` 로 간다** (아키텍처 5절 · `.gitignore` 49행). 지도는
빌드 산출물이라 커밋하지 않는다 — 같은 이유로 `models/` 도 제외되어 있다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

#: 로그오즈 상·하한. 없으면 오래 본 셀의 확신이 무한정 커져서 **새 관측이
#: 지도를 못 고친다** — 치웠는데 벽으로 남아 A* 가 영원히 우회하게 된다.
LOGODDS_MIN: float = -5.0
LOGODDS_MAX: float = 5.0

#: PGM 헤더의 줄바꿈. 상수로 둔 것은 주석 건너뛰기에서만 쓰기 때문이다.
NEWLINE: bytes = bytes([10])


@dataclass
class MapMeta:
    """격자와 실공간을 잇는 값. **지도 파일과 함께 저장되어야 한다.**

    이것 없이 `.npy` 만 남기면 격자는 그냥 숫자 배열이다 — 어느 셀이 실공간의
    어디인지 복원할 방법이 없어서 구역 좌표도 경로도 의미를 잃는다.
    """

    resolution: float  # m / cell
    origin_x: float  # 격자 (0,0) 셀의 실공간 좌표 (m)
    origin_y: float
    width: int  # cells
    height: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def of(cls, raw: dict[str, Any]) -> MapMeta:
        return cls(
            resolution=float(raw["resolution"]),
            origin_x=float(raw["origin_x"]),
            origin_y=float(raw["origin_y"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
        )


class OccupancyGrid:
    """로그오즈 점유격자. 셀 좌표는 `(row, col)` = `(y, x)` 다.

    ⚠️ **행이 y 이고 열이 x 다.** numpy 의 `[row, col]` 관례를 따르는데 실공간은
    `(x, y)` 로 말하므로 두 순서가 뒤집혀 있다. 변환은 `to_cell`·`to_world` 두
    함수만 하며, 나머지 코드는 절대 인덱스를 직접 계산하지 않는다 — 합치기 전
    코드에서 이 계산이 세 파일에 복사되어 있었고, 그중 하나만 고치면 조용히
    어긋난다.
    """

    def __init__(self, meta: MapMeta, cells: np.ndarray | None = None) -> None:
        self.meta = meta
        if cells is None:
            cells = np.zeros((meta.height, meta.width), dtype=np.float32)
        self.cells = cells
        self._sync_meta()

    # ── 생성 ──────────────────────────────────────────────────
    @classmethod
    def blank(cls, *, resolution: float, span_cells: int) -> OccupancyGrid:
        """원점을 중앙에 둔 빈 격자. 로봇이 어느 방향으로 갈지 모르기 때문이다."""
        half = span_cells / 2 * resolution
        return cls(
            MapMeta(
                resolution=resolution,
                origin_x=-half,
                origin_y=-half,
                width=span_cells,
                height=span_cells,
            )
        )

    def _sync_meta(self) -> None:
        self.meta.height, self.meta.width = (int(n) for n in self.cells.shape)

    # ── 좌표 변환 ─────────────────────────────────────────────
    def to_cell(self, x: float, y: float) -> tuple[int, int]:
        col = int(math.floor((x - self.meta.origin_x) / self.meta.resolution))
        row = int(math.floor((y - self.meta.origin_y) / self.meta.resolution))
        return row, col

    def to_world(self, row: int, col: int) -> tuple[float, float]:
        """셀의 **중심** 좌표. 모서리를 쓰면 반 셀만큼 치우친 경로가 나온다."""
        x = self.meta.origin_x + (col + 0.5) * self.meta.resolution
        y = self.meta.origin_y + (row + 0.5) * self.meta.resolution
        return x, y

    def inside(self, row: int, col: int) -> bool:
        height, width = self.cells.shape
        return 0 <= row < height and 0 <= col < width

    @property
    def extent(self) -> list[float]:
        """matplotlib `imshow` 의 `extent`. 시각화가 실좌표로 그리게 한다."""
        height, width = self.cells.shape
        res = self.meta.resolution
        return [
            self.meta.origin_x,
            self.meta.origin_x + width * res,
            self.meta.origin_y,
            self.meta.origin_y + height * res,
        ]

    # ── 확장 ──────────────────────────────────────────────────
    def expand_for(self, xs: np.ndarray, ys: np.ndarray, pad_cells: int) -> None:
        """관측이 격자 밖으로 나가면 그 방향으로 늘린다.

        `pad_cells` 만큼 여유를 함께 붙이는 이유는, 딱 맞게 늘리면 다음 스캔에서
        또 늘려야 해서 매 사이클 배열 복사가 일어나기 때문이다.
        """
        if xs.size == 0:
            return
        res = self.meta.resolution
        height, width = self.cells.shape
        min_col = int(math.floor((float(xs.min()) - self.meta.origin_x) / res))
        max_col = int(math.floor((float(xs.max()) - self.meta.origin_x) / res))
        min_row = int(math.floor((float(ys.min()) - self.meta.origin_y) / res))
        max_row = int(math.floor((float(ys.max()) - self.meta.origin_y) / res))

        pad_left = max(0, pad_cells - min_col) if min_col < 0 else 0
        pad_bottom = max(0, pad_cells - min_row) if min_row < 0 else 0
        pad_right = max(0, max_col - (width - 1) + pad_cells) if max_col > width - 1 else 0
        pad_top = max(0, max_row - (height - 1) + pad_cells) if max_row > height - 1 else 0
        if not (pad_left or pad_right or pad_bottom or pad_top):
            return

        self.cells = np.pad(
            self.cells,
            ((pad_bottom, pad_top), (pad_left, pad_right)),
            constant_values=0.0,
        )
        self.meta.origin_x -= pad_left * res
        self.meta.origin_y -= pad_bottom * res
        self._sync_meta()

    # ── 갱신 ──────────────────────────────────────────────────
    def integrate(
        self,
        origin: tuple[float, float],
        endpoints: np.ndarray,
        *,
        hit: float,
        miss: float,
        pad_cells: int = 0,
    ) -> None:
        """광선을 따라 통과 셀을 내리고 끝점을 올린다.

        ⚠️ **끝점을 먼저 올리고 나중에 통과를 내리면 안 된다.** 이웃한 두 광선의
        끝점이 서로의 경로에 걸리면, 한쪽이 올린 셀을 다른 쪽이 곧바로 내려서
        벽이 얇아진다. 그래서 `bresenham` 의 마지막 셀은 통과에서 제외한다.
        """
        if endpoints.size == 0:
            return
        xs = np.append(endpoints[:, 0], origin[0])
        ys = np.append(endpoints[:, 1], origin[1])
        if pad_cells:
            self.expand_for(xs, ys, pad_cells)

        row0, col0 = self.to_cell(*origin)
        for x, y in endpoints:
            row1, col1 = self.to_cell(float(x), float(y))
            if not self.inside(row1, col1):
                continue
            path = bresenham(row0, col0, row1, col1)
            for row, col in path[:-1]:
                if self.inside(row, col):
                    self.cells[row, col] = max(LOGODDS_MIN, self.cells[row, col] + miss)
            self.cells[row1, col1] = min(LOGODDS_MAX, self.cells[row1, col1] + hit)

    def score(self, points_world: np.ndarray, occ_thresh: float) -> int:
        """점들이 이미 아는 벽 위에 얼마나 얹히는지. 스캔 정합의 점수 함수다."""
        if points_world.size == 0:
            return 0
        height, width = self.cells.shape
        res = self.meta.resolution
        rows = np.floor((points_world[:, 1] - self.meta.origin_y) / res).astype(np.int64)
        cols = np.floor((points_world[:, 0] - self.meta.origin_x) / res).astype(np.int64)
        valid = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
        if not valid.any():
            return 0
        return int(np.count_nonzero(self.cells[rows[valid], cols[valid]] > occ_thresh))

    def known_cells(self, epsilon: float = 0.01) -> int:
        """한 번이라도 관측된 셀 수. 정합을 시작할 만한지 판단할 때 쓴다."""
        return int(np.count_nonzero(np.abs(self.cells) > epsilon))

    # ── 저장 · 적재 ────────────────────────────────────────────
    def save(self, directory: Path, *, stem: str = "slam_map") -> dict[str, Path]:
        """지도를 네 파일로 쓴다 — 작업 형식 한 쌍 + **ROS2 형식 한 쌍**.

        | 파일 | 무엇 | 왜 |
        | :--- | :--- | :--- |
        | `<stem>.npy` | 로그오즈 float32 | **작업 형식.** 이어서 매핑하고 정합하는 데 쓴다 |
        | `map_meta.json` | 해상도·원점·크기 | 위와 한 쌍 |
        | `<stem>.pgm` | 8비트 점유 이미지 | **`maps/README.md` 가 지정한 형식** |
        | `<stem>.yaml` | ROS2 맵 서버 메타 | 위와 한 쌍 |

        ⚠️ **둘 다 쓰는 이유가 있다.** `maps/README.md` 는 이 디렉터리에 ROS2
        occupancy grid(`*.pgm` + `*.yaml`)를 두라고 정해 두었다(ADR-18). 그런데
        **`.pgm` 은 8비트라 로그오즈의 누적 확신을 잃는다** — 그것으로는 다음
        세션에 이어서 매핑할 수 없고, 미관측(0)과 반쯤 관측된 셀을 구분하지
        못해 `planner.inflate` 의 판단(*모르는 곳은 빈 곳이 아니다*)도 못
        내린다. 그래서 작업 형식을 따로 두고, 지정된 형식도 함께 낸다.

        PNG 는 여기서 만들지 않는다 (`viz.py` 소관) — 핵심 경로가 matplotlib 를
        import 하면 그것이 런타임 의존성이 된다.
        """
        directory.mkdir(parents=True, exist_ok=True)
        npy_path = directory / f"{stem}.npy"
        meta_path = directory / "map_meta.json"
        np.save(npy_path, self.cells)
        meta_path.write_text(
            json.dumps(self.meta.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pgm_path, yaml_path = self.save_ros2(directory, stem=stem)
        return {
            "cells": npy_path,
            "meta": meta_path,
            "pgm": pgm_path,
            "yaml": yaml_path,
        }

    def save_ros2(self, directory: Path, *, stem: str = "slam_map") -> tuple[Path, Path]:
        """ROS2 맵 서버 형식으로 쓴다 — `maps/README.md` 가 지정한 형식.

        **의존성을 늘리지 않는다.** PGM(P5)은 헤더 세 줄 + 원시 바이트라
        numpy 로 직접 쓴다. 이미지 라이브러리를 끌어오면 그것이 지도를 저장하는
        모든 경로의 요구사항이 된다.

        규약이 두 가지다 (map_server 기준, `negate: 0`).

        * **픽셀 255 가 자유, 0 이 점유다.** `p = (255 - pixel) / 255`.
        * **첫 행이 가장 위(y 최대)다.** 우리 격자는 행 0 이 y 최소이므로
          **위아래를 뒤집어야 한다.** 뒤집지 않으면 지도가 상하 반전되어
          로드되고, 벽은 그럴듯한데 경로가 전부 틀린다 — 조용한 종류의 오류다.
        """
        pgm_path = directory / f"{stem}.pgm"
        yaml_path = directory / f"{stem}.yaml"

        # 로그오즈 → 확률. 미관측(0)은 정확히 0.5 가 되어 회색으로 남는다.
        probability = 1.0 / (1.0 + np.exp(-self.cells.astype(np.float64)))
        pixels = np.clip(np.rint((1.0 - probability) * 255.0), 0, 255).astype(np.uint8)
        pixels = np.flipud(pixels)  # 첫 행이 y 최대

        height, width = pixels.shape
        with pgm_path.open("wb") as handle:
            handle.write(b"P5\n")
            handle.write(b"# mechadog LiDAR SLAM (host/slam/grid.py)\n")
            handle.write(f"{width} {height}\n255\n".encode("ascii"))
            handle.write(pixels.tobytes())

        # 임계는 map_server 기본값이다. `occupied_logodds`/`free_logodds` 를
        # 여기 옮기지 않는 이유 — 이 파일은 **다른 도구가 읽는 형식**이고, 우리
        # 계획은 `.npy` 를 쓴다. 두 임계를 묶으면 남의 형식이 우리 계획을 정한다.
        yaml_path.write_text(
            "\n".join(
                (
                    f"image: {pgm_path.name}",
                    f"resolution: {self.meta.resolution}",
                    f"origin: [{self.meta.origin_x}, {self.meta.origin_y}, 0.0]",
                    "negate: 0",
                    "occupied_thresh: 0.65",
                    "free_thresh: 0.196",
                    "",
                )
            ),
            encoding="utf-8",
        )
        return pgm_path, yaml_path

    @classmethod
    def load(cls, directory: Path, *, stem: str = "slam_map") -> OccupancyGrid:
        """지도를 읽는다. **두 형식을 모두 받는다.**

        | 우선순위 | 형식 | 누가 만들었나 |
        | :--- | :--- | :--- |
        | ① | `<stem>.npy` + `map_meta.json` | 우리 스캔 정합 (P2 전 잠정) |
        | ② | `<stem>.yaml` + `<stem>.pgm` | **ROS2 `slam_toolbox`** |

        ⚠️ **② 를 읽을 수 있어야 교체가 성립한다.** ADR-9 가 `slam_toolbox` 를
        *"스캔을 넣으면 맵과 위치를 반환하는 블랙박스"* 로 한정했으므로, 그
        블랙박스가 낸 맵을 구역 지정(`3.9.1`)과 경로계획이 **그대로 소비**할 수
        있어야 한다. 그렇지 않으면 P2 에서 이쪽 코드를 다시 손대야 한다.

        ① 을 먼저 보는 이유는 로그오즈를 온전히 갖고 있기 때문이다. 8비트
        이미지로는 누적 확신을 복원할 수 없어 이어서 매핑할 수 없다.
        `slam_toolbox` 만 돌린 경우에는 `.npy` 가 없으므로 자연히 ② 로 간다.
        """
        npy_path = directory / f"{stem}.npy"
        meta_path = directory / "map_meta.json"
        if npy_path.is_file() and meta_path.is_file():
            cells = np.load(npy_path)
            meta = MapMeta.of(json.loads(meta_path.read_text(encoding="utf-8")))
            grid = cls(meta, cells)
            # 저장 당시의 width·height 와 배열이 어긋나면 **배열을 믿는다.**
            # 메타는 사람이 편집할 수 있는 텍스트이고 배열은 아니다.
            grid._sync_meta()
            return grid

        yaml_path = directory / f"{stem}.yaml"
        if yaml_path.is_file():
            return cls.load_ros2(yaml_path)

        raise FileNotFoundError(
            f"지도가 없다: {npy_path} · {yaml_path} — "
            "tools/lidar_slam.py 를 먼저 실행하거나 slam_toolbox 산출물을 두어야 한다"
        )

    @classmethod
    def load_ros2(cls, yaml_path: Path) -> OccupancyGrid:
        """ROS2 맵 서버 형식을 읽는다 — `slam_toolbox` 산출물의 입구.

        ⚠️ **`free_thresh`·`occupied_thresh` 를 반드시 써야 한다.** 픽셀값을
        확률로 바꿔 그대로 로그오즈에 넣으면 **미지 영역이 자유로 둔갑한다.**

        `slam_toolbox`(map_server)의 기본 미지값은 **205** 이고 `negate: 0`
        에서 확률로는 `(255-205)/255 = 0.196` 이다. 그런데 그 값이 하필
        `free_thresh` 기본값 `0.196` 과 같아서, 임계를 쓰지 않으면 미지가
        **자유쪽으로 떨어진다.** 그러면 `planner.inflate` 가 지키는 불변식
        (*"모르는 곳은 빈 곳이 아니다"*)이 깨지고, A* 가 한 번도 관측하지 않은
        공간을 최단 경로로 골라 로봇을 내보낸다.

        그래서 세 갈래로 **분류**한 뒤 로그오즈로 옮긴다. 8비트에서 누적 확신을
        복원할 방법은 없으므로 이 변환은 의도적으로 거칠다 — 확신의 정도가
        아니라 **자유·점유·미지의 구분**만 보존한다.

        | 확률 | 뜻 | 로그오즈 |
        | :--- | :--- | ---: |
        | `> occupied_thresh` | 점유 | `LOGODDS_MAX` |
        | `< free_thresh` | 자유 | `LOGODDS_MIN` |
        | 그 사이 | **미지** | `0` |
        """
        spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError(f"맵 yaml 최상위가 매핑이 아님: {yaml_path}")
        for key in ("image", "resolution", "origin"):
            if key not in spec:
                raise ValueError(f"맵 yaml 에 {key} 가 없음: {yaml_path}")

        image_path = yaml_path.parent / str(spec["image"])
        pixels = read_pgm(image_path)

        negate = bool(int(spec.get("negate", 0)))
        # `negate: 1` 이면 픽셀이 그대로 점유 확률이다 (map_server 규약).
        probability = pixels / 255.0 if negate else (255.0 - pixels) / 255.0
        occupied_thresh = float(spec.get("occupied_thresh", 0.65))
        free_thresh = float(spec.get("free_thresh", 0.196))

        cells = np.zeros_like(probability, dtype=np.float32)
        cells[probability > occupied_thresh] = LOGODDS_MAX
        cells[probability < free_thresh] = LOGODDS_MIN
        # 그 사이는 0 으로 남는다 = 미관측. `inflate` 가 통행 불가로 본다.

        # ROS2 는 첫 행이 y 최대다. 우리 격자는 행 0 이 y 최소이므로 되뒤집는다.
        cells = np.flipud(cells)

        origin = spec["origin"]
        if not isinstance(origin, list | tuple) or len(origin) < 2:
            raise ValueError(f"origin 은 [x, y, yaw] 여야 함: {yaml_path}")
        if len(origin) >= 3 and abs(float(origin[2])) > 1e-6:
            # 회전된 원점은 지원하지 않는다. 조용히 무시하면 지도 전체가
            # 어긋난 채로 순찰이 돌아간다 — 그것보다 기동을 막는 것이 낫다.
            raise ValueError(
                f"origin yaw 가 0 이 아님({origin[2]}) — 회전된 맵 원점은 지원하지 않는다"
            )

        height, width = cells.shape
        meta = MapMeta(
            resolution=float(spec["resolution"]),
            origin_x=float(origin[0]),
            origin_y=float(origin[1]),
            width=width,
            height=height,
        )
        return cls(meta, cells)


def bresenham(row0: int, col0: int, row1: int, col1: int) -> list[tuple[int, int]]:
    """두 셀을 잇는 경로. 마지막 원소가 끝점이다."""
    cells: list[tuple[int, int]] = []
    d_row, d_col = abs(row1 - row0), abs(col1 - col0)
    step_row = 1 if row1 > row0 else -1
    step_col = 1 if col1 > col0 else -1
    err = d_row - d_col
    row, col = row0, col0
    while True:
        cells.append((row, col))
        if row == row1 and col == col1:
            return cells
        err2 = 2 * err
        if err2 > -d_col:
            err -= d_col
            row += step_row
        if err2 < d_row:
            err += d_row
            col += step_col


def read_pgm(path: Path) -> np.ndarray:
    """PGM(P5) 을 읽어 `uint8` 배열로 돌려준다. **첫 행이 파일의 첫 행이다.**

    이미지 라이브러리를 쓰지 않는 이유는 저장할 때와 같다 — 지도를 읽는 모든
    경로에 의존성이 하나 붙는다. PGM 은 헤더 네 토큰 + 원시 바이트라 직접
    읽는 것이 어렵지 않다.

    ⚠️ **주석은 헤더 어디에나 올 수 있다.** `slam_toolbox` 는 넣지 않지만
    다른 도구가 넣으므로, 토큰을 세면서 `#` 부터 줄 끝까지 건너뛴다. 고정
    오프셋으로 읽으면 그런 파일에서 조용히 어긋난 배열이 나온다.
    """
    raw = path.read_bytes()
    if not raw.startswith(b"P5"):
        raise ValueError(f"P5(PGM 이진) 형식이 아님: {path}")

    tokens: list[bytes] = []
    index = 2
    while len(tokens) < 3:
        while index < len(raw) and raw[index : index + 1].isspace():
            index += 1
        if raw[index : index + 1] == b"#":
            while index < len(raw) and raw[index : index + 1] != NEWLINE:
                index += 1
            continue
        start = index
        while index < len(raw) and not raw[index : index + 1].isspace():
            index += 1
        tokens.append(raw[start:index])
    index += 1  # 최대값 뒤의 공백 한 바이트가 화소의 시작을 가른다

    width, height, maxval = (int(t) for t in tokens)
    if maxval != 255:
        raise ValueError(f"8비트 PGM 만 지원한다 (maxval={maxval}): {path}")
    expected = width * height
    body = raw[index : index + expected]
    if len(body) != expected:
        raise ValueError(f"화소 수가 헤더와 다름 ({len(body)} != {expected}): {path}")
    return np.frombuffer(body, dtype=np.uint8).reshape(height, width)
