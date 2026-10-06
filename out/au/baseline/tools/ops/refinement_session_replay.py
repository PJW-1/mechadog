"""Replay recorded calibration sessions into file-only decisions and commands.

There is no socket, robot transport, live serve path, automatic safety reset or
SIM/map activation. Generated protocol lines are test evidence, never delivery.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from host.behavior.commander import Commander
from host.behavior.patrol import drive_params_from_config, steering_for
from host.behavior.refinement_mission import (
    MissionStop,
    ObservationFrame,
    RefinementMission,
    mission_params_from_config,
)
from host.common.lidar_link import Scan, ScanDecoder, scan_of
from host.common.protocol import CommandEncoder, TelemetryDecoder
from host.common.units import wrap_pi
from host.slam import settings
from host.slam.continuous_map import MAX_BATCH_PACKETS, MAX_POSE_AGE_MS, LidarPose
from host.slam.map_refinement import MapRefinement
from host.slam.scan_match import Pose
from host.telemetry.receiver import Reading
from tools.ops.refinement_prepare import file_hash, load_grid, read_mapping


@dataclass(frozen=True)
class SessionReplay:
    report: dict[str, Any]
    decisions: list[dict[str, Any]]
    commands: list[str]


@dataclass(frozen=True)
class _PendingScan:
    raw: str
    received_ms: int
    laser_pose: LidarPose | None
    laser_context: dict[str, Any]
    localization_valid: bool


def _raw_payload(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    if not isinstance(payload, str):
        raise ValueError("Recorded payload must be the original JSON object or string")
    return payload


def _metric_pose(event: dict[str, Any]) -> Pose | None:
    values = tuple(event.get(name) for name in ("x_m", "y_m", "yaw_rad"))
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        return None
    return float(values[0]), float(values[1]), wrap_pi(float(values[2]))


def _observed_bool(event: dict[str, Any], name: str) -> bool | None:
    value = event.get(name)
    return value if type(value) is bool else None


def _telemetry_problem(reading: Reading | None, command_timeout_ms: int) -> str | None:
    if reading is None:
        return "telemetry_missing_or_invalid"
    if type(reading.safety_latched) is not bool:
        return "telemetry_safety_latch_unverified"
    if reading.last_cmd_age_ms is None or reading.last_cmd_age_ms >= command_timeout_ms:
        return "robot_command_not_accepted"
    if not reading.link_ok:
        return "onboard_link_loss_reported"
    return None


def run_session(
    events: Iterable[dict[str, Any]],
    mapper: MapRefinement,
    mission: RefinementMission,
    config: dict[str, Any],
    expected_robot_device: str,
    *,
    frame_verified: bool = False,
) -> SessionReplay:
    """Compose the mission, map gate and existing command encoder offline.

    ``localization`` events declare ``pose_origin=base_link``. ``pose`` events
    declare ``pose_origin=laser`` and ``yaw_frame=base_link`` or ``body_forward``:
    XY is the laser centre, yaw is body forward, because ScanDecoder applies the
    configured laser mounting angle once. Laser-frame yaw cannot be reused here.
    ``telemetry`` and ``scan`` events carry original protocol payloads. Only
    explicit ``tick`` events evaluate the mission. All event timestamps use one
    host monotonic clock. A candidate frame remains stopped by default.
    """
    if not expected_robot_device:
        raise ValueError("Expected robot device is required")
    if mapper.frame_id != mission.map_frame_id or mapper.revision != mission.map_revision:
        raise ValueError("Mapper and mission must start with the same frame and revision")
    drive = drive_params_from_config(config)
    period_ms = round(1000 / float(config["network"]["cmd_rate_hz"]))
    clock = [0]
    commander = Commander(CommandEncoder(clock=lambda: clock[0]), period_ms=period_ms)
    commands = [commander.open_session()]
    decisions: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    scan_decoder = ScanDecoder(
        float(config["lidar"].get("mount_yaw_deg", 0)),
        int(config["lidar"].get("angle_direction", 1)),
    )
    telemetry_decoder = TelemetryDecoder()
    minimum, maximum = settings.range_from_config(config)
    localization: dict[str, Any] = {}
    base_pose: Pose | None = None
    base_received_ms: int | None = None
    laser_context: dict[str, Any] = {}
    laser_pose: LidarPose | None = None
    reading: Reading | None = None
    telemetry_ms: int | None = None
    scan: Scan | None = None
    scan_ms: int | None = None
    scan_pose: LidarPose | None = None
    pending: list[_PendingScan] = []
    last_raw: str | None = None
    observing_since_ms: int | None = None
    map_batch_open = False
    backlog_fault: str | None = None
    max_pending_packets = 0
    last_ms = -1
    event_count = 0
    initial_revision = mapper.revision

    def reset_map_batch(now_ms: int) -> None:
        nonlocal map_batch_open
        if map_batch_open and last_raw is not None:
            # Re-submit an actual recorded packet under an explicitly invalid
            # localization gate to clear the partial batch; no invented scan.
            mapper.submit(
                last_raw,
                now_ms,
                None,
                pose_frame_id=mapper.frame_id,
                localization_valid=False,
                laser_extrinsics_verified=False,
                stationary_verified=False,
                level_verified=False,
            )
        map_batch_open = False

    def invalidate_episode(now_ms: int, reason: str) -> None:
        nonlocal observing_since_ms
        pending.clear()
        observing_since_ms = None
        reset_map_batch(now_ms)
        # Revoke the mission's settled observation immediately, even when an
        # invalid event and subsequent recovery occur between two ticks.
        mission.step(
            now_ms,
            ObservationFrame(
                None,
                "lost",
                None,
                None,
                None,
                None,
                None,
                None,
                safety_latched=reading is not None
                and (
                    reading.safety_latched is True or reading.state == "FAILSAFE" or reading.tipped
                ),
            ),
        )
        discarded.append({"received_ms": now_ms, "kind": "observation_episode", "reason": reason})

    for event in events:
        event_count += 1
        stamp = event.get("received_ms")
        if type(stamp) is not int or stamp < 0 or stamp < last_ms:
            raise ValueError("Session events require monotonic nonnegative integer received_ms")
        last_ms = stamp
        clock[0] = stamp
        kind = event.get("kind")
        if kind == "localization":
            localization = dict(event)
            base_pose = _metric_pose(event)
            base_received_ms = stamp
            if (
                event.get("pose_origin") != "base_link"
                or (event.get("child_frame_id", "base_link") != "base_link")
                or (event.get("device_id", expected_robot_device) != expected_robot_device)
            ):
                base_pose = None
            if (
                base_pose is None
                or event.get("localization_valid") is not True
                or event.get("frame_id") != mapper.frame_id
                or event.get("laser_extrinsics_verified") is not True
                or event.get("stationary") is not True
                or event.get("level") is not True
            ):
                invalidate_episode(stamp, "localization_observation_interrupted")
        elif kind == "localization_lost":
            localization = {}
            base_pose = None
            base_received_ms = None
            laser_context = {}
            laser_pose = None
            scan, scan_ms, scan_pose = None, None, None
            invalidate_episode(stamp, "localization_lost")
        elif kind == "pose":
            laser_context = dict(event)
            measured = _metric_pose(event)
            laser_pose = None
            if (
                event.get("pose_origin") == "laser"
                and event.get("yaw_frame") in ("base_link", "body_forward")
                and measured is not None
                and event.get("frame_id") == mapper.frame_id
                and event.get("localization_valid") is True
                and event.get("laser_extrinsics_verified") is True
            ):
                laser_pose = LidarPose(
                    stamp,
                    *measured,
                    event.get("stationary") is True,
                    event.get("level") is True,
                )
            if laser_pose is None:
                # The previously pinned scan must also stop contributing after
                # the laser pose contract is explicitly revoked. A new valid
                # pose and new scan are required before projecting body rays.
                scan_pose = None
            if laser_pose is None or not laser_pose.stationary or not laser_pose.level:
                invalidate_episode(stamp, "laser_observation_interrupted")
        elif kind == "telemetry":
            decoded = telemetry_decoder.decode(_raw_payload(event))
            reading, telemetry_ms = None, None
            if decoded.accepted and decoded.message is not None:
                candidate = Reading.of(decoded.message)
                if candidate.device_id == expected_robot_device:
                    reading, telemetry_ms = candidate, stamp
            if reading is None:
                discarded.append(
                    {"received_ms": stamp, "kind": kind, "reason": "invalid_or_foreign_telemetry"}
                )
            if _telemetry_problem(reading, drive.cmd_timeout_ms) is not None or (
                reading is not None
                and (
                    reading.safety_latched is True
                    or reading.state in ("FAILSAFE", "AVOID")
                    or reading.obstacle is True
                    or reading.tipped
                )
            ):
                invalidate_episode(stamp, "telemetry_observation_interrupted")
        elif kind == "scan":
            raw = _raw_payload(event)
            last_raw = raw
            candidate_scan = scan_of(scan_decoder.decode(raw))
            if candidate_scan is None or candidate_scan.device_id != mapper.lidar_device:
                discarded.append(
                    {"received_ms": stamp, "kind": kind, "reason": "invalid_or_foreign_scan"}
                )
                scan, scan_ms, scan_pose = None, None, None
                invalidate_episode(stamp, "scan_observation_interrupted")
                continue
            scan = replace(
                candidate_scan,
                points=tuple((a, d) for a, d in candidate_scan.points if minimum <= d <= maximum),
            )
            scan_ms = stamp
            scan_pose = laser_pose
            if len(pending) >= MAX_BATCH_PACKETS:
                discarded.append(
                    {
                        "received_ms": stamp,
                        "kind": "scan_backlog",
                        "reason": "input_backlog_overflow",
                        "packet_count": len(pending) + 1,
                    }
                )
                invalidate_episode(stamp, "input_backlog_overflow")
                backlog_fault = "input_backlog_overflow"
                continue
            pending.append(
                _PendingScan(
                    raw,
                    stamp,
                    laser_pose,
                    dict(laser_context),
                    localization.get("localization_valid") is True and frame_verified is True,
                )
            )
            max_pending_packets = max(max_pending_packets, len(pending))
        elif kind == "tick":
            telemetry_issue = _telemetry_problem(reading, drive.cmd_timeout_ms)
            valid_localization = (
                frame_verified is True
                and localization.get("localization_valid") is True
                and base_pose is not None
                and backlog_fault is None
            )
            latched = reading is not None and (
                reading.safety_latched is True or reading.state == "FAILSAFE" or reading.tipped
            )
            frame = ObservationFrame(
                base_pose,
                "localized" if valid_localization else "candidate",
                base_received_ms,
                scan,
                scan_ms,
                telemetry_ms if telemetry_issue is None else None,
                _observed_bool(localization, "stationary"),
                _observed_bool(localization, "level"),
                safety_latched=latched,
                obstacle_active=reading is not None
                and (reading.obstacle is True or reading.state == "AVOID"),
                pose_frame_id=str(localization.get("frame_id", "")),
                sensor_extrinsics_verified=localization.get("laser_extrinsics_verified") is True,
                scan_pose=None
                if scan_pose is None
                else (scan_pose.x_m, scan_pose.y_m, scan_pose.yaw_rad),
                scan_pose_received_ms=None if scan_pose is None else scan_pose.received_ms,
            )
            directive = mission.step(stamp, frame)
            if telemetry_issue is not None and directive.action != "halted":
                directive = replace(directive, action="stop", reason=telemetry_issue)
            if backlog_fault is not None and directive.action != "halted":
                directive = replace(directive, action="stop", reason=backlog_fault)
            backlog_fault = None
            if directive.action == "observe":
                if observing_since_ms is None:
                    observing_since_ms = stamp
                for packet_index, packet in enumerate(pending):
                    if packet.received_ms < observing_since_ms:
                        continue
                    if not 0 <= stamp - packet.received_ms <= mission.params.scan_timeout_ms or (
                        packet.laser_pose is not None
                        and not 0 <= stamp - packet.laser_pose.received_ms <= MAX_POSE_AGE_MS
                    ):
                        invalidate_episode(stamp, "queued_scan_or_pose_stale")
                        directive = replace(
                            directive, action="stop", reason="queued_scan_or_pose_stale"
                        )
                        break
                    result = mapper.submit(
                        packet.raw,
                        packet.received_ms,
                        packet.laser_pose,
                        pose_frame_id=str(packet.laser_context.get("frame_id", "")),
                        localization_valid=(
                            valid_localization
                            and packet.localization_valid
                            and packet.laser_context.get("localization_valid") is True
                        ),
                        laser_extrinsics_verified=packet.laser_context.get(
                            "laser_extrinsics_verified"
                        )
                        is True,
                        stationary_verified=packet.laser_context.get("stationary") is True,
                        level_verified=packet.laser_context.get("level") is True,
                    )
                    updates.append({"received_ms": packet.received_ms, **asdict(result)})
                    map_batch_open = result.reason == "awaiting_revolution"
                    if result.accepted:
                        assert directive.stop_id is not None
                        mission.accept_observation(
                            directive.stop_id,
                            mapper.snapshot(),
                            result.revision,
                            observation_id=result.observation_id,
                        )
                        observing_since_ms = None
                        if packet_index + 1 < len(pending):
                            discarded.append(
                                {
                                    "received_ms": stamp,
                                    "kind": "scan_backlog",
                                    "reason": "remaining_scans_belong_to_previous_stop",
                                    "packet_count": len(pending) - packet_index - 1,
                                }
                            )
                        break
            else:
                observing_since_ms = None
                reset_map_batch(stamp)
            pending.clear()
            commander.halt()
            if (
                directive.action == "move"
                and directive.waypoint is not None
                and base_pose is not None
            ):
                heading = math.atan2(
                    directive.waypoint[1] - base_pose[1], directive.waypoint[0] - base_pose[0]
                )
                steering = steering_for(heading - base_pose[2], drive)
                if steering.step_mm > 0:
                    commander.drive(steering.step_mm, steering.angle_deg)
                else:
                    directive = replace(
                        directive, action="stop", reason="blind_reverse_not_permitted"
                    )
            state = (
                "FAILSAFE"
                if directive.action == "halted"
                else "PATROL"
                if directive.action == "move"
                else "IDLE"
            )
            commander.announce(state)
            commands.extend(commander.tick(stamp))
            decisions.append({"received_ms": stamp, **asdict(directive)})
        else:
            raise ValueError(f"Unsupported session event kind: {kind!r}")
    commander.halt()
    clock[0] = max(0, last_ms) + period_ms
    commands.extend(commander.tick(clock[0]))
    return SessionReplay(
        {
            "schema_version": 1,
            "mode": "offline_calibration_session_replay",
            "frame_verified": frame_verified is True,
            "events": event_count,
            "ticks": len(decisions),
            "pending_scan_limit": MAX_BATCH_PACKETS,
            "max_pending_scan_packets": max_pending_packets,
            "initial_revision": initial_revision,
            "final_revision": mapper.revision,
            "committed_revolutions": sum(update["accepted"] for update in updates),
            "completed_stops": mission.stop_index,
            "updates": updates,
            "discarded": discarded,
            "map": mapper.metadata(),
            "generated_protocol_lines": len(commands),
            "physical_commands_sent": 0,
            "commands_enabled": False,
            "navigation_ready": False,
            "sim_automatic_overwrite": False,
        },
        decisions,
        commands,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--map-dir", type=Path, required=True)
    parser.add_argument("--stem", default="slam_map")
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--frame-id", required=True)
    parser.add_argument("--lidar-device", required=True)
    parser.add_argument("--robot-device", required=True)
    parser.add_argument(
        "--no-go", type=Path, required=True, help="JSON rectangles_m in the declared map frame"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        parser.error("Output directory must be empty; existing recordings and maps are preserved")
    sources = {
        "input": args.input,
        "mission": args.mission,
        "no_go": args.no_go,
        "seed_map": args.map_dir / f"{args.stem}.npy",
        "seed_map_meta": args.map_dir / "map_meta.json",
    }
    input_hashes = {name: file_hash(path) for name, path in sources.items()}
    config = settings.load(args.robot_device)
    specification = read_mapping(args.mission)
    if specification.get("map_frame_id") != args.frame_id:
        parser.error("Mission and CLI frame ID must match")
    exclusions = read_mapping(args.no_go)
    if exclusions.get("frame_id", args.frame_id) != args.frame_id:
        parser.error("No-go rectangles and map must use the same frame")
    mapper = MapRefinement(
        config,
        args.lidar_device,
        load_grid(args.map_dir, args.stem),
        args.frame_id,
        no_go_rectangles=exclusions["rectangles_m"],
        allowed_rectangles=exclusions.get("allowed_rectangles_m"),
    )
    params = mission_params_from_config(config)
    mission = RefinementMission(
        mapper.snapshot(),
        mapper.revision,
        tuple(
            MissionStop(str(item["id"]), float(item["x_m"]), float(item["y_m"]))
            for item in specification["stops"]
        ),
        params,
        map_frame_id=args.frame_id,
    )
    with args.input.open(encoding="utf-8-sig") as stream:
        result = run_session(
            (json.loads(line) for line in stream if line.strip()),
            mapper,
            mission,
            config,
            args.robot_device,
            frame_verified=specification.get("frame_verified") is True,
        )
    if any(file_hash(path) != input_hashes[name] for name, path in sources.items()):
        raise RuntimeError("A source changed during replay; do not use this candidate output")
    result.report["source_sha256"] = input_hashes
    args.out.mkdir(parents=True, exist_ok=True)
    mapper.snapshot().save(args.out, stem="refinement_candidate")
    (args.out / "session_report.json").write_text(
        json.dumps(result.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "decisions.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in result.decisions),
        encoding="utf-8",
    )
    (args.out / "commands.jsonl").write_text(
        "".join(line + "\n" for line in result.commands), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ticks": result.report["ticks"],
                "committed_revolutions": result.report["committed_revolutions"],
                "physical_commands_sent": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
