"""점유격자 — 로그오즈 누적 · 자동 확장 · 저장 (FR-6 · Phase 2).

지도는 **로그오즈 격자** 하나다. 셀마다 "여기가 막혀 있다"는 확신의 누적값을
들고 있고, 광선이 지나가면 내려가고 끝점에 닿으면 올라간다. 확률로 저장하지
않는 이유는 갱신이 곱셈이 되어 부동소수 아래로 눌리기 때문이다 — 로그오즈에서는
더하기이고 상·하한만 두면 된다.

**공간 크기를 미리 입력받지 않는다.** 사용자가 로봇을 끌고 다니는 동안 스캔이
격자 경계에 닿으면 그 방향으로 늘린다(`expand`). 시연 공간의 치수를 모르는
상태에서 매핑을 시작해야 하기 때문이다.

저장 산출물은 `maps/` 로 간다(빌드 산출물이라 커밋하지 않는다 · 아키텍처 5절).
"""

from __future__ import annotations

import json
import math
import zipfile
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import numpy as np
import yaml

from host.common.config import _finite_number

#: 로그오즈 상·하한 — 오래 본 셀도 새 관측이 고칠 수 있게 한다.
LOGODDS_MIN: float = -5.0
LOGODDS_MAX: float = 5.0

#: PGM 헤더의 줄바꿈. 상수로 둔 것은 주석 건너뛰기에서만 쓰기 때문이다.
NEWLINE: bytes = bytes([10])


@dataclass
class MapMeta:
    """격자와 실공간을 잇는 값. 지도 파일과 함께 저장한다."""

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
    def of(cls, raw: dict[str, Any], source: Path | str = "map_meta.json") -> MapMeta:
        if not isinstance(raw, dict):
            raise ValueError(f"지도 메타가 객체가 아님: {source}")
        for key in ("resolution", "origin_x", "origin_y", "width", "height"):
            if key not in raw:
                raise ValueError(f"지도 메타에 {key} 가 없음: {source}")
        return cls(
            resolution=_positive(_number(raw["resolution"], "resolution", source), source),
            origin_x=_number(raw["origin_x"], "origin_x", source),
            origin_y=_number(raw["origin_y"], "origin_y", source),
            width=int(_number(raw["width"], "width", source)),
            height=int(_number(raw["height"], "height", source)),
        )


