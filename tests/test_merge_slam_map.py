"""세션 지도 병합 도구 (`tools/lidar/merge_slam_map.py`) — «빠진 벽·물체만 보탠다».

실기·ROS2 없이 닫힌다 — 합성 점유격자를 임시 폴더에 저장하고 도구를 그대로 돌린다.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from host.slam.occupancy import MapMeta, OccupancyGrid
from tools.lidar import merge_slam_map as mm

RES = 0.05
SIZE = 100
WALL = 4.0
FREE = -2.0


def _room(*, offset: int = 0, block: bool = False) -> OccupancyGrid:
    """네 벽이 있는 방 — `offset` 칸 밀면 같은 방이 어긋난 지도가 된다."""
    cells = np.zeros((SIZE, SIZE), dtype=np.float32)
    lo, hi = 20 + offset, 80 + offset
    cells[lo + 1 : hi, lo + 1 : hi] = FREE
    cells[lo, lo : hi + 1] = WALL
    cells[hi, lo : hi + 1] = WALL
    cells[lo : hi + 1, lo] = WALL
    cells[lo : hi + 1, hi] = WALL
    if block:  # 방 한가운데의 3x3 물체 — 운용 지도에는 없는 것
        cells[49:52, 49:52] = WALL
    meta = MapMeta(resolution=RES, origin_x=-2.5, origin_y=-2.5, width=SIZE, height=SIZE)
    return OccupancyGrid(meta, cells)


def _write_maps(tmp_path: Path, target: OccupancyGrid, session: OccupancyGrid) -> tuple[Path, Path]:
    maps = tmp_path / "maps"
    target.save(maps)
    (tmp_path / "slam").mkdir()
    _pgm, yaml_path = session.save_ros2(tmp_path / "slam")
    return maps, yaml_path


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["merge_slam_map", *argv])
    return mm.main()


def test_occupied_points_are_cell_centres_of_occupied_cells() -> None:
    """점유 셀만 골라 셀 중심의 실공간 좌표로 바꾼다 (행 = y, 열 = x)."""
    grid = _room()
    grid.cells[:] = 0
    grid.cells[10, 30] = WALL  # row 10 = y, col 30 = x
    points = mm.occupied_points(grid, 1.0)
    assert points.shape == (1, 2)
    assert points[0] == pytest.approx([-2.5 + 30.5 * RES, -2.5 + 10.5 * RES])


def test_first_fix_returns_first_updated_localization(tmp_path: Path) -> None:
    """갱신되지 않은 측위는 건너뛰고 처음 «갱신된» 자세를 초기값으로 쓴다."""
    events = [
        {"kind": "scan", "t": 0},
        {"kind": "localization", "updated": False, "pose": [9, 9, 9]},
        {"kind": "localization", "updated": True, "pose": [1.0, 2.0, 0.5]},
        {"kind": "localization", "updated": True, "pose": [3.0, 3.0, 3.0]},
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    assert mm.first_fix(tmp_path) == (1.0, 2.0, 0.5)


def test_first_fix_is_none_without_an_updated_localization(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"kind": "localization", "updated": False, "pose": [0, 0, 0]}) + "\n",
        encoding="utf-8",
    )
    assert mm.first_fix(tmp_path) is None


def test_align_recovers_identity_for_the_same_walls() -> None:
    """같은 벽끼리면 거의 제자리에서 정렬률이 높다."""
    target = _room()
    points = mm.occupied_points(_room(), 1.0)
    pose, frac = mm.align(target, points, (0.0, 0.0, 0.0), 1.0, 0.1)
    assert math.hypot(pose[0], pose[1]) < 0.06
    assert frac > 0.8


def test_main_adds_only_the_missing_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """운용 지도에 없는 물체(3x3)만 보태고, 기존 벽·빈 공간은 그대로 둔다."""
    target = _room()
    maps, slam = _write_maps(tmp_path, target, _room(block=True))
    out = tmp_path / "merged.npy"
    rc = _run(
        monkeypatch, "--maps", str(maps), "--slam", str(slam), "--guess", "0,0,0", "--out", str(out)
    )
    assert rc == 0
    merged = np.load(out)
    assert (merged[49:52, 49:52] >= 3.0).all()  # 물체가 들어왔다
    outside = np.ones((SIZE, SIZE), dtype=bool)
    outside[49:52, 49:52] = False
    assert np.array_equal(merged[outside], target.cells[outside])  # 나머지는 불변
    report = json.loads(out.with_suffix(".merge.json").read_text(encoding="utf-8"))
    assert report["added"] == 9
    assert report["applied"] is False
    assert not (maps / "slam_map.orig.npy").exists()
    assert "세션 지도 점유 셀" in capsys.readouterr().out


def test_main_apply_replaces_map_and_backs_up_original_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--apply` 는 운용 지도를 교체하되 원본은 처음 한 번만 백업한다."""
    target = _room()
    maps, slam = _write_maps(tmp_path, target, _room(block=True))
    argv = ("--maps", str(maps), "--slam", str(slam), "--guess", "0,0,0", "--apply")
    assert _run(monkeypatch, *argv) == 0
    backup = maps / "slam_map.orig.npy"
    assert np.array_equal(np.load(backup), target.cells)
    assert (np.load(maps / "slam_map.npy")[49:52, 49:52] >= 3.0).all()
    # 두 번째 적용이 백업(원본)을 덮어쓰지 않는다
    assert _run(monkeypatch, *argv) == 0
    assert np.array_equal(np.load(backup), target.cells)


def test_main_refuses_when_alignment_is_poor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """정렬이 안 맞으면(벽이 좁은 창 밖) 반영하지 않고 1 을 돌려준다 — 벽 두 겹 방지."""
    maps, slam = _write_maps(tmp_path, _room(), _room(offset=-12, block=True))
    out = tmp_path / "merged.npy"
    rc = _run(
        monkeypatch, "--maps", str(maps), "--slam", str(slam), "--guess", "0,0,0", "--out", str(out)
    )
    assert rc == 1
    assert not out.exists()
    assert "반영하지 않는다" in capsys.readouterr().out


def test_main_requires_a_session_or_guess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """초기값(세션 첫 측위·`--guess`)이 없으면 추측하지 않고 멈춘다."""
    maps, slam = _write_maps(tmp_path, _room(), _room())
    with pytest.raises(SystemExit, match="--guess"):
        _run(monkeypatch, "--maps", str(maps), "--slam", str(slam))
