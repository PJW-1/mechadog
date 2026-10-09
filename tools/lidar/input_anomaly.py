"""Offline SCAN diagnostics for a runtime --record-dir (never opens a port).

    python -m tools.lidar.input_anomaly RECORD_DIR --out report.json

Prints a window/comparison table and writes JSON plus a sibling .txt table.
Completion is replayed through the current host decoder/assembler, not inferred
from packet count. Raw angles include zero-distance points. No source is changed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

from host.common.lidar_link import ScanDecoder, scan_of
from host.telemetry.lidar_feed import REV_MAX_AGE_MS, RevolutionAssembler


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _stats(values: list) -> dict:
    return {
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "max": max(values) if values else None,
    }


class _Window:
    def __init__(self, start: int, end: int) -> None:
        self.start, self.end = start, end
        self.counts: Counter = Counter()
        self.angles: Counter = Counter()
        self.points: list[int] = []
        self.gaps: list[int] = []
        self.boots: list[str] = []
        self.transitions: list[dict] = []

    def finish(self, observed_ms: int, zero_ratio: float, concentration: float) -> dict:
        total_angles = sum(self.angles.values())
        bins: Counter = Counter()
        for angle, count in self.angles.items():
            bins[int(angle / 5)] += count
        dominant = max(bins.values(), default=0) / total_angles if total_angles else None
        zero = (
            self.counts["zero_points"] / self.counts["distance_points"]
            if self.counts["distance_points"]
            else None
        )
        ordered = sorted(self.angles)
        if len(ordered) > 1:
            gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False)]
            gaps.append(ordered[0] + 360 - ordered[-1])
            arc = 360 - max(gaps)
        else:
            arc = 0.0 if ordered else None
        reasons = []
        enough = observed_ms > REV_MAX_AGE_MS
        if enough and not self.counts["packets"]:
            reasons.append("no_packets")
        if enough and self.counts["packets"] >= 3:
            if not self.counts["completed"]:
                reasons.append("no_complete_scans")
            if dominant is not None and dominant >= concentration:
                reasons.append("angle_concentrated")
            if zero is not None and zero >= zero_ratio:
                reasons.append("zero_distance_high")
        healthy = not reasons and self.counts["completed"] > 0 and enough
        return {
            "start_ms": self.start,
            "end_ms": self.end,
            "observed_ms": observed_ms,
            "status": "anomaly" if reasons else "normal" if healthy else "insufficient_data",
            "reasons": reasons,
            **{
                key: self.counts[key]
                for key in (
                    "packets",
                    "accepted",
                    "rejected",
                    "completed",
                    "discarded_batches",
                    "raw_points",
                    "valid_points",
                    "invalid_points",
                    "zero_points",
                    "distance_points",
                    "seq_missing",
                    "seq_duplicate_or_reordered",
                    "boot_changes",
                )
            },
            "packet_gap_ms": _stats(self.gaps),
            "points_per_packet": _stats(self.points),
            "angle_min_deg": ordered[0] if ordered else None,
            "angle_max_deg": ordered[-1] if ordered else None,
            "angle_circular_span_deg": arc,
            "angle_bins_5deg": len(bins),
            "angle_histogram_5deg": [bins[i] for i in range(72)],
            "dominant_bin_fraction": dominant,
            "most_common_angles_deg": self.angles.most_common(5),
            "zero_distance_fraction": zero,
            "boot_ids": self.boots,
            "boot_transitions": self.transitions,
        }


def analyze_recording(
    directory: Path,
    *,
    window_ms: int = 1000,
    zero_ratio: float = 0.5,
    concentration: float = 0.8,
    mount_yaw_deg: float = 0.0,
    angle_direction: int = 1,
    device_id: str | None = None,
) -> dict:
    """Stream events.jsonl; split devices, preserve boot/sequence boundary evidence.

    Windows use host receive t, never device ts. Sequence gaps belong to the
    receiving window, including its first packet, and never cross boot IDs.
    """
    if type(window_ms) is not int or window_ms <= REV_MAX_AGE_MS:
        raise ValueError("window_ms must exceed the assembler age limit (300 ms)")
    if not 0 < zero_ratio <= 1 or not 0 < concentration <= 1:
        raise ValueError("ratio thresholds must be in (0, 1]")
    if not _number(mount_yaw_deg) or angle_direction not in (-1, 1):
        raise ValueError("invalid mounting transform")
    directory = Path(directory)
    issues: Counter = Counter()
    streams: dict = {}
    digest = hashlib.sha256()
    first_t = last_t = None
    with (directory / "events.jsonl").open("rb") as source:
        for line in source:
            digest.update(line)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                issues["malformed_event_lines"] += 1
                continue
            if not isinstance(event, dict) or type(event.get("t")) is not int or event["t"] < 0:
                issues["invalid_event_timestamp"] += 1
                continue
            t = event["t"]
            first_t = t if first_t is None else min(first_t, t)
            last_t = t if last_t is None else max(last_t, t)
            if event.get("kind") != "scan":
                continue
            try:
                raw = event.get("raw")
                if raw is None:
                    raw = base64.b64decode(event["raw_b64"], validate=True)
                msg = json.loads(raw)
                if not isinstance(msg, dict) or msg.get("type") != "SCAN":
                    raise ValueError("not SCAN")
                identity = msg.get("device_id")
                boot = msg.get("boot_id")
                seq = msg.get("seq")
                if (
                    not isinstance(identity, str)
                    or not identity
                    or not isinstance(boot, str)
                    or not boot
                ):
                    raise ValueError("invalid identity")
                if type(seq) is not int or seq < 1 or not isinstance(msg.get("points"), list):
                    raise ValueError("invalid seq/points")
            except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
                issues["malformed_scan_records"] += 1
                continue
            if device_id is not None and identity != device_id:
                issues["foreign_device_packets"] += 1
                continue
            if identity not in streams:
                streams[identity] = {
                    "decoder": ScanDecoder(mount_yaw_deg, angle_direction),
                    "assembler": RevolutionAssembler(),
                    "windows": {},
                    "seqs": {},
                    "last_t": None,
                    "first_t": t,
                    "last_boot": None,
                }
            state = streams[identity]
            if state["last_t"] is not None and t < state["last_t"]:
                issues["backward_scan_timestamps_skipped"] += 1
                continue
            start = t // window_ms * window_ms
            window = state["windows"].setdefault(start, _Window(start, start + window_ms))
            c = window.counts
            c["packets"] += 1
            if state["last_t"] is not None:
                window.gaps.append(t - state["last_t"])
            state["last_t"] = t
            if boot not in window.boots:
                window.boots.append(boot)
            if state["last_boot"] is not None and boot != state["last_boot"]:
                c["boot_changes"] += 1
                window.transitions.append({"t_ms": t, "from": state["last_boot"], "to": boot})
            state["last_boot"] = boot
            previous = state["seqs"].get(boot)
            if previous is not None:
                c["seq_missing"] += max(0, seq - previous - 1)
                c["seq_duplicate_or_reordered"] += int(seq <= previous)
            state["seqs"][boot] = max(seq, previous or seq)
            window.points.append(len(msg["points"]))
            c["raw_points"] += len(msg["points"])
            for point in msg["points"]:
                if not isinstance(point, list) or len(point) < 2:
                    continue
                angle, distance = point[:2]
                if _number(angle):
                    window.angles[float(angle) % 360] += 1
                if _number(distance):
                    c["distance_points"] += 1
                    c["zero_points"] += int(distance == 0)
            scan = scan_of(state["decoder"].decode(raw))
            if scan is None:
                c["rejected"] += 1
                continue
            c["accepted"] += 1
            c["valid_points"] += len(scan.points)
            c["invalid_points"] += scan.dropped
            assembler = state["assembler"]
            old_discarded = assembler.discarded
            c["completed"] += int(assembler.add(scan, t) is not None)
            c["discarded_batches"] += assembler.discarded - old_discarded

    devices = {}
    for identity, state in streams.items():
        rows = []
        begin = state["first_t"] // window_ms * window_ms
        end = last_t // window_ms * window_ms
        # Prevent corrupt timestamps from expanding into millions of empty rows.
        if (end - begin) // window_ms > 1_000_000:
            raise ValueError("recording time span exceeds one million windows")
        for start in range(begin, end + 1, window_ms):
            window = state["windows"].get(start, _Window(start, start + window_ms))
            observed = max(0, min(window.end, last_t) - max(start, state["first_t"]))
            rows.append(window.finish(observed, zero_ratio, concentration))
        episodes = []
        last_normal = None
        active = None
        for index, row in enumerate(rows):
            if row["status"] == "anomaly":
                if active is None:
                    active = {
                        "first_window": index,
                        "last_window": index,
                        "previous_normal_window": last_normal,
                        "recovery_window": None,
                        "reasons": [],
                    }
                    episodes.append(active)
                active["last_window"] = index
                active["reasons"] = sorted(set(active["reasons"]) | set(row["reasons"]))
            elif row["status"] == "normal":
                if active is not None:
                    active["recovery_window"] = index
                    active = None
                last_normal = index
        devices[identity] = {"windows": rows, "episodes": episodes}

    recorder = {}
    summary_path = directory / "summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
            recorder = {key: summary.get(key) for key in ("written", "dropped", "failed")}
            if recorder.get("dropped") or recorder.get("failed"):
                issues["recorder_loss_or_failure"] += 1
        except (ValueError, AttributeError):
            issues["invalid_recorder_summary"] += 1
    else:
        issues["missing_recorder_summary"] += 1
    return {
        "schema_version": 1,
        "events_sha256": digest.hexdigest(),
        "status": "analyzed" if devices else "no_scan_data",
        "first_event_ms": first_t,
        "last_event_ms": last_t,
        "settings": {
            "window_ms": window_ms,
            "zero_ratio": zero_ratio,
            "concentration": concentration,
            "mount_yaw_deg": mount_yaw_deg,
            "angle_direction": angle_direction,
        },
        "completion_method": "current host ScanDecoder + RevolutionAssembler replay",
        "limitations": [
            "Window boundaries limit onset/recovery precision; last partial window may be insufficient.",
            "Replayed completion is not a recorded runtime counter; use the runtime mounting transform.",
            "Sequence gaps may be UDP loss or recorder loss; raw SCAN has no UART CRC/speed/stamp fields.",
            "Boot ID changes identify relay sessions, not independent sensor power cycles.",
            "Normal means only that these input heuristics passed, not localization accuracy.",
        ],
        "issues": dict(issues),
        "recorder": recorder,
        "devices": devices,
    }


def format_table(report: dict) -> str:
    """Human-readable windows followed by explicit before/during/after comparisons."""
    lines = ["LiDAR input anomaly (host receive milliseconds; completion = replay)"]
    header = (
        "window   start_ms       state              pkt complete pts/pkt gap_med/max "
        "bins arc_deg zero% peak% missing boot"
    )

    def show(index: int, row: dict) -> str:
        def fmt(value: object) -> str:
            return "-" if value is None else f"{value:.1f}"

        zero = row["zero_distance_fraction"]
        peak = row["dominant_bin_fraction"]
        return (
            f"{index:6d} {row['start_ms']:14d} {row['status']:18s} "
            f"{row['packets']:4d} {row['completed']:8d} "
            f"{fmt(row['points_per_packet']['median']):>7} "
            f"{fmt(row['packet_gap_ms']['median'])}/{fmt(row['packet_gap_ms']['max'])} "
            f"{row['angle_bins_5deg']:4d} {fmt(row['angle_circular_span_deg']):>7} "
            f"{fmt(None if zero is None else zero * 100):>5} "
            f"{fmt(None if peak is None else peak * 100):>5} "
            f"{row['seq_missing']:7d} {row['boot_changes']:4d} " + ",".join(row["reasons"])
        )

    for identity, data in report["devices"].items():
        lines.extend([f"\nDevice {identity}", header])
        lines.extend(show(i, row) for i, row in enumerate(data["windows"]))
        for number, episode in enumerate(data["episodes"], 1):
            lines.append(
                f"Episode {number}: windows {episode['first_window']}..{episode['last_window']}"
            )
            for label, key in (
                ("before", "previous_normal_window"),
                ("onset", "first_window"),
                ("last bad", "last_window"),
                ("recovery", "recovery_window"),
            ):
                index = episode[key]
                lines.append(
                    f"  {label}: "
                    + ("unavailable" if index is None else show(index, data["windows"][index]))
                )
    lines.append("\nIssues: " + json.dumps(report["issues"], sort_keys=True))
    lines.extend("Note: " + note for note in report["limitations"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_dir", type=Path)
    parser.add_argument("--out", type=Path, help="new JSON path; sibling .txt is also created")
    parser.add_argument("--window-ms", type=int, default=1000)
    parser.add_argument("--zero-ratio", type=float, default=0.5)
    parser.add_argument("--concentration", type=float, default=0.8)
    parser.add_argument("--mount-yaw-deg", type=float, default=0.0)
    parser.add_argument("--angle-direction", type=int, choices=(-1, 1), default=1)
    parser.add_argument("--device-id")
    args = parser.parse_args(argv)
    output = args.out or args.record_dir / "input-anomaly.json"
    table_path = output.with_suffix(".txt")
    try:
        if output.suffix.lower() != ".json":
            raise ValueError("--out must have a .json suffix")
        if output.exists() or table_path.exists():
            raise ValueError("output exists; choose a new --out path to preserve evidence")
        report = analyze_recording(
            args.record_dir,
            window_ms=args.window_ms,
            zero_ratio=args.zero_ratio,
            concentration=args.concentration,
            mount_yaw_deg=args.mount_yaw_deg,
            angle_direction=args.angle_direction,
            device_id=args.device_id,
        )
        table = format_table(report)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as target:
            json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
        with table_path.open("x", encoding="utf-8") as target:
            target.write(table)
        print(table)
    except (OSError, ValueError) as exc:
        print(f"input_anomaly: {exc}", file=sys.stderr)
        return 2
    return 0 if report["devices"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
