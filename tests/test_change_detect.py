"""구역별 기준 스냅샷·객체 목록 저장 검증 (WBS 3.6.1 · FR-8.1).

완료 기준은 *"구역 도착 시 스냅샷 + 검출 객체 목록(클래스·개수·대략 위치)이
구역 ID 로 저장됨"* 이다. 그래서 보는 것은 셋 — **무엇이 담기는가**, **구역
ID 로 되찾아지는가**, **다음 사이클에 견줄 수 있는 형태인가**.

FR-8.2 의 필수 제약(픽셀 차분 금지)은 저장 형식에서 이미 갈린다. 이 파일은
**이미지가 비교 입력이 아님**을 형식으로 확인한다.

디스크를 쓰므로 `tmp_path` 로 격리한다. 카메라도 로봇도 필요 없다.
"""

from __future__ import annotations

import json

import pytest

from host.behavior.change_detect import (
    BaselineStore,
    ObjectEntry,
    summarize_detections,
)
from host.vision.detector import Detection

FRAME = (640, 480)
GRID = (3, 3)
NOW = 1_700_000_000_000


def _store(tmp_path, *, grid=None) -> BaselineStore:
    section = {"snapshot_dir": str(tmp_path / "snapshots")}
    if grid is not None:
        section["baseline_grid"] = grid
    return BaselineStore({"change_detect": section})


def _det(label: str, cx: float, cy: float, size: float = 40.0) -> Detection:
    half = size / 2
    return Detection(label=label, score=0.9, box=(cx - half, cy - half, cx + half, cy + half))


# ── 목록에 무엇이 담기는가 ───────────────────────────────────────


def test_label_count_and_cell_are_recorded():
    entries = summarize_detections([_det("chair", 320, 240)], frame_size=FRAME, grid=GRID)
    assert entries == (ObjectEntry(label="chair", count=1, cell=(1, 1)),)


def test_same_label_in_the_same_cell_is_counted_together():
    entries = summarize_detections(
        [_det("bottle", 100, 100), _det("bottle", 110, 105)], frame_size=FRAME, grid=GRID
    )
    assert entries == (ObjectEntry(label="bottle", count=2, cell=(0, 0)),)


def test_same_label_in_different_cells_stays_separate():
    entries = summarize_detections(
        [_det("bottle", 100, 100), _det("bottle", 540, 400)], frame_size=FRAME, grid=GRID
    )
    assert [e.cell for e in entries] == [(0, 0), (2, 2)]
    assert all(e.count == 1 for e in entries)


def test_person_is_never_part_of_a_baseline():
    """사람은 놓인 물건이 아니다. 기준에 섞이면 자리를 뜬 것이 반출이 된다."""
    entries = summarize_detections(
        [_det("person", 320, 240), _det("chair", 320, 240)], frame_size=FRAME, grid=GRID
    )
    assert [e.label for e in entries] == ["chair"]


def test_detection_order_does_not_change_the_baseline():
    """검출 순서가 바뀌었다고 기준이 달라 보이면 비교가 성립하지 않는다."""
    a = _det("chair", 100, 100)
    b = _det("bottle", 540, 400)
    assert summarize_detections([a, b], frame_size=FRAME, grid=GRID) == summarize_detections(
        [b, a], frame_size=FRAME, grid=GRID
    )


# ── 위치는 칸으로 뭉갠다 ─────────────────────────────────────────


def test_small_shift_keeps_the_same_cell():
    """**이것이 격자를 두는 이유다.** 로봇이 몇 cm 달리 서도 같은 칸이어야 한다."""
    first = summarize_detections([_det("chair", 320, 240)], frame_size=FRAME, grid=GRID)
    second = summarize_detections([_det("chair", 335, 255)], frame_size=FRAME, grid=GRID)
    assert first == second


def test_corners_land_in_the_expected_cells():
    corners = {
        (10, 10): (0, 0),
        (630, 10): (2, 0),
        (10, 470): (0, 2),
        (630, 470): (2, 2),
    }
    for (x, y), cell in corners.items():
        (entry,) = summarize_detections([_det("chair", x, y)], frame_size=FRAME, grid=GRID)
        assert entry.cell == cell


def test_box_outside_the_frame_is_clamped_not_dropped():
    """버리면 반출로 보인다. 가장자리 칸으로 받는다."""
    entries = summarize_detections([_det("chair", -50, -50)], frame_size=FRAME, grid=GRID)
    assert entries == (ObjectEntry(label="chair", count=1, cell=(0, 0)),)
    entries = summarize_detections([_det("chair", 9999, 9999)], frame_size=FRAME, grid=GRID)
    assert entries == (ObjectEntry(label="chair", count=1, cell=(2, 2)),)


@pytest.mark.parametrize("frame,grid", [((0, 480), GRID), ((640, 0), GRID), (FRAME, (0, 3))])
def test_impossible_geometry_is_rejected(frame, grid):
    with pytest.raises(ValueError):
        summarize_detections([], frame_size=frame, grid=grid)


# ── 구역 ID 로 저장되고 되찾아진다 ───────────────────────────────


def test_baseline_round_trips_by_zone_id(tmp_path):
    store = _store(tmp_path)
    saved = store.register("A", [_det("chair", 320, 240)], frame_size=FRAME, now_ms=NOW)
    assert store.load("A") == saved


def test_unknown_zone_is_none(tmp_path):
    assert _store(tmp_path).load("Z") is None


