"""Offline replay of one logged blockage retry. No sockets or robot commands.

The observed poses/scans/command acknowledgements reconstruct the live overlay;
nav_poll supplies the exact blockage memory at failure. This replays the retry
and its terminal state, not the robot's counterfactual physical trajectory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.blockage import Blockage, Recovery
from host.behavior.commander import Commander
from host.behavior.patrol import GOAL_LABEL, Phase, controller_from_config
from host.behavior.planner import Plan, plan_to, segment_clear
from host.behavior.zones import ZoneStore
from host.common.config import load_config
from host.common.lidar_link import ScanDecoder, scan_of
from host.common.protocol import CommandEncoder
from host.slam.occupancy import OccupancyGrid
from host.telemetry.lidar_feed import RevolutionAssembler


def replay(
    record: Path, config: dict, maps: Path, *, after_ms: int, goal: tuple[float, float]
) -> dict:
    sources = [
        record / name for name in ("events.jsonl", "manifest.json", "runtime.log", "nav_poll.log")
    ]
    sources += [maps / name for name in ("slam_map.npy", "map_meta.json", "zones.json")]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest = json.loads((record / "manifest.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (record / "nav_poll.log").read_text(encoding="utf-8").splitlines()
    ]
    row = next(r for r in rows if r["t"] * 1000 >= after_ms)
    now = round(row["t"] * 1000)
    grid = OccupancyGrid.load(maps)
    c = controller_from_config(
        config,
        Commander(CommandEncoder()),
        grid,
        ZoneStore.load(maps, tuple(config["zones"]["ids"])),
        None,
    )
    decoder = ScanDecoder(manifest["mount_yaw_deg"], manifest["angle_direction"])
    assembler = RevolutionAssembler()
    scans: deque = deque(maxlen=4)
    with (record / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event["t"] > now:
                break
            if event["kind"] == "scan":
                scan = scan_of(decoder.decode(event["raw"]))
                if scan is not None:
                    merged = assembler.add(scan, event["t"])
                    if merged is not None:
                        scans.append((event["t"], merged))
            elif event["kind"] == "command_sent":
                c.note_sent(event["lines"], event["t"])
            elif event["kind"] == "localization" and event.get("updated"):
                c.observe_map_pose(tuple(event["pose"]), event["t"])
                c.localization.verified = event["verified"]
                if scans:
                    received, scan = scans[-1]
                    c.observe_obstacle_scan(scan, received)
    c._now_ms = now
    c.pose = (row["pose"][0], row["pose"][1], math.radians(row["pose"][2]))
    c.recovery.memory.items = [
        Blockage(
            b["id"],
            {grid.to_cell(*p): tuple(p) for p in b["points"]},
            now - 30000,
            b["expires_ms"],
            b["confirmed"],
        )
        for b in row["blockage"]["obstacles"]
    ]
    c._refresh_navigation(now)
    trial = plan_to(
        GOAL_LABEL,
        goal,
        c.pose[:2],
        c.navigation_grid,
        c.blocked,
        c.plan_params,
        body_blocked=c.body_blocked,
        costs=c.navigation_costs,
    )
    c._goal = goal
    c.plan = Plan(GOAL_LABEL)
    c.phase = Phase.PLANNING
    c.recovery.active = Recovery(
        GOAL_LABEL,
        now - 2100,
        c.pose[2],
        c.recovery.memory.items[0].id if c.recovery.memory.items else None,
        scanning=True,
        settling_ms=now - 2000,
    )
    result = {
        "retry_ms": now,
        "recorded_phase": row["phase"],
        "recorded_recovery": row["blockage"]["recovery"],
        "pose": c.pose,
        "plan_reason": trial.fail_reason,
        "static_body_point_clear": segment_clear(grid, c._body_blocked, c.pose[:2], c.pose[:2]),
        "dynamic_body_point_clear": segment_clear(grid, c._dynamic, c.pose[:2], c.pose[:2]),
        "front_m": c._local_scan.distance(),
        "latest_scan_nearest_m": min(d for _, d in c._local_scan.points),
        "nearest_memory_m": min(
            (math.dist(c.pose[:2], p) for b in c.recovery.memory.items for p in b.points.values()),
            default=None,
        ),
        "last_sent_moving": c._last_sent_moving,
        "stopped_since_ms": c._stopped_since_ms,
    }
    nearest = min(
        (p for b in c.recovery.memory.items for p in b.points.values()),
        key=lambda p: math.dist(c.pose[:2], p),
        default=None,
    )
    result["geometry"] = {
        "resolution_m": grid.meta.resolution,
        "body_radius_m": c.plan_params.body_radius_m,
        "static_clearance_m": c.plan_params.clearance_m,
        "soft_clearance_m": c.plan_params.soft_clearance_m,
        "memory_xy": nearest,
        "old_cell_centre_distance_m": None
        if nearest is None
        else math.dist(
            grid.to_world(*grid.to_cell(*nearest)), grid.to_world(*grid.to_cell(*c.pose[:2]))
        ),
        "endpoint_clear_exclusion_cells": 2,
    }
    corridor = c._corridor_to(goal)
    result["corridor"] = (
        None
        if corridor is None
        else {
            "relative_heading_deg": math.degrees(corridor[0]),
            "clearance_m": corridor[1],
            "swept_width_m": corridor[2],
            "required_width_m": 2 * (c.plan_params.body_radius_m + c.nav_params.corridor_margin_m),
        }
    )
    c.recovery.step()
    result["after_retry"] = {
        "phase": c.phase.value,
        "goal": c.goal,
        "holding_goal": c.holding_goal,
        "reason": c.goal_hold_reason,
        "recovery": c.blockage_status["recovery"],
        "local": c.local_status,
        "avoidance": c._avoidance,
    }
    if c._avoidance is not None:
        # 새 정책이 움직이기로 했다면 기록의 정지 궤적을 미래 이동으로 쓰지 않는다.
        # 같은 최신 관측에서 다음 명령 의도까지만 확인한다(실제 송신 없음).
        c._avoid()
        result["next_intent"] = {
            "type": c.commander.intent.type_,
            "fields": c.commander.intent.fields,
        }
        if c._avoidance is not None:
            # 같은 끝점의 좌표계만 선택 방위로 회전한 기하 검사. 실제 후속 센서/궤적 아님.
            heading = c._avoidance[2]
            scan_pose = c._local_scan_pose or c.pose
            points = []
            for a, d in c._local_scan.points:
                x = scan_pose[0] + math.cos(scan_pose[2] + a) * d - c.pose[0]
                y = scan_pose[1] + math.sin(scan_pose[2] + a) * d - c.pose[1]
                points.append((math.atan2(y, x) - heading, math.hypot(x, y)))
            c.observe_map_pose((*c.pose[:2], heading), now)
            scan = scans[-1][1]
            aligned = replace(scan, seq=scan.seq + 1, points=tuple(points))
            c.observe_obstacle_scan(aligned, now)
            c._avoid()
            result["alignment_geometry_probe"] = {
                "type": c.commander.intent.type_,
                "fields": c.commander.intent.fields,
                "physical_motion_verified": False,
            }
        result["after_tail"] = None
        result["tail_replay"] = "not_applicable_after_counterfactual_motion"
        result["events"] = c.take_navigation_events()
        result["source_hashes"] = before
        result["sources_unchanged"] = all(
            hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest
            for p, digest in before.items()
        )
        return result
    # Exercise the former goal-hold return for the entire observed stalled tail.
    for later in rows:
        if later["t"] * 1000 <= now:
            continue
        c._now_ms = round(later["t"] * 1000)
        c._advance()
    result["after_tail"] = {
        "phase": c.phase.value,
        "goal": c.goal,
        "holding_goal": c.holding_goal,
        "reason": c.goal_hold_reason,
        "recovery": c.blockage_status["recovery"],
    }
    result["events"] = c.take_navigation_events()
    result["source_hashes"] = before
    result["sources_unchanged"] = all(
        hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in before.items()
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--maps", type=Path, required=True)
    parser.add_argument("--devices-dir", type=Path, default=Path("config/devices"))
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--after-ms", type=int)
    parser.add_argument("--goal", type=float, nargs=2, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    after_ms = args.after_ms
    if after_ms is None:
        manifest = json.loads((args.record / "manifest.json").read_text(encoding="utf-8"))
        start = datetime.fromisoformat(manifest["started_utc"]) + timedelta(hours=9)
        log = (args.record / "runtime.log").read_text(encoding="utf-8")
        clock = re.search(r"(\d\d:\d\d:\d\d).*zone_skipped.*reason=", log)
        if clock is None:
            parser.error("no logged zone_skipped; provide --after-ms")
        hour, minute, second = map(int, clock[1].split(":"))
        failure = start.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if failure < start:
            failure += timedelta(days=1)
        # Select the first poll after the entire logged one-second failure window.
        after_ms = (
            manifest["started_mono_ms"] + round((failure - start).total_seconds() * 1000) + 1000
        )
    config = load_config(args.device, devices_dir=args.devices_dir)
    result = replay(args.record, config, args.maps, after_ms=after_ms, goal=tuple(args.goal))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("plan_reason", "after_retry", "after_tail", "sources_unchanged")
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["sources_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
