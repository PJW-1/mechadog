"""Offline floor-marker truth check; never connects to hardware or changes recordings.

Record A(0,0,0), B(.8,0,0), C(.8,.6,90), D(0,.6,180), A_return(0,0,0)
for 20 s each with --motion-lock, carrying the robot between markers.

    python -m tools.lidar.marker_truth C:/dev/out/session
    python -m tools.lidar.marker_truth SESSION --windows \
        A:0-20,B:20-40,C:40-60,D:60-80,A_return:80-100 --anchor 2,3,90

--markers accepts a JSON file or inline JSON: an ordered list of objects such as
[{"name":"A","pose":[0,0,0]},{"name":"B","pose":[0.8,0,0]}]. Names are unique;
use A_return for a repeat. Marker/anchor poses use metres and degrees; recorded
localization yaw is radians. --anchor is the independently measured map pose of
the FIRST marker, fixing a single rigid transform for the whole experiment.
Windows are [start,end) seconds since the earliest event (host t is milliseconds).
Both manual and automatic windows discard their first 5 seconds.

Without windows, split when XY moves >0.3 m or IMU yaw >30 deg from the current
segment's first sample; discard segments without localization after settling,
then assign markers in order. This heuristic can confuse localization jumps with
carrying and retain the start of a slow carry before a threshold is crossed:
check windows, especially on mismatch or motion below thresholds.
Statistics include stale/LOST poses, with event-weighted update/LOST fractions
and population XY std (radial std = hypot(std_x,std_y)). Heading means/ranges
are circular; IMU comparisons interpolate only inside the same retained window,
without extrapolation, and report their actual common interval. Missing data is
null, never zero. Sparse IMU and >180 deg rotation between samples are ambiguous.
JSON goes to stdout; human tables/warnings go to stderr. Redirect stdout to save
a report outside the recording directory. No anchor means no absolute error.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from itertools import combinations
from pathlib import Path
from statistics import fmean, pstdev

DEFAULT_MARKERS = [
    {"name": "A", "pose": [0.0, 0.0, 0.0]},
    {"name": "B", "pose": [0.8, 0.0, 0.0]},
    {"name": "C", "pose": [0.8, 0.6, 90.0]},
    {"name": "D", "pose": [0.0, 0.6, 180.0]},
    {"name": "A_return", "pose": [0.0, 0.0, 0.0]},
]


def wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def pose3(value) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("pose must be [x_m, y_m, yaw]")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise ValueError("pose components must be numbers")
    if not all(math.isfinite(v) for v in value):
        raise ValueError("pose components must be finite")
    return [float(v) for v in value]


def validate_markers(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("markers must be a nonempty ordered JSON list")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each marker needs name and pose")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or any(c in name for c in ":,"):
            raise ValueError("marker names must be nonempty and contain no ':' or ','")
        result.append({"name": name, "pose": pose3(item.get("pose"))})
    if len({item["name"] for item in result}) != len(result):
        raise ValueError("marker names must be unique; name revisits separately")
    return result


def circular_mean(values: list[float]) -> float | None:
    sine = fmean(math.sin(math.radians(v)) for v in values)
    cosine = fmean(math.cos(math.radians(v)) for v in values)
    if math.hypot(sine, cosine) < 1e-12:
        return None
    return wrap(math.degrees(math.atan2(sine, cosine)))


def heading_range(values: list[float]) -> float:
    ordered = sorted(v % 360 for v in values)
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False)]
    gaps.append(ordered[0] + 360 - ordered[-1])
    return 360 - max(gaps)


def unwrap(values: list[float]) -> list[float]:
    result = [values[0]]
    for before, after in zip(values, values[1:], strict=False):
        result.append(result[-1] + wrap(after - before))
    return result


def load_session(session: Path) -> tuple[list[dict], list[tuple[float, float]], float, float]:
    """Keep only localization and IMU data in memory; validate JSONL with line numbers."""
    locations, imu = [], []
    start = end = None
    with (session / "events.jsonl").open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                stamp = event["t"]
                if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                    raise ValueError("t must be milliseconds")
                if not math.isfinite(stamp):
                    raise ValueError("t must be finite")
                start = stamp if start is None else min(start, stamp)
                end = stamp if end is None else max(end, stamp)
                if event.get("kind") == "localization":
                    pose = event.get("pose")
                    if pose is not None:
                        pose = pose3(pose)
                        pose[2] = math.degrees(pose[2])
                    flags = {key: event.get(key) for key in ("updated", "lost")}
                    if any(v is not None and not isinstance(v, bool) for v in flags.values()):
                        raise ValueError("updated/lost must be bool or null")
                    locations.append({"t": stamp, "pose": pose, **flags})
                elif event.get("kind") == "telemetry":
                    raw = event.get("raw", {})
                    raw = json.loads(raw) if isinstance(raw, str) else raw
                    yaw = (raw.get("imu") or {}).get("yaw")
                    if yaw is not None:
                        if isinstance(yaw, bool) or not isinstance(yaw, (int, float)):
                            raise ValueError("imu.yaw must be degrees")
                        if not math.isfinite(yaw):
                            raise ValueError("imu.yaw must be finite")
                        imu.append((stamp, float(yaw)))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f"events.jsonl:{number}: {exc}") from exc
    if start is None or not locations:
        raise ValueError("session contains no localization events")
    for row in locations:
        row["t"] = (row["t"] - start) / 1000.0
    locations.sort(key=lambda row: row["t"])
    # A repeated telemetry timestamp represents the last sample at that instant.
    imu = sorted({(t - start) / 1000.0: yaw for t, yaw in imu}.items())
    return locations, imu, start, (end - start + 1.0) / 1000.0


def manual_windows(spec: str, markers: list[dict]) -> list[dict]:
    names = {m["name"] for m in markers}
    windows = []
    for entry in spec.split(","):
        try:
            name, span = entry.strip().split(":")
            start, end = (float(v) for v in span.split("-"))
        except ValueError as exc:
            raise ValueError("windows must be NAME:start-end,... in relative seconds") from exc
        if name not in names or any(w["name"] == name for w in windows):
            raise ValueError(f"unknown or repeated window name: {name}")
        if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
            raise ValueError("windows require finite 0 <= start < end")
        if windows and start < windows[-1]["end_s"]:
            raise ValueError("windows must be chronological and non-overlapping")
        windows.append({"name": name, "start_s": start, "end_s": end})
    return windows


def auto_windows(locations: list[dict], imu: list[tuple], end: float) -> list[dict]:
    observations = [(r["t"], "xy", r["pose"][:2]) for r in locations if r["pose"]]
    observations.extend((t, "yaw", yaw) for t, yaw in imu)
    observations.sort(key=lambda row: row[0])
    cuts = [0.0]
    references = {}
    for stamp, kind, value in observations:
        old = references.get(kind)
        changed = old is not None and (
            math.dist(old, value) > 0.3 if kind == "xy" else abs(wrap(value - old)) > 30
        )
        if changed:
            if stamp > cuts[-1]:
                cuts.append(stamp)
            references.clear()
        references.setdefault(kind, value)
    cuts.append(end)
    return [{"start_s": a, "end_s": b} for a, b in zip(cuts, cuts[1:], strict=False)]


def imu_comparison(rows: list[dict], imu: list[tuple]) -> dict | None:
    if len(rows) < 2 or len(imu) < 2:
        return None
    common = [r for r in rows if imu[0][0] <= r["t"] <= imu[-1][0]]
    if len(common) < 2 or common[-1]["t"] <= common[0]["t"]:
        return None
    times = [t for t, _ in imu]
    yaws = unwrap([v for _, v in imu])

    def interpolate(stamp: float) -> float:
        right = bisect.bisect_left(times, stamp)
        if times[right] == stamp:
            return yaws[right]
        left = right - 1
        fraction = (stamp - times[left]) / (times[right] - times[left])
        return yaws[left] + fraction * (yaws[right] - yaws[left])

    loc = unwrap([r["pose"][2] for r in common])
    loc_delta = loc[-1] - loc[0]
    imu_delta = interpolate(common[-1]["t"]) - interpolate(common[0]["t"])
    return {
        "start_s": common[0]["t"],
        "end_s": common[-1]["t"],
        "localization_delta_deg": loc_delta,
        "imu_delta_deg": imu_delta,
        "difference_deg": loc_delta - imu_delta,
        "mean_localization_deg": circular_mean([r["pose"][2] for r in common]),
        "mean_imu_deg": circular_mean([interpolate(r["t"]) for r in common]),
    }


def summarize(window: dict, locations: list[dict], imu: list[tuple]) -> dict:
    start, end = window["start_s"] + 5.0, window["end_s"]
    rows = [r for r in locations if start <= r["t"] < end]
    valid = [r for r in rows if r["pose"] is not None]
    imu_rows = [(t, yaw) for t, yaw in imu if start <= t < end]
    report = {
        **window,
        "retained_start_s": start,
        "sample_count": len(rows),
        "pose_count": len(valid),
        "mean_pose": None,
        "position_std_xy_m": None,
        "position_std_m": None,
        "heading_range_deg": None,
        "imu_comparison": imu_comparison(valid, imu_rows),
    }
    for key in ("updated", "lost"):
        values = [r[key] for r in rows if r[key] is not None]
        report[f"{key}_known_count"] = len(values)
        report[f"{key}_fraction"] = fmean(values) if values else None
    if valid:
        xs, ys, yaws = zip(*(r["pose"] for r in valid), strict=True)
        report.update(
            mean_pose=[fmean(xs), fmean(ys), circular_mean(yaws)],
            position_std_xy_m=[pstdev(xs), pstdev(ys)],
            position_std_m=math.hypot(pstdev(xs), pstdev(ys)),
            heading_range_deg=heading_range(yaws),
        )
    return report


def analyze(session: Path, *, markers=None, windows: str | None = None, anchor=None) -> dict:
    markers = validate_markers(DEFAULT_MARKERS if markers is None else markers)
    anchor = None if anchor is None else pose3(anchor)
    locations, imu, origin, end = load_session(Path(session))
    warnings = []
    discarded = []
    if windows is None:
        selected = []
        for window in auto_windows(locations, imu, end):
            if any(window["start_s"] + 5 <= r["t"] < window["end_s"] for r in locations):
                index = len(selected)
                name = markers[index]["name"] if index < len(markers) else f"unassigned_{index + 1}"
                selected.append({**window, "name": name})
            else:
                discarded.append(window)
        warnings.append("Automatic marker assignment is provisional; verify with --windows.")
    else:
        selected = manual_windows(windows, markers)
    if len(selected) != len(markers):
        warnings.append(f"Found {len(selected)} windows for {len(markers)} markers.")
    segments = [summarize(w, locations, imu) for w in selected]
    truth = {m["name"]: m["pose"] for m in markers}
    for segment in segments:
        if not segment["pose_count"]:
            warnings.append(f"{segment['name']}: no valid poses after the first 5 seconds.")
        elif segment["mean_pose"][2] is None:
            warnings.append(f"{segment['name']}: circular mean heading is undefined.")
    usable = [s for s in segments if s["mean_pose"] is not None and s["name"] in truth]
    pairs, revisits = [], []
    for left, right in combinations(usable, 2):
        a, b = left["mean_pose"], right["mean_pose"]
        ta, tb = truth[left["name"]], truth[right["name"]]
        expected = math.dist(ta[:2], tb[:2])
        measured = math.dist(a[:2], b[:2])
        expected_yaw = wrap(tb[2] - ta[2])
        measured_yaw = None if a[2] is None or b[2] is None else wrap(b[2] - a[2])
        error_yaw = None if measured_yaw is None else wrap(measured_yaw - expected_yaw)
        pair = {
            "from": left["name"],
            "to": right["name"],
            "expected_distance_m": expected,
            "measured_distance_m": measured,
            "distance_error_m": measured - expected,
            "expected_heading_delta_deg": expected_yaw,
            "measured_heading_delta_deg": measured_yaw,
            "heading_delta_error_deg": error_yaw,
            "imu_comparison": None,
        }
        li, ri = left["imu_comparison"], right["imu_comparison"]
        if (
            li
            and ri
            and all(
                row[key] is not None
                for row in (li, ri)
                for key in ("mean_localization_deg", "mean_imu_deg")
            )
        ):
            loc_delta = wrap(ri["mean_localization_deg"] - li["mean_localization_deg"])
            imu_delta = wrap(ri["mean_imu_deg"] - li["mean_imu_deg"])
            pair["imu_comparison"] = {
                "localization_delta_deg": loc_delta,
                "imu_delta_deg": imu_delta,
                "difference_deg": wrap(loc_delta - imu_delta),
            }
        pairs.append(pair)
        if expected < 1e-9 and abs(expected_yaw) < 1e-9:
            revisits.append(
                {
                    "from": left["name"],
                    "to": right["name"],
                    "position_error_m": measured,
                    "heading_error_deg": None if error_yaw is None else abs(error_yaw),
                }
            )
    transform = None
    if anchor is not None:
        first = markers[0]["pose"]
        rotation = wrap(anchor[2] - first[2])
        cosine, sine = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
        tx = anchor[0] - cosine * first[0] + sine * first[1]
        ty = anchor[1] - sine * first[0] - cosine * first[1]
        transform = {"rotation_deg": rotation, "translation_m": [tx, ty]}
        for segment in usable:
            x, y, yaw = truth[segment["name"]]
            target = [cosine * x - sine * y + tx, sine * x + cosine * y + ty, wrap(yaw + rotation)]
            mean = segment["mean_pose"]
            segment["absolute_error"] = {
                "expected_map_pose": target,
                "position_error_m": math.dist(mean[:2], target[:2]),
                "heading_error_deg": None if mean[2] is None else abs(wrap(mean[2] - target[2])),
            }
    return {
        "session": str(session),
        "origin_host_ms": origin,
        "units": {"position": "m", "heading": "deg", "time": "s since origin_host_ms"},
        "mode": "auto" if windows is None else "manual",
        "settle_s": 5.0,
        "markers": markers,
        "anchor_transform": transform,
        "segments": segments,
        "pairs": pairs,
        "revisits": revisits,
        "discarded_windows": discarded,
        "warnings": warnings,
    }


def print_table(report: dict) -> None:
    def fmt(value):
        return "n/a" if value is None else f"{value:.4f}"

    print(
        "marker         n   x(m)    y(m)   yaw(deg) std(m) range(deg) update lost", file=sys.stderr
    )
    for row in report["segments"]:
        pose = row["mean_pose"] or [None, None, None]
        values = [
            *pose,
            row["position_std_m"],
            row["heading_range_deg"],
            row["updated_fraction"],
            row["lost_fraction"],
        ]
        print(
            f"{row['name']:<13} {row['sample_count']:4} " + " ".join(map(fmt, values)),
            file=sys.stderr,
        )
        if row["imu_comparison"]:
            print(
                f"  loc-IMU change: {fmt(row['imu_comparison']['difference_deg'])} deg",
                file=sys.stderr,
            )
        if "absolute_error" in row:
            error = row["absolute_error"]
            print(
                f"  absolute: {fmt(error['position_error_m'])} m, {fmt(error['heading_error_deg'])} deg",
                file=sys.stderr,
            )
    print("pair                    distance error(m)  heading-change error(deg)", file=sys.stderr)
    for pair in report["pairs"]:
        print(
            f"{pair['from'] + ' -> ' + pair['to']:<24}{fmt(pair['distance_error_m']):>10} {fmt(pair['heading_delta_error_deg']):>15}",
            file=sys.stderr,
        )
        if pair["imu_comparison"]:
            print(
                f"  loc-IMU change: {fmt(pair['imu_comparison']['difference_deg'])} deg",
                file=sys.stderr,
            )
    for row in report["revisits"]:
        print(
            f"revisit {row['from']} -> {row['to']}: {fmt(row['position_error_m'])} m, {fmt(row['heading_error_deg'])} deg",
            file=sys.stderr,
        )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("session", type=Path)
    parser.add_argument("--markers", help="ordered JSON list, inline or file path")
    parser.add_argument("--windows", help="A:0-20,B:20-40,... (relative seconds)")
    parser.add_argument("--anchor", help="first marker's measured map x,y,yaw_deg")
    args = parser.parse_args(argv)
    try:
        markers = None
        if args.markers is not None:
            source = args.markers.strip()
            markers = json.loads(
                source if source.startswith("[") else Path(source).read_text(encoding="utf-8-sig")
            )
        anchor = None if args.anchor is None else [float(v) for v in args.anchor.split(",")]
        report = analyze(args.session, markers=markers, windows=args.windows, anchor=anchor)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print_table(report)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
