"""Bounded camera-only MJPEG measurement; never sends robot commands or changes profiles.

    python tools/camera_link_check.py --url http://192.168.0.42:81/stream \
        --seconds 30 --output-dir logs/camera-new-run --serial-port COM7

The output directory must not exist. Arrival times are PC monotonic times, not camera
capture times. UART, partial frames and failure summaries survive a failed capture.
One successful VGA >= 15 fps run is only one rate check, not complete WBS acceptance.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.vision.stream_client import MjpegParser, boundary_from, decode_jpeg  # noqa: E402

READ_TIMEOUT_S = 1.0
MAX_READ_TIMEOUT_S = 30.0
# Same discontinuity threshold as config/config.yaml vision.stall_timeout_ms.
# A longer diagnostic socket timeout must not make such a gap pass acceptance.
FRAME_STALL_LIMIT_MS = 2000.0
READ_BYTES = 16384
MAX_SECONDS = 3600.0


def validate_url(url: str) -> str:
    """Accept an explicit camera HTTP stream; refuse credentials and redirects elsewhere."""
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path != "/stream"
        or parts.port != 81
    ):
        raise ValueError("URL must be http://<camera>:81/stream without credentials or query")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        raise ValueError("camera stream redirects are refused")


def open_stream(url: str, *, timeout: float):
    # No OS proxy credentials and no redirect from a camera to another service.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(url, timeout=timeout)


def error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".encode("ascii", "backslashreplace").decode("ascii")


def percentile(values: Sequence[float], fraction: float = 0.95) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def metrics(rows: Sequence[dict[str, Any]], elapsed_s: float) -> dict[str, Any]:
    """Report interval rate and full-window rate separately, including start/end silence."""
    times = [row["received_monotonic_s"] for row in rows]
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:], strict=False)]
    span = times[-1] - times[0] if len(times) >= 2 else 0.0
    fps = (len(rows) - 1) / span if span > 0 else None
    window_fps = len(rows) / elapsed_s if elapsed_s > 0 else 0.0
    vga = bool(rows) and all((r["width"], r["height"]) == (640, 480) for r in rows)
    return {
        "frames": len(rows),
        "elapsed_s": elapsed_s,
        "observed_span_s": span,
        "fps": fps,
        "window_fps": window_fps,
        "gap_p95_ms": percentile(gaps),
        "gap_max_ms": max(gaps) if gaps else None,
        "decode_p95_us": percentile([r["decode_us"] for r in rows]),
        "decode_max_us": max((r["decode_us"] for r in rows), default=None),
        # Keep the first-call outlier in all-frame metrics and acceptance. Lazy imports
        # may make startup slower; show later calls separately instead of hiding it.
        "first_decode_us": rows[0]["decode_us"] if rows else None,
        "decode_after_first_p95_us": percentile([r["decode_us"] for r in rows[1:]]),
        "decode_after_first_max_us": max((r["decode_us"] for r in rows[1:]), default=None),
        "jpeg_mean_bytes": sum(r["bytes"] for r in rows) / len(rows) if rows else None,
        "all_vga": vga,
        # Full-window rate prevents a short burst followed by silence passing the check.
        "vga_15fps_rate_pass": bool(vga and fps is not None and fps >= 15 and window_fps >= 15),
        "rate_check_scope": "one run; not end-to-end latency or complete WBS acceptance",
    }


class SerialCapture:
    """No reset: configure deasserted modem lines before opening the port."""

    def __init__(self, port: str, path: Path, factory: Callable[..., Any] | None = None):
        if factory is None:
            import serial

            factory = serial.Serial
        self._port = factory(port=None, baudrate=115200, timeout=0.2)
        self._port.dtr = False
        self._port.rts = False
        self._port.port = port
        self._path = path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []
        self.bytes = 0

    def start(self) -> None:
        self._port.open()
        self._thread = threading.Thread(target=self._read, name="camera-check-uart", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            with self._path.open("xb") as log:
                while not self._stop.is_set():
                    chunk = self._port.read(4096)
                    if chunk:
                        log.write(chunk)
                        log.flush()
                        self.bytes += len(chunk)
        except Exception as exc:  # noqa: BLE001 — preserve worker failures in the summary
            if not self._stop.is_set():
                self.errors.append(error_text(exc))

    def stop(self) -> None:
        self._stop.set()
        try:
            cancel = getattr(self._port, "cancel_read", None)
            if cancel is not None:
                cancel()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(error_text(exc))
        finally:
            try:
                self._port.close()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(error_text(exc))
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                self.errors.append("SerialCleanupError: UART thread did not stop")


def capture(
    url: str,
    seconds: float,
    output_dir: Path,
    *,
    serial_port: str | None = None,
    read_timeout: float = READ_TIMEOUT_S,
    opener: Callable[..., Any] = open_stream,
    decoder: Callable[..., Any] = decode_jpeg,
    clock: Callable[[], float] = time.perf_counter,
    serial_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    validate_url(url)
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS:
        raise ValueError("seconds must be finite and within (0, 3600]")
    if not math.isfinite(read_timeout) or not 0 < read_timeout <= MAX_READ_TIMEOUT_S:
        raise ValueError("read-timeout must be finite and within (0, 30]")
    actual_read_timeout = min(read_timeout, seconds)
    output_dir.mkdir(parents=True, exist_ok=False)
    started_utc = datetime.now(UTC).isoformat()
    started = clock()
    deadline = started + seconds
    rows: list[dict[str, Any]] = []
    parser = MjpegParser()
    response = None
    serial_capture = None
    last_jpeg = None
    errors: list[str] = []
    complete = False
    reason = "not_started"
    try:
        with (output_dir / "frames.jsonl").open("x", encoding="utf-8") as log:
            if serial_port is not None:
                serial_capture = SerialCapture(serial_port, output_dir / "uart.log", serial_factory)
                serial_capture.start()
            response = opener(url, timeout=actual_read_timeout)
            content_type = response.headers.get("Content-Type", "")
            if "multipart/x-mixed-replace" not in content_type.lower():
                raise ValueError("camera response is not multipart MJPEG")
            parser = MjpegParser(boundary_from(content_type))
            while clock() < deadline:
                # HTTPResponse.read1 returns available bytes without filling a fixed-size block.
                chunk = response.read1(READ_BYTES)
                received = clock()
                if not chunk:
                    reason = "unexpected_eof"
                    break
                for frame in parser.feed(chunk, int(received * 1000)):
                    decode_started = clock()
                    image = decoder(frame.payload)
                    decode_us = (clock() - decode_started) * 1_000_000
                    height, width = image.shape[:2]
                    row = {
                        "seq": frame.seq,
                        "received_monotonic_s": received,
                        "received_elapsed_s": received - started,
                        "decode_us": decode_us,
                        "width": int(width),
                        "height": int(height),
                        "bytes": frame.size_bytes,
                    }
                    log.write(json.dumps(row, ensure_ascii=True) + "\n")
                    log.flush()
                    rows.append(row)
                    if len(rows) == 1:
                        (output_dir / "first.jpg").write_bytes(frame.payload)
                    last_jpeg = frame.payload
            else:
                complete = True
                reason = "duration_reached"
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 — preserve partial evidence
        errors.append(error_text(exc))
        reason = "interrupted" if isinstance(exc, KeyboardInterrupt) else "capture_error"
    finally:
        ended = clock()
        if response is not None:
            try:
                response.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(error_text(exc))
        if serial_capture is not None:
            serial_capture.stop()
            errors.extend(serial_capture.errors)
        if last_jpeg is not None:
            (output_dir / "last.jpg").write_bytes(last_jpeg)
    if complete and errors:
        reason = "cleanup_error"
    elif complete and not rows:
        reason = "no_frames"
    complete = complete and not errors and bool(rows)
    rate_metrics = metrics(rows, max(0.0, ended - started))
    first_wait = rows[0]["received_elapsed_s"] if rows else ended - started
    end_silence = ended - rows[-1]["received_monotonic_s"] if rows else ended - started
    maximum_silence_ms = max(
        first_wait * 1000, end_silence * 1000, rate_metrics["gap_max_ms"] or 0.0
    )
    acceptance_failures = []
    if not complete:
        acceptance_failures.append("capture_incomplete")
    if not rate_metrics["all_vga"]:
        acceptance_failures.append("no_frames" if not rows else "non_vga_frames")
    if rate_metrics["fps"] is None or rate_metrics["fps"] < 15:
        acceptance_failures.append("interval_rate_below_15fps_or_unmeasured")
    if rate_metrics["window_fps"] < 15:
        acceptance_failures.append("window_rate_below_15fps")
    if parser.stats.discarded:
        acceptance_failures.append("parser_discarded_frames")
    if parser.stats.resyncs:
        acceptance_failures.append("parser_resynchronizations")
    if maximum_silence_ms > FRAME_STALL_LIMIT_MS:
        acceptance_failures.append("frame_silence_over_2000ms")
    summary = {
        "started_utc": started_utc,
        "url": url,
        "requested_seconds": seconds,
        "requested_read_timeout_s": read_timeout,
        "read_timeout_s": actual_read_timeout,
        "clock_source": "time.perf_counter" if clock is time.perf_counter else "injected_clock",
        "clock_resolution_s": (
            time.get_clock_info("perf_counter").resolution if clock is time.perf_counter else None
        ),
        "complete": complete,
        "failure": None if complete else reason,
        "errors": errors,
        "parser": asdict(parser.stats),
        "uart_bytes": serial_capture.bytes if serial_capture is not None else None,
        **rate_metrics,
        "vga_15fps_pass": not acceptance_failures,
        "vga_15fps_failures": acceptance_failures,
        "first_frame_wait_s": first_wait if rows else None,
        "end_silence_s": end_silence,
        "max_frame_silence_ms": maximum_silence_ms,
        "frame_stall_limit_ms": FRAME_STALL_LIMIT_MS,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--read-timeout", type=float, default=READ_TIMEOUT_S)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--serial-port", default=None)
    args = parser.parse_args(argv)
    try:
        result = capture(
            args.url,
            args.seconds,
            args.output_dir,
            serial_port=args.serial_port,
            read_timeout=args.read_timeout,
        )
    except (OSError, ValueError) as exc:
        print(error_text(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
