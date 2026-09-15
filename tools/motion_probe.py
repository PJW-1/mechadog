"""동작 실측 로깅 — 명령한 것과 실제로 일어난 것을 나란히 기록한다 (WBS 2.2.3 준비).

보정의 원료 데이터를 모으는 도구다. 로봇에 정해진 동작 조각(segment)을
시키면서, 그 사이에 관측된 것을 함께 적는다:

    - IMU yaw 변화량  — 텔레메트리(5101)에서 구간 시작·끝의 yaw 차이
    - IMU 피치·롤 진폭 — 구간 안 |pitch|·|roll| 의 최대값과 p95 (WBS 2.2.3 ③)
    - 스캔 정합 이동량 — 라이다(5201) 구간 전·후 스캔의 scan-to-scan 정합

산출물 두 개:

    segments.csv — 구간당 한 줄. 명령(step/angle/duration)과 실측
                  (imu_yaw_delta, scan dx/dy/dyaw, match 점수)이 나란히 온다.
    events.jsonl — 받은 텔레메트리·스캔 전부. 구간 요약으로 부족할 때
                  나중에 다시 까보는 원본.

    python tools/motion_probe.py --robot 192.168.0.39
    python tools/motion_probe.py --robot 192.168.0.39 --reps 3 --out out/probe

⚠️ 라이다가 아직 ESP32 에 물리지 않았으면 스캔 없이 IMU 만 기록된다 —
   그래도 CSV 행은 나오며 scan 열은 비어 있다.

명령 규약(PROTOCOL.md)을 지킨다: 세션 첫 전문은 STOP, 이후 10Hz 고정 송신,
끝나면 STOP 으로 닫는다. cmd_timeout_ms=300 의 펌웨어 워치독이 이 도구가
죽어도 로봇을 멈춘다.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import socket
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.console import survive_encoding_errors
from host.common.lidar_link import ScanDecoder, scan_of
from host.common.protocol import CommandEncoder, TelemetryDecoder
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import (
    MatchParams,
    integrate_scan,
    match,
    merge_batch,
    preprocess,
)

CMD_PERIOD_S = 0.1  # 10Hz — network.cmd_rate_hz 와 같은 값

# 정합 탐색 창. 순찰용 설정(localization.move_increment_mm 기반)은 한 틱의
# 이동량용이라, 초 단위로 움직이는 이 도구에는 좁다. 구간 이동량(수백 mm,
# 수십 도)을 덮는 값으로 둔다 — 넓히는 대신 탐색이 커지는 것은 이 도구의
# 성격상 감수한다.
PROBE_MATCH = MatchParams(
    search_lin_m=1.0,
    search_lin_step_m=0.05,
    search_ang_rad=math.radians(45),
    search_ang_step_rad=math.radians(2),
    occ_thresh=0.0,
    min_known_cells=50,
)

GRID_RESOLUTION_M = 0.05
GRID_HIT = 0.85
GRID_MISS = -0.2
RANGE_MIN_M = 0.12
RANGE_MAX_M = 8.0


@dataclass(frozen=True, slots=True)
class Segment:
    """동작 조각 하나. kind 는 기록용 이름일 뿐 로봇에는 step·angle 만 간다."""

    name: str
    step_mm: float
    angle_dps: float
    duration_s: float


@dataclass(frozen=True, slots=True)
class Phase:
    """시간표의 한 칸 — move 는 명령을 쏘고 settle 은 STOP 을 쏜다."""

    kind: str  # "move" | "settle" | "done"
    seg: Segment | None
    start_s: float
    end_s: float


def build_schedule(segments: list[Segment], settle_s: float) -> list[Phase]:
    """각 구간 뒤에 settle 을 끼운 시간표 — 정지 관측이 정합·IMU 의 기준점."""
    out: list[Phase] = []
    t = 0.0
    for seg in segments:
        out.append(Phase("move", seg, t, t + seg.duration_s))
        t += seg.duration_s
        out.append(Phase("settle", seg, t, t + settle_s))
        t += settle_s
    out.append(Phase("done", None, t, t))
    return out


def phase_at(schedule: list[Phase], elapsed_s: float) -> Phase:
    for phase in schedule:
        if phase.start_s <= elapsed_s < phase.end_s:
            return phase
    return schedule[-1]


def default_segments(turn_dps: float, step_mm: float, duration_s: float) -> list[Segment]:
    return [
        Segment("turn_left", 0.0, turn_dps, duration_s),
        Segment("turn_right", 0.0, -turn_dps, duration_s),
        Segment("forward", step_mm, 0.0, duration_s),
        Segment("backward", -step_mm, 0.0, duration_s),
    ]


def scan_displacement(
    before: tuple[tuple[float, float], ...],
    after: tuple[tuple[float, float], ...],
    yaw_delta_rad: float,
) -> dict[str, float | int | bool] | None:
    """구간 전·후 스캔의 scan-to-scan 정합으로 이동량을 잰다.

    before 스캔으로 격자를 만들고 after 를 정합한다 — 절대 지도가 아니라
    상대 변위만 필요하므로 매 구간 새 격자를 쓴다. `yaw_delta_rad` 는 IMU 가
    본 회전량으로 정합 탐색의 중심을 잡아준다 (scan_match.py 머리말 — 절대
    yaw 가 아니라 변화량).
    """
    before_pts = preprocess(before, RANGE_MIN_M, RANGE_MAX_M)
    after_pts = preprocess(after, RANGE_MIN_M, RANGE_MAX_M)
    if before_pts.size == 0 or after_pts.size == 0:
        return None
    span = int(math.ceil(RANGE_MAX_M * 2 / GRID_RESOLUTION_M)) + 8
    grid = OccupancyGrid.blank(resolution=GRID_RESOLUTION_M, span_cells=span)
    integrate_scan(grid, (0.0, 0.0, 0.0), before_pts, hit=GRID_HIT, miss=GRID_MISS, pad_cells=2)
    result = match(grid, after_pts, (0.0, 0.0, 0.0), PROBE_MATCH, yaw_delta=yaw_delta_rad)
    if result.skipped or result.score <= 0:
        return None
    x, y, yaw = result.pose
    return {
        "dx_m": round(x, 3),
        "dy_m": round(y, 3),
        "dyaw_deg": round(math.degrees(yaw), 1),
        "match_score": result.score,
    }


@dataclass(slots=True)
class SegmentRecord:
    """CSV 한 줄에 해당. scan 필드는 라이다가 없으면 빈 문자열로 남는다."""

    index: int
    rep: int
    seg: Segment
    ts_start_ms: int = 0
    ts_end_ms: int = 0
    yaw_start: float | None = None
    yaw_end: float | None = None
    imu_samples: int = 0
    # WBS 2.2.3 ③ — 구간 안의 피치·롤 절대값 표본. 트롯 진폭의 max/p95 를
    # 낸다. 목록으로 들고 있어야 백분위를 계산할 수 있다.
    pitch_abs: list[float] = field(default_factory=list)
    roll_abs: list[float] = field(default_factory=list)
    scans_before: int = 0
    scans_after: int = 0
    displacement: dict[str, float | int | bool] | None = None
    # 정합은 루프가 끝난 뒤 계산한다 — 수집 루프 안에서 ICP 를 돌리면 그 사이
    # 명령 송신이 끊겨 로봇의 300ms 감시(watchdog)가 걸리고, 구간이 통째로
    # 건너뛰어진다.
    before_scan: tuple[tuple[float, float], ...] | None = None
    after_scan: tuple[tuple[float, float], ...] | None = None

    @property
    def yaw_delta_deg(self) -> float | None:
        if self.yaw_start is None or self.yaw_end is None:
            return None
        return round(self.yaw_end - self.yaw_start, 2)


def _amp_stats(samples: list[float]) -> tuple[float | None, float | None]:
    """최대값과 p95. 표본이 2개 미만이면 p95 는 의미가 없어 비운다."""
    if not samples:
        return None, None
    ordered = sorted(samples)
    p95 = None
    if len(ordered) >= 2:
        p95 = round(statistics.quantiles(ordered, n=20)[18], 2)
    return round(ordered[-1], 2), p95


CSV_FIELDS = [
    "index",
    "rep",
    "name",
    "cmd_step_mm",
    "cmd_angle_dps",
    "cmd_duration_s",
    "ts_start_ms",
    "ts_end_ms",
    "imu_yaw_start_deg",
    "imu_yaw_end_deg",
    "imu_yaw_delta_deg",
    "imu_samples",
    "pitch_abs_max_deg",
    "pitch_abs_p95_deg",
    "roll_abs_max_deg",
    "roll_abs_p95_deg",
    "scans_before",
    "scans_after",
    "scan_dx_m",
    "scan_dy_m",
    "scan_dyaw_deg",
    "match_score",
]


def record_row(rec: SegmentRecord) -> dict[str, object]:
    disp = rec.displacement or {}
    pitch_max, pitch_p95 = _amp_stats(rec.pitch_abs)
    roll_max, roll_p95 = _amp_stats(rec.roll_abs)
    return {
        "index": rec.index,
        "rep": rec.rep,
        "name": rec.seg.name,
        "cmd_step_mm": rec.seg.step_mm,
        "cmd_angle_dps": rec.seg.angle_dps,
        "cmd_duration_s": rec.seg.duration_s,
        "ts_start_ms": rec.ts_start_ms,
        "ts_end_ms": rec.ts_end_ms,
        "imu_yaw_start_deg": rec.yaw_start,
        "imu_yaw_end_deg": rec.yaw_end,
        "imu_yaw_delta_deg": rec.yaw_delta_deg,
        "imu_samples": rec.imu_samples,
        "pitch_abs_max_deg": pitch_max if pitch_max is not None else "",
        "pitch_abs_p95_deg": pitch_p95 if pitch_p95 is not None else "",
        "roll_abs_max_deg": roll_max if roll_max is not None else "",
        "roll_abs_p95_deg": roll_p95 if roll_p95 is not None else "",
        "scans_before": rec.scans_before,
        "scans_after": rec.scans_after,
        "scan_dx_m": disp.get("dx_m", ""),
        "scan_dy_m": disp.get("dy_m", ""),
        "scan_dyaw_deg": disp.get("dyaw_deg", ""),
        "match_score": disp.get("match_score", ""),
    }


@dataclass(slots=True)
class Observers:
    """구간 경계에서 쓸 최신 관측값을 들고 있는 수집기."""

    yaw: float | None = None
    telemetry_count: int = 0
    last_scan: tuple[tuple[float, float], ...] | None = None
    scan_count: int = 0
    scan_points: int = 0


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    csv_path = out_dir / "segments.csv"

    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tele_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    scan_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock, port in ((tele_sock, args.telemetry_port), (scan_sock, args.scan_port)):
        # SO_REUSEADDR 를 쓰지 않는다 — Windows 에서 UDP 는 같은 포트에 조용히
        # 이중 바인드돼 표본 0 개로 끝난다. 점유 중이면 bind 가 즉시 실패해야 한다.
        sock.bind(("", port))
        sock.setblocking(False)

    encoder = CommandEncoder()
    tele_decoder = TelemetryDecoder()
    scan_decoder = ScanDecoder()
    peer = (args.robot, args.cmd_port)
    obs = Observers()

    one_rep = default_segments(args.turn_dps, args.step_mm, args.duration)
    schedule = build_schedule(one_rep * args.reps, args.settle)
    records: list[SegmentRecord] = []
    current: SegmentRecord | None = None

    # ① 세션 첫 전문은 STOP — 아니면 이전 세션 최대 seq 아래의 명령이 폐기된다.
    cmd_sock.sendto(encoder.stop().encode("utf-8"), peer)
    cmd_sock.sendto(encoder.encode("RESET_SAFE").encode("utf-8"), peer)

    # 첫 텔레메트리가 오기 전에 움직이면 구간 0 의 yaw 기준값이 비게 된다.
    # 최대 2초 기다리되, 안 오면 그냥 시작한다 (라이다 없이도 IMU 만으로 쓸 수
    # 있게 한 것과 같은 이유 — 하나가 없어도 나머지는 기록한다). 스캔도 이 창에
    # 받아두면 구간 0 의 정합 기준점이 된다.
    warmup_deadline = time.monotonic() + 2.0
    while (obs.yaw is None or obs.scan_count == 0) and (time.monotonic() < warmup_deadline):
        for sock, kind in ((tele_sock, "telemetry"), (scan_sock, "scan")):
            try:
                raw, _ = sock.recvfrom(65535)
            except (BlockingIOError, OSError):
                continue
            if kind == "telemetry":
                result = tele_decoder.decode(raw)
                if result.accepted and result.message is not None:
                    yaw = (result.message.get("imu") or {}).get("yaw")
                    if isinstance(yaw, (int, float)):
                        obs.yaw = float(yaw)
                        obs.telemetry_count += 1
            else:
                scan = scan_of(scan_decoder.decode(raw))
                if scan is not None:
                    obs.last_scan = scan.points
                    obs.scan_count += 1
        time.sleep(0.01)

    print(
        f"[probe] robot={args.robot} 구간 {len(schedule) - 1}개 (reps={args.reps}) → {out_dir}",
        flush=True,
    )

    started = time.monotonic()
    next_send = started
    opened_phase: Phase | None = None
    try:
        with events_path.open("w", encoding="utf-8") as events:
            while True:
                elapsed = time.monotonic() - started
                phase = phase_at(schedule, elapsed)
                if phase.kind == "done":
                    break

                # ── 구간 경계 — move 시작 시 기준점을 잡고 settle 끝에 닫는다
                if phase.kind == "move" and opened_phase is not phase:
                    opened_phase = phase
                    current = SegmentRecord(
                        index=len(records),
                        rep=len(records) // len(one_rep),
                        seg=phase.seg,
                    )
                    current.ts_start_ms = int(elapsed * 1000)
                    current.yaw_start = obs.yaw
                    current.scans_before = obs.scan_count
                    current.before_scan = obs.last_scan

                # ── 명령 송신 — 변화가 없어도 10Hz 고정 (그것이 링크 신호)
                if time.monotonic() >= next_send:
                    if phase.kind == "move" and phase.seg is not None:
                        wire = encoder.move(phase.seg.step_mm, phase.seg.angle_dps)
                    else:
                        wire = encoder.stop()
                    with contextlib.suppress(OSError):
                        cmd_sock.sendto(wire.encode("utf-8"), peer)
                    next_send += CMD_PERIOD_S

                # ── 수신 — 텔레메트리와 스캔을 논블로킹으로 훑는다
                for sock, kind in ((tele_sock, "telemetry"), (scan_sock, "scan")):
                    while True:
                        try:
                            raw, _ = sock.recvfrom(65535)
                        except BlockingIOError:
                            break
                        except OSError:
                            break
                        if kind == "telemetry":
                            result = tele_decoder.decode(raw)
                            if not result.accepted or result.message is None:
                                continue
                            msg = result.message
                            imu = msg.get("imu") or {}
                            yaw = imu.get("yaw")
                            if isinstance(yaw, (int, float)):
                                obs.yaw = float(yaw)
                            obs.telemetry_count += 1
                            if current is not None:
                                current.imu_samples += 1
                                for key, bucket in (
                                    ("pitch", current.pitch_abs),
                                    ("roll", current.roll_abs),
                                ):
                                    value = imu.get(key)
                                    if isinstance(value, (int, float)):
                                        bucket.append(abs(float(value)))
                            events.write(
                                json.dumps(
                                    {
                                        "kind": "telemetry",
                                        "seq": msg.get("seq"),
                                        "state": msg.get("state"),
                                        "yaw": yaw,
                                        "pitch": imu.get("pitch"),
                                        "roll": imu.get("roll"),
                                        "tipped": (msg.get("flags") or {}).get("tipped"),
                                    },
                                    separators=(",", ":"),
                                )
                                + "\n"
                            )
                        else:
                            result = scan_decoder.decode(raw)
                            scan = scan_of(result)
                            if scan is None:
                                continue
                            obs.last_scan = scan.points
                            obs.scan_count += 1
                            obs.scan_points += len(scan.points)
                            events.write(
                                json.dumps(
                                    {
                                        "kind": "scan",
                                        "seq": scan.seq,
                                        "points": len(scan.points),
                                    },
                                    separators=(",", ":"),
                                )
                                + "\n"
                            )

                # ── settle 의 마지막 틱에서 구간을 닫는다
                if (
                    phase.kind == "settle"
                    and current is not None
                    and time.monotonic() - started >= phase.end_s - CMD_PERIOD_S
                ):
                    current.ts_end_ms = int(elapsed * 1000)
                    current.yaw_end = obs.yaw
                    current.scans_after = obs.scan_count
                    current.after_scan = obs.last_scan
                    records.append(current)
                    current = None

                time.sleep(0.005)  # 송신 10Hz 보다 촘촘한 수신 폴링 간격
    except KeyboardInterrupt:
        print("[probe] 중단 — 지금까지 모은 구간을 기록한다", flush=True)
    finally:
        with contextlib.suppress(OSError):
            cmd_sock.sendto(encoder.stop().encode("utf-8"), peer)
        cmd_sock.close()
        tele_sock.close()
        scan_sock.close()

    # 루프가 끝난 뒤 정합 — 느린 계산을 명령 경로에서 떼어냈다.
    for rec in records:
        if rec.before_scan is None or rec.after_scan is None:
            continue
        delta_rad = math.radians(rec.yaw_delta_deg) if rec.yaw_delta_deg else 0.0
        rec.displacement = scan_displacement(
            merge_batch([rec.before_scan]), merge_batch([rec.after_scan]), delta_rad
        )
        print(
            f"  #{rec.index} {rec.seg.name:<10} yaw Δ={rec.yaw_delta_deg}  scan={rec.displacement}",
            flush=True,
        )

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for rec in records:
            writer.writerow(record_row(rec))
    print(f"[probe] {len(records)} 구간 → {csv_path} (+ {events_path})", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--robot", required=True, help="MechDog IP")
    p.add_argument("--cmd-port", type=int, default=5001)
    p.add_argument("--telemetry-port", type=int, default=5101)
    p.add_argument("--scan-port", type=int, default=5201)
    p.add_argument("--turn-dps", type=float, default=20.0, help="회전 구간의 angle (deg/s)")
    p.add_argument("--step-mm", type=float, default=60.0, help="직진 구간의 step (mm)")
    p.add_argument("--duration", type=float, default=3.0, help="구간당 동작 시간 s")
    p.add_argument("--settle", type=float, default=3.0, help="구간 뒤 정지 관측 시간 s")
    p.add_argument("--reps", type=int, default=2, help="4구간 세트 반복 수")
    p.add_argument("--out", required=True, help="출력 디렉터리")
    return p


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
