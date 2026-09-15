"""4.1.4 수신 도구의 관측 계산을 검증한다. 실제 UDP 소켓을 열지 않는다."""

import io
import json

import pytest
from conftest import FIXTURES, load_jsonl

from tools.telemetry_probe import ProbeStats, capture, exit_code, main


def packet(seq=1, device="dog-a", boot="boot-a", **overrides):
    # Actual protocol golden input; only the session/sequence varies per test.
    msg = dict(load_jsonl(FIXTURES / "telemetry_samples.jsonl")[0])
    msg.update(seq=seq, device_id=device, boot_id=boot, **overrides)
    return json.dumps(msg).encode()


class FakeSocket:
    """No send/bind methods: tests cannot communicate with real hardware."""

    def __init__(self, events):
        self.now = 0.0
        self.events = iter(events)
        self.timeouts = []

    def clock(self):
        return self.now

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def recvfrom(self, _size):
        event = next(self.events, None)
        if event is None:
            self.now += self.timeouts[-1]
            raise TimeoutError
        seconds, value = event
        self.now = seconds
        if isinstance(value, BaseException):
            raise value
        return value, ("127.0.0.1", 9999)


def test_golden_packet_keeps_sensor_fields_without_arbitrary_extras():
    stats = ProbeStats()
    record = stats.ingest(packet(secret="must not log"), 0.1)
    assert record["kind"] == "telemetry"
    assert record["received_monotonic_s"] == 0.1
    assert set(record["telemetry"]["imu"]) == {"pitch", "roll", "yaw"}
    assert "secret" not in record["telemetry"]
    summary = stats.summary(0, 0.2)
    assert summary["accepted"] == 1
    assert summary["rate_check"] == "unverified"
    assert exit_code(summary) == 2


def test_rate_uses_n_minus_one_intervals_and_reports_capture_silence():
    stats = ProbeStats()
    for index in range(101):
        stats.ingest(packet(index + 1), 3.0 + index / 10)
    summary = stats.summary(0, 30)
    session = summary["sessions"][0]
    assert session["receive_hz"] == pytest.approx(10)
    assert session["observed_span_s"] == 10
    assert session["end_silence_s"] == 17
    assert session["max_interval_s"] == pytest.approx(0.1)
    assert summary["rate_check"] == "pass"
    assert summary["success"] is False
    assert summary["capture_check"] == "fail"
    assert exit_code(summary) == 1
    full_window = stats.summary(3, 13.1)
    assert full_window["success"] is True
    assert exit_code(full_window) == 0


def test_duplicate_out_of_order_and_missing_sequence_are_not_normal_samples():
    stats = ProbeStats()
    for index, seq in enumerate([8, 9, 9, 7, 12]):
        stats.ingest(packet(seq), index / 10)
    summary = stats.summary(0, 1)
    session = summary["sessions"][0]
    assert summary["accepted"] == 3
    assert summary["discard_reasons"] == {"seq 역전·중복": 2}
    assert session["sequence_gaps"] == 2
    assert session["first_seq"] == 8
    assert session["first_seq_is_one"] is False
    assert session["new_boot_seq1"] == "not_applicable"


def test_devices_and_boots_stay_separate_and_unseen_seq_one_is_not_failure():
    stats = ProbeStats()
    stats.ingest(packet(5), 0)
    stats.ingest(packet(1, device="dog-b"), 0.1)
    stats.ingest(packet(1, boot="boot-new"), 0.2)
    stats.ingest(packet(6), 0.3)  # delayed old boot, not another reboot
    stats.ingest(packet(4, boot="boot-third"), 0.4)
    sessions = stats.summary(0, 1)["sessions"]
    assert len(sessions) == 4
    assert sessions[0]["accepted"] == 2
    assert sessions[1]["new_boot_observed"] is False
    assert sessions[2]["new_boot_seq1"] == "observed"
    assert sessions[3]["new_boot_seq1"] == "not_observed"
    assert sessions[3]["rate_status"] == "unverified"


def test_device_filter_and_invalid_payload_logs_do_not_copy_raw_data():
    stats = ProbeStats("dog-a")
    assert stats.ingest(packet(device="dog-b"), 0)["kind"] == "filtered"
    assert stats.ingest(packet(), 0.1)["kind"] == "telemetry"
    secret = b'broken-json credential="private"'
    invalid = stats.ingest(secret, 0.2)
    unknown = stats.ingest(packet(seq=2, state="SECRET UNKNOWN STATE"), 0.3)
    assert "private" not in json.dumps(invalid)
    assert "SECRET" not in json.dumps(unknown)
    summary = stats.summary(0, 1)
    assert summary["filtered"] == 1
    assert summary["accepted"] == 1
    assert summary["discarded"] == 2
    # Protocol-valid escaped identity strings must not crash UTF-8 output.
    sock = FakeSocket([(0.1, packet(device="dog-\ud800"))])
    output = io.StringIO()
    capture(sock, ProbeStats(), 0.2, clock=sock.clock, output=output)
    output.getvalue().encode("utf-8")


def test_timeout_uses_remaining_deadline_and_empty_capture_fails():
    sock = FakeSocket([(0.25, TimeoutError())])
    output = io.StringIO()
    summary = capture(sock, ProbeStats(), 1.0, clock=sock.clock, output=output)
    assert sock.timeouts == [1.0, 0.75]
    assert summary["capture_elapsed_s"] == 1.0
    assert summary["accepted"] == 0
    assert exit_code(summary) == 1
    assert json.loads(output.getvalue())["kind"] == "summary"
    late = FakeSocket([(1.1, packet())])
    assert capture(late, ProbeStats(), 1.0, clock=late.clock)["accepted"] == 0


def test_interrupt_finishes_with_partial_unverified_summary():
    sock = FakeSocket([(0.1, packet()), (0.2, KeyboardInterrupt())])
    output = io.StringIO()
    summary = capture(sock, ProbeStats(), 30, clock=sock.clock, output=output)
    assert summary["interrupted"] is True
    assert summary["accepted"] == 1
    assert exit_code(summary) == 2
    assert len(output.getvalue().splitlines()) == 2


def test_out_of_range_rate_and_output_refuses_existing_file(tmp_path, monkeypatch):
    stats = ProbeStats()
    for index in range(101):
        stats.ingest(packet(index + 1), index / 5)
    assert stats.summary(0, 20)["rate_check"] == "fail"
    target = tmp_path / "capture.jsonl"
    target.write_text("existing measurement", encoding="utf-8")

    def no_socket(*_args, **_kwargs):
        pytest.fail("existing output must be rejected before opening a socket")

    monkeypatch.setattr("tools.telemetry_probe.socket.socket", no_socket)
    assert main(["--output", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "existing measurement"
