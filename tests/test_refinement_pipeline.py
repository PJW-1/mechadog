"""Synthetic end-to-end records, never a real calibration or driving claim."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from host.behavior.refinement_mission import MissionStop, RefinementMission
from host.common.lidar_link import encode_scan
from host.common.protocol import CommandDecoder, TelemetryEncoder
from host.slam import settings
from host.slam.continuous_map import MAX_BATCH_PACKETS, LidarPose
from host.slam.map_refinement import MapRefinement
from host.slam.occupancy import MapMeta, OccupancyGrid
from tools.ops.refinement_session_replay import SessionReplay, main, run_session


def configuration() -> dict[str, Any]:
    config = settings.load()
    config["lidar"]["mount_yaw_deg"] = 0
    config["lidar"]["angle_direction"] = 1
    return config


def pipeline(
    stops: tuple[MissionStop, ...],
) -> tuple[dict[str, Any], MapRefinement, RefinementMission]:
    config = configuration()
    grid = OccupancyGrid(MapMeta(0.05, 0, 0, 80, 80), np.full((80, 80), -3.0, dtype=np.float32))
    mapper = MapRefinement(config, "fixture-laser", grid, "map")
    return config, mapper, RefinementMission(mapper.snapshot(), mapper.revision, stops)


def observations(stamp: int, seq: int, **overrides: Any) -> list[dict[str, Any]]:
    shared = {
        "received_ms": stamp,
        "frame_id": "map",
        "x_m": 1.525,
        "y_m": 1.525,
        "yaw_rad": 0.0,
        "stationary": True,
        "level": True,
        "localization_valid": True,
        "laser_extrinsics_verified": True,
    }
    telemetry = TelemetryEncoder(
        "fixture-robot", "boot-a", clock=lambda: stamp, start_seq=seq
    ).build(
        "PATROL",
        100,
        {"pitch": 0.0, "roll": 0.0, "yaw": 0.0},
        8.0,
        20,
        {"lowbatt": False, "tipped": False, "link_ok": True, "obstacle": False},
        safety_latched=False,
    )
    telemetry.update(overrides.get("telemetry", {}))
    laser = {
        **shared,
        "kind": "pose",
        "pose_origin": "laser",
        "yaw_frame": "base_link",
        **overrides.get("laser", {}),
    }
    localization = {
        **shared,
        "kind": "localization",
        "pose_origin": "base_link",
        **overrides.get("localization", {}),
    }
    raw = encode_scan(
        seq=seq,
        ts_ms=stamp,
        device_id="fixture-laser",
        boot_id="boot-a",
        points_wire=[[angle + 0.5, 1000] for angle in overrides.get("angles", range(360))],
    )
    return [
        localization,
        laser,
        {"received_ms": stamp, "kind": "telemetry", "payload": telemetry},
        {"received_ms": stamp, "kind": "scan", "payload": raw},
        {"received_ms": stamp, "kind": "tick"},
    ]


def decoded_commands(result: SessionReplay) -> list[dict[str, Any]]:
    decoder = CommandDecoder()
    messages = []
    for line in result.commands:
        decoded = decoder.decode(line)
        assert decoded.accepted and decoded.message is not None
        messages.append(decoded.message)
    return messages


def test_observe_commits_then_stops_for_route_invalidation() -> None:
    config, mapper, mission = pipeline(
        (MissionStop("capture", 1.525, 1.525), MissionStop("next", 2.525, 1.525))
    )
    initial_revision = mapper.revision
    events = observations(0, 1) + observations(100, 2) + observations(850, 3) + observations(950, 4)
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert result.report["physical_commands_sent"] == 0
    assert result.report["committed_revolutions"] == 1
    assert mapper.revision != initial_revision
    assert result.report["completed_stops"] == 1
    assert result.decisions[2]["action"] == "observe"
    assert result.decisions[3]["action"] == "stop"
    assert result.decisions[3]["reason"] == "map_revision_changed_route_invalidated"
    assert all(message["type"] != "MOVE" for message in decoded_commands(result))
    json.dumps(result.report)


def test_stale_pose_and_latch_block_further_file_move_intents() -> None:
    config, mapper, mission = pipeline((MissionStop("next", 2.525, 1.525),))
    events = observations(0, 1) + observations(100, 2)
    events += [{"kind": "tick", "received_ms": 700}]
    events += observations(800, 3, telemetry={"safety_latched": True})
    events += observations(900, 4)
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    messages = decoded_commands(result)
    assert any(message["type"] == "MOVE" for message in messages if message["ts"] == 100)
    assert all(message["type"] != "MOVE" for message in messages if message["ts"] >= 700)
    assert result.decisions[2]["reason"] in ("pose_stale", "scan_pose_stale")
    assert result.decisions[3]["action"] == "halted"
    assert result.decisions[4]["action"] == "halted"
    assert result.report["committed_revolutions"] == 0


def test_arbitrary_truthy_laser_flags_cannot_commit() -> None:
    config, mapper, mission = pipeline((MissionStop("capture", 1.525, 1.525),))
    events = []
    for stamp, seq in ((0, 1), (100, 2), (850, 3)):
        events += observations(
            stamp, seq, laser={"stationary": "true", "laser_extrinsics_verified": 1}
        )
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert result.report["committed_revolutions"] == 0
    assert result.report["updates"] == []
    assert any(
        item["reason"] == "laser_observation_interrupted" for item in result.report["discarded"]
    )
    assert mission.stop_index == 0
    assert all(message["type"] != "MOVE" for message in decoded_commands(result))


def test_candidate_frame_or_unknown_safety_latch_stays_stopped() -> None:
    config, mapper, mission = pipeline((MissionStop("next", 2.525, 1.525),))
    events = observations(0, 1) + observations(100, 2)
    candidate = run_session(events, mapper, mission, config, "fixture-robot")
    assert all(message["type"] != "MOVE" for message in decoded_commands(candidate))
    assert candidate.decisions[-1]["reason"] == "pose_candidate_unverified"
    config, mapper, mission = pipeline((MissionStop("next", 2.525, 1.525),))
    events = observations(0, 1, telemetry={"safety_latched": "false"})
    unknown = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert unknown.decisions[-1]["reason"] == "telemetry_safety_latch_unverified"
    assert all(message["type"] != "MOVE" for message in decoded_commands(unknown))


@pytest.mark.parametrize("interruption", ["localization", "moving_pose", "invalid_telemetry"])
def test_between_tick_interruption_revokes_observation_and_old_scan_queue(
    interruption: str,
) -> None:
    config, mapper, mission = pipeline((MissionStop("capture", 1.525, 1.525),))
    events = observations(0, 1) + observations(100, 2)
    events += observations(850, 3, angles=range(90))
    queued = observations(860, 4, angles=range(90, 180))
    events.append(next(event for event in queued if event["kind"] == "scan"))
    bad = observations(870, 5)
    if interruption == "localization":
        event = next(item for item in bad if item["kind"] == "localization")
        event["localization_valid"] = False
    elif interruption == "moving_pose":
        event = next(item for item in bad if item["kind"] == "pose")
        event["stationary"] = False
    else:
        event = {"kind": "telemetry", "received_ms": 870, "payload": "{}"}
    events.append(event)
    events += observations(880, 6, angles=range(270, 360))
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert result.report["committed_revolutions"] == 0
    assert result.decisions[-1]["action"] == "stop"
    assert result.decisions[-1]["reason"] == "settling"
    assert len(result.report["updates"]) == 1
    assert result.report["updates"][0]["reason"] == "awaiting_revolution"
    assert mission.stop_index == 0


def test_accepted_unchanged_map_observation_still_advances_mission() -> None:
    config = configuration()
    config["lidar"].update(hit_logodds=5.0, miss_logodds=-5.0)
    grid = OccupancyGrid(MapMeta(0.05, 0, 0, 80, 80), np.full((80, 80), -3.0, dtype=np.float32))
    mapper = MapRefinement(config, "fixture-laser", grid, "map")
    for seq in range(1, 10):
        raw = encode_scan(
            seq=seq,
            ts_ms=0,
            device_id="fixture-laser",
            boot_id="prime-boot",
            points_wire=[[angle + 0.5, 1000] for angle in range(360)],
        )
        result = mapper.submit(
            raw,
            0,
            LidarPose(0, 1.525, 1.525, 0, True, True),
            pose_frame_id="map",
            localization_valid=True,
            laser_extrinsics_verified=True,
            stationary_verified=True,
            level_verified=True,
        )
        if result.changed_cells == 0:
            break
    else:
        pytest.fail("Synthetic map did not saturate")
    mission = RefinementMission(
        mapper.snapshot(), mapper.revision, (MissionStop("capture", 1.525, 1.525),)
    )
    initial_revision = mapper.revision
    replay = run_session(
        observations(0, 1) + observations(100, 2) + observations(850, 3),
        mapper,
        mission,
        config,
        "fixture-robot",
        frame_verified=True,
    )
    assert replay.report["committed_revolutions"] == 1
    assert replay.report["completed_stops"] == 1
    assert mapper.revision == initial_revision
    assert replay.report["updates"][0]["changed_cells"] == 0
    assert replay.report["updates"][0]["observation_id"] is not None


def test_cli_candidate_emits_only_file_stop_and_preserves_existing_output(tmp_path: Path) -> None:
    config, mapper, _ = pipeline((MissionStop("next", 2.525, 1.525),))
    del config
    maps = tmp_path / "maps"
    mapper.snapshot().save(maps)
    mission_file = tmp_path / "mission.json"
    mission_file.write_text(
        json.dumps(
            {
                "map_frame_id": "map",
                "frame_verified": False,
                "stops": [{"id": "next", "x_m": 2.525, "y_m": 1.525}],
            }
        ),
        encoding="utf-8",
    )
    no_go = tmp_path / "no_go.json"
    no_go.write_text(json.dumps({"frame_id": "map", "rectangles_m": []}), encoding="utf-8")
    recording = tmp_path / "session.jsonl"
    recording.write_text(
        "".join(json.dumps(event) + "\n" for event in observations(0, 1)), encoding="utf-8"
    )
    output = tmp_path / "result"
    argv = [
        "--input",
        str(recording),
        "--map-dir",
        str(maps),
        "--mission",
        str(mission_file),
        "--frame-id",
        "map",
        "--lidar-device",
        "fixture-laser",
        "--robot-device",
        "mechdog-02",
        "--no-go",
        str(no_go),
        "--out",
        str(output),
    ]
    assert main(argv) == 0
    report = json.loads((output / "session_report.json").read_text(encoding="utf-8"))
    commands = [
        json.loads(line)
        for line in (output / "commands.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert report["physical_commands_sent"] == 0 and not report["frame_verified"]
    assert all(command["type"] != "MOVE" for command in commands)
    preserved = (output / "session_report.json").read_bytes()
    with pytest.raises(SystemExit):
        main(argv)
    assert (output / "session_report.json").read_bytes() == preserved


def test_latest_fresh_input_cannot_promote_an_old_queued_scan() -> None:
    config, mapper, mission = pipeline((MissionStop("capture", 1.525, 1.525),))
    events = observations(0, 1) + observations(100, 2) + observations(850, 3, angles=range(90))
    events += observations(900, 4, angles=range(90, 180))[:-1]
    events += observations(1500, 5)
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert result.report["committed_revolutions"] == 0
    assert result.decisions[-1]["reason"] == "queued_scan_or_pose_stale"
    assert result.decisions[-1]["action"] == "stop"
    assert mission.stop_index == 0


def test_scan_backlog_is_bounded_and_requires_a_new_settle_episode() -> None:
    config, mapper, mission = pipeline((MissionStop("capture", 1.525, 1.525),))
    events = observations(0, 1) + observations(100, 2) + observations(850, 3, angles=range(90))
    for offset in range(MAX_BATCH_PACKETS + 1):
        packet = observations(860 + offset, 4 + offset, angles=range(90))
        events.append(next(event for event in packet if event["kind"] == "scan"))
    events.append({"kind": "tick", "received_ms": 880})
    events += observations(900, 4 + MAX_BATCH_PACKETS + 1)
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert result.report["max_pending_scan_packets"] <= MAX_BATCH_PACKETS
    assert result.report["committed_revolutions"] == 0
    assert result.decisions[-2]["reason"] == "input_backlog_overflow"
    assert result.decisions[-1]["reason"] == "settling"
    assert any(item["reason"] == "input_backlog_overflow" for item in result.report["discarded"])
    assert all(message["type"] != "MOVE" for message in decoded_commands(result))


def test_remaining_capture_packets_cannot_be_accepted_for_the_next_stop() -> None:
    config, mapper, mission = pipeline(
        (MissionStop("first", 1.525, 1.525), MissionStop("second", 1.525, 1.525))
    )
    events = observations(0, 1) + observations(100, 2)
    capture = observations(850, 3)
    extra_packet = next(event for event in observations(850, 4) if event["kind"] == "scan")
    events += capture[:-1] + [extra_packet, capture[-1]]
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert result.report["committed_revolutions"] == 1
    assert result.report["completed_stops"] == 1
    assert mission.current_stop is not None and mission.current_stop.id == "second"
    discarded = [
        item
        for item in result.report["discarded"]
        if item["reason"] == "remaining_scans_belong_to_previous_stop"
    ]
    assert len(discarded) == 1 and discarded[0]["packet_count"] == 1


@pytest.mark.parametrize(
    "bad_contract",
    [
        {"frame_id": "foreign_map", "x_m": 100.0},
        {"laser_extrinsics_verified": False},
        {"localization_valid": False},
        {"pose_origin": "base_link"},
        {"yaw_frame": "laser"},
        {"x_m": float("nan")},
    ],
)
def test_invalid_laser_pose_cannot_be_promoted_by_later_valid_base_pose(
    bad_contract: dict[str, Any],
) -> None:
    config, mapper, mission = pipeline((MissionStop("next", 2.525, 1.525),))
    events = observations(0, 1)
    bad_pose = next(
        event for event in observations(100, 2, laser=bad_contract) if event["kind"] == "pose"
    )
    recovered_base = [event for event in observations(110, 3) if event["kind"] != "pose"]
    result = run_session(
        events + [bad_pose] + recovered_base,
        mapper,
        mission,
        config,
        "fixture-robot",
        frame_verified=True,
    )
    assert result.decisions[-1]["action"] == "stop"
    assert result.decisions[-1]["reason"] == "scan_pose_missing"
    assert all(message["type"] != "MOVE" for message in decoded_commands(result))
    assert result.report["committed_revolutions"] == 0


def test_verified_moving_laser_pose_remains_available_for_avoidance() -> None:
    config, mapper, mission = pipeline((MissionStop("next", 2.525, 1.525),))
    events = []
    for stamp, seq in ((0, 1), (100, 2)):
        events += observations(
            stamp, seq, localization={"stationary": False}, laser={"stationary": False}
        )
    result = run_session(events, mapper, mission, config, "fixture-robot", frame_verified=True)
    assert result.decisions[-1]["action"] == "move"
    assert any(message["type"] == "MOVE" for message in decoded_commands(result))
    assert result.report["committed_revolutions"] == 0
