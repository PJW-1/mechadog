"""UART analysis tests only; these fixtures never communicate with hardware."""

import hashlib
import json

import pytest

from tools.sensor_log_check import analyze, main


def status(**overrides):
    fields = {
        "imu_valid": 1,
        "imu_error": "none",
        "dist_valid": 1,
        "dist_error": "none",
        "batt_valid": 1,
        "batt_error": "none",
        "pitch": 0.5,
        "roll": -0.3,
        "yaw": 180,
        "dist_cm": 24,
        "batt_v": 7.8,
    }
    fields.update(overrides)
    return "Sensor status: " + " ".join(f"{k}={v}" for k, v in fields.items()) + "\n"


def test_ranges_and_wrap_are_distinct_from_accuracy():
    report = analyze([status(yaw=359), status(yaw=0), status(yaw=1, batt_v=7.7)])
    segment = report["segments"][0]
    assert segment["boot_id"] is None
    assert segment["sensors"]["batt"]["values"]["batt_v"] == {
        "first": 7.8,
        "last": 7.7,
        "min": 7.7,
        "max": 7.8,
    }
    assert segment["yaw_runs"][0]["net_change_deg"] == 2
    assert segment["yaw_runs"][0]["max_step_deg"] == 1
    assert report["accuracy_verified"] is False
    reverse = analyze([status(yaw=1), status(yaw=359)])
    assert reverse["segments"][0]["yaw_runs"][0]["net_change_deg"] == -2


def test_invalid_values_never_enter_stats_and_break_yaw():
    report = analyze(
        [status(yaw=100), status(imu_valid=0, imu_error="stale", yaw="nan"), status(yaw=200)]
    )
    segment = report["segments"][0]
    assert report["invalid_sensor_observations"] == 1
    assert segment["sensors"]["imu"]["errors"] == {"stale": 1}
    assert segment["sensors"]["dist"]["valid_rows"] == 3
    assert [r["net_change_deg"] for r in segment["yaw_runs"]] == [0, 0]


@pytest.mark.parametrize(
    "bad",
    [
        status(yaw="nan"),
        status(roll="inf"),
        status(yaw=360),
        status(yaw=-1),
        status(dist_cm=-1),
        status(batt_v=-1),
        status(imu_valid=2),
        status(imu_error="stale"),
        status(imu_valid=0),
        status(imu_error="secret"),
        status().replace("roll=-0.3 ", ""),
        status() + "yaw=19",
        "Sensor status: broken",
        "Sensor status:",
    ],
)
def test_corrupt_rows_not_partially_accepted(bad):
    report = analyze([status(yaw=10), bad, status(yaw=80)])
    segment = report["segments"][0]
    assert report["malformed_rows"] == 1
    assert segment["sensors"]["dist"]["valid_rows"] == 2
    assert len(segment["yaw_runs"]) == 2


def test_reboots_split_sessions_even_if_no_new_identity_is_logged():
    report = analyze(
        [
            status(yaw=20),
            "MechDog command receiver booting\n",
            "Telemetry identity: dog-a boot=0123456789abcdef port=5101\n",
            status(yaw=180),
            "MechDog command receiver booting\n",
            status(yaw=180),
            "Telemetry identity: dog-a boot=fedcba9876543210 port=5101\n",
            status(yaw=180),
        ]
    )
    assert [s["boot_id"] for s in report["segments"]] == [
        None,
        "0123456789abcdef",
        None,
        "fedcba9876543210",
    ]
    assert all(s["status_rows"] == 1 for s in report["segments"])


def test_empty_noise_and_unknown_fields_are_not_copied():
    assert analyze([])["lines"] == 0
    assert analyze(["password=secret"])["status_rows"] == 0
    report = analyze([status(extra="secret")])
    assert "secret" not in json.dumps(report)


def test_cli_hash_and_no_overwrite(tmp_path, capsys):
    source = tmp_path / "uart.log"
    source.write_bytes(status().encode())
    output = tmp_path / "report.json"
    assert main([str(source), "--output", str(output)]) == 0
    original = output.read_bytes()
    report = json.loads(original)
    assert report["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert main([str(source), "--output", str(output)]) == 2
    assert output.read_bytes() == original
    assert main([str(source), "--output", str(source)]) == 2
    assert main([str(tmp_path / "absent")]) == 2
    assert "Log/file error" in capsys.readouterr().err


@pytest.mark.parametrize(
    "text,code",
    [
        ("", 2),
        ("Sensor status: broken\n", 2),
        (status(imu_valid=0, imu_error="calibrating"), 1),
        (status() + "Sensor status: broken\n", 1),
        (status(), 0),
    ],
)
def test_cli_exit_codes(tmp_path, capsys, text, code):
    source = tmp_path / "uart.log"
    source.write_text(text, encoding="utf-8")
    assert main([str(source)]) == code
    assert json.loads(capsys.readouterr().out)["accuracy_verified"] is False
