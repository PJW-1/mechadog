"""Stationary runtime capture and offline comparison; never flash or reset a robot.

capture requires an explicit PC IPv4 address, pinned-TLS config, expected image,
device identity and core. Only STOP and STATE IDLE are transmitted. UART bytes
and JSONL are exclusive new files and remain available when capture fails.
summarize and compare are entirely offline and do not import pyserial.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import queue
import re
import select
import socket
import sys
import threading
import time
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, serialize  # noqa: E402
from tools.ota_update import RobotOta  # noqa: E402
from tools.telemetry_probe import ProbeStats  # noqa: E402

BINS_US = (
    0,
    100,
    250,
    500,
    1000,
    2000,
    4000,
    8000,
    12000,
    20000,
    30000,
    40000,
    50000,
    100000,
    250000,
    1000000,
)
METRICS = ("cycle", "wake", "imu", "sonar", "adc")
COMMAND_PERIOD_S = 0.1
ACK_TIMEOUT_S = 2.0


class CaptureError(RuntimeError):
    """A safety/identity/measurement invariant failed; preserve the evidence."""


def parse_fields(line: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in re.findall(r"(\w+)=([^\s]+)", line):
        if key in fields:
            raise ValueError(f"duplicate UART field: {key}")
        if re.fullmatch(r"-?\d+", value):
            fields[key] = int(value)
        else:
            fields[key] = value
    return fields


def parse_perf(line: str) -> dict[str, Any] | None:
    if not line.startswith("Perf: "):
        return None
    fields = parse_fields(line)
    required = (
        "v",
        "at_us",
        "core",
        "cfg",
        "stack_b",
        "drop",
        "pub_drop",
        "metric",
        "n",
        "sum_us",
        "max_us",
        "p95_ub_us",
        "p99_ub_us",
        "sat",
        "h",
    )
    if any(key not in fields for key in required):
        raise ValueError("incomplete Perf record")
    if fields["v"] != 1 or fields["metric"] not in METRICS:
        raise ValueError("unsupported Perf version/metric")
    for key in required:
        if key not in ("metric", "h") and (type(fields[key]) is not int or fields[key] < 0):
            raise ValueError(f"invalid Perf integer: {key}")
    if fields["core"] not in (0, 1) or fields["cfg"] not in (0, 1) or fields["sat"] not in (0, 1):
        raise ValueError("invalid Perf core/config/saturation")
    try:
        histogram = [int(item) for item in fields["h"].split(",")]
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid Perf histogram") from exc
    if len(histogram) != len(BINS_US) + 1 or any(not 0 <= n <= 0xFFFFFFFF for n in histogram):
        raise ValueError("invalid Perf histogram bins")
    if not fields["sat"] and sum(histogram) != fields["n"]:
        raise ValueError("Perf histogram count differs from n")
    fields["h"] = histogram
    return fields


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean": sum(values) / len(values) if values else None,
        "max": max(values, default=None),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
    }


def histogram_bound(histogram: list[int], fraction: float) -> int | None:
    """Inclusive bucket upper bound; overflow has no finite bound."""
    if not sum(histogram):
        return None
    target, cumulative = math.ceil(sum(histogram) * fraction), 0
    for index, count in enumerate(histogram):
        cumulative += count
        if cumulative >= target:
            return BINS_US[index] if index < len(BINS_US) else None
    return None


def histogram_delta(before: dict, after: dict) -> dict:
    for key in ("metric", "core", "cfg"):
        if before[key] != after[key]:
            raise ValueError(f"Perf identity changed: {key}")
    if before["sat"] or after["sat"]:
        raise ValueError("saturated Perf counters cannot be differenced")
    if after["at_us"] <= before["at_us"]:
        raise ValueError("Perf timestamp did not advance")
    histogram = [b - a for a, b in zip(before["h"], after["h"], strict=True)]
    count, total = after["n"] - before["n"], after["sum_us"] - before["sum_us"]
    if min(histogram) < 0 or count < 0 or total < 0 or sum(histogram) != count:
        raise ValueError("Perf counters reset or became inconsistent")
    return {
        "n": count,
        "sum_us": total,
        "mean_us": total / count if count else None,
        "p95_ub_us": histogram_bound(histogram, 0.95),
        "p99_ub_us": histogram_bound(histogram, 0.99),
        "h": histogram,
        "overflow_n": histogram[-1],
        "start_at_us": before["at_us"],
        "end_at_us": after["at_us"],
        "span_s": (after["at_us"] - before["at_us"]) / 1e6,
        "max_us_cumulative": after["max_us"],
        "max_scope": "cumulative since boot; not the differenced interval",
        "quantile_scope": "inclusive histogram bucket upper bound; null for overflow/empty",
    }


def validate_status(status: dict, expected_image: str, boot: str | None = None) -> str:
    if status.get("image_sha256") != expected_image:
        raise CaptureError("unexpected runtime image")
    if status.get("actuators") is not False:
        raise CaptureError("actuators are not explicitly OFF")
    if status.get("healthy") is not True or status.get("confirmed") is not True:
        raise CaptureError("runtime is not healthy and confirmed")
    if status.get("updating") is not False:
        raise CaptureError("OTA update active or unknown")
    observed = status.get("boot")
    if not isinstance(observed, str) or not observed or (boot is not None and observed != boot):
        raise CaptureError("runtime boot identity missing or changed")
    return observed


def validate_ack(message: dict, command: dict) -> None:
    if type(message.get("seq")) is not int or message["seq"] != command["seq"]:
        raise CaptureError("ACK sequence mismatch")
    if message.get("type") != command["type"] or message.get("ok") is not True:
        raise CaptureError("command was not accepted")
    if message.get("applied") is not True:
        raise CaptureError("command was not applied")
    if message.get("safe_latched") is not True or message.get("actuators") is not False:
        raise CaptureError("ACK did not confirm SAFE latched and actuators OFF")


def sensor_status_kind(fields: dict) -> str:
    """A failed nonblocking snapshot read is unavailable, never a valid sample."""
    valid = [fields.get(key) for key in ("imu_valid", "dist_valid", "batt_valid")]
    errors = [fields.get(key) for key in ("imu_error", "dist_error", "batt_error")]
    if valid == [0, 0, 0] and errors == ["snapshot_busy"] * 3:
        return "snapshot_busy"
    if valid == [1, 1, 1] and errors == ["none"] * 3:
        return "valid"
    return "fault"


def summarize(records: list[dict]) -> dict:
    metadata = next((row for row in records if row.get("kind") == "metadata"), {})
    end_rows = [row for row in records if row.get("kind") == "capture_end"]
    ended = end_rows[-1]["t"] if end_rows else max((r.get("t", 0) for r in records), default=0)
    warmup = metadata.get("warmup_s", 10.0)
    errors = [row for row in records if row.get("kind") == "error"]
    perf_rows, sensor_rows, uart_errors = [], [], []
    for row in records:
        if row.get("kind") != "uart":
            continue
        try:
            if perf := parse_perf(row["line"]):
                perf_rows.append({**row, "perf": perf})
            elif row["line"].startswith("Sensor status: "):
                sensor_rows.append({**row, "sensor": parse_fields(row["line"])})
        except ValueError as exc:
            uart_errors.append({"t": row["t"], "error": str(exc)})
    boots = {r["status"].get("boot") for r in records if r.get("kind") == "https" and "status" in r}
    telemetry_boots = set()
    for row in records:
        if row.get("kind") == "telemetry" and isinstance(row.get("telemetry"), dict):
            telemetry_boots.add(row["telemetry"].get("boot_id"))
    result: dict[str, Any] = {
        "metadata": metadata,
        "elapsed_s": ended,
        "errors": errors,
        "completed": bool(end_rows and end_rows[-1].get("completed")),
        # OTA and telemetry generate independent boot IDs: compare within each channel.
        "boot_unchanged": len(boots) == 1 and len(telemetry_boots) == 1,
        "ota_boots": sorted(boots, key=str),
        "telemetry_boots": sorted(telemetry_boots, key=str),
        "uart_parse_errors": uart_errors,
        "windows": {},
        "limitations": [
            "RTT includes host scheduling and network latency.",
            "Sequence gaps are observations, not proven wire loss.",
            "Perf steady windows use first UART receipt >= warmup + 1 second guard.",
            "UART receipt and device clocks are unsynchronized; the guard is approximate, not proof of exact warmup exclusion.",
            "UART reset markers are observed only while capture is open.",
        ],
    }
    all_sent = {r["seq"]: r for r in records if r.get("kind") == "sent"}
    acks: dict[int, list[dict]] = {}
    for row in records:
        if row.get("kind") == "ack":
            acks.setdefault(row["ack"]["seq"], []).append(row)
    for label, start, stop in (("warmup", 0.0, min(warmup, ended)), ("steady", warmup, ended)):
        rows = [r for r in records if start <= r.get("t", -1) < stop]
        sent = [r for r in rows if r.get("kind") == "sent"]
        received = [acks[r["seq"]][0] for r in sent if r["seq"] in acks]
        rtts = [(r["t"] - all_sent[r["ack"]["seq"]]["t"]) * 1000 for r in received]
        stats = ProbeStats(metadata.get("device"))
        for row in rows:
            if row.get("kind") == "telemetry_raw":
                stats.ingest(row["raw"].encode("utf-8"), row["t"])
        https = [r for r in rows if r.get("kind") == "https" and r.get("phase") == "during"]
        sensors = [r["sensor"] for r in sensor_rows if start <= r["t"] < stop]
        sensor_kinds = Counter(sensor_status_kind(fields) for fields in sensors)
        sensor_flags = {
            key: dict(Counter(str(r.get(key, "missing")) for r in sensors))
            for key in ("imu_valid", "dist_valid", "batt_valid", "imu_timing")
        }
        window: dict[str, Any] = {
            "start_s": start,
            "end_s": stop,
            "commands": {
                "sent": len(sent),
                "acked": len(received),
                "unacknowledged": len(sent) - len(received),
                "duplicates": sum(max(0, len(acks.get(r["seq"], [])) - 1) for r in sent),
                "ack_rtt_ms": distribution(rtts),
                "send_lateness_ms": distribution([r.get("late_ms", 0) for r in sent]),
                "send_hz": (
                    (len(sent) - 1) / (sent[-1]["t"] - sent[0]["t"])
                    if len(sent) > 1 and sent[-1]["t"] > sent[0]["t"]
                    else None
                ),
            },
            "telemetry": stats.summary(start, stop),
            "https": {
                "requests": len(https),
                "errors": sum("error" in r for r in https),
                "rtt_ms": distribution([r["rtt_ms"] for r in https if "status" in r]),
                "target_attempt_hz": 1.0,
                "maximum_in_flight": 1,
                "completed_per_window_hz": len(https) / (stop - start) if stop > start else None,
                "backpressured_requests": sum(r.get("rtt_ms", 0) > 1000 for r in https),
                "load_scope": "one request at a time; RTT above 1s limits executed rate below the 1Hz target",
            },
            "sensors": {
                "rows": len(sensors),
                "valid_rows": sensor_kinds["valid"],
                "snapshot_busy_rows": sensor_kinds["snapshot_busy"],
                "fault_rows": sensor_kinds["fault"],
                "flags": sensor_flags,
                "loop_gap_max_us": max(
                    (r.get("loop_gap_max_us", 0) for r in sensors), default=None
                ),
                "heap_free_min": min(
                    (r["heap_free"] for r in sensors if "heap_free" in r), default=None
                ),
                "heap_min_cumulative": min(
                    (r["heap_min"] for r in sensors if "heap_min" in r), default=None
                ),
            },
            "perf": {},
            "reset_or_panic_lines": [
                r["line"]
                for r in rows
                if r.get("kind") == "uart"
                and re.search(r"rst:0x|receiver booting|Guru Meditation|panic", r["line"], re.I)
            ],
        }
        for metric in METRICS:
            perf_start = start + 1.0 if label == "steady" else start
            candidates = [
                r
                for r in perf_rows
                if perf_start <= r["t"] < stop and r["perf"]["metric"] == metric
            ]
            if len(candidates) < 2:
                window["perf"][metric] = {"verified": False, "reason": "fewer than two snapshots"}
                continue
            first, last = candidates[0], candidates[-1]
            try:
                for previous, current in zip(candidates, candidates[1:], strict=False):
                    histogram_delta(previous["perf"], current["perf"])
                window["perf"][metric] = {
                    "verified": True,
                    **histogram_delta(first["perf"], last["perf"]),
                    "first_received_s": first["t"],
                    "last_received_s": last["t"],
                    "receipt_cutoff_s": perf_start,
                    "cutoff_scope": "host receipt cutoff with 1s steady guard; device clock alignment approximate",
                    "core": last["perf"]["core"],
                    "cfg": last["perf"]["cfg"],
                    "stack_b_min_observed": min(r["perf"]["stack_b"] for r in candidates),
                    "drop_cumulative": last["perf"]["drop"],
                    "pub_drop_cumulative": last["perf"]["pub_drop"],
                }
            except ValueError as exc:
                window["perf"][metric] = {"verified": False, "reason": str(exc)}
        result["windows"][label] = window
    https_rows = [r for r in records if r.get("kind") == "https"]
    status_phases = {r.get("phase") for r in https_rows if "status" in r}
    result["https_fatal_errors"] = [r for r in https_rows if r.get("fatal")]
    issues = []
    for row in records:
        if row.get("kind") in ("invalid_ack", "unmatched_ack"):
            issues.append("invalid/unmatched ACK received")
        if row.get("kind") == "ack":
            try:
                validate_ack(row["ack"], all_sent[row["ack"]["seq"]])
            except (CaptureError, KeyError) as exc:
                issues.append(f"invalid stored ACK: {exc}")
    for row in https_rows:
        if "error" in row:
            issues.append("HTTPS request failed")
        if "status" in row:
            try:
                validate_status(row["status"], metadata.get("expected_image_sha256"))
            except CaptureError as exc:
                issues.append(str(exc))
    if uart_errors:
        issues.append("UART Perf parse failed")
    for row in sensor_rows:
        if sensor_status_kind(row["sensor"]) == "fault":
            issues.append("UART sensor invalid")
    for row in perf_rows:
        if any(row["perf"][key] != metadata.get("expected_core") for key in ("core", "cfg")):
            issues.append("Perf core differs from expected core")
    if any(
        r.get("kind") == "uart"
        and re.search(r"rst:0x|receiver booting|Guru Meditation|panic", r["line"], re.I)
        for r in records
    ):
        issues.append("UART reboot/panic observed")
    steady = result["windows"]["steady"]
    if steady["commands"]["sent"] == 0 or steady["commands"]["acked"] == 0:
        issues.append("no steady command/ACK evidence")
    if not steady["telemetry"]["success"]:
        issues.append("steady telemetry rate/continuity not verified")
    if any(len(acks.get(seq, [])) != 1 for seq in all_sent):
        issues.append("commands have missing or duplicate ACKs")
    send_hz = steady["commands"]["send_hz"]
    if send_hz is None or not 9 <= send_hz <= 11:
        issues.append("steady command send rate outside 9..11 Hz")
    minimum_sensor_rows = max(1, math.ceil(max(0, ended - warmup) * 0.7))
    if steady["sensors"]["valid_rows"] < minimum_sensor_rows:
        issues.append("insufficient steady sensor status rows")
    if steady["https"]["requests"] < max(1, math.ceil(max(0, ended - warmup) / 3)):
        issues.append("insufficient steady HTTPS observations")
    if not all(value["verified"] and value.get("n", 0) > 0 for value in steady["perf"].values()):
        issues.append("not all five steady Perf intervals are verified")
    result["measurement_issues"] = sorted(set(issues))
    result["measurement_complete"] = not issues
    result["capture_ok"] = (
        result["completed"]
        and not errors
        and result["boot_unchanged"]
        and not result["https_fatal_errors"]
        and {"before", "after"}.issubset(status_phases)
        and result["measurement_complete"]
    )
    return result


def open_passive_serial(port: str, baud: int):
    import serial  # Optional: offline analysis and unit tests need no serial package.

    connection = serial.Serial(port=None, baudrate=baud, timeout=0.1, write_timeout=0.1)
    connection.dtr = False
    connection.rts = False
    connection.port = port
    connection.open()
    return connection


def uart_worker(connection, raw_output, events, stopped, origin):
    pending = bytearray()
    try:
        while not stopped.is_set():
            chunk = connection.read(min(max(connection.in_waiting, 1), 8192))
            if not chunk:
                continue
            raw_output.write(chunk)
            raw_output.flush()
            pending.extend(chunk)
            if len(pending) > 65536:
                raise CaptureError("UART line exceeds 64 KiB")
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                events.put(
                    {
                        "kind": "uart",
                        "t": time.monotonic() - origin,
                        "line": line.decode("utf-8", errors="replace").rstrip("\r"),
                    }
                )
    except Exception as exc:
        events.put(
            {
                "kind": "error",
                "t": time.monotonic() - origin,
                "error": f"UART: {type(exc).__name__}: {exc}",
            }
        )
    finally:
        if pending:
            events.put(
                {"kind": "uart_partial", "t": time.monotonic() - origin, "bytes": len(pending)}
            )


def https_worker(client, events, stopped, origin, expected_image, expected_boot):
    next_request = time.monotonic()
    while not stopped.is_set():
        began = time.monotonic()
        row = {"kind": "https", "phase": "during", "started_t": began - origin}
        try:
            status = client.status()
            row["status"] = status
            validate_status(status, expected_image, expected_boot)
        except CaptureError as exc:
            row["error"] = str(exc)
            row["fatal"] = True
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["fatal"] = True
        row.update(t=time.monotonic() - origin, rtt_ms=(time.monotonic() - began) * 1000)
        events.put(row)
        if row.get("fatal"):
            return
        next_request = max(next_request + 1.0, time.monotonic())
        stopped.wait(max(0.0, next_request - time.monotonic()))


def run_capture(args) -> int:
    # Validate credentials locally before opening any device. Do not log config/token.
    config = json.loads(args.config.read_text(encoding="utf-8"))
    target = (str(ipaddress.IPv4Address(config["host"])), args.command_port)
    client = RobotOta(config)
    origin = time.monotonic()
    records: list[dict] = []
    events: queue.Queue = queue.Queue()
    stopped = threading.Event()
    threads = []
    completed = False
    with ExitStack() as stack:
        output = stack.enter_context(args.output.open("x", encoding="utf-8"))
        raw_output = stack.enter_context(args.uart_output.open("xb"))

        def record(row):
            records.append(row)
            output.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n")
            output.flush()

        def status_check(phase, boot=None):
            began = time.monotonic()
            status = client.status()
            record(
                {
                    "kind": "https",
                    "phase": phase,
                    "t": time.monotonic() - origin,
                    "rtt_ms": (time.monotonic() - began) * 1000,
                    "status": status,
                }
            )
            return validate_status(status, args.expected_image_sha256, boot)

        record(
            {
                "kind": "metadata",
                "schema": 1,
                "t": 0,
                "device": args.device,
                "expected_image_sha256": args.expected_image_sha256,
                "expected_core": args.expected_core,
                "pc_ip": args.pc_ip,
                "serial_port": args.serial_port,
                "duration_s": args.duration,
                "warmup_s": args.warmup,
                "started_utc": datetime.now(UTC).isoformat(),
                "histogram_inclusive_bounds_us": BINS_US,
                "uart_output": str(args.uart_output),
            }
        )
        try:
            boot = status_check("before")
            command_socket = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            command_socket.bind((args.pc_ip, 0))
            command_socket.setblocking(False)
            telemetry_socket = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                telemetry_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            telemetry_socket.bind((args.pc_ip, args.telemetry_port))
            telemetry_socket.setblocking(False)
            serial_connection = stack.enter_context(
                open_passive_serial(args.serial_port, args.baud)
            )
            # Capture duration/warmup are measured from the first STOP, not TLS preflight.
            origin = time.monotonic()
            record({"kind": "capture_start", "t": 0})
            for target_worker, worker_args in (
                (uart_worker, (serial_connection, raw_output, events, stopped, origin)),
                (https_worker, (client, events, stopped, origin, args.expected_image_sha256, boot)),
            ):
                thread = threading.Thread(target=target_worker, args=worker_args, daemon=True)
                threads.append(thread)
                thread.start()
            encoder = CommandEncoder()
            sent: dict[int, dict] = {}
            acknowledged = set()
            handshake = False
            next_send, deadline = origin, origin + args.duration
            telemetry_stats = ProbeStats(args.device)
            telemetry_boot = None

            def drain_events(check=True):
                for _ in range(256):
                    try:
                        row = events.get_nowait()
                    except queue.Empty:
                        return
                    record(row)
                    if not check:
                        continue
                    if row["kind"] == "error" or row.get("fatal"):
                        raise CaptureError(row.get("error", "worker failure"))
                    if row["kind"] == "uart":
                        line = row["line"]
                        if re.search(r"rst:0x|receiver booting|Guru Meditation|panic", line, re.I):
                            raise CaptureError("UART reboot/panic observed")
                        if (perf := parse_perf(line)) and (
                            perf["core"] != args.expected_core or perf["cfg"] != args.expected_core
                        ):
                            raise CaptureError("Perf observed core differs from expected core")
                        if line.startswith("Sensor status: "):
                            fields = parse_fields(line)
                            if sensor_status_kind(fields) == "fault":
                                raise CaptureError("UART sensor invalid")

            while (now := time.monotonic()) < deadline:
                drain_events()
                now = time.monotonic()
                if now >= deadline:
                    break
                if not handshake and now - origin > ACK_TIMEOUT_S:
                    raise CaptureError("STOP handshake ACK timeout")
                if handshake and not any(
                    now - sent[seq]["monotonic"] < ACK_TIMEOUT_S for seq in acknowledged
                ):
                    raise CaptureError("no recent safe ACK")
                if now >= next_send and (handshake or not sent):
                    command = (
                        encoder.build("STATE", state="IDLE") if handshake else encoder.build("STOP")
                    )
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    command_socket.sendto(serialize(command).encode(), target)
                    sent[command["seq"]] = {"command": command, "monotonic": now}
                    record(
                        {
                            "kind": "sent",
                            "t": now - origin,
                            **command,
                            "late_ms": max(0, now - next_send) * 1000,
                        }
                    )
                    # No catch-up burst after host scheduling delay.
                    next_send += COMMAND_PERIOD_S
                    if next_send <= now:
                        next_send = now + COMMAND_PERIOD_S
                ready, _, _ = select.select(
                    [command_socket, telemetry_socket],
                    [],
                    [],
                    min(0.02, max(0, deadline - time.monotonic())),
                )
                for channel in ready:
                    raw, source = channel.recvfrom(65535)
                    received = time.monotonic() - origin
                    if channel is command_socket:
                        if source != target:
                            record({"kind": "foreign_ack", "t": received})
                            continue
                        try:
                            ack = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            record({"kind": "invalid_ack", "t": received, "bytes": len(raw)})
                            continue
                        if (
                            not isinstance(ack, dict)
                            or type(ack.get("seq")) is not int
                            or ack["seq"] not in sent
                        ):
                            record({"kind": "unmatched_ack", "t": received})
                            continue
                        record({"kind": "ack", "t": received, "ack": ack})
                        validate_ack(ack, sent[ack["seq"]]["command"])
                        acknowledged.add(ack["seq"])
                        if ack["seq"] == 1:
                            handshake = True
                    else:
                        if source[0] != target[0]:
                            record({"kind": "foreign_telemetry", "t": received})
                            continue
                        record(
                            {
                                "kind": "telemetry_raw",
                                "t": received,
                                "raw": raw.decode("utf-8", errors="replace"),
                            }
                        )
                        decoded = telemetry_stats.ingest(raw, received)
                        record({**decoded, "t": received})
                        if decoded["kind"] == "telemetry":
                            telemetry = decoded["telemetry"]
                            if telemetry_boot is None:
                                telemetry_boot = telemetry["boot_id"]
                            elif telemetry["boot_id"] != telemetry_boot:
                                raise CaptureError("telemetry boot changed")
                            if telemetry.get("safety_latched") is not True:
                                raise CaptureError("telemetry SAFE latch lost or absent")
            drain_events()
            # Drain pending ACKs without extending the command/measurement window.
            ack_drain_deadline = time.monotonic() + 0.3
            while len(acknowledged) < len(sent) and time.monotonic() < ack_drain_deadline:
                ready, _, _ = select.select([command_socket], [], [], 0.02)
                if ready:
                    raw, source = command_socket.recvfrom(65535)
                    if source != target:
                        continue
                    ack = json.loads(raw)
                    received = time.monotonic() - origin
                    if (
                        not isinstance(ack, dict)
                        or type(ack.get("seq")) is not int
                        or ack["seq"] not in sent
                    ):
                        raise CaptureError("unmatched final ACK")
                    record({"kind": "ack", "t": received, "ack": ack})
                    validate_ack(ack, sent[ack["seq"]]["command"])
                    acknowledged.add(ack["seq"])
                drain_events()
            completed = handshake
        except (Exception, KeyboardInterrupt) as exc:
            record(
                {
                    "kind": "error",
                    "t": time.monotonic() - origin,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            stopped.set()
            for thread in threads:
                thread.join(timeout=18)
            while not events.empty():
                record(events.get_nowait())
            if any(thread.is_alive() for thread in threads):
                completed = False
                record(
                    {
                        "kind": "error",
                        "t": time.monotonic() - origin,
                        "error": "capture worker did not finish within timeout",
                    }
                )
            elif threads:
                try:
                    status_check("after", boot)
                except Exception as exc:
                    completed = False
                    record(
                        {
                            "kind": "error",
                            "t": time.monotonic() - origin,
                            "error": f"post-status: {type(exc).__name__}: {exc}",
                        }
                    )
            record(
                {
                    "kind": "capture_end",
                    "t": min(time.monotonic() - origin, args.duration),
                    "completed": completed,
                }
            )
    report = summarize(records)
    print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))
    return 0 if report["capture_ok"] else 1


def load_records(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"invalid JSONL at line {number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL line {number} is not an object")
            rows.append(row)
    return rows


def main(argv=None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--config", type=Path, required=True)
    capture_parser.add_argument("--pc-ip", required=True)
    capture_parser.add_argument("--device", required=True)
    capture_parser.add_argument("--expected-image-sha256", required=True)
    capture_parser.add_argument("--expected-core", type=int, choices=(0, 1), required=True)
    capture_parser.add_argument("--serial-port", default="COM5")
    capture_parser.add_argument("--baud", type=int, default=115200)
    capture_parser.add_argument("--command-port", type=int, default=5001)
    capture_parser.add_argument("--telemetry-port", type=int, default=5101)
    capture_parser.add_argument("--duration", type=float, default=90)
    capture_parser.add_argument("--warmup", type=float, default=10)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--uart-output", type=Path, required=True)
    summarize_parser = sub.add_parser("summarize")
    summarize_parser.add_argument("input", type=Path)
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("inputs", type=Path, nargs=2)
    args = parser.parse_args(argv)
    if args.mode == "capture":
        try:
            address = ipaddress.IPv4Address(args.pc_ip)
            if address.is_unspecified or address.is_multicast or address.is_loopback:
                raise ValueError("explicit PC LAN IPv4 address required")
        except ValueError as exc:
            parser.error(str(exc))
        if not re.fullmatch(r"[0-9a-f]{64}", args.expected_image_sha256):
            parser.error("expected-image-sha256 must be a lowercase 64-digit hex digest")
        if (
            not all(math.isfinite(v) for v in (args.duration, args.warmup))
            or not 0 <= args.warmup < args.duration
        ):
            parser.error("require finite 0 <= warmup < duration")
        if not args.device or not all(
            1 <= p <= 65535 for p in (args.command_port, args.telemetry_port)
        ):
            parser.error("nonempty device and valid UDP ports required")
        if args.output.resolve() == args.uart_output.resolve():
            parser.error("JSONL and raw UART paths must differ")
    try:
        if args.mode == "capture":
            return run_capture(args)
        paths = [args.input] if args.mode == "summarize" else args.inputs
        reports = [summarize(load_records(path)) for path in paths]
        report = (
            reports[0]
            if args.mode == "summarize"
            else {
                "captures": [
                    {"input": str(path), **report}
                    for path, report in zip(paths, reports, strict=True)
                ],
                "comparison_scope": "stationary observed windows only; no causal or long-term reliability conclusion",
            }
        )
        print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))
        return 0 if all(item["capture_ok"] for item in reports) else 1
    except (OSError, ValueError) as exc:
        print(f"runtime_compare: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
