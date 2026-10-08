"""AG 오프라인 재생: 원본 지도/기록은 읽기만, 소켓/로봇 명령은 사용하지 않는다."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.commander import Commander
from host.behavior.patrol import controller_from_config
from host.behavior.planner import body_collision_mask, inflate, mark_obstacle, plan_to
from host.behavior.zones import ZoneStore
from host.common.config import load_base_config
from host.common.lidar_link import ScanDecoder, scan_of
from host.common.protocol import CommandEncoder
from host.slam.occupancy import OccupancyGrid
from host.telemetry.lidar_feed import RevolutionAssembler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-ref", default="c5a3d29")
    args = parser.parse_args()
    # 이전 계획기는 지정한 Git 객체에서 읽는다. 새 출발 경계 수정이 이전 결과에
    # 섞이지 않도록 분리하며 index/작업 파일은 변경하지 않는다.
    source = subprocess.run(
        ["git", "show", f"{args.baseline_ref}:host/behavior/planner.py"],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout
    baseline = ModuleType("_ag_baseline_planner")
    sys.modules[baseline.__name__] = baseline
    exec(compile(source, "baseline_planner.py", "exec"), baseline.__dict__)
    manifest = json.loads((args.record / "manifest.json").read_text(encoding="utf-8"))
    maps = Path(manifest["maps"])
    sources = [args.record / name for name in ("events.jsonl", "manifest.json", "runtime.log")]
    sources += [
        maps / name
        for name in ("slam_map.npy", "slam_map_loc.npy", "map_meta.json", "zones.json")
        if (maps / name).is_file()
    ]
    before_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    config = load_base_config()
    grid = OccupancyGrid.load(maps)
    zones = ZoneStore.load(maps, tuple(config["zones"]["ids"]))
    controller = controller_from_config(config, Commander(CommandEncoder()), grid, zones, None)
    loc = (
        OccupancyGrid.load(maps, stem="slam_map_loc")
        if (maps / "slam_map_loc.npy").exists()
        else None
    )
    controller.loc_grid = loc
    controller.localization.own_localization = True
    old_params = replace(controller.plan_params, clearance_m=0.25, soft_clearance_m=0.15)
    old_blocked = inflate(grid, old_params, obstacle_grid=loc)
    old_body = body_collision_mask(grid, old_params, obstacle_grid=loc)
    old_dynamic = np.zeros_like(old_blocked)
    decoder = ScanDecoder(manifest["mount_yaw_deg"], manifest["angle_direction"])
    assembler = RevolutionAssembler()
    recent: deque = deque(maxlen=4)
    start_time = datetime.fromisoformat(manifest["started_utc"]) + timedelta(hours=9)
    start_ms = manifest["started_mono_ms"]
    rows = []
    frames = {}
    # 16:33:49 goal_set 좌표는 콘솔 기록에 있고 GOAL 레코드에는 좌표가 없다.
    goal_entries = []
    obstacle_events = deque()
    for line in (args.record / "runtime.log").read_text(encoding="utf-8").splitlines():
        found = re.search(r"(\d\d:\d\d:\d\d).*goal_set.*x=([-\d.]+) y=([-\d.]+)", line)
        if found:
            goal_entries.append((found[1], (float(found[2]), float(found[3]))))
        hit = re.search(r"(\d\d:\d\d:\d\d).*obstacle_confirmed.*x=([-\d.]+) y=([-\d.]+)", line)
        cleared = re.search(r"(\d\d:\d\d:\d\d).*dynamic_obstacles_cleared", line)
        if hit:
            obstacle_events.append((hit[1], (float(hit[2]), float(hit[3]))))
        elif cleared:
            obstacle_events.append((cleared[1], None))
    for line in (args.record / "events.jsonl").open(encoding="utf-8"):
        event = json.loads(line)
        timestamp = event["t"]
        wall = start_time + timedelta(milliseconds=timestamp - start_ms)
        episode = (
            "sofa_1622"
            if (wall.hour, wall.minute) == (16, 22)
            else "return_1634"
            if wall.hour == 16 and (wall.minute == 34 or (wall.minute == 35 and wall.second <= 35))
            else None
        )
        while obstacle_events and obstacle_events[0][0] <= wall.strftime("%H:%M:%S"):
            _, point = obstacle_events.popleft()
            if point is None:
                old_dynamic.fill(False)
            else:
                mark_obstacle(old_dynamic, grid, point, 0.15)
        if event["kind"] == "scan":
            scan = scan_of(decoder.decode(event["raw"]))
            if scan is not None:
                merged = assembler.add(scan, timestamp)
                if merged is not None:
                    recent.append((timestamp, merged))
        elif event["kind"] == "localization" and episode is not None and event.get("updated"):
            if not recent:
                continue
            received, scan = min(recent, key=lambda item: abs(item[1].seq - event["scan_seq"]))
            if abs(scan.seq - event["scan_seq"]) > 7 or not 0 <= timestamp - received <= 200:
                continue
            pose = tuple(event["pose"])
            controller.observe_map_pose(pose, timestamp)
            controller.localization.verified = bool(event["verified"])
            controller.observe_obstacle_scan(scan, timestamp)
            target = event.get("target")
            logged_goals = [
                goal for clock, goal in goal_entries if clock <= wall.strftime("%H:%M:%S")
            ]
            if target == "GOAL" and logged_goals:
                goal = logged_goals[-1]
            elif target in zones.labels:
                goal = zones.xy(target)
            else:
                continue
            old = baseline.plan_to(
                target,
                goal,
                pose[:2],
                grid,
                old_blocked | old_dynamic,
                old_params,
                body_blocked=old_body | old_dynamic,
            )
            nav = controller.navigation_grid
            new = plan_to(
                target,
                goal,
                pose[:2],
                nav,
                controller.blocked,
                controller.plan_params,
                body_blocked=controller.body_blocked,
            )
            gap = controller._local_scan.gap(controller.plan_params.body_radius_m)
            row = {
                "episode": episode,
                "time": wall.isoformat(),
                "seq": scan.seq,
                "pose": pose,
                "target": target,
                "verified": event["verified"],
                "old_path": old.reachable,
                "old_reason": old.fail_reason,
                "old_departure": old.reachable
                and not bool((old_body | old_dynamic)[grid.to_cell(*pose[:2])]),
                "new_path": new.reachable,
                "new_reason": new.fail_reason,
                "live_clear_cells": int(controller._live_clear.mask(grid, timestamp).sum()),
                "dynamic_points": len(controller.obstacles),
                "front_m": controller._local_scan.distance(),
                "gap_deg": None if gap is None else math.degrees(gap[0]),
                "escape_available": gap is not None
                and bool(controller.body_blocked[grid.to_cell(*pose[:2])]),
            }
            rows.append(row)
            # First improvement, otherwise the richest live-clear frame.
            previous = frames.get(episode)
            priority = (new.reachable and not old.reachable, row["live_clear_cells"])
            if previous is None or priority > previous[0]:
                frames[episode] = (
                    priority,
                    grid.cells.copy(),
                    nav.cells.copy(),
                    (old_blocked | old_dynamic).copy(),
                    controller.blocked.copy(),
                    pose,
                    goal,
                    old,
                    new,
                )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "replay-results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Exportable scientific figures: source map, effective map/path, same axes in metres.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    for episode, (_, original, nav, old_mask, new_mask, pose, goal, old, new) in frames.items():
        fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)
        extent = (
            grid.meta.origin_x,
            grid.meta.origin_x + grid.meta.width * grid.meta.resolution,
            grid.meta.origin_y,
            grid.meta.origin_y + grid.meta.height * grid.meta.resolution,
        )
        for ax, cells, mask, plan, title in zip(
            axes,
            (original, nav),
            (old_mask, new_mask),
            (old, new),
            (
                "Old map policy + logged accumulated hits",
                "Live-clear overlay + body-only dynamic hits",
            ),
            strict=True,
        ):
            ax.imshow(cells, origin="lower", extent=extent, cmap="Greys", vmin=-5, vmax=5)
            ax.imshow(
                np.ma.masked_where(~mask, np.ones_like(cells)),
                origin="lower",
                extent=extent,
                cmap="Reds",
                vmin=0,
                vmax=1,
                alpha=0.18,
            )
            ax.scatter(*pose[:2], c="tab:blue", label="Recorded pose")
            ax.scatter(*goal, c="tab:red", marker="x", label="Target")
            if plan.reachable:
                points = np.array(plan.waypoints)
                ax.plot(points[:, 0], points[:, 1], color="tab:blue", label="A* path")
            ax.set(
                title=f"{title}\n{plan.fail_reason or 'Path found'}",
                xlabel="Patrol x (m)",
                ylabel="Patrol y (m)",
            )
            ax.legend(loc="upper right")
        fig.suptitle(f"{episode} — recorded pose replay, physical safety not verified")
        fig.savefig(args.output / f"{episode}.png", dpi=160)
        plt.close(fig)
    summary = {}
    for episode in ("sofa_1622", "return_1634"):
        selected = [row for row in rows if row["episode"] == episode]
        summary[episode] = {
            "frames": len(selected),
            "verified": sum(row["verified"] for row in selected),
            "old_paths": sum(row["old_path"] for row in selected),
            "old_departures": sum(row["old_departure"] for row in selected),
            "new_paths": sum(row["new_path"] for row in selected),
            "improved_frames": sum(row["new_path"] and not row["old_path"] for row in selected),
            "escape_available": sum(row["escape_available"] for row in selected),
            "new_failures": {
                reason: sum(row["new_reason"] == reason for row in selected)
                for reason in {row["new_reason"] for row in selected if row["new_reason"]}
            },
        }
    (args.output / "replay-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    after_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    (args.output / "source-audit.json").write_text(
        json.dumps(
            {
                "before": before_hashes,
                "after": after_hashes,
                "unchanged": before_hashes == after_hashes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report = [
        "# AG 실기 기록 재생",
        "",
        f"이전 계획기: Git {args.baseline_ref} 객체(read-only git show). 새 계획기의 출발 격자 경계 수정은 이전 결과에 섞지 않았다.",
        "",
        "원본 SCAN을 mount_yaw=270도, angle_direction=-1로 해석하고 한 바퀴로 조립했다. 당시 recorded pose/verified를 고정 입력했으며 재측위와 보행은 실행하지 않았다.",
        "",
        "이전 정책: 정적 25cm/가구 15cm 팽창 + runtime.log의 obstacle_confirmed/해제 순서를 재구성. 로그 좌표는 2자리 반올림, 시간은 1초 해상도여서 경계 프레임은 근사다. 새 정책: 20cm 지도 팽창 + 연속 3바퀴 비움/2초 감쇠 + 실제 점/몸 반경/2초 TTL.",
        "",
        "16:34 귀환 GOAL은 16:33:49 goal_set의 (2.95, -0.7)m. 몸체 여유 정지 증상이 16:35까지 이어져 16:34:00~16:35:35를 재생했다.",
        "",
    ]
    for episode, result in summary.items():
        report += [
            f"## {episode}",
            "",
            f"- 평가 {result['frames']}프레임, verified {result['verified']}.",
            f"- 이전 경로 {result['old_paths']}, 이전 출발 관문 통과 {result['old_departures']}; 새 경로 {result['new_paths']}; 막힘→경로 생성 {result['improved_frames']}.",
            f"- 새 경로 실패 {result['new_failures']}; 최신 gap 출발 탈출 가능 {result['escape_available']}.",
            f"![{episode}]({episode}.png)",
            "",
        ]
    report += [
        "## 해석의 한계",
        "",
        "- 경로 생성과 최신 gap 후보는 회피 후 실제 완주를 뜻하지 않는다. recorded pose를 고정한 재생이므로 기동에 따른 새 자세와 이후 경로는 실기/목업에서 따로 검증한다.",
        "- 기록의 verified는 실제 절대 위치 정답이 아니다. 틀린 확정 자세로 벽을 지우는 위험, 라이다 높이 밖 장애물, 기체 보행/조향과 지연은 실기 검증이 필요하다.",
        f"- 원본 {len(sources)}개 SHA-256 재읽기 동일: {before_hashes == after_hashes}. 구체 해시는 source-audit.json.",
        "",
    ]
    (args.output / "replay.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
