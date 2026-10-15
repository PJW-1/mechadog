"""Synthetic offline observations; no hardware or private map data."""

import json
import math
from pathlib import Path

import pytest

from tools.maps.marker_observations import detect_stationary, generate_line_truth

EPOCH_MS = 1_800_000_000_000


def save(tmp_path: Path, events: list) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


def localization(second: float, x: float = 1, y: float = 2, yaw: float = 30, **flags) -> dict:
    return {
        "t": EPOCH_MS + second * 1000,
        "kind": "localization",
        "pose": [x, y, math.radians(yaw)],
        "updated": True,
        "lost": False,
        **flags,
    }


def test_runtime_seconds_duration_and_unrelated_events(tmp_path):
    events = []
    for second in range(7):
        events.extend([localization(second), {"t": EPOCH_MS + second * 1000, "kind": "scan"}])
    save(tmp_path, events)
    assert detect_stationary(tmp_path) == [
        {
            "start_ms": EPOCH_MS,
            "end_ms": EPOCH_MS + 6000,
            "x": 1,
            "y": 2,
            "yaw": pytest.approx(math.pi / 6),
        }
    ]


@pytest.mark.parametrize("nested", [False, True])
def test_nav_polling_seconds_and_degree_conversion(tmp_path, nested):
    events = []
    for second in range(6):
        nav = {"pose": [1, 2, 90], "stale": False, "available": True}
        events.append({"t": EPOCH_MS / 1000 + second, **({"nav": nav} if nested else nav)})
    rows = detect_stationary(save(tmp_path, events))
    assert rows[0]["start_ms"] == EPOCH_MS
    assert rows[0]["end_ms"] == EPOCH_MS + 5000
    assert rows[0]["yaw"] == pytest.approx(math.pi / 2)


@pytest.mark.parametrize(
    "bad",
    [
        {"lost": True},
        {"stale": True},
        {"updated": False},
        {"valid": False},
        {"available": False},
        {"starting": True},
        {"phase": "NavPhase.LOST"},
        {"status": "INVALID"},
        {"pose": None},
        {"pose": [0, 0, math.nan]},
        {"pose": [0, 0, True]},
        {"lost": "false"},
        {"t": None},
        {"t": math.inf},
    ],
)
def test_invalid_observation_breaks_stationary_interval(tmp_path, bad):
    events = [localization(second) for second in range(11)]
    events[5].update(bad)
    assert detect_stationary(save(tmp_path, events)) == []


def test_missing_pose_and_nonobject_breaks_interval(tmp_path):
    events = [localization(second) for second in range(11)]
    del events[5]["pose"]
    assert detect_stationary(save(tmp_path, events)) == []
    events[5] = None
    assert detect_stationary(save(tmp_path, events)) == []


def test_gap_does_not_count_as_standstill(tmp_path):
    assert detect_stationary(save(tmp_path, [localization(0), localization(10)])) == []


@pytest.mark.parametrize("seconds", [[0, 1, 2, 3, 3, 4, 5], [0, 1, 2, 3, 2, 3, 4]])
def test_duplicate_or_reversed_time_breaks_interval(tmp_path, seconds):
    assert detect_stationary(save(tmp_path, [localization(second) for second in seconds])) == []


def test_slow_drift_uses_fixed_anchor(tmp_path):
    events = [localization(second, x=second * 0.01) for second in range(30)]
    assert detect_stationary(save(tmp_path, events)) == []


def test_slow_angular_drift_uses_fixed_anchor(tmp_path):
    events = [localization(second, yaw=second * 2) for second in range(30)]
    assert detect_stationary(save(tmp_path, events)) == []


def test_angle_wraparound_and_boundary_tolerances(tmp_path):
    events = [
        localization(
            second, x=(0 if second % 2 == 0 else 0.03), yaw=(178 if second % 2 == 0 else -177)
        )
        for second in range(6)
    ]
    rows = detect_stationary(save(tmp_path, events))
    assert len(rows) == 1
    assert rows[0]["x"] == pytest.approx(0.015)
    assert rows[0]["yaw"] == pytest.approx(math.radians(-179.5))


