"""Offline runtime-capture safety and measurement tests: no real sockets or UART."""

import io
import json
import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from host.common.protocol import TelemetryEncoder
from tools import runtime_compare as runtime

IMAGE = "a" * 64


def status(**changes):
    return {
        "image_sha256": IMAGE,
        "actuators": False,
        "healthy": True,
        "confirmed": True,
        "updating": False,
        "boot": "ota-boot",
        **changes,
    }


def perf(at=1_000_000, n=10, metric="cycle", **changes):
    values = {
        "v": 1,
        "at_us": at,
        "core": 0,
        "cfg": 0,
        "stack_b": 2048,
        "drop": 0,
        "pub_drop": 0,
        "metric": metric,
        "n": n,
        "sum_us": n * 50,
        "max_us": 70,
        "p95_ub_us": 100,
        "p99_ub_us": 100,
        "sat": 0,
        "h": [0, n] + [0] * 15,
        **changes,
    }
    return "Perf: " + " ".join(
        f"{k}={','.join(map(str, v)) if k == 'h' else v}" for k, v in values.items()
    )


def telemetry(seq=1, boot="telemetry-boot"):
    encoder = TelemetryEncoder("robot", boot, start_seq=seq)
    return encoder.build(
        state="FAILSAFE",
        dist_cm=20,
        imu={"pitch": 0, "roll": 0, "yaw": 0},
        batt_v=7.6,
        last_cmd_age_ms=0,
        flags={"lowbatt": False, "tipped": False, "link_ok": True},
        safety_latched=True,
    )


def basic_records(duration=20):
    message = telemetry()
    return [
        {
            "kind": "metadata",
            "t": 0,
            "warmup_s": 10,
            "duration_s": duration,
            "device": "robot",
            "expected_image_sha256": IMAGE,
            "expected_core": 0,
        },
        {"kind": "https", "t": 0, "phase": "before", "status": status()},
        {"kind": "telemetry", "t": 0.2, "telemetry": message},
        {"kind": "telemetry_raw", "t": 0.2, "raw": json.dumps(message)},
        {"kind": "https", "t": duration + 0.1, "phase": "after", "status": status()},
        {"kind": "capture_end", "t": duration, "completed": True},
    ]


def complete_records():
    rows = [r for r in basic_records(30) if r["kind"] not in ("telemetry", "telemetry_raw")]
    for index in range(300):
        at, seq = 0.01 + index / 10, index + 1
        command_type = "STOP" if index == 0 else "STATE"
        rows.append({"kind": "sent", "t": at, "seq": seq, "type": command_type})
        rows.append(
            {
                "kind": "ack",
                "t": at + 0.005,
                "ack": {
                    "seq": seq,
                    "type": command_type,
                    "ok": True,
                    "applied": True,
                    "safe_latched": True,
                    "actuators": False,
                },
            }
        )
        message = telemetry(seq)
        rows.append({"kind": "telemetry", "t": at + 0.01, "telemetry": message})
        rows.append({"kind": "telemetry_raw", "t": at + 0.01, "raw": json.dumps(message)})
    for at in range(30):
        rows.append(
            {"kind": "https", "phase": "during", "t": at + 0.1, "status": status(), "rtt_ms": 4}
        )
        rows.append(
            {
                "kind": "uart",
                "t": at + 0.2,
                "line": "Sensor status: imu_valid=1 imu_error=none dist_valid=1 dist_error=none batt_valid=1 batt_error=none imu_timing=none loop_gap_max_us=2000 heap_free=200000 heap_min=199000",
            }
        )
    for at in (11, 16, 21, 26):
        for metric in runtime.METRICS:
            rows.append(
                {"kind": "uart", "t": at, "line": perf(at=at * 1_000_000, n=at * 25, metric=metric)}
            )
    return sorted(rows, key=lambda row: row["t"])


def test_complete_measurement_passes_strict_gate():
    report = runtime.summarize(complete_records())
    assert report["capture_ok"], report["measurement_issues"]
    assert report["measurement_complete"]
    assert report["windows"]["steady"]["commands"]["send_hz"] == pytest.approx(10)