class OccupancyGrid:
    """로그오즈 점유격자. 셀 좌표는 `(row, col)` = `(y, x)` 다.

    행이 y, 열이 x 다(실공간 `(x, y)` 와 순서가 반대). 변환은 `to_cell`·`to_world` 만 하고
    다른 코드는 인덱스를 직접 계산하지 않는다.
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

    # ── 스냅샷 ────────────────────────────────────────────────
    def snapshot(self) -> tuple[np.ndarray, MapMeta]:
        """셀·메타의 독립 사본 — 검증 안 된 적분을 되돌릴 기준점으로 쓴다."""
        return self.cells.copy(), replace(self.meta)

    def restore(self, snapshot: tuple[np.ndarray, MapMeta]) -> None:
        """`snapshot` 상태로 되돌린다. 그 뒤 자란 영역(원점 이동)도 함께 되돌아간다."""
        cells, meta = snapshot
        self.cells = cells.copy()
        self.meta.origin_x, self.meta.origin_y = meta.origin_x, meta.origin_y
        self._sync_meta()

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
        return cast(bool, 0 <= row < height and 0 <= col < width)

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

        `pad_cells` 만큼 여유를 붙여 매 사이클 배열 복사를 피한다.
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

        `bresenham` 의 마지막 셀(끝점)은 통과에서 제외한다 — 이웃 광선이 벽을 얇게 깎지 않게.
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

    # ── 우도장 (likelihood field) ─────────────────────────────
    def likelihood_field(self, occ_thresh: float, sigma_m: float) -> np.ndarray:
        """셀마다 «가장 가까운 벽까지의 거리» 로 매긴 점수 `exp(-d²/2σ²)` 의 격자.

        적중 개수 점수(`score`)는 점이 벽 셀 **위에** 있어야만 1 이고 한 셀(5cm) 비껴가면
        0 이다 — 점수 지형이 계단식이라 가짜 봉우리에 갇히고, 1셀 두께 물체(가구 다리)는
        정확한 자세에서만 점수가 난다. 거리 점수는 벽 근처에서 매끄럽게 줄어 봉우리가
        하나로 모인다(Thrun·Burgard·Fox 《Probabilistic Robotics》 6.4).

        거리는 반경 1·2·…·R 셀 원판 팽창으로 셀 단위로 양자화한다(scipy 없이). 지도 내용의
        지문(합·점유 수·모양)이 같으면 캐시를 돌려준다 — 정합 한 번에 한 번만 계산된다.
        """
        res = self.meta.resolution
        radius_cells = max(1, int(math.ceil(3.0 * sigma_m / res)))
        occupied = self.cells > occ_thresh
        # 지문은 **배치까지** 본다 — 합·점유 수만 보면 같은 수의 장애물이 옮겨져도 옛 장을
        # 재사용한다 (Codex 검토 P2). crc32 는 이 크기(수만 셀)에서 0.1ms 안쪽이다.
        fingerprint = (
            self.cells.shape,
            zlib.crc32(np.ascontiguousarray(self.cells).tobytes()),
            round(occ_thresh, 6),
            round(sigma_m, 6),
        )
        cached = getattr(self, "_lf_cache", None)
        if cached is not None and cached[0] == fingerprint:
            return cast(np.ndarray, cached[1])
        distance = np.full(self.cells.shape, np.inf, dtype=np.float32)
        distance[occupied] = 0.0
        covered = occupied.copy()
        for k in range(1, radius_cells + 1):
            ring = _disk_dilate(occupied, k) & ~covered
            distance[ring] = k * res
            covered |= ring
        field = np.exp(-(distance**2) / (2.0 * sigma_m * sigma_m)).astype(np.float32)
        field[~covered] = 0.0
        self._lf_cache = (fingerprint, field)
        return field

    def score_field(self, points_world: np.ndarray, field: np.ndarray) -> float:
        """우도장 점수 — 점마다 0~1, 합이 «설명된 점의 수» 에 해당한다. 격자 밖은 0."""
        if points_world.size == 0:
            return 0.0
        height, width = field.shape
        res = self.meta.resolution
        rows = np.floor((points_world[:, 1] - self.meta.origin_y) / res).astype(np.int64)
        cols = np.floor((points_world[:, 0] - self.meta.origin_x) / res).astype(np.int64)
        valid = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
        if not valid.any():
            return 0.0
        return float(field[rows[valid], cols[valid]].sum())

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

        8비트 `.pgm` 은 로그오즈의 누적 확신을 잃으므로 작업 형식을 따로 둔다. PNG 는 `viz.py`
        소관이다.
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

        PGM(P5)은 numpy 로 직접 쓴다. map_server 규약(`negate: 0`)을 따른다 — 픽셀 255 가
        자유·0 이 점유(`p = (255 - pixel) / 255`)이고, 첫 행이 y 최대라 위아래를 뒤집는다.
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

        # 임계는 map_server 기본값이다 — 우리 계획 임계(`occupied_logodds` 등)와 묶지 않는다.
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

        ② 를 그대로 소비할 수 있어야 `slam_toolbox` 교체가 성립한다 (ADR-9). ① 은 로그오즈를
        온전히 가지므로 먼저 본다.
        """
        npy_path = directory / f"{stem}.npy"
        meta_path = directory / "map_meta.json"
        if npy_path.is_file() and meta_path.is_file():
            try:
                cells = np.load(npy_path)
            except zipfile.BadZipFile as exc:  # 깨진 zip 은 `ValueError` 가 아니다
                raise ValueError(f"지도 배열을 읽을 수 없음: {npy_path}") from exc
            if not isinstance(cells, np.ndarray):  # zip 서명으로 시작하면 `NpzFile` 이 온다
                raise ValueError(f"지도 배열이 아님: {npy_path}")
            meta = MapMeta.of(json.loads(meta_path.read_text(encoding="utf-8")), meta_path)
            grid = cls(meta, cells)
            # 저장 당시의 width·height 와 배열이 어긋나면 배열을 믿는다.
            grid._sync_meta()
            return grid

        yaml_path = directory / f"{stem}.yaml"
        if yaml_path.is_file():
            return cls.load_ros2(yaml_path)

        raise FileNotFoundError(
            f"지도가 없다: {npy_path} · {yaml_path} — "
            "tools/lidar/lidar_slam.py 를 먼저 실행하거나 slam_toolbox 산출물을 두어야 한다"
        )

    @classmethod
    def load_ros2(cls, yaml_path: Path) -> OccupancyGrid:
        """ROS2 맵 서버 형식을 읽는다 — `slam_toolbox` 산출물의 입구.

        ⚠️ `free_thresh`·`occupied_thresh` 로 자유·점유·미지를 분류한 뒤 옮긴다 — map_server 기본
        미지값 205 의 확률(0.196)이 `free_thresh` 기본값과 같아, 확률을 그대로 넣으면 관측하지
        않은 공간이 통행 가능으로 새어 A* 가 로봇을 그리로 보낸다.

        | 확률 | 뜻 | 로그오즈 |
        | :--- | :--- | ---: |
        | `> occupied_thresh` | 점유 | `LOGODDS_MAX` |
        | `< free_thresh` | 자유 | `LOGODDS_MIN` |
        | 그 사이 | **미지** | `0` |
        """
        try:
            spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # `YAMLError` 는 `ValueError` 가 아니다
            raise ValueError(f"맵 yaml 파싱 실패: {yaml_path}") from exc
        if not isinstance(spec, dict):
            raise ValueError(f"맵 yaml 최상위가 매핑이 아님: {yaml_path}")
        for key in ("image", "resolution", "origin"):
            if key not in spec:
                raise ValueError(f"맵 yaml 에 {key} 가 없음: {yaml_path}")

        image_path = yaml_path.parent / str(spec["image"])
        pixels = read_pgm(image_path)

        negate_raw = spec.get("negate", 0)
        negate = (
            negate_raw
            if isinstance(negate_raw, bool)
            else bool(int(_number(negate_raw, "negate", yaml_path)))
        )
        # `negate: 1` 이면 픽셀이 그대로 점유 확률이다 (map_server 규약).
        probability = pixels / 255.0 if negate else (255.0 - pixels) / 255.0
        occupied_thresh = _number(spec.get("occupied_thresh", 0.65), "occupied_thresh", yaml_path)
        free_thresh = _number(spec.get("free_thresh", 0.196), "free_thresh", yaml_path)

        cells = np.zeros_like(probability, dtype=np.float32)
        cells[probability > occupied_thresh] = LOGODDS_MAX
        cells[probability < free_thresh] = LOGODDS_MIN
        # 그 사이는 0 으로 남는다 = 미관측. `inflate` 가 통행 불가로 본다.

        # ROS2 는 첫 행이 y 최대다. 우리 격자는 행 0 이 y 최소이므로 되뒤집는다.
        cells = np.flipud(cells)

        origin = spec["origin"]
        if not isinstance(origin, list | tuple) or len(origin) < 2:
            raise ValueError(f"origin 은 [x, y, yaw] 여야 함: {yaml_path}")
        origin_xy = [_number(v, f"origin[{i}]", yaml_path) for i, v in enumerate(origin[:2])]
        if len(origin) >= 3 and abs(_number(origin[2], "origin[2]", yaml_path)) > 1e-6:
            # 회전된 원점은 지원하지 않는다 — 무시하지 않고 거부한다.
            raise ValueError(
                f"origin yaw 가 0 이 아님({origin[2]}) — 회전된 맵 원점은 지원하지 않는다"
            )

        height, width = cells.shape
        meta = MapMeta(
            resolution=_positive(_number(spec["resolution"], "resolution", yaml_path), yaml_path),
            origin_x=origin_xy[0],
            origin_y=origin_xy[1],
            width=width,
            height=height,
        )
        return cls(meta, cells)


