"""motion_probe 시험 — 시간표·정합 변위·종단간 수집 루프.

run() 시험은 루프백 UDP 에 가짜 텔레메트리·스캔을 실제로 쏘는 종단간이다.
실제 장치 포트는 열지 않는다.
"""

from __future__ import annotations

import csv
import math
import socket
import threading
import time
from pathlib import Path

import pytest

from host.common.lidar_link import encode_scan
from host.common.protocol import CommandDecoder, TelemetryEncoder
from tools.motion_probe import (
    Segment,
    SegmentRecord,
    build_schedule,
    default_segments,
    phase_at,
    record_row,
    run,
    scan_displacement,
)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── 시간표 ───────────────────────────────────────────────────


def test_schedule_alternates_move_and_settle() -> None:
    segs = default_segments(turn_dps=20.0, step_mm=60.0, duration_s=3.0)
    schedule = build_schedule(segs, settle_s=2.0)
    kinds = [p.kind for p in schedule]
    assert kinds == ["move", "settle"] * 4 + ["done"]
    assert schedule[-1].start_s == 4 * (3.0 + 2.0)
    # 경계 바로 전후로 phase 가 바뀐다
    assert phase_at(schedule, 0.0).kind == "move"
    assert phase_at(schedule, 2.99).kind == "move"
    assert phase_at(schedule, 3.01).kind == "settle"
    assert phase_at(schedule, 999).kind == "done"


# ── scan-to-scan 변위 ────────────────────────────────────────


def _room_scan(pose: tuple[float, float, float]) -> tuple[tuple[float, float], ...]:
    """직사각형 방(|x|<2, |y|<3)에서 pose 가 얻는 360° 스캔을 합성한다."""
    x, y, yaw = pose
    points = []
    for deg in range(0, 360, 2):
        a = math.radians(deg) + yaw  # 세계 기준 빔 방향
        dx, dy = math.cos(a), math.sin(a)
        cand: list[float] = []
        if abs(dx) > 1e-9:
            cand += [(2.0 - x) / dx, (-2.0 - x) / dx]
        if abs(dy) > 1e-9:
            cand += [(3.0 - y) / dy, (-3.0 - y) / dy]
        hits = [t for t in cand if t > 0]
        if hits:
            points.append((math.radians(deg), min(hits)))
    return tuple(points)


def test_scan_displacement_recovers_turn() -> None:
    true_dyaw = math.radians(15.0)
    before = _room_scan((0.0, 0.0, 0.0))
    after = _room_scan((0.0, 0.0, true_dyaw))
    got = scan_displacement(before, after, yaw_delta_rad=true_dyaw)
    assert got is not None
    assert got["dyaw_deg"] == pytest.approx(15.0, abs=3.0)


def test_scan_displacement_none_without_points() -> None:
    assert scan_displacement((), (), 0.0) is None


# ── CSV 행 ───────────────────────────────────────────────────


def test_record_row_blanks_when_no_scan() -> None:
    rec = SegmentRecord(index=0, rep=0, seg=Segment("forward", 60.0, 0.0, 3.0))
    rec.yaw_start, rec.yaw_end = 10.0, 24.0
    row = record_row(rec)
    assert row["imu_yaw_delta_deg"] == 14.0
    assert row["scan_dx_m"] == ""


# ── 종단간 run() — 루프백 UDP ────────────────────────────────


def test_run_collects_segments(tmp_path: Path) -> None:
    cmd_port, tele_port, scan_port = free_port(), free_port(), free_port()

    captured: list[bytes] = []
    stop_flag = threading.Event()

    def cmd_listener() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", cmd_port))
        sock.settimeout(0.2)
        while not stop_flag.is_set():
            try:
                data, _ = sock.recvfrom(2048)
                captured.append(data)
            except TimeoutError:
                continue
        sock.close()

    def telemetry_feeder() -> None:
        enc = TelemetryEncoder("mechdog-test", "boot-test", clock=lambda: 1000)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0 = time.monotonic()
        while not stop_flag.is_set():
            # yaw 가 초당 30°씩 증가하는 가짜 기체 — 회전 관측 시뮬레이션
            yaw = (time.monotonic() - t0) * 30.0 % 360
            raw = enc.encode(
                "PATROL",
                50.0,
                {"pitch": 0.0, "roll": 0.0, "yaw": yaw},
                7.8,
                10,
                {"lowbatt": False, "tipped": False, "link_ok": True},
            )
            sock.sendto(raw.encode("utf-8"), ("127.0.0.1", tele_port))
            time.sleep(0.05)
        sock.close()

    def scan_feeder() -> None:
        # 텔레메트리와 같은 30°/s 로 도는 가짜 라이다 — IMU 와 정합이
        # 같은 회전을 봐야 한다
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0, seq = time.monotonic(), 0
        while not stop_flag.is_set():
            seq += 1
            yaw = (time.monotonic() - t0) * 30.0
            wire = [
                [math.degrees(a), int(d * 1000)]
                for a, d in _room_scan((0.0, 0.0, math.radians(yaw)))
            ]
            line = encode_scan(
                seq=seq, ts_ms=1000, device_id="lidar-test", boot_id="b1", points_wire=wire
            )
            sock.sendto(line.encode("utf-8"), ("127.0.0.1", scan_port))
            time.sleep(0.15)
        sock.close()

    listener = threading.Thread(target=cmd_listener, daemon=True)
    feeder = threading.Thread(target=telemetry_feeder, daemon=True)
    scans = threading.Thread(target=scan_feeder, daemon=True)
    listener.start()
    feeder.start()
    scans.start()

    try:
        rc = run(
            build_ns(
                robot="127.0.0.1",
                cmd_port=cmd_port,
                telemetry_port=tele_port,
                scan_port=scan_port,
                duration=0.3,
                settle=0.4,
                reps=1,
                out=str(tmp_path / "probe"),
            )
        )
    finally:
        stop_flag.set()
        listener.join(timeout=2)
        feeder.join(timeout=2)
        scans.join(timeout=2)

    assert rc == 0
    with (tmp_path / "probe" / "segments.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4  # turn_left·turn_right·forward·backward 1회분
    for row in rows:
        assert float(row["imu_yaw_delta_deg"]) > 0  # yaw 가 증가하는 가짜 기체
        assert int(row["imu_samples"]) > 0
        assert int(row["scans_after"]) > int(row["scans_before"])
        assert row["scan_dyaw_deg"] != ""  # 스캔 정합 변위도 기록됐다

    # 명령 스트림 — 첫 전문은 STOP, move 구간엔 MOVE, settle·끝엔 STOP
    decoder = CommandDecoder()
    decoded = []
    for raw in captured:
        r = decoder.decode(raw)
        assert r.accepted, r.reason
        decoded.append(r.message)
    assert decoded[0]["type"] == "STOP"
    moves = [m for m in decoded if m["type"] == "MOVE"]
    assert moves, "MOVE 가 하나도 송신되지 않음"
    assert decoded[-1]["type"] == "STOP"


def build_ns(**kw: object):
    from tools.motion_probe import build_parser

    argv = ["--robot", str(kw.pop("robot"))]
    for key, value in kw.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return build_parser().parse_args(argv)