BUSY_FIELDS = {
    "imu_valid": 0,
    "dist_valid": 0,
    "batt_valid": 0,
    "imu_error": "snapshot_busy",
    "dist_error": "snapshot_busy",
    "batt_error": "snapshot_busy",
}
BUSY_LINE = "Sensor status: " + " ".join(f"{key}={value}" for key, value in BUSY_FIELDS.items())


def test_busy_snapshot_is_unavailable_and_does_not_abort_valid_capture():
    assert runtime.sensor_status_kind(BUSY_FIELDS) == "snapshot_busy"
    rows = complete_records()
    rows.append({"kind": "uart", "t": 15.5, "line": BUSY_LINE})
    report = runtime.summarize(rows)
    assert report["capture_ok"], report["measurement_issues"]
    sensors = report["windows"]["steady"]["sensors"]
    assert sensors["rows"] == 21
    assert sensors["valid_rows"] == 20
    assert sensors["snapshot_busy_rows"] == 1
    assert sensors["fault_rows"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"imu_error": "timing_fault"},
        {"dist_error": "none"},
        {"batt_error": "read_failed"},
        {"imu_valid": 1},
    ],
)
def test_mixed_or_real_fault_is_not_excused_as_snapshot_busy(changes):
    assert runtime.sensor_status_kind({**BUSY_FIELDS, **changes}) == "fault"


def test_busy_snapshots_do_not_satisfy_valid_sensor_row_minimum():
    rows = complete_records()
    for row in rows:
        if row["kind"] == "uart" and row["line"].startswith("Sensor status:") and row["t"] >= 10:
            row["line"] = BUSY_LINE
    report = runtime.summarize(rows)
    assert not report["capture_ok"]
    assert "insufficient steady sensor status rows" in report["measurement_issues"]
    assert report["windows"]["steady"]["sensors"]["valid_rows"] == 0


def test_https_backpressure_is_reported_without_inventing_1hz_execution():
    rows = [
        r for r in complete_records() if not (r["kind"] == "https" and r.get("phase") == "during")
    ]
    for index in range(9):
        rows.append(
            {
                "kind": "https",
                "phase": "during",
                "t": 10.1 + 2.2 * index,
                "rtt_ms": 2100,
                "status": status(),
            }
        )
    report = runtime.summarize(rows)
    assert report["capture_ok"], report["measurement_issues"]
    https = report["windows"]["steady"]["https"]
    assert https["target_attempt_hz"] == 1
    assert https["maximum_in_flight"] == 1
    assert https["completed_per_window_hz"] == pytest.approx(0.45)
    assert https["backpressured_requests"] == 9
    reduced = [r for r in rows if r["kind"] != "https" or r.get("phase") != "during" or r["t"] < 22]
    assert not runtime.summarize(reduced)["capture_ok"]


@pytest.mark.parametrize("missing", ["ack", "sent", "telemetry_raw", "uart", "https"])
def test_completed_capture_with_missing_channel_is_not_measurement_success(missing):
    rows = [row for row in complete_records() if row["kind"] != missing]
    report = runtime.summarize(rows)
    assert report["completed"]
    assert not report["capture_ok"]


@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "https", "phase": "during", "t": 30.1, "error": "TimeoutError"},
        {"kind": "uart", "t": 30.1, "line": "rst:0xc"},
        {"kind": "uart", "t": 30.1, "line": "Sensor status: imu_valid=0 dist_valid=1 batt_valid=1"},
        {"kind": "uart", "t": 30.1, "line": perf(core=1)},
        {"kind": "uart", "t": 30.1, "line": "Perf: truncated"},
    ],
)
def test_error_drained_after_measurement_still_fails_gate(extra):
    assert not runtime.summarize([*complete_records(), extra])["capture_ok"]


