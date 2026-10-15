"""Independent synthetic marker truth, recorded in the runtime's JSONL format."""

import json
import math
from pathlib import Path

import pytest

from tools.lidar.marker_truth import DEFAULT_MARKERS, analyze, main

WINDOWS = "A:0-20,B:20-40,C:40-60,D:60-80,A_return:80-100"
ORIGIN = 1791009847936


def save(tmp_path: Path, events: list[dict]) -> Path:
    events.sort(key=lambda e: e["t"])
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return tmp_path


def location(second, pose, **flags):
    return {
        "t": ORIGIN + second * 1000,
        "kind": "localization",
        "pose": None if pose is None else [pose[0], pose[1], math.radians(pose[2])],
        "updated": True,
        "lost": False,
        **flags,
    }


def telemetry(second, yaw):
    return {
        "t": ORIGIN + second * 1000,
        "kind": "telemetry",
        "raw": json.dumps({"imu": {"yaw": yaw}}),
    }


def experiment(tmp_path, *, errors=None, rotation=0.0, translation=(0.0, 0.0)):
    events = []
    cosine, sine = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    for index, marker in enumerate(DEFAULT_MARKERS):
        x, y, yaw = marker["pose"]
        dx, dy, dyaw = (errors or {}).get(marker["name"], (0, 0, 0))
        pose = [
            cosine * x - sine * y + translation[0] + dx,
            sine * x + cosine * y + translation[1] + dy,
            yaw + rotation + dyaw,
        ]
        for offset in range(20):
            second = index * 20 + offset
            events.extend([telemetry(second, yaw + 137), location(second, pose)])
    return save(tmp_path, events)


def pair(report, left, right):
    return next(p for p in report["pairs"] if p["from"] == left and p["to"] == right)


@pytest.mark.parametrize("windows", [None, WINDOWS])
def test_perfect_rectangle_in_arbitrary_map_frame(tmp_path, windows):
    session = experiment(tmp_path, rotation=73, translation=(4, -2))
    report = analyze(session, windows=windows)
    assert len(report["segments"]) == 5
    assert len(report["pairs"]) == 10
    for row in report["segments"]:
        assert row["sample_count"] == 15
        assert row["position_std_m"] == pytest.approx(0, abs=1e-12)
        assert row["heading_range_deg"] == pytest.approx(0, abs=1e-12)
        assert row["updated_fraction"] == 1
        assert row["lost_fraction"] == 0
        assert row["imu_comparison"]["difference_deg"] == pytest.approx(0)
        assert "absolute_error" not in row
    for row in report["pairs"]:
        assert row["distance_error_m"] == pytest.approx(0, abs=1e-12)
        assert row["heading_delta_error_deg"] == pytest.approx(0, abs=1e-12)
    for right, distance in [("B", 0.8), ("C", 1), ("D", 0.6)]:
        assert pair(report, "A", right)["measured_distance_m"] == pytest.approx(distance)
    assert pair(report, "B", "C")["measured_distance_m"] == pytest.approx(0.6)
    assert pair(report, "A", "C")["measured_heading_delta_deg"] == pytest.approx(90)
    assert abs(pair(report, "A", "D")["measured_heading_delta_deg"]) == pytest.approx(180)
    assert report["revisits"][0]["position_error_m"] == pytest.approx(0)


def test_three_centimetres_five_degrees_and_fixed_anchor(tmp_path):
    session = experiment(tmp_path, errors={"B": (0.03, 0, 5)})
    report = analyze(session, windows=WINDOWS, anchor=[0, 0, 0])
    ab = pair(report, "A", "B")
    assert ab["distance_error_m"] == pytest.approx(0.03)
    assert ab["heading_delta_error_deg"] == pytest.approx(5)
    assert ab["imu_comparison"]["difference_deg"] == pytest.approx(5)
    error = report["segments"][1]["absolute_error"]
    assert error["position_error_m"] == pytest.approx(0.03)
    assert error["heading_error_deg"] == pytest.approx(5)


def test_revisit_drift_is_not_realigned(tmp_path):
    session = experiment(
        tmp_path, rotation=90, translation=(2, 3), errors={"A_return": (0.03, 0.04, 7)}
    )
    report = analyze(session, anchor=[2, 3, 90])
    assert report["anchor_transform"]["rotation_deg"] == 90
    assert report["anchor_transform"]["translation_m"] == pytest.approx([2, 3])
    revisit = report["revisits"][0]
    assert revisit["position_error_m"] == pytest.approx(0.05)
    assert revisit["heading_error_deg"] == pytest.approx(7)
    error = report["segments"][-1]["absolute_error"]
    assert error["position_error_m"] == pytest.approx(0.05)
    assert error["heading_error_deg"] == pytest.approx(7)


def test_common_bias_requires_independent_anchor(tmp_path):
    session = experiment(tmp_path, rotation=5, translation=(0.03, 0))
    report = analyze(session, windows=WINDOWS, anchor=[0, 0, 0])
    assert all(abs(p["distance_error_m"]) < 1e-12 for p in report["pairs"])
    error = report["segments"][0]["absolute_error"]
    assert error["position_error_m"] == pytest.approx(0.03)
    assert error["heading_error_deg"] == pytest.approx(5)