def _number(value: Any, key: str, source: Path | str) -> float:
    """지도 파일의 숫자 값 — `null`·문자열·거대한 정수는 `TypeError`·`OverflowError` 대신 `ValueError`."""
    if not _finite_number(value):
        raise ValueError(f"{key} 는 유한한 수여야 함: {source}")
    return float(value)


def _positive(value: float, source: Path | str) -> float:
    """해상도 — 0 은 `to_cell` 의 나눗셈에서 죽고 음수는 기하를 조용히 뒤집는다."""
    if value <= 0:
        raise ValueError(f"resolution 은 0 보다 커야 함: {source}")
    return value


def _disk_dilate(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """원판 커널 팽창 — `planner.dilate` 와 같은 잘라 붙이기 방식(감김 없음)."""
    out = mask.copy()
    height, width = mask.shape
    for d_row in range(-radius_cells, radius_cells + 1):
        for d_col in range(-radius_cells, radius_cells + 1):
            if d_row * d_row + d_col * d_col > radius_cells * radius_cells:
                continue
            src_rows = slice(max(0, -d_row), height - max(0, d_row))
            src_cols = slice(max(0, -d_col), width - max(0, d_col))
            dst_rows = slice(max(0, d_row), height - max(0, -d_row))
            dst_cols = slice(max(0, d_col), width - max(0, -d_col))
            out[dst_rows, dst_cols] |= mask[src_rows, src_cols]
    return out


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

    헤더 주석(`#` 부터 줄 끝)은 헤더 어디에 와도 건너뛴다.
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