def test_perf_delta_excludes_warmup_and_does_not_subtract_maximum():
    rows = basic_records(30)
    rows += [
        {"kind": "uart", "t": t, "line": perf(at=at, n=n, max_us=999)}
        for t, at, n in [
            (9, 9_000_000, 100),
            (11, 11_000_000, 120),
            (16, 16_000_000, 170),
            (26, 26_000_000, 270),
        ]
    ]
    report = runtime.summarize(rows)
    measured = report["windows"]["steady"]["perf"]["cycle"]
    assert measured["n"] == 150
    assert measured["sum_us"] == 7500
    assert measured["span_s"] == 15
    assert measured["first_received_s"] == 11
    assert measured["start_at_us"] == 11_000_000
    assert measured["max_us_cumulative"] == 999
    assert "max_us" not in measured
    assert measured["p95_ub_us"] == 100
    assert report["boot_unchanged"]  # Separate OTA and telemetry boot identities are valid.
    assert not report["capture_ok"]  # Missing channels cannot certify a comparison capture.


def test_histogram_overflow_is_unknown_upper_bound():
    before = runtime.parse_perf(perf(n=0))
    after = runtime.parse_perf(perf(at=2_000_000, n=100, h=[0] * 16 + [100]))
    delta = runtime.histogram_delta(before, after)
    assert delta["p95_ub_us"] is None
    assert delta["p99_ub_us"] is None
    assert delta["overflow_n"] == 100
    assert runtime.histogram_bound([1] + [0] * 16, 0.99) == 0


@pytest.mark.parametrize(
    "changes", [{"sat": 1}, {"n": 9}, {"core": 1}, {"cfg": 1}, {"at_us": 1_000_000}]
)
def test_bad_perf_delta_is_not_silently_used(changes):
    before = runtime.parse_perf(perf())
    after = runtime.parse_perf(perf(**{"at": 2_000_000, "n": 20, **changes}))
    with pytest.raises(ValueError):
        runtime.histogram_delta(before, after)


@pytest.mark.parametrize(
    "line",
    ["Perf: v=1", perf() + " n=3", perf(h=[1]), perf(v=2), perf(n=5, h=[0] * 17), perf(core=7)],
)
def test_corrupt_perf_record_rejected(line):
    with pytest.raises(ValueError):
        runtime.parse_perf(line)


@pytest.mark.parametrize(
    "changes",
    [
        {"healthy": False},
        {"confirmed": False},
        {"actuators": True},
        {"updating": True},
        {"image_sha256": "b" * 64},
        {"boot": "changed"},
    ],
)
def test_https_identity_and_health_gate(changes):
    with pytest.raises(runtime.CaptureError):
        runtime.validate_status(status(**changes), IMAGE, "ota-boot")


def test_unsafe_ack_never_passes_as_stationary():
    command = {"seq": 1, "type": "STOP"}
    ack = {
        "seq": 1,
        "type": "STOP",
        "ok": True,
        "applied": True,
        "safe_latched": True,
        "actuators": False,
    }
    runtime.validate_ack(ack, command)
    for changes in (
        {"seq": True},
        {"safe_latched": False},
        {"actuators": True},
        {"ok": False},
        {"type": "MOVE"},
    ):
        with pytest.raises(runtime.CaptureError):
            runtime.validate_ack({**ack, **changes}, command)


def test_summary_command_window_is_send_time_and_ack_loss_is_explicit():
    rows = basic_records()
    rows += [
        {"kind": "sent", "t": 9.9, "seq": 1},
        {"kind": "sent", "t": 10.1, "seq": 2},
        {"kind": "sent", "t": 10.2, "seq": 3},
        {"kind": "ack", "t": 10.0, "ack": {"seq": 1}},
        {"kind": "ack", "t": 10.12, "ack": {"seq": 2}},
        {"kind": "ack", "t": 10.15, "ack": {"seq": 2}},
    ]
    windows = runtime.summarize(rows)["windows"]
    assert windows["warmup"]["commands"]["acked"] == 1
    steady = windows["steady"]["commands"]
    assert (steady["sent"], steady["acked"], steady["unacknowledged"], steady["duplicates"]) == (
        2,
        1,
        1,
        1,
    )
    assert steady["ack_rtt_ms"]["mean"] == pytest.approx(20)


def test_summary_separates_telemetry_sessions_and_reports_gap():
    rows = basic_records(25)
    for offset, sequence in [(0, 1), (0.1, 2), (0.2, 4), (0.3, 4)]:
        message = telemetry(sequence)
        rows.append({"kind": "telemetry_raw", "t": 11 + offset, "raw": json.dumps(message)})
    result = runtime.summarize(rows)["windows"]["steady"]["telemetry"]
    assert result["accepted"] == 3
    assert result["discarded"] == 1
    assert result["sessions"][0]["sequence_gaps"] == 1