def test_empty_zone_is_not_the_same_as_missing(tmp_path):
    """물건이 없는 구역의 기준은 빈 목록이지 `None` 이 아니다."""
    store = _store(tmp_path)
    store.register("B", [], frame_size=FRAME, now_ms=NOW)
    loaded = store.load("B")
    assert loaded is not None
    assert loaded.objects == ()


def test_zones_do_not_overwrite_each_other(tmp_path):
    store = _store(tmp_path)
    store.register("A", [_det("chair", 100, 100)], frame_size=FRAME, now_ms=NOW)
    store.register("B", [_det("bottle", 540, 400)], frame_size=FRAME, now_ms=NOW)
    assert [e.label for e in store.load("A").objects] == ["chair"]
    assert [e.label for e in store.load("B").objects] == ["bottle"]
    assert store.zone_ids() == ("A", "B")


def test_re_registering_replaces_the_previous_baseline(tmp_path):
    """기준이 여러 벌이면 어느 것과 비교할지 정할 수 없다."""
    store = _store(tmp_path)
    store.register("A", [_det("chair", 100, 100)], frame_size=FRAME, now_ms=NOW)
    store.register("A", [_det("bottle", 540, 400)], frame_size=FRAME, now_ms=NOW + 1000)
    loaded = store.load("A")
    assert [e.label for e in loaded.objects] == ["bottle"]
    assert loaded.captured_ms == NOW + 1000


@pytest.mark.parametrize("zone_id", ["", "   ", "../escape", "a/b", "a\\b", "a:b"])
def test_unusable_zone_ids_are_rejected(tmp_path, zone_id):
    with pytest.raises(ValueError):
        _store(tmp_path).register(zone_id, [], frame_size=FRAME, now_ms=NOW)


def test_negative_timestamp_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _store(tmp_path).register("A", [], frame_size=FRAME, now_ms=-1)


# ── 스냅샷은 곁들이는 것이지 비교 입력이 아니다 ──────────────────


def test_snapshot_bytes_are_stored_verbatim(tmp_path):
    """재인코딩하면 사람이 보는 근거가 원본과 달라진다."""
    store = _store(tmp_path)
    jpeg = b"\xff\xd8\xff\xe0 not-a-real-jpeg \xff\xd9"
    saved = store.register("A", [], frame_size=FRAME, now_ms=NOW, jpeg=jpeg)
    assert saved.snapshot is not None
    assert (tmp_path / "snapshots" / saved.snapshot).read_bytes() == jpeg


def test_baseline_without_a_snapshot_is_valid(tmp_path):
    saved = _store(tmp_path).register("A", [], frame_size=FRAME, now_ms=NOW)
    assert saved.snapshot is None


def test_stale_snapshot_is_removed_when_the_new_baseline_has_none(tmp_path):
    """다른 시점의 그림과 목록이 한 벌로 보이면 사람이 잘못 판단한다."""
    store = _store(tmp_path)
    store.register("A", [], frame_size=FRAME, now_ms=NOW, jpeg=b"old")
    image = tmp_path / "snapshots" / "A.jpg"
    assert image.exists()
    store.register("A", [], frame_size=FRAME, now_ms=NOW + 1)
    assert not image.exists()
    assert store.load("A").snapshot is None


def test_stored_form_is_an_object_list_not_an_image(tmp_path):
    """FR-8.2 — 비교의 입력이 목록임을 저장 형식으로 확인한다."""
    store = _store(tmp_path)
    store.register("A", [_det("chair", 320, 240)], frame_size=FRAME, now_ms=NOW, jpeg=b"x")
    data = json.loads((tmp_path / "snapshots" / "A.json").read_text(encoding="utf-8"))
    assert data["objects"] == [{"label": "chair", "count": 1, "cell": [1, 1]}]
    # 그림은 파일 이름으로만 참조된다 — 목록 안에 픽셀이 들어가지 않는다.
    assert data["snapshot"] == "A.jpg"


# ── 설정 ─────────────────────────────────────────────────────────


def test_grid_comes_from_config_not_code(tmp_path):
    store = _store(tmp_path, grid=[4, 2])
    assert store.grid == (4, 2)
    (entry,) = summarize_detections([_det("chair", 620, 460)], frame_size=FRAME, grid=store.grid)
    assert entry.cell == (3, 1)


def test_real_config_provides_the_grid(cfg):
    assert tuple(cfg["change_detect"]["baseline_grid"]) == GRID


@pytest.mark.parametrize(
    "section",
    [
        {},
        {"snapshot_dir": ""},
        {"snapshot_dir": "snapshots", "baseline_grid": [3]},
        {"snapshot_dir": "snapshots", "baseline_grid": [0, 3]},
        {"snapshot_dir": "snapshots", "baseline_grid": "3x3"},
    ],
)
def test_bad_config_is_rejected_at_construction(section):
    with pytest.raises(ValueError):
        BaselineStore({"change_detect": section})


def test_missing_section_is_rejected():
    with pytest.raises(ValueError):
        BaselineStore({})


def test_baseline_with_a_different_schema_is_rejected(tmp_path):
    """형식이 바뀐 옛 기준을 조용히 오독하면 반출·반입이 통째로 틀린다."""
    store = _store(tmp_path)
    store.register("A", [], frame_size=FRAME, now_ms=NOW)
    meta = tmp_path / "snapshots" / "A.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["schema"] = 99
    meta.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        store.load("A")
