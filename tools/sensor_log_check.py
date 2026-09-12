"""Summarize existing Sensor status UART logs; never opens a device or sends commands.

No timestamps exist in these firmware lines: do not infer Hz, duration or drift/sec.
Valid acquisition is not sensor accuracy, stationary posture or battery health.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

PREFIX = "Sensor status:"
IDENTITY = re.compile(r"^Telemetry identity: ([A-Za-z0-9_-]+) boot=([0-9a-f]{16}) port=\d+$")
GROUPS = {"imu": ("pitch", "roll", "yaw"), "dist": ("dist_cm",), "batt": ("batt_v",)}
ERRORS = {
    "none",
    "disabled",
    "starting",
    "task_creation_failed",
    "snapshot_busy",
    "bus_init_failed",
    "wrong_identity",
    "configuration_failed",
    "calibrating",
    "read_failed",
    "invalid_reading",
    "timing_fault",
    "stale",
}


def parse_status(line: str) -> dict:
    """Validate complete fields before accumulating any measurements; ignore extra values."""
    fields = {}
    for token in line.removeprefix(PREFIX).split():
        key, value = token.split("=", 1)
        if key in fields:
            raise ValueError("duplicate field")
        fields[key] = value
    parsed = {}
    for group, names in GROUPS.items():
        flag = fields[f"{group}_valid"]
        error = fields[f"{group}_error"]
        if flag not in {"0", "1"} or error not in ERRORS:
            raise ValueError("invalid status")
        valid = flag == "1"
        if valid != (error == "none"):
            raise ValueError("inconsistent status")
        values = {}
        for name in names:
            value = float(fields[name])
            if valid:
                if not math.isfinite(value):
                    raise ValueError("nonfinite valid measurement")
                if name == "yaw" and not 0 <= value < 360:
                    raise ValueError("invalid yaw")
                if name in {"dist_cm", "batt_v"} and value < 0:
                    raise ValueError("negative measurement")
                values[name] = value
        parsed[group] = {"valid": valid, "error": error, "values": values}
    return parsed


def _new_segment(line_number: int, device=None, boot=None) -> dict:
    return {
        "start_line": line_number,
        "device_id": device,
        "boot_id": boot,
        "status_rows": 0,
        "malformed_rows": 0,
        "sensors": {
            group: {"valid_rows": 0, "invalid_rows": 0, "errors": Counter(), "values": {}}
            for group in GROUPS
        },
        "yaw_runs": [],
    }


def analyze(lines: Iterable[str]) -> dict:
    segments = []
    current = None
    yaw_run = None
    line_count = 0
    for line_count, raw in enumerate(lines, 1):
        line = raw.strip()
        identity = IDENTITY.fullmatch(line)
        if line == "MechDog command receiver booting":
            current = _new_segment(line_count)
            segments.append(current)
            yaw_run = None
            continue
        if identity:
            device, boot = identity.groups()
            if current is None or current["status_rows"] or current["boot_id"] is not None:
                current = _new_segment(line_count, device, boot)
                segments.append(current)
            else:
                current.update(device_id=device, boot_id=boot)
            yaw_run = None
            continue
        if not line.startswith(PREFIX):
            continue
        if current is None:
            current = _new_segment(line_count)
            segments.append(current)
        current["status_rows"] += 1
        try:
            parsed = parse_status(line)
        except (ValueError, KeyError):
            current["malformed_rows"] += 1
            yaw_run = None
            continue
        for group, observation in parsed.items():
            sensor = current["sensors"][group]
            if not observation["valid"]:
                sensor["invalid_rows"] += 1
                sensor["errors"][observation["error"]] += 1
                if group == "imu":
                    yaw_run = None
                continue
            sensor["valid_rows"] += 1
            for name, value in observation["values"].items():
                stats = sensor["values"].setdefault(
                    name, {"first": value, "last": value, "min": value, "max": value}
                )
                stats.update(last=value, min=min(stats["min"], value), max=max(stats["max"], value))
            if group == "imu":
                yaw = observation["values"]["yaw"]
                if yaw_run is None:
                    yaw_run = {
                        "first_line": line_count,
                        "rows": 0,
                        "first_deg": yaw,
                        "last_deg": yaw,
                        "net_change_deg": 0.0,
                        "max_step_deg": 0.0,
                    }
                    current["yaw_runs"].append(yaw_run)
                delta = (yaw - yaw_run["last_deg"] + 180) % 360 - 180
                yaw_run["net_change_deg"] += delta
                yaw_run["max_step_deg"] = max(yaw_run["max_step_deg"], abs(delta))
                yaw_run.update(last_deg=yaw, last_line=line_count, rows=yaw_run["rows"] + 1)
    rows = sum(s["status_rows"] for s in segments)
    malformed = sum(s["malformed_rows"] for s in segments)
    invalid = sum(v["invalid_rows"] for s in segments for v in s["sensors"].values())
    return {
        "schema_version": 1,
        "lines": line_count,
        "status_rows": rows,
        "malformed_rows": malformed,
        "invalid_sensor_observations": invalid,
        "segments": segments,
        "accuracy_verified": False,
        "limitations": [
            "UART status is best-effort at about 1 Hz, not every 25 Hz sensor sample.",
            "No sample timestamps: duration, rate, missing lines and drift/sec are unknown.",
            "Null boot_id means no identity observed; missing reboot logs cannot be detected.",
            "Yaw changes use shortest signed steps (<180 degrees between observed rows).",
            "Invalid/malformed IMU rows and observed boot boundaries break yaw runs.",
            "No external posture/distance/voltage reference: acquisition is not accuracy or health.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Existing UTF-8 UART log")
    parser.add_argument("--output", type=Path, help="New JSON file; never overwrite")
    args = parser.parse_args(argv)
    digest = hashlib.sha256()
    try:
        with args.input.open("rb") as source:

            def lines():
                for raw in source:
                    digest.update(raw)
                    yield raw.decode("utf-8", errors="replace")

            report = analyze(lines())
        report["source_sha256"] = digest.hexdigest()
        encoded = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(encoded)
        else:
            print(encoded, end="")
    except OSError as exc:
        print(f"Log/file error: {exc}", file=sys.stderr)
        return 2
    if report["status_rows"] == report["malformed_rows"]:
        return 2
    return int(bool(report["malformed_rows"] or report["invalid_sensor_observations"]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
