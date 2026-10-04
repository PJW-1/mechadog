"""Offline synthetic evidence: healthy -> fixed 347 degrees/zero -> recovery."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tools.lidar.input_anomaly import analyze_recording, format_table, main


def packet(
    t: int,
    seq: int,
    *,
    bad: bool = False,
    boot: str = "boot-a",
    device: str = "lidar-test",
    zero: bool = False,
) -> dict:
    # Six chunks per revolution; stay away from floating-point bin boundaries.
    angles = [347.38] * 12 if bad else [(seq % 6) * 60 + i * 5 + 0.5 for i in range(12)]
    msg = {
        "type": "SCAN",
        "device_id": device,
        "boot_id": boot,
        "seq": seq,
        "ts": t,
        "points": [[a, 0 if bad or zero else 1000] for a in angles],
    }
    return {"t": t, "kind": "scan", "raw": json.dumps(msg)}


def recording(path: Path, events: list[dict], *, dropped: int = 0) -> Path:
    (path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    (path / "summary.json").write_text(
        json.dumps({"written": len(events), "dropped": dropped, "failed": None}),
        encoding="utf-8",
    )
    return path


def rows(report: dict, device: str = "lidar-test") -> list[dict]:
    return report["devices"][device]["windows"]


def test_normal_fixed_347_recovery(tmp_path: Path) -> None:
    events = [packet(t, t // 10 + 1, bad=1000 <= t < 3000) for t in range(0, 5000, 10)]
    report = analyze_recording(recording(tmp_path, events))
    windows = rows(report)
    assert [w["status"] for w in windows] == ["normal", "anomaly", "anomaly", "normal", "normal"]
    bad = windows[1]
    assert set(bad["reasons"]) == {"no_complete_scans", "angle_concentrated", "zero_distance_high"}
    assert bad["raw_points"] == 1200 and bad["valid_points"] == 0
    assert bad["angle_min_deg"] == bad["angle_max_deg"] == 347.38
    assert bad["angle_bins_5deg"] == 1 and bad["angle_circular_span_deg"] == 0
    assert bad["zero_distance_fraction"] == bad["dominant_bin_fraction"] == 1
    assert bad["packet_gap_ms"] == {"min": 10, "median": 10, "max": 10}
    assert bad["points_per_packet"]["median"] == 12
    assert bad["seq_missing"] == bad["boot_changes"] == 0
    (episode,) = report["devices"]["lidar-test"]["episodes"]
    assert episode["previous_normal_window"] == 0
    assert episode["first_window"] == 1 and episode["last_window"] == 2
    assert episode["recovery_window"] == 3
    assert "before:" in format_table(report) and "recovery:" in format_table(report)


def test_zero_distances_do_not_complete_a_scan(tmp_path: Path) -> None:
    report = analyze_recording(
        recording(tmp_path, [packet(t, t // 10 + 1, zero=True) for t in range(0, 2000, 10)])
    )
    assert rows(report)[0]["angle_bins_5deg"] == 72
    assert rows(report)[0]["completed"] == 0
    assert rows(report)[0]["reasons"] == ["no_complete_scans", "zero_distance_high"]


def test_boundary_seq_gap_duplicates_and_boot_change(tmp_path: Path) -> None:
    events = [packet(t, t // 10 + 1) for t in range(0, 1000, 10)]
    events += [
        packet(1000, 105),
        packet(1010, 105),
        packet(1020, 104),
        packet(1030, 1, boot="boot-b"),
        packet(1040, 3, boot="boot-b"),
    ]
    report = analyze_recording(recording(tmp_path, events))
    row = rows(report)[1]
    assert row["seq_missing"] == 5  # 101..104, then boot-b seq 2
    assert row["seq_duplicate_or_reordered"] == row["rejected"] == 2
    assert row["boot_changes"] == 1
    assert row["boot_transitions"] == [{"t_ms": 1030, "from": "boot-a", "to": "boot-b"}]


def test_devices_do_not_supply_each_others_coverage(tmp_path: Path) -> None:
    events = []
    for t in range(0, 2000, 10):
        events.append(packet(t, t // 10 + 1, device="healthy"))
        events.append(packet(t, t // 10 + 1, device="stuck", bad=True))
    report = analyze_recording(recording(tmp_path, events))
    assert rows(report, "healthy")[0]["status"] == "normal"
    assert rows(report, "stuck")[0]["status"] == "anomaly"
    filtered = analyze_recording(tmp_path, device_id="healthy")
    assert set(filtered["devices"]) == {"healthy"}


def test_no_packets_and_recorder_loss_are_visible(tmp_path: Path) -> None:
    events = [packet(t, t // 10 + 1) for t in range(0, 1000, 10)]
    events += [{"t": 2999, "kind": "telemetry"}]
    report = analyze_recording(recording(tmp_path, events, dropped=5))
    assert rows(report)[1]["reasons"] == ["no_packets"]
    assert report["issues"]["recorder_loss_or_failure"] == 1
    assert report["devices"]["lidar-test"]["episodes"][0]["recovery_window"] is None


def test_bad_input_base64_and_partial_tail(tmp_path: Path) -> None:
    event = packet(0, 1)
    event["raw_b64"] = base64.b64encode(event.pop("raw").encode()).decode()
    recording(
        tmp_path,
        [
            event,
            {"t": 10, "kind": "scan", "raw_b64": "!!!"},
            {"t": 20, "kind": "scan", "raw": "[]"},
        ],
    )
    with (tmp_path / "events.jsonl").open("ab") as target:
        target.write(b'{"t":')
    report = analyze_recording(tmp_path)
    assert report["issues"]["malformed_scan_records"] == 2
    assert report["issues"]["malformed_event_lines"] == 1
    assert rows(report)[0]["packets"] == 1
    assert rows(report)[0]["status"] == "insufficient_data"


def test_circular_span_handles_north_wrap(tmp_path: Path) -> None:
    event = packet(0, 1)
    msg = json.loads(event["raw"])
    msg["points"] = [[359, 0], [1, 1000]]
    event["raw"] = json.dumps(msg)
    report = analyze_recording(recording(tmp_path, [event]))
    assert rows(report)[0]["angle_circular_span_deg"] == 2
    assert rows(report)[0]["zero_distance_fraction"] == 0.5


def test_starts_bad_has_no_invented_baseline(tmp_path: Path) -> None:
    report = analyze_recording(
        recording(tmp_path, [packet(t, t // 10 + 1, bad=True) for t in range(0, 2000, 10)])
    )
    (episode,) = report["devices"]["lidar-test"]["episodes"]
    assert episode["previous_normal_window"] is None
    assert episode["recovery_window"] is None


def test_backward_receive_time_is_reported(tmp_path: Path) -> None:
    report = analyze_recording(recording(tmp_path, [packet(100, 1), packet(50, 2)]))
    assert report["issues"]["backward_scan_timestamps_skipped"] == 1
    assert rows(report)[0]["packets"] == 1


def test_cli_preserves_source_and_existing_output(tmp_path: Path, capsys) -> None:
    recording(tmp_path, [packet(t, t // 10 + 1) for t in range(0, 2000, 10)])
    source = (tmp_path / "events.jsonl").read_bytes()
    assert main([str(tmp_path)]) == 0
    assert json.loads((tmp_path / "input-anomaly.json").read_text())["status"] == "analyzed"
    assert (tmp_path / "input-anomaly.txt").exists()
    assert main([str(tmp_path)]) == 2
    assert "output exists" in capsys.readouterr().err
    assert main([str(tmp_path), "--out", str(tmp_path / "events.jsonl")]) == 2
    assert (tmp_path / "events.jsonl").read_bytes() == source


def test_empty_recording_is_not_healthy(tmp_path: Path) -> None:
    recording(tmp_path, [])
    assert analyze_recording(tmp_path)["status"] == "no_scan_data"
    assert main([str(tmp_path)]) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_ms": 300},
        {"zero_ratio": 0},
        {"concentration": 1.1},
        {"mount_yaw_deg": float("nan")},
    ],
)
def test_bad_options_are_rejected(tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        analyze_recording(tmp_path, **kwargs)
