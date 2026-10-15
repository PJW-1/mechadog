"""Marker refinement uses synthetic maps and scans, never private recordings."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from host.common.lidar_link import encode_scan
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.scan_match import MatchResult
from tools.maps import refine_from_markers as markers


def config() -> dict[str, Any]:
    return {
        "lidar": {
            "resolution_mm": 50,
            "initial_span_cells": 40,
            "mount_yaw_deg": 0,
            "angle_direction": 1,
            "range_min_mm": 100,
            "range_max_mm": 8000,
            "hit_logodds": 0.85,
            "miss_logodds": -0.4,
            "expand_pad_cells": 4,
            "search_span_ratio": 1.0,
            "search_step_mm": 50,
            "search_angle_deg": 10,
            "search_angle_step_deg": 5,
            "occupied_logodds": 0.5,
            "free_logodds": -0.5,
            "min_known_cells": 1,
        },
        "localization": {"move_increment_mm": 100},
    }


def interval(start: int = 1000, end: int = 2000, **changes: Any) -> dict[str, Any]:
    value = {"start_ms": start, "end_ms": end, "x": 0.0, "y": 0.0, "yaw": 0.0}
    value.update(changes)
    return value


def scan_event(
    timestamp: int,
    seq: int,
    *,
    angles: range = range(360),
    device: str = "lidar-test",
) -> dict[str, Any]:
    return {
        "kind": "scan",
        "t": timestamp,
        "raw": encode_scan(
            seq=seq,
            ts_ms=1000 + seq,
            device_id=device,
            boot_id="synthetic-boot",
            points_wire=[[angle + 0.5, 1000] for angle in angles],
        ),
    }


def write_events(session: Path, events: list[dict[str, Any]]) -> None:
    session.mkdir(exist_ok=True)
    (session / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def write_truth(path: Path, intervals: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(
            {
                "frame_id": "patrol",
                "pose_origin": "lidar_centre",
                "yaw_unit": "radians",
                "intervals": intervals,
            }
        ),
        encoding="utf-8",
    )


def inputs(tmp_path: Path, *, cells: int = 40) -> dict[str, Path]:
    source = tmp_path / "source"
    source.mkdir()
    grid = OccupancyGrid.blank(resolution=0.05, span_cells=cells)
    grid.cells.fill(-2)
    grid.save(source)
    localization = np.zeros((cells, cells), dtype=np.float32)
    localization[0, :] = 2
    localization[:, -1] = -3
    np.save(source / "slam_map_loc.npy", localization)
    (source / "provenance.json").write_text('{"source": "synthetic"}\n', encoding="utf-8")
    truth = tmp_path / "truth.json"
    write_truth(truth, [interval()])
    rectangles = tmp_path / "rectangles.json"
    rectangles.write_text(json.dumps([[-0.225, -0.2, 0.25, 0.225]]), encoding="utf-8")
    session = tmp_path / "session"
    write_events(session, [scan_event(1100, 1), scan_event(1200, 2)])
    return {
        "session": session,
        "map_directory": source,
        "truth_path": truth,
        "rectangles_path": rectangles,
        "output": tmp_path / "candidate",
    }


def file_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_interval_schema_preserves_measured_radians():
    values = [interval(yaw=math.pi / 2), interval(3000, 4000, x=0.3)]
    parsed = markers.parse_intervals(
        {
            "frame_id": "patrol",
            "pose_origin": "lidar_centre",
            "yaw_unit": "radians",
            "intervals": values,
        }
    )
    assert parsed == values
    assert markers.parse_intervals(values) == values


@pytest.mark.parametrize(
    "values",
    [
        [],
        [interval(end=999)],
        [interval(start=True)],
        [interval(start=1000.5)],
        [interval(x=float("nan"))],
        [interval(y=float("inf"))],
        [interval(yaw=float("nan"))],
        [interval(), interval(1900, 3000)],
        [interval(3000, 4000), interval()],
    ],
)
def test_invalid_truth_is_rejected(values):
    with pytest.raises(ValueError):
        markers.parse_intervals(values)


@pytest.mark.parametrize(
    "metadata",
    [{"pose_origin": "base_link"}, {"yaw_unit": "degrees"}],
)
def test_incompatible_pose_units_and_origins_are_rejected(metadata):
    value = {
        "frame_id": "patrol",
        "pose_origin": "lidar_centre",
        "yaw_unit": "radians",
        "intervals": [interval()],
        **metadata,
    }
    with pytest.raises(ValueError):
        markers.parse_intervals(value)


def test_rectangle_mask_excludes_cells_crossing_any_boundary():
    grid = OccupancyGrid(MapMeta(0.1, -0.2, -0.3, 6, 5))
    mask = markers.rectangle_mask(grid, [(-0.15, -0.25, 0.2, 0.1), (0.2, 0.1, 0.4, 0.2)])
    expected = np.zeros((5, 6), dtype=bool)
    expected[1:4, 1:4] = True
    expected[4, 4:6] = True
    np.testing.assert_array_equal(mask, expected)


def test_expanded_grid_is_cropped_by_world_coordinates_before_merging():
    original = OccupancyGrid(
        MapMeta(0.1, -0.2, -0.3, 5, 4), np.arange(20, dtype=np.float32).reshape(4, 5)
    )
    updated = OccupancyGrid(
        MapMeta(0.1, -0.5, -0.7, 13, 12),
        np.arange(156, dtype=np.float32).reshape(12, 13) + 100,
    )
    before = original.cells.copy()
    mask = np.zeros((4, 5), dtype=bool)
    mask[1:3, 2:4] = True
    merged = markers.merge_refined(original, updated, mask)
    expected = before.copy()
    expected[mask] = updated.cells[4:8, 3:8][mask]
    np.testing.assert_array_equal(merged.cells, expected)
    np.testing.assert_array_equal(original.cells, before)
    assert merged.meta.as_dict() == original.meta.as_dict()
    assert merged.cells.dtype == original.cells.dtype


@pytest.mark.parametrize("cells", [20, 40])
def test_real_scan_refinement_preserves_originals_navigation_and_outside_cells(tmp_path, cells):
    paths = inputs(tmp_path, cells=cells)
    before = file_bytes(paths["map_directory"])
    report = markers.refine(**paths, config=config(), lidar_device="lidar-test")
    assert report["accepted_revolutions"] == 2
    assert report["intervals"][0]["accepted_revolutions"] == 2
    candidate = np.load(paths["output"] / "slam_map_loc.npy")
    original = np.load(paths["map_directory"] / "slam_map_loc.npy")
    assert candidate.shape == original.shape
    assert candidate.dtype == original.dtype
    meta = json.loads((paths["map_directory"] / "map_meta.json").read_text(encoding="utf-8"))
    grid = OccupancyGrid(MapMeta.of(meta), original)
    rectangles = json.loads(paths["rectangles_path"].read_text(encoding="utf-8"))
    mask = markers.rectangle_mask(grid, rectangles)
    np.testing.assert_array_equal(candidate[~mask], original[~mask])
    assert np.any(candidate[mask] != original[mask])
    assert report["changed_cells"] == int(np.count_nonzero(candidate != original))
    assert report["cells_sign_changed"] == int(
        np.count_nonzero(np.sign(candidate) != np.sign(original))
    )
    assert file_bytes(paths["map_directory"]) == before
    for name, contents in before.items():
        if name != "slam_map_loc.npy":
            assert (paths["output"] / name).read_bytes() == contents
    saved = json.loads((paths["output"] / markers.REPORT_NAME).read_text(encoding="utf-8"))
    assert saved == report


@pytest.mark.parametrize("kind", ["wrong_device", "partial_revolution", "outside_interval"])
def test_no_accepted_scans_leaves_no_candidate_directory(tmp_path, kind):
    paths = inputs(tmp_path)
    event = {
        "wrong_device": scan_event(1100, 1, device="another-lidar"),
        "partial_revolution": scan_event(1100, 1, angles=range(180)),
        "outside_interval": scan_event(2100, 1),
    }[kind]
    write_events(paths["session"], [event])
    before = file_bytes(paths["map_directory"])
    with pytest.raises(ValueError):
        markers.refine(**paths, config=config(), lidar_device="lidar-test")
    assert not paths["output"].exists()
    assert file_bytes(paths["map_directory"]) == before


def test_partial_revolutions_cannot_cross_distinct_stationary_windows(tmp_path):
    paths = inputs(tmp_path)
    write_truth(paths["truth_path"], [interval(1000, 1100), interval(1200, 1300)])
    write_events(
        paths["session"],
        [scan_event(1050, 1, angles=range(180)), scan_event(1250, 2, angles=range(180, 360))],
    )
    with pytest.raises(ValueError):
        markers.refine(**paths, config=config(), lidar_device="lidar-test")
    assert not paths["output"].exists()


def test_duplicate_sequence_is_not_reused_after_window_changes(tmp_path):
    paths = inputs(tmp_path)
    write_truth(paths["truth_path"], [interval(), interval(3000, 4000)])
    write_events(paths["session"], [scan_event(1100, 1), scan_event(3100, 1)])
    report = markers.refine(**paths, config=config(), lidar_device="lidar-test")
    assert report["accepted_revolutions"] == 1
    assert [item["accepted_revolutions"] for item in report["intervals"]] == [1, 0]


def test_reversed_scan_times_are_rejected_before_writing(tmp_path):
    paths = inputs(tmp_path)
    write_events(paths["session"], [scan_event(1200, 1), scan_event(1100, 2)])
    with pytest.raises(ValueError):
        markers.refine(**paths, config=config(), lidar_device="lidar-test")
    assert not paths["output"].exists()


def test_distinct_packets_with_the_same_receive_millisecond_are_valid(tmp_path):
    paths = inputs(tmp_path)
    write_events(paths["session"], [scan_event(1100, 1), scan_event(1100, 2)])
    report = markers.refine(**paths, config=config(), lidar_device="lidar-test")
    assert report["accepted_revolutions"] == 2


@pytest.mark.parametrize("destination", ["source", "nested_source", "session", "existing"])
def test_output_must_be_new_and_separate_from_inputs(tmp_path, destination):
    paths = inputs(tmp_path)
    if destination == "source":
        paths["output"] = paths["map_directory"]
    elif destination == "nested_source":
        paths["output"] = paths["map_directory"] / "nested"
    elif destination == "session":
        paths["output"] = paths["session"] / "nested"
    else:
        paths["output"].mkdir()
        (paths["output"] / "keep.txt").write_text("preserve", encoding="utf-8")
    before = file_bytes(tmp_path)
    with pytest.raises((ValueError, FileExistsError)):
        markers.refine(**paths, config=config(), lidar_device="lidar-test")
    assert file_bytes(tmp_path) == before


def test_validation_compares_both_maps_and_reports_errors_peers_and_circularity(
    tmp_path, monkeypatch
):
    paths = inputs(tmp_path)
    write_truth(paths["truth_path"], [interval(yaw=math.radians(179))])
    calls = []

    def result(grid, points, mode):
        refined = grid.cells[20, 20] < 0
        calls.append((refined, mode, len(points)))
        return MatchResult(
            (0.006, 0.008, math.radians(179)) if refined else (0.03, 0.04, math.radians(-179)),
            270 if refined else 180,
            peers=1 if refined else 5,
            competing_peaks=0 if refined else 2,
        )

    def local(grid, points, pose, _params):
        assert pose[1] == 0
        assert pose[2] == pytest.approx(math.radians(179))
        assert pose[0] in (0, 0.5)
        return result(grid, points, "local")

    def global_fit(grid, points, **_kwargs):
        return result(grid, points, "global")

    monkeypatch.setattr(markers, "match", local)
    monkeypatch.setattr(markers, "global_match", global_fit)
    report = markers.refine(
        **paths, config=config(), lidar_device="lidar-test", validate=True, max_revolutions=1
    )
    validation = report["validation"]
    assert validation["revolutions_per_interval"] == [1]
    assert validation["intervals"] == [interval(yaw=math.radians(179))]
    assert validation["input_sha256"] == {
        "events": hashlib.sha256((paths["session"] / "events.jsonl").read_bytes()).hexdigest(),
        "truth": hashlib.sha256(paths["truth_path"].read_bytes()).hexdigest(),
    }
    assert validation["training_overlap"] is True
    assert validation["independent_accuracy_verified"] is False
    assert any("circular" in item.lower() for item in validation["warnings"])
    assert len(calls) == 6
    assert sum(mode == "local" for _, mode, _ in calls) == 4
    assert all(size == 360 for _, _, size in calls)
    assert len(validation["rows"]) == 6
    assert {row["mode"] for row in validation["rows"]} == {
        "local_truth",
        "local_offset_x_0.5m",
        "global",
    }
    for row in validation["rows"]:
        refined = row["map"] == "refined"
        assert row["status"] == "matched"
        assert row["xy_error_m"] == pytest.approx(0.01 if refined else 0.05)
        assert row["yaw_error_deg"] == pytest.approx(0 if refined else 2)
        if row["mode"] == "global":
            assert row["peers"] == (1 if refined else 5)
            assert row["peer_diagnostic"] == "legacy_global_peers"
        else:
            assert row["peers"] is None
            assert row["peer_diagnostic"] == "not_computed_locally"
        assert row["competing_peaks"] is None
        assert row["search_complete"] is None
        assert row["score_fraction"] == pytest.approx(0.75 if refined else 0.5)
    saved = json.loads((paths["output"] / markers.REPORT_NAME).read_text(encoding="utf-8"))
    assert saved["validation"] == validation


@pytest.mark.parametrize("result", [None, MatchResult((0, 0, 0), 0, skipped=True)])
def test_missing_or_skipped_fit_is_not_reported_as_zero_error(result):
    metrics = markers.fit_metrics(result, interval(), 360)
    assert metrics["status"] == "no_match"
    assert metrics["xy_error_m"] is None
    assert metrics["yaw_error_deg"] is None
    assert metrics["peers"] is None


def test_validation_warns_when_training_provenance_is_unknown(tmp_path, monkeypatch):
    paths = inputs(tmp_path)
    grid = OccupancyGrid.blank(resolution=0.05, span_cells=40)
    monkeypatch.setattr(markers, "match", lambda *_args: MatchResult((0, 0, 0), 0, skipped=True))
    monkeypatch.setattr(markers, "global_match", lambda *_args, **_kwargs: None)
    report = markers.validate_maps(
        paths["session"], grid, grid, paths["truth_path"], config(), "lidar-test", max_revolutions=1
    )
    assert report["training_overlap"] is None
    assert all(row["status"] == "no_match" for row in report["rows"])
    assert any("provenance" in item.lower() for item in report["warnings"])
    assert any("circular" in item.lower() for item in report["warnings"])


def test_real_matchers_recover_synthetic_room_after_refinement(tmp_path):
    paths = inputs(tmp_path)
    np.save(paths["map_directory"] / "slam_map_loc.npy", np.zeros((40, 40), dtype=np.float32))
    paths["rectangles_path"].write_text(json.dumps([[-1, -1, 1, 1]]), encoding="utf-8")
    report = markers.refine(
        **paths, config=config(), lidar_device="lidar-test", validate=True, max_revolutions=1
    )
    validation = report["validation"]
    assert validation["revolutions_per_interval"] == [1]
    assert len(validation["rows"]) == 6
    before = [row for row in validation["rows"] if row["map"] == "original"]
    after = [row for row in validation["rows"] if row["map"] == "refined"]
    assert all(row["status"] == "no_match" for row in before)
    for row in after:
        assert row["status"] == "matched"
        assert row["xy_error_m"] < 0.1
        assert row["score_fraction"] > 0.8
        # A circular synthetic wall does not supply an independent heading reference.
        assert row["yaw_error_deg"] is not None
        assert row["competing_peaks"] is None
        assert row["search_complete"] is None
    assert validation["training_overlap"] is True
    assert validation["global_full_scan_ambiguity"] is False


def refine_args(paths: dict[str, Path], truth: Path | None = None) -> list[str]:
    return [
        "refine",
        "--session",
        str(paths["session"]),
        "--truth",
        str(truth or paths["truth_path"]),
        "--maps",
        str(paths["map_directory"]),
        "--rectangles",
        str(paths["rectangles_path"]),
        "--output",
        str(paths["output"]),
        "--lidar-device",
        "lidar-test",
    ]


def test_refine_and_standalone_validation_cli_save_reports_and_print_tables(
    tmp_path, monkeypatch, capsys
):
    paths = inputs(tmp_path)
    before = file_bytes(paths["map_directory"])
    monkeypatch.setattr(markers, "load", lambda _device: config())
    markers.main([*refine_args(paths), "--validate", "--max-revolutions", "1"])
    output = capsys.readouterr().out
    assert "interval | map | mode | xy error cm | yaw error deg | peers" in output
    assert "Circular validation" in output
    report = json.loads((paths["output"] / markers.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["accepted_revolutions"] == 2
    assert report["validation"]["training_overlap"] is True
    comparison = tmp_path / "comparison.json"
    markers.main(
        [
            "validate",
            "--session",
            str(paths["session"]),
            "--truth",
            str(paths["truth_path"]),
            "--baseline",
            str(paths["map_directory"]),
            "--candidate",
            str(paths["output"]),
            "--output",
            str(comparison),
            "--lidar-device",
            "lidar-test",
            "--max-revolutions",
            "1",
        ]
    )
    standalone = json.loads(comparison.read_text(encoding="utf-8"))
    assert standalone == report["validation"]
    printed = capsys.readouterr().out
    assert "original | local_truth" in printed
    assert "refined | global" in printed
    assert "Circular validation" in printed
    assert file_bytes(paths["map_directory"]) == before
    for name in ("slam_map.npy", "map_meta.json", "provenance.json"):
        assert (paths["output"] / name).read_bytes() == before[name]


def test_detect_then_line_cli_requires_measured_geometry_before_refinement(
    tmp_path, monkeypatch, capsys
):
    paths = inputs(tmp_path)
    observations = tmp_path / "localization.jsonl"
    events = [
        {
            "kind": "localization",
            "t": start + step * 1000,
            "pose": [8 + index, 9, 1],
            "updated": True,
            "lost": False,
        }
        for index, start in enumerate((1000, 8000))
        for step in range(6)
    ]
    observations.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    detected = tmp_path / "detected.json"
    markers.main(["detect", "--input", str(observations), "--output", str(detected)])
    candidate = json.loads(detected.read_text(encoding="utf-8"))
    assert candidate["truth_source"] == "localization_candidate"
    assert len(candidate["intervals"]) == 2
    assert "2 intervals" in capsys.readouterr().out
    monkeypatch.setattr(markers, "load", lambda _device: config())
    with pytest.raises(SystemExit) as failure:
        markers.main(refine_args(paths, detected))
    assert failure.value.code == 2
    assert "not measured ground truth" in capsys.readouterr().err
    assert not paths["output"].exists()
    measured = tmp_path / "measured.json"
    markers.main(
        [
            "line",
            "--intervals",
            str(detected),
            "--origin",
            "0",
            "0",
            "--direction-deg",
            "0",
            "--yaw-deg",
            "90",
            "--output",
            str(measured),
        ]
    )
    generated = json.loads(measured.read_text(encoding="utf-8"))
    assert generated["truth_source"] == "measured_line_geometry"
    assert generated["frame_id"] == "patrol"
    assert generated["pose_origin"] == "lidar_centre"
    assert generated["yaw_unit"] == "radians"
    assert markers.parse_intervals(generated) == [
        interval(1000, 6000, yaw=pytest.approx(math.pi / 2)),
        interval(8000, 13000, x=pytest.approx(0.3), yaw=pytest.approx(math.pi / 2)),
    ]
    assert "measured_line_geometry" in capsys.readouterr().out
    write_events(
        paths["session"], [scan_event(t, i) for i, t in enumerate((1100, 1200, 8100, 8200), 1)]
    )
    markers.main(refine_args(paths, measured))
    report = json.loads((paths["output"] / markers.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["accepted_revolutions"] == 4
    assert [item["accepted_revolutions"] for item in report["intervals"]] == [2, 2]