def test_late_worker_health_failure_and_boot_change_cannot_report_success():
    rows = basic_records()
    rows.append(
        {
            "kind": "https",
            "phase": "during",
            "t": 20.1,
            "status": status(healthy=False),
            "fatal": True,
            "error": "health failed",
        }
    )
    assert not runtime.summarize(rows)["capture_ok"]
    rows = basic_records()
    rows.append({"kind": "telemetry", "t": 11, "telemetry": telemetry(1, "new-boot")})
    assert not runtime.summarize(rows)["boot_unchanged"]


def test_https_worker_exits_on_unhealthy_status_and_keeps_response():
    events = queue.Queue()
    client = SimpleNamespace(status=lambda: status(healthy=False))
    runtime.https_worker(client, events, threading.Event(), 0, IMAGE, "ota-boot")
    row = events.get_nowait()
    assert row["fatal"] is True
    assert row["status"]["healthy"] is False
    assert row["rtt_ms"] >= 0


def test_uart_worker_preserves_original_bytes_and_partial_tail():
    stopped = threading.Event()

    class SerialInput:
        in_waiting = 20

        def read(self, _size):
            stopped.set()
            return b"Sensor status: imu_valid=1\r\n\xffpartial"

    output, events = io.BytesIO(), queue.Queue()
    runtime.uart_worker(SerialInput(), output, events, stopped, 0)
    assert output.getvalue() == b"Sensor status: imu_valid=1\r\n\xffpartial"
    assert events.get_nowait()["line"] == "Sensor status: imu_valid=1"
    assert events.get_nowait()["kind"] == "uart_partial"


def test_serial_open_sets_inactive_control_lines_before_open(monkeypatch):
    actions = []

    class PassiveSerial:
        def __init__(self, **kwargs):
            assert kwargs["port"] is None

        def __setattr__(self, key, value):
            actions.append((key, value))

        def open(self):
            actions.append(("open", None))

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=PassiveSerial))
    runtime.open_passive_serial("COM5", 115200)
    assert actions == [("dtr", False), ("rts", False), ("port", "COM5"), ("open", None)]


def test_summarize_cli_never_constructs_a_network_client(tmp_path, monkeypatch, capsys):
    path = tmp_path / "capture.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in basic_records()), encoding="utf-8")
    client = MagicMock(side_effect=AssertionError("network used in offline mode"))
    monkeypatch.setattr(runtime, "RobotOta", client)
    assert runtime.main(["summarize", str(path)]) == 1  # Deliberately incomplete measurement.
    assert json.loads(capsys.readouterr().out)["boot_unchanged"]
    client.assert_not_called()


def test_existing_capture_is_never_overwritten(tmp_path, monkeypatch):
    path, config = tmp_path / "capture.jsonl", tmp_path / "config.json"
    path.write_text("preserve", encoding="utf-8")
    config.write_text(json.dumps({"host": "192.168.0.39"}), encoding="utf-8")
    monkeypatch.setattr(runtime, "RobotOta", MagicMock())
    assert (
        runtime.main(
            [
                "capture",
                "--config",
                str(config),
                "--pc-ip",
                "192.168.0.29",
                "--device",
                "robot",
                "--expected-image-sha256",
                IMAGE,
                "--expected-core",
                "0",
                "--output",
                str(path),
                "--uart-output",
                str(tmp_path / "uart.log"),
            ]
        )
        == 1
    )
    assert path.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "224.0.0.1", "localhost"])
def test_capture_requires_explicit_lan_address(address):
    with pytest.raises(SystemExit):
        runtime.main(
            [
                "capture",
                "--config",
                "unused.json",
                "--pc-ip",
                address,
                "--device",
                "robot",
                "--expected-image-sha256",
                IMAGE,
                "--expected-core",
                "0",
                "--output",
                "unused.jsonl",
                "--uart-output",
                "unused.log",
            ]
        )
