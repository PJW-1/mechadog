"""실기 세션 기록 요약 — `python -m host.runtime --record-dir` 가 남긴 폴더를 읽는다.

    python tools/ops/session_summary.py <기록 폴더> [--map <지도 폴더>] [--png 궤적.png]

판독만 한다. 기록을 고치거나 지도를 갱신하지 않는다.

| 항목 | 무엇을 보나 |
| :--- | :--- |
| 흐름별 수·Hz·최대 공백 | SCAN·TELEMETRY·명령·측위가 끊김 없이 들어왔나 (공백 = 같은 종류 연속 사건 간격) |
| IMU | 원본 텔레메트리의 `imu.yaw` 처음·끝·범위 — 보정값이 아니라 펌웨어가 보낸 값 |
| 측위 | 갱신 비율, 처음·끝 자세, 이동 거리, 한 바퀴 점 수 |
| 명령 | 실제로 나간 타입별 수와 보행 잠금이 막은 수 |
| 기록기 | 버림·실패 (summary.json) |
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def load_events(directory: Path) -> list[dict[str, Any]]:
    events = []
    with (directory / "events.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _gaps(times: list[int]) -> tuple[float | None, int | None]:
    if len(times) < 2:
        return None, None
    span = (times[-1] - times[0]) / 1000.0
    rate = (len(times) - 1) / span if span > 0 else None
    return rate, max(b - a for a, b in zip(times, times[1:], strict=False))


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_kind[event["kind"]].append(event)
    t0 = min((e["t"] for e in events), default=0)
    t1 = max((e["t"] for e in events), default=0)
    streams = {}
    for kind in ("scan", "telemetry", "command_sent", "localization", "vision", "camera_frame"):
        times = sorted(e["t"] for e in by_kind.get(kind, []))
        rate, max_gap = _gaps(times)
        streams[kind] = {
            "count": len(times),
            "hz": None if rate is None else round(rate, 2),
            "max_gap_ms": max_gap,
            "first_ms": None if not times else times[0] - t0,
            "last_ms": None if not times else times[-1] - t0,
        }

    yaws, states, accepted = [], Counter(), 0
    for event in by_kind.get("telemetry", []):
        if not event.get("accepted"):
            continue
        accepted += 1
        try:
            message = json.loads(event["raw"])
        except (KeyError, ValueError):
            continue
        imu = message.get("imu") or {}
        if isinstance(imu.get("yaw"), int | float):
            yaws.append(float(imu["yaw"]))
        states[str(message.get("state"))] += 1
    imu = None
    if yaws:
        imu = {
            "samples": len(yaws),
            "first_deg": yaws[0],
            "last_deg": yaws[-1],
            "min_deg": min(yaws),
            "max_deg": max(yaws),
        }

    poses = [e for e in by_kind.get("localization", []) if e.get("updated")]
    loc = by_kind.get("localization", [])
    travel = 0.0
    for a, b in zip(poses, poses[1:], strict=False):
        travel += math.hypot(b["pose"][0] - a["pose"][0], b["pose"][1] - a["pose"][1])
    localization = {
        "attempts": len(loc),
        "updated": len(poses),
        "updated_ratio": None if not loc else round(len(poses) / len(loc), 3),
        "points_median": None if not loc else sorted(e["points"] for e in loc)[len(loc) // 2],
        "first_pose": None if not poses else poses[0]["pose"],
        "last_pose": None if not poses else poses[-1]["pose"],
        "travel_m": round(travel, 3),
    }

    sent_types: Counter[str] = Counter()
    for event in by_kind.get("command_sent", []):
        for line in event.get("lines", []):
            try:
                sent_types[json.loads(line).get("type", "?")] += 1
            except ValueError:
                sent_types["?"] += 1
    blocked = sum(len(e.get("lines", [])) for e in by_kind.get("command_blocked", []))

    return {
        "duration_s": round((t1 - t0) / 1000.0, 2),
        "events": len(events),
        "streams": streams,
        "telemetry_accepted": accepted,
        "robot_states": dict(states),
        "imu_yaw": imu,
        "localization": localization,
        "commands_sent": dict(sent_types),
        "commands_blocked": blocked,
        "fsm": [
            {"t_ms": e["t"] - t0, "from": e.get("previous"), "to": e.get("state")}
            for e in by_kind.get("fsm", [])
        ],
        "navigator_phases": [
            {"t_ms": e["t"] - t0, "phase": e.get("phase"), "reason": e.get("halt_reason")}
            for e in by_kind.get("navigator_phase", [])
        ],
    }


def draw(events: list[dict[str, Any]], map_dir: Path, out: Path) -> None:
    """측위 궤적을 지도 위에 그린다(지도 좌표). matplotlib 가 없으면 건너뛴다."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from host.slam.occupancy import OccupancyGrid

    grid = OccupancyGrid.load(map_dir)
    poses = [e["pose"] for e in events if e["kind"] == "localization" and e.get("updated")]
    fig, ax = plt.subplots(figsize=(10, 7))
    image = (grid.cells > 0.5) * 0.0 + (grid.cells < -0.5) * 1.0 + (abs(grid.cells) <= 0.5) * 0.6
    ax.imshow(image, origin="lower", cmap="gray", extent=grid.extent, vmin=0, vmax=1)
    if poses:
        xs, ys = [p[0] for p in poses], [p[1] for p in poses]
        ax.plot(xs, ys, "-", color="#1f77b4", linewidth=1.5, label="LiDAR 측위")
        ax.plot(xs[0], ys[0], "o", color="#2ca02c", label="시작")
        ax.plot(xs[-1], ys[-1], "s", color="#d62728", label="끝")
        step = max(1, len(poses) // 20)
        for p in poses[::step]:
            ax.arrow(
                p[0],
                p[1],
                0.15 * math.cos(p[2]),
                0.15 * math.sin(p[2]),
                head_width=0.05,
                color="#1f77b4",
            )
        ax.legend(loc="upper right")
    ax.set_aspect("equal")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    fig.tight_layout()
    fig.savefig(out, dpi=110)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실기 세션 기록 요약")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--map", type=Path, default=None, help="궤적을 그릴 지도 폴더")
    parser.add_argument("--png", type=Path, default=None, help="궤적 그림 경로")
    args = parser.parse_args(argv)
    events = load_events(args.directory)
    report = summarize(events)
    recorder = args.directory / "summary.json"
    if recorder.is_file():
        rec = json.loads(recorder.read_text(encoding="utf-8"))
        report["recorder"] = {
            k: rec.get(k) for k in ("written", "dropped", "max_backlog", "failed")
        }
    (args.directory / "session_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=1, ensure_ascii=False))
    if args.map is not None:
        draw(events, args.map, args.png or args.directory / "trajectory.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
