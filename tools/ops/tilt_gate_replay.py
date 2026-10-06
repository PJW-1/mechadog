"""AP 오프라인 관문 재생. 송신/소켓 없이 원본 시간축과 고정 참조 점수를 대조한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.scan_gate import ScanGate
from host.common.config import load_config
from host.common.lidar_link import ScanDecoder, scan_of
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import match, preprocess
from host.slam.settings import match_params_from_config, range_from_config
from host.telemetry.lidar_feed import RevolutionAssembler
from host.telemetry.receiver import Reading


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def replay(record: Path, maps: Path) -> dict:
    config = load_config("mechdog-02")
    lidar = config["lidar"]
    files = [record / n for n in ("events.jsonl", "manifest.json")]
    files += [maps / n for n in ("slam_map.npy", "slam_map_loc.npy", "map_meta.json")]
    before = {str(p): digest(p) for p in files}
    manifest = json.loads(files[1].read_text(encoding="utf-8"))
    events = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    # 수신/처리 이벤트의 기록 순서가 1ms 역전될 수 있어 실제 t를 기준으로 재생한다.
    events.sort(key=lambda e: e["t"])
    cutoff = next(
        (e["t"] for e in events if e["kind"] == "fsm" and e.get("trigger") == "LINK_LOST"),
        events[-1]["t"],
    )
    gate = ScanGate(
        max_imu_age_ms=int(lidar["scan_tilt_imu_max_age_ms"]),
        max_tilt_deg=float(lidar["scan_tilt_max_deg"]),
        walking_tilt_deg=float(lidar["scan_tilt_walking_max_deg"]),
        settle_ms=int(lidar["scan_tilt_settle_ms"]),
        imu_pitch_offset_deg=float(lidar["scan_tilt_pitch_offset_deg"]),
        imu_roll_offset_deg=float(lidar["scan_tilt_roll_offset_deg"]),
        pose_roll_offset_deg=float(config["posture"]["roll_offset_deg"]),
    )
    decoder = ScanDecoder(manifest["mount_yaw_deg"], manifest["angle_direction"])
    assembler = RevolutionAssembler()
    scans = {}
    reasons: Counter = Counter()
    stale: Counter = Counter()
    pose_commands = []
    rows = []
    accepted_times = []
    grid = OccupancyGrid.load(maps, stem="slam_map_loc")
    params = match_params_from_config(config)
    range_m = range_from_config(config)
    for e in events:
        if e["t"] >= cutoff:
            break
        if e["kind"] == "telemetry" and e.get("accepted"):
            gate.observe_imu(Reading.of(json.loads(e["raw"])), e["t"])
        elif e["kind"] == "command_sent":
            for line in e["lines"]:
                msg = json.loads(line)
                if msg["type"] == "POSE":
                    gate.note_pose(msg, e["t"])
                    pose_commands.append({"t": e["t"], "pitch": msg["pitch"], "roll": msg["roll"]})
                elif msg["type"] == "MOVE" and (msg.get("step") or msg.get("angle")):
                    gate.note_move(e["t"])
        elif e["kind"] == "scan":
            scan = scan_of(decoder.decode(e["raw"]))
            if scan is None:
                continue
            scan = assembler.add(scan, e["t"])
            if scan is None:
                continue
            reason = gate.check(scan, e["t"])
            reasons[reason or "accepted"] += 1
            if reason is None:
                accepted_times.append(e["t"])
            if not gate.status["imu_fresh"]:
                stale[reason or "accepted"] += 1
            scans[(scan.boot_id, scan.seq)] = (scan, reason, dict(gate.status))
        elif e["kind"] == "localization":
            found = scans.get((e["scan_boot"], e["scan_seq"]))
            if found is None:
                continue
            scan, reason, status = found
            row = {
                "t": e["t"],
                "seq": scan.seq,
                "reason": reason,
                "zone": e.get("zone"),
                "recorded_score": e["score_frac"],
                "recorded_updated": e["updated"],
                **status,
            }
            # 같은 기록 자세·지도·스캔을 사용한다. 지도 갱신/위치 정답 평가가 아니다.
            points = preprocess(scan.points, *range_m)
            if e["updated"] and len(points):
                result = match(grid, points, tuple(e["pose"]), params)
                row["fixed_reference_score"] = result.score / len(points)
            rows.append(row)
    after = {str(p): digest(p) for p in files}
    if before != after:
        raise RuntimeError("원본 입력 SHA 변경")

    def scores(group):
        measured = [r for r in group if "fixed_reference_score" in r]
        accepted = [r for r in measured if r["reason"] is None]
        return {
            "samples_before": len(measured),
            "samples_after": len(accepted),
            "recorded_median_before": median([r["recorded_score"] for r in measured]),
            "recorded_median_after": median([r["recorded_score"] for r in accepted]),
            "fixed_reference_median_before": median([r["fixed_reference_score"] for r in measured]),
            "fixed_reference_median_after": median([r["fixed_reference_score"] for r in accepted]),
            "retained_same_scan_score_delta": 0.0 if accepted else None,
        }

    total = sum(reasons.values())
    return {
        "mode": "gate_filter_and_fixed_recorded_pose_match_no_counterfactual_navigation",
        "cutoff_first_link_lost_ms": cutoff,
        "completed_scans": total,
        "reasons": dict(reasons),
        "stale_or_missing_imu": dict(stale),
        "rejected_fraction": 1 - reasons["accepted"] / total,
        "max_accepted_scan_gap_ms": max(
            (b - a for a, b in zip(accepted_times, accepted_times[1:], strict=False)), default=None
        ),
        "recorded_localization_rows": sum(
            e["kind"] == "localization" and e["t"] < cutoff for e in events
        ),
        "matched_localization_rows": len(rows),
        "scores": scores(rows),
        "kitchen_scores": scores([r for r in rows if r["zone"] == "C"]),
        "pose_commands": pose_commands,
        "rows": rows,
        "input_sha256": before,
        "inputs_unchanged": before == after,
        "limitations": [
            "기록 자세는 독립 정답이 아님",
            "점수 변화는 제외 표본의 선택 효과",
            "전역 워커/조향/물리 궤적을 가상 재생하지 않음",
            "기준 지도는 명시 maps 입력",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--maps", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.record, args.maps)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("rows", "input_sha256")},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
