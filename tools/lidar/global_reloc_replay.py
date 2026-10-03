"""Read-only comparison of legacy and full-scan global ambiguity decisions.

Run with ``python -m tools.lidar.global_reloc_replay --maps <maps> --sessions
<session> ... --every 400``. Use --every 1 for consecutive-scan evaluation.
No sockets, robot commands, map writes, or private data files are created.
JSON goes to stdout; progress goes to stderr. Session labels are anonymized.

This is an offline candidate/vote audit, not a simulation of worker scheduling or
robot motion. Votes conservatively treat samples as stationary. Recorded poses
are comparison references only, never search priors. Their consistency is not
independent physical ground truth. Missing/LOST/not-updated references remain
unassessed and cannot count as zero false fixes. A zero false-fix count alone
does not qualify the feature for activation.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, OrderedDict
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path

from host.behavior.commander import Commander
from host.behavior.patrol import controller_from_config, load_patrol_map
from host.common.config import load_config
from host.common.lidar_link import ScanDecoder, scan_of
from host.common.protocol import CommandEncoder
from host.common.units import wrap_pi
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import MatchResult, global_match, preprocess
from host.slam.settings import validate_section
from host.telemetry.lidar_feed import RevolutionAssembler


def reference_pose(event: dict) -> tuple[float, float, float] | None:
    """A stale/default pose in a localization event is not a reference trajectory."""
    pose = event.get("pose")
    if (
        event.get("updated") is not True
        or event.get("lost") is not False
        or not isinstance(pose, list)
        or len(pose) != 3
        or any(isinstance(v, bool) or not isinstance(v, int | float) for v in pose)
        or not all(math.isfinite(v) for v in pose)
    ):
        return None
    return tuple(float(v) for v in pose)


def agreement(pose, reference) -> bool | None:
    if reference is None:
        return None
    return (
        math.hypot(pose[0] - reference[0], pose[1] - reference[1]) <= 0.3 + 1e-12
        and abs(wrap_pi(pose[2] - reference[2])) <= math.radians(10) + 1e-12
    )


def iter_samples(session: Path, every: int, stats: Counter):
    """Pair exact scan boot/sequence IDs; never attach the next unrelated pose."""
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    # The recording's transform is authoritative; no current-config substitution.
    decoder = ScanDecoder(manifest["mount_yaw_deg"], manifest["angle_direction"])
    device = manifest["lidar_device"]
    assembler = RevolutionAssembler()
    pending = OrderedDict()
    index = 0
    with (session / "events.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("kind") == "scan":
                scan = scan_of(decoder.decode(event["raw"]))
                if scan is None or scan.device_id != device:
                    continue
                revolution = assembler.add(scan, int(event["t"]))
                if revolution is not None:
                    pending[(revolution.boot_id, revolution.seq)] = (event["t"], revolution)
                    if len(pending) > 64:
                        pending.popitem(last=False)
            elif event.get("kind") == "localization":
                current = index
                index += 1
                pair = pending.pop((event.get("scan_boot"), event.get("scan_seq")), None)
                if current % every:
                    continue
                stats["selected"] += 1
                if pair is None or event.get("points") != len(pair[1].points):
                    stats["unpaired"] += 1
                    yield current, None, None, None
                    continue
                reference = reference_pose(event)
                stats["paired"] += 1
                stats["reference_available"] += int(reference is not None)
                yield current, int(pair[0]), pair[1], reference
    stats["assembled"] = assembler.completed
    stats["incomplete_scans"] = assembler.discarded


def rejection(result: MatchResult | None, size: int, lidar: dict) -> str | None:
    if result is None or result.skipped:
        return "no_match"
    if result.score < max(1, math.ceil(float(lidar.get("min_match_frac", 0.45)) * size)):
        return "weak_score"
    if result.competing_peaks:
        return "competing_full_scan_peaks"
    if not result.search_complete:
        return "unfinished_search"
    if result.peers > int(lidar.get("reloc_max_peers", 60)):
        return "peers"
    return None


def classify(before: str | None, after: str | None) -> str:
    if after == "competing_full_scan_peaks":
        # Evidence of geometric ambiguity, not proof of physical room symmetry.
        return "geometric_ambiguity_evidence"
    if after == "unfinished_search":
        return "unresolved_budget"
    if before == "peers" and after is None:
        return "sampling_or_score_scale_rejection"
    if after == "peers":
        return "remaining_grid_peers"
    return "other_or_clear"


def evaluate(result, points, reference, lidar, voter, elapsed_s):
    reason = rejection(result, len(points), lidar)
    eligible = reason is None
    late = elapsed_s * 1000 > int(lidar.get("global_result_max_age_ms", 3000))
    confirmed = False
    if not eligible or late:
        voter._global_votes.clear()
    else:
        voter._moved_since_vote = False
        confirmed = voter._vote_global(result.pose, result.peers)
    diagnostic = None if result is None else asdict(result)
    if diagnostic is not None:
        diagnostic.pop("pose")  # No measured house coordinates in the report.
    return {
        "decision": reason or "eligible",
        "eligible": eligible,
        "late": late,
        "confirmed": confirmed,
        "agrees_with_local": None if result is None else agreement(result.pose, reference),
        "elapsed_s": round(elapsed_s, 6),
        "diagnostic": diagnostic,
    }


def summarize(rows: list[dict], mode: str) -> dict:
    values = [row[mode] for row in rows]
    fixes = [value for value in values if value["confirmed"]]
    assessed = [value for value in fixes if value["agrees_with_local"] is not None]
    return {
        "samples": len(values),
        "eligible": sum(value["eligible"] for value in values),
        "eligible_rate": sum(value["eligible"] for value in values) / len(values)
        if values
        else None,
        "vote_confirmations": len(fixes),
        "confirmation_rate": len(fixes) / len(values) if values else None,
        "assessed_confirmations": len(assessed),
        "false_fixes_vs_local": sum(value["agrees_with_local"] is False for value in assessed),
        "unassessed_confirmations": len(fixes) - len(assessed),
        "late_results": sum(value["late"] for value in values),
        "rejections": dict(Counter(value["decision"] for value in values)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps", required=True, type=Path)
    parser.add_argument("--sessions", required=True, nargs="+", type=Path)
    parser.add_argument("--device", default="mechdog-02")
    parser.add_argument("--every", type=int, default=400)
    parser.add_argument("--max-samples", type=int, default=0, help="per session; 0 means all")
    args = parser.parse_args()
    if args.every < 1 or args.max_samples < 0:
        parser.error("--every must be positive; --max-samples must be nonnegative")
    config = load_config(args.device)
    lidar = config["lidar"]
    validate_section(lidar)
    nav, zones = load_patrol_map(config, args.maps)
    loc = (
        OccupancyGrid.load(args.maps, stem="slam_map_loc")
        if (args.maps / "slam_map_loc.npy").is_file()
        else nav
    )
    loc.cells.setflags(write=False)
    kwargs = {
        "lin_step_m": float(lidar.get("global_match_step_mm", 100)) / 1000,
        "ang_step_rad": math.radians(float(lidar.get("global_match_angle_deg", 15))),
        "occ_thresh": float(lidar["occupied_logodds"]),
        "min_known_cells": int(lidar["min_known_cells"]),
        "free_thresh": float(lidar["free_logodds"]),
        "sigma_m": float(lidar.get("match_sigma_mm", 0)) / 1000,
    }
    rows, sessions = [], []
    for session_id, session in enumerate(args.sessions, 1):
        stats = Counter()
        with redirect_stdout(sys.stderr):
            voters = {
                mode: controller_from_config(config, Commander(CommandEncoder()), nav, zones, None)
                for mode in ("before", "after")
            }
        count = 0
        for index, _t_ms, scan, reference in iter_samples(session, args.every, stats):
            if scan is None:
                for voter in voters.values():
                    voter._global_votes.clear()
                continue
            points = preprocess(
                scan.points,
                float(lidar["range_min_mm"]) / 1000,
                float(lidar["range_max_mm"]) / 1000,
            )
            row = {"session": session_id, "sample_index": index, "reference": reference is not None}
            # Alternate execution order so field-cache/CPU warmup does not always favor after.
            order = ("before", "after") if count % 2 == 0 else ("after", "before")
            for mode in order:
                started = time.perf_counter()
                result = global_match(loc, points, full_scan_ambiguity=mode == "after", **kwargs)
                elapsed = time.perf_counter() - started
                with redirect_stdout(sys.stderr):
                    row[mode] = evaluate(result, points, reference, lidar, voters[mode], elapsed)
            row["cause"] = classify(
                rejection_from_entry(row["before"]), rejection_from_entry(row["after"])
            )
            rows.append(row)
            count += 1
            print(f"session {session_id}, sample {index}: {row['cause']}", file=sys.stderr)
            if args.max_samples and count >= args.max_samples:
                break
        sessions.append({"session": session_id, "input": dict(stats)})
    report = {
        "scope": "offline candidate and stationary-vote audit; no worker/motion simulation",
        "reference": "same-scan updated non-LOST local trajectory, not independent physical truth",
        "activation_validated": False,
        "every": args.every,
        "sessions": sessions,
        "before": summarize(rows, "before"),
        "after": summarize(rows, "after"),
        "causes": dict(Counter(row["cause"] for row in rows)),
        "samples": rows,
    }
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


def rejection_from_entry(entry: dict) -> str | None:
    return None if entry["eligible"] else entry["decision"]


if __name__ == "__main__":
    raise SystemExit(main())