def test_settling_circular_statistics_and_status_denominators(tmp_path):
    events = [location(s, [100, 100, 0]) for s in range(5)]
    events += [
        location(5, [-0.03, 0, 179], updated=False, lost=True),
        location(6, [0.03, 0, -179]),
        location(7, None, updated=False, lost=True),
    ]
    session = save(tmp_path, events)
    row = analyze(session, windows="A:0-8")["segments"][0]
    assert row["sample_count"] == 3 and row["pose_count"] == 2
    assert row["mean_pose"][:2] == pytest.approx([0, 0])
    assert abs(row["mean_pose"][2]) == pytest.approx(180)
    assert row["position_std_m"] == pytest.approx(0.03)
    assert row["heading_range_deg"] == pytest.approx(2)
    assert row["updated_fraction"] == pytest.approx(1 / 3)
    assert row["lost_fraction"] == pytest.approx(2 / 3)
    assert row["imu_comparison"] is None


def test_imu_uses_common_times_and_unwraps(tmp_path):
    events = [location(0, [0, 0, 160])]
    # Common localization interval is 6..8; interpolate IMU to precisely those times.
    events += [location(s, [0, 0, 170 + 4 * (s - 5)]) for s in range(5, 10)]
    events += [telemetry(s, ((178 + 2 * (s - 5)) + 180) % 360 - 180) for s in [5.5, 6.5, 7.5, 8.5]]
    result = analyze(save(tmp_path, events), windows="A:0-10")["segments"][0]["imu_comparison"]
    assert result["start_s"] == 6 and result["end_s"] == 8
    assert result["localization_delta_deg"] == pytest.approx(8)
    assert result["imu_delta_deg"] == pytest.approx(4)
    assert result["difference_deg"] == pytest.approx(4)


def test_auto_cumulative_motion_and_short_transition_segments(tmp_path):
    events = [location(s, [0, 0, 0]) for s in range(20)]
    # Small individual steps must still detect the 0.8 m carry.
    events += [location(20 + i, [0.2 * (i + 1), 0, 0]) for i in range(4)]
    events += [location(s, [0.8, 0, 0]) for s in range(24, 45)]
    result = analyze(save(tmp_path, events))
    assert len(result["segments"]) == 2
    assert result["discarded_windows"]
    # The first subthreshold carry sample is retained: auto windows are provisional.
    assert pair(result, "A", "B")["distance_error_m"] == pytest.approx(-0.2 / 16)
    assert result["warnings"]
    manual = analyze(tmp_path, windows="A:0-20,B:24-45")
    assert pair(manual, "A", "B")["distance_error_m"] == pytest.approx(0)


def test_auto_imu_only_split_and_wrap(tmp_path):
    events = []
    for s in range(40):
        yaw = (179 if s % 2 == 0 else -179) if s < 20 else -130
        events += [location(s, [0, 0, 0]), telemetry(s, yaw)]
    result = analyze(save(tmp_path, events))
    assert len(result["segments"]) == 2
    assert result["segments"][1]["start_s"] == 20


def test_empty_short_windows_missing_flags_and_undefined_heading(tmp_path):
    session = save(
        tmp_path,
        [
            location(0, [0, 0, 0]),
            location(5, [0, 0, 0], lost=None, updated=None),
            location(6, [0, 0, 180], lost=None, updated=None),
        ],
    )
    result = analyze(session, windows="A:0-7,B:7-9")
    a, b = result["segments"]
    assert a["mean_pose"][2] is None
    assert a["updated_fraction"] is None and a["lost_fraction"] is None
    assert b["mean_pose"] is None and b["sample_count"] == 0
    assert result["pairs"] == []
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "spec", ["A:0-20,B:19-40", "A:0-20,A:20-40", "X:0-20", "A:5-4", "A:0-inf", "A:0-nan", "bad"]
)
def test_bad_windows_rejected(tmp_path, spec):
    with pytest.raises(ValueError):
        analyze(experiment(tmp_path), windows=spec)


def test_custom_markers_nonzero_origin_and_cli(tmp_path, capsys):
    markers = [{"name": "P", "pose": [1, 2, 30]}, {"name": "Q", "pose": [1, 3, 120]}]
    events = [
        location(s, [10, 20, 90] if s < 20 else [10 - math.sqrt(3) / 2, 20.5, 180])
        for s in range(40)
    ]
    save(tmp_path, events)
    assert (
        main(
            [
                str(tmp_path),
                "--markers",
                json.dumps(markers),
                "--anchor",
                "10,20,90",
                "--windows",
                "P:0-20,Q:20-40",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert "marker" in output.err and "P -> Q" in output.err
    for row in result["segments"]:
        assert row["absolute_error"]["position_error_m"] == pytest.approx(0, abs=1e-12)
        assert row["absolute_error"]["heading_error_deg"] == pytest.approx(0, abs=1e-12)
    marker_file = tmp_path / "markers.json"
    marker_file.write_text(json.dumps(markers), encoding="utf-8")
    assert main([str(tmp_path), "--markers", str(marker_file)]) == 0
    assert len(json.loads(capsys.readouterr().out)["markers"]) == 2


def test_bad_record_has_line_number(tmp_path):
    (tmp_path / "events.jsonl").write_text('{"t":0,"kind":"scan"}\n{bad}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="events.jsonl:2"):
        analyze(tmp_path)


@pytest.mark.parametrize(
    "markers",
    [[], [{"name": "X", "pose": [0, 0, math.nan]}], [{"name": "X", "pose": [0, 0, 0]}] * 2],
)
def test_bad_markers_rejected(tmp_path, markers):
    with pytest.raises(ValueError):
        analyze(tmp_path, markers=markers)
