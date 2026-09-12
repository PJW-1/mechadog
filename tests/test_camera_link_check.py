"""Camera measurement tests use only mock HTTP/serial transports, never device ports."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from host.vision.stream_client import DEFAULT_BOUNDARY
from tools.camera_link_check import SerialCapture, capture, error_text, metrics, validate_url

URL = "http://192.168.0.42:81/stream"
JPEG = b"\xff\xd8test\xff\xd9"


def packet(payload: bytes = JPEG) -> bytes:
    return (
        (
            f"--{DEFAULT_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode()
        + payload
        + b"\r\n"
    )


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class Response:
    headers = {"Content-Type": f"multipart/x-mixed-replace; boundary={DEFAULT_BOUNDARY}"}
    interval_s = 0.1

    def __init__(self, clock, chunks):
        self.clock = clock
        self.chunks = iter(chunks)
        self.closed = False
        self.reads = 0

    def read1(self, _size):
        self.reads += 1
        self.clock.now += self.interval_s
        chunk = next(self.chunks, b"")
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self):
        self.closed = True


def run(tmp_path, chunks, *, decoder=None, seconds=0.3, **kwargs):
    clock = Clock()
    response = Response(clock, chunks)
    calls = []

    def opener(url, *, timeout):
        calls.append((url, timeout))
        return response

    def default_decoder(_payload):
        clock.now += 0.001
        return SimpleNamespace(shape=(480, 640, 3))

    result = capture(
        URL,
        seconds,
        tmp_path / "new",
        clock=clock,
        opener=opener,
        decoder=decoder or default_decoder,
        **kwargs,
    )
    return result, response, calls


def test_rate_metrics_require_full_window_rate_and_vga():
    rows = [
        {
            "received_monotonic_s": i / 20,
            "decode_us": 100 + i,
            "bytes": 1000,
            "width": 640,
            "height": 480,
        }
        for i in range(20)
    ]
    result = metrics(rows, 1.0)
    assert result["fps"] == pytest.approx(20)
    assert result["gap_p95_ms"] == pytest.approx(50)
    assert result["decode_p95_us"] == 118
    assert result["vga_15fps_rate_pass"] is True
    assert metrics(rows, 2.0)["vga_15fps_rate_pass"] is False
    rows[0]["width"] = 320
    assert metrics(rows, 1.0)["vga_15fps_rate_pass"] is False
    assert metrics([], 1.0)["fps"] is None


def test_first_decode_cost_is_separate_but_not_removed_from_total_metrics():
    rows = [
        {
            "received_monotonic_s": i / 20,
            "decode_us": cost,
            "bytes": 1000,
            "width": 640,
            "height": 480,
        }
        for i, cost in enumerate([180000, 900, 1200])
    ]
    result = metrics(rows, 0.15)
    assert result["first_decode_us"] == 180000
    assert result["decode_after_first_p95_us"] == 1200
    assert result["decode_after_first_max_us"] == 1200
    assert result["decode_max_us"] == 180000
    assert result["decode_p95_us"] == 180000
    assert result["frames"] == 3
    assert metrics(rows[:1], 0.1)["decode_after_first_max_us"] is None
    assert metrics([], 1)["first_decode_us"] is None


@pytest.mark.parametrize("corrupt", [False, True])
def test_fast_vga_requires_clean_parser_for_acceptance(tmp_path, monkeypatch, corrupt):
    monkeypatch.setattr(Response, "interval_s", 0.01)
    first = (packet(b"not a JPEG") if corrupt else b"") + packet()
    result, response, _ = run(tmp_path, [first] + [packet()] * 30, seconds=0.12)
    assert response.closed and result["complete"] is True
    assert result["vga_15fps_rate_pass"] is True
    assert result["fps"] > 15
    assert result["vga_15fps_pass"] is (not corrupt)
    assert result["vga_15fps_failures"] == (
        ["parser_discarded_frames", "parser_resynchronizations"] if corrupt else []
    )


def test_fast_partial_stream_keeps_rate_but_cannot_pass_acceptance(tmp_path, monkeypatch):
    monkeypatch.setattr(Response, "interval_s", 0.01)
    result, _, _ = run(tmp_path, [packet(), packet(), b""], seconds=1)
    assert result["vga_15fps_rate_pass"] is True
    assert result["complete"] is False
    assert result["failure"] == "unexpected_eof"
    assert result["vga_15fps_pass"] is False
    assert result["vga_15fps_failures"] == ["capture_incomplete"]


def test_capture_records_frames_and_uses_read1_then_closes(tmp_path):
    result, response, calls = run(tmp_path, [packet()] * 4)
    assert result["complete"] is True
    assert result["frames"] == response.reads == 3
    assert calls == [(URL, 0.3)]
    assert response.closed
    assert result["decode_p95_us"] == pytest.approx(1000)
    assert result["parser"]["discarded"] == 0
    folder = tmp_path / "new"
    assert (folder / "first.jpg").read_bytes() == JPEG
    assert (folder / "last.jpg").read_bytes() == JPEG
    assert len((folder / "frames.jsonl").read_text().splitlines()) == 3
    assert json.loads((folder / "summary.json").read_text())["complete"] is True


def test_default_clock_is_high_resolution_performance_counter():
    assert capture.__kwdefaults__["clock"] is time.perf_counter


def test_explicit_diagnostic_read_timeout_is_recorded(tmp_path):
    result, response, calls = run(tmp_path, [packet(), b""], seconds=30, read_timeout=4)
    assert response.closed
    assert calls == [(URL, 4)]
    assert result["read_timeout_s"] == result["requested_read_timeout_s"] == 4
    assert result["clock_source"] == "injected_clock"


def test_read_timeout_does_not_exceed_capture_duration(tmp_path):
    result, _, calls = run(tmp_path, [packet()] * 4, seconds=0.3, read_timeout=4)
    assert calls == [(URL, 0.3)]
    assert result["requested_read_timeout_s"] == 4
    assert result["read_timeout_s"] == 0.3


def test_recovered_long_gap_fails_acceptance_despite_good_average_rate(tmp_path, monkeypatch):
    original_read = Response.read1

    def read_with_gap(self, size):
        if self.reads == 1:
            self.clock.now += 2.1
        return original_read(self, size)

    monkeypatch.setattr(Response, "interval_s", 0.01)
    monkeypatch.setattr(Response, "read1", read_with_gap)
    result, _, _ = run(tmp_path, [packet() * 4] * 100, seconds=3, read_timeout=4)
    assert result["complete"] is True
    assert result["vga_15fps_rate_pass"] is True
    assert result["gap_max_ms"] > 2000
    assert result["vga_15fps_pass"] is False
    assert result["vga_15fps_failures"] == ["frame_silence_over_2000ms"]


@pytest.mark.parametrize(
    "ending", [b"", TimeoutError("read stalled"), OSError("lost"), KeyboardInterrupt()]
)
def test_partial_frames_survive_eof_timeout_error_and_interrupt(tmp_path, ending):
    result, response, _ = run(tmp_path, [packet(), ending], seconds=1)
    assert result["complete"] is False
    assert result["vga_15fps_pass"] is False
    assert result["frames"] == 1
    assert response.closed
    assert (tmp_path / "new" / "last.jpg").read_bytes() == JPEG
    assert (tmp_path / "new" / "summary.json").exists()


def test_decode_error_still_closes_transport_and_records_failure(tmp_path):
    def broken_decoder(_payload):
        raise ValueError("invalid JPEG")

    result, response, _ = run(tmp_path, [packet()], decoder=broken_decoder)
    assert response.closed
    assert result["complete"] is False
    assert result["parser"]["frames"] == 1
    assert result["errors"] == ["ValueError: invalid JPEG"]


def test_bytes_without_frames_cannot_pass_a_full_capture(tmp_path):
    result, response, _ = run(tmp_path, [b"garbage"] * 5)
    assert response.closed
    assert result["complete"] is False
    assert result["failure"] == "no_frames"
    assert result["vga_15fps_pass"] is False


def test_cleanup_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    def broken_close(self):
        self.closed = True
        raise OSError("close failed")

    monkeypatch.setattr(Response, "close", broken_close)
    result, response, _ = run(tmp_path, [packet()] * 4)
    assert response.closed
    assert result["complete"] is False
    assert result["failure"] == "cleanup_error"
    assert result["frames"] == 3
    assert (tmp_path / "new" / "last.jpg").read_bytes() == JPEG


def test_existing_directory_is_never_overwritten(tmp_path):
    folder = tmp_path / "new"
    folder.mkdir()
    marker = folder / "summary.json"
    marker.write_text("preserve")
    with pytest.raises(FileExistsError):
        run(tmp_path, [])
    assert marker.read_text() == "preserve"


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@192.168.0.42:81/stream",
        "https://host:81/stream",
        "http://host:81/stream?token=x",
        "http://host:80/",
        "http://host:81/stream#x",
    ],
)
def test_bad_urls_are_refused_before_network_or_output(tmp_path, url):
    with pytest.raises(ValueError):
        capture(url, 1, tmp_path / "new")
    assert not (tmp_path / "new").exists()
    assert validate_url(URL) == URL


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), 3601])
def test_duration_is_finite(tmp_path, seconds):
    with pytest.raises(ValueError):
        capture(URL, seconds, tmp_path / "new")
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 31])
def test_read_timeout_is_positive_finite_and_bounded(tmp_path, timeout):
    with pytest.raises(ValueError):
        capture(URL, 10, tmp_path / "new", read_timeout=timeout)
    assert not (tmp_path / "new").exists()


class SerialPort:
    def __init__(self, **kwargs):
        assert kwargs == {"port": None, "baudrate": 115200, "timeout": 0.2}
        self.closed = False
        self.cancelled = threading.Event()
        self.read_started = threading.Event()
        self.sent = False

    def open(self):
        assert self.dtr is False and self.rts is False
        assert self.port == "COM7"

    def read(self, _size):
        self.read_started.set()
        if not self.sent:
            self.sent = True
            return b"STREAM_STATS fps=20\r\n"
        self.cancelled.wait(0.2)
        return b""

    def cancel_read(self):
        self.cancelled.set()

    def close(self):
        self.closed = True
        self.cancelled.set()


def test_uart_raw_capture_is_stopped_and_modem_lines_never_asserted(tmp_path):
    ports = []

    def factory(**kwargs):
        port = SerialPort(**kwargs)
        ports.append(port)
        return port

    reader = SerialCapture("COM7", tmp_path / "uart.log", factory)
    reader.start()
    assert ports[0].read_started.wait(1)
    reader.stop()
    assert ports[0].closed and ports[0].cancelled.is_set()
    assert reader._thread is not None and not reader._thread.is_alive()
    assert reader.errors == []
    assert (tmp_path / "uart.log").read_bytes() == b"STREAM_STATS fps=20\r\n"


def test_network_open_failure_still_closes_uart(tmp_path):
    port = SerialPort(port=None, baudrate=115200, timeout=0.2)

    def opener(_url, *, timeout):
        assert timeout == 1.0
        raise TimeoutError("connect timed out")

    result = capture(
        URL,
        2,
        tmp_path / "new",
        serial_port="COM7",
        opener=opener,
        serial_factory=lambda **_kwargs: port,
    )
    assert not result["complete"] and port.closed
    assert result["errors"] == ["TimeoutError: connect timed out"]
    assert result["clock_source"] == "time.perf_counter"
    assert result["clock_resolution_s"] > 0
    assert not any(t.name == "camera-check-uart" for t in threading.enumerate())


def test_error_printing_is_ascii_safe():
    assert error_text(ValueError("연결 실패")).isascii()