def test_separate_stationary_positions_and_final_short_interval(tmp_path):
    events = [localization(second, x=0 if second < 6 else 0.3) for second in range(12)]
    events += [localization(second, x=0.6) for second in range(12, 14)]
    rows = detect_stationary(save(tmp_path, events))
    assert [(row["start_ms"], row["end_ms"]) for row in rows] == [
        (EPOCH_MS, EPOCH_MS + 5000),
        (EPOCH_MS + 6000, EPOCH_MS + 11000),
    ]


def test_bad_json_reports_line_number(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{}\n{"t": invalid}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="events.jsonl:2"):
        detect_stationary(path)


@pytest.mark.parametrize(
    "options",
    [
        {"position_tolerance_m": 0},
        {"position_tolerance_m": math.nan},
        {"yaw_tolerance_deg": 180},
        {"yaw_tolerance_deg": -1},
        {"min_duration_ms": 0},
        {"min_duration_ms": True},
        {"max_gap_ms": 1.5},
    ],
)
def test_invalid_detection_parameters(tmp_path, options):
    with pytest.raises(ValueError):
        detect_stationary(save(tmp_path, []), **options)


def windows() -> list[dict]:
    return [
        {"start_ms": EPOCH_MS + i * 10000, "end_ms": EPOCH_MS + i * 10000 + 5000} for i in range(3)
    ]


def test_line_uses_only_times_with_independent_origin_direction_and_yaw():
    intervals = windows()
    for interval in intervals:
        interval.update(x=math.nan, y="wrong", yaw=None)
    rows = generate_line_truth(intervals, origin=(2, -3), direction_deg=90, yaw_deg=-45)
    assert [row["x"] for row in rows] == pytest.approx([2, 2, 2])
    assert [row["y"] for row in rows] == pytest.approx([-3, -2.7, -2.4])
    assert [row["yaw"] for row in rows] == pytest.approx([-math.pi / 4] * 3)
    assert [(row["start_ms"], row["end_ms"]) for row in rows] == [
        (row["start_ms"], row["end_ms"]) for row in intervals
    ]
    assert math.isnan(intervals[0]["x"])


def test_line_custom_spacing_and_heading_wrap():
    rows = generate_line_truth(
        windows(), origin=(0, 0), direction_deg=180, yaw_deg=450, spacing_m=0.5
    )
    assert rows[2]["x"] == pytest.approx(-1)
    assert rows[2]["y"] == pytest.approx(0, abs=1e-12)
    assert rows[2]["yaw"] == pytest.approx(math.pi / 2)


@pytest.mark.parametrize(
    "intervals",
    [
        [],
        [None],
        [{"start_ms": 5, "end_ms": 5}],
        [{"start_ms": -1, "end_ms": 5}],
        [{"start_ms": True, "end_ms": 5}],
        [{"start_ms": math.inf, "end_ms": 5}],
        [{"start_ms": 0.5, "end_ms": 5}],
        [{"start_ms": 0, "end_ms": 5}, {"start_ms": 5, "end_ms": 10}],
        [{"start_ms": 10, "end_ms": 15}, {"start_ms": 0, "end_ms": 5}],
    ],
)
def test_invalid_line_intervals(intervals):
    with pytest.raises(ValueError):
        generate_line_truth(intervals, origin=(0, 0), direction_deg=0, yaw_deg=0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"origin": (math.nan, 0)},
        {"origin": (True, 0)},
        {"origin": (0,)},
        {"direction_deg": math.inf},
        {"yaw_deg": None},
        {"spacing_m": 0},
        {"spacing_m": -1},
    ],
)
def test_invalid_line_geometry(overrides):
    options = {"origin": (0, 0), "direction_deg": 0, "yaw_deg": 0, **overrides}
    with pytest.raises(ValueError):
        generate_line_truth(windows(), **options)
