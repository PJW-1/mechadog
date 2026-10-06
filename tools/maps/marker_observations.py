"""Offline stationary candidates and independently measured straight-line geometry.

Navigation polling JSONL has ``t`` in epoch seconds and ``pose`` in metres/degrees
(either directly or under ``nav``). Runtime ``kind=localization`` events instead
have ``t`` in epoch milliseconds and yaw in radians. Outputs use epoch milliseconds,
metres and radians. A stable localization estimate is only a candidate interval,
never evidence of the robot's true position or of physical standstill.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _milliseconds(value: Any, name: str) -> int:
    result = _number(value, name)
    if result < 0 or not result.is_integer():
        raise ValueError(f"{name} must be nonnegative integer epoch milliseconds")
    return int(result)


def _wrap(yaw: float) -> float:
    return (yaw + math.pi) % math.tau - math.pi


def _unusable(record: dict) -> bool:
    for key in ("lost", "stale", "starting"):
        if key in record and record[key] is not False:
            return True
    for key in ("valid", "available", "updated"):
        if key in record and record[key] is not True:
            return True
    return any(
        str(record.get(key, "")).upper().split(".")[-1] in {"LOST", "INVALID"}
        for key in ("phase", "state", "status")
    )


def _sample(event: dict) -> tuple[int, float, float, float] | None:
    runtime = event.get("kind") == "localization"
    record = event if runtime else event.get("nav", event)
    if not isinstance(record, dict) or _unusable(record):
        return None
    try:
        stamp = _number(event.get("t"), "t")
        if not runtime:
            stamp *= 1000
        if not math.isfinite(stamp) or stamp < 0:
            return None
        pose = record.get("pose")
        if not isinstance(pose, list) or len(pose) != 3:
            return None
        x, y, yaw = [_number(component, "pose") for component in pose]
        if not runtime:
            yaw = math.radians(yaw)
        return round(stamp), x, y, _wrap(yaw)
    except ValueError:
        return None


def detect_stationary(
    path: Path,
    *,
    position_tolerance_m: float = 0.03,
    yaw_tolerance_deg: float = 5.0,
    min_duration_ms: int = 5000,
    max_gap_ms: int = 1000,
) -> list[dict]:
    """Find consecutive estimates near each segment's fixed first pose.

    A fixed anchor prevents subthreshold steps accumulating into an unlimited
    slow-drift segment. Angular distance and averaging handle the ±180° seam.
    Missing/invalid/stale/LOST/unupdated observations, nonincreasing times and
    excessive gaps break a segment; unrelated runtime events do not. The final
    observed timestamp is the interval end; no duration is extrapolated.

    This heuristic cannot detect physical motion hidden by a frozen estimate.
    Review the intervals and supply independently measured marker geometry.
    """
    position_tolerance_m = _number(position_tolerance_m, "position_tolerance_m")
    yaw_tolerance_deg = _number(yaw_tolerance_deg, "yaw_tolerance_deg")
    min_duration_ms = _milliseconds(min_duration_ms, "min_duration_ms")
    max_gap_ms = _milliseconds(max_gap_ms, "max_gap_ms")
    if position_tolerance_m <= 0 or not 0 < yaw_tolerance_deg < 180:
        raise ValueError("position tolerance must be positive; yaw tolerance must be in (0, 180)")
    if min_duration_ms <= 0 or max_gap_ms <= 0:
        raise ValueError("minimum duration and maximum gap must be positive")
    yaw_tolerance = math.radians(yaw_tolerance_deg)
    result: list[dict] = []
    segment: list[tuple[int, float, float, float]] = []

    def finish() -> None:
        if segment and segment[-1][0] - segment[0][0] >= min_duration_ms:
            result.append(
                {
                    "start_ms": segment[0][0],
                    "end_ms": segment[-1][0],
                    "x": fmean(row[1] for row in segment),
                    "y": fmean(row[2] for row in segment),
                    "yaw": math.atan2(
                        fmean(math.sin(row[3]) for row in segment),
                        fmean(math.cos(row[3]) for row in segment),
                    ),
                }
            )
        segment.clear()

    source = path / "events.jsonl" if path.is_dir() else path
    with source.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{source.name}:{line_number}: invalid JSON") from exc
            if not isinstance(event, dict):
                finish()
                continue
            if "kind" in event and event["kind"] != "localization":
                continue
            sample = _sample(event)
            if sample is None:
                finish()
                continue
            if segment:
                first, previous = segment[0], segment[-1]
                if (
                    not 0 < sample[0] - previous[0] <= max_gap_ms
                    or math.hypot(sample[1] - first[1], sample[2] - first[2])
                    > position_tolerance_m + 1e-12
                    or abs(_wrap(sample[3] - first[3])) > yaw_tolerance + 1e-12
                ):
                    finish()
            segment.append(sample)
    finish()
    return result


def generate_line_truth(
    intervals: list[dict],
    *,
    origin: tuple[float, float],
    direction_deg: float,
    yaw_deg: float,
    spacing_m: float = 0.30,
) -> list[dict]:
    """Place ordered intervals on an independently measured straight line.

    Only interval timestamps are used: estimated poses never become truth.
    ``direction_deg`` specifies increasing marker position; ``yaw_deg`` specifies
    the robot's measured heading at every marker. Both are in the patrol frame.
    """
    if not isinstance(intervals, list) or not intervals:
        raise ValueError("intervals must be a nonempty ordered list")
    if not isinstance(origin, (tuple, list)) or len(origin) != 2:
        raise ValueError("origin must contain independently measured x and y")
    origin_x, origin_y = [_number(value, "origin") for value in origin]
    direction = math.radians(_number(direction_deg, "direction_deg"))
    yaw = _wrap(math.radians(_number(yaw_deg, "yaw_deg")))
    spacing_m = _number(spacing_m, "spacing_m")
    if spacing_m <= 0:
        raise ValueError("spacing_m must be positive")
    result = []
    previous_end = -1
    for index, interval in enumerate(intervals):
        if not isinstance(interval, dict):
            raise ValueError("each interval must contain start_ms and end_ms")
        start = _milliseconds(interval.get("start_ms"), "start_ms")
        end = _milliseconds(interval.get("end_ms"), "end_ms")
        if end <= start or start <= previous_end:
            raise ValueError("intervals must have positive duration, be ordered and not overlap")
        x = origin_x + index * spacing_m * math.cos(direction)
        y = origin_y + index * spacing_m * math.sin(direction)
        result.append(
            {
                "start_ms": start,
                "end_ms": end,
                "x": _number(x, "generated x"),
                "y": _number(y, "generated y"),
                "yaw": yaw,
            }
        )
        previous_end = end
    return result
