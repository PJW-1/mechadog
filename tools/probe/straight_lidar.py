"""라이다 벽 기준 직진 왕복 시험 — 어느 쪽으로 휘는지 재고, 벽을 보며 일직선으로 가게 한다.

    python tools/probe/straight_lidar.py --record-dir <폴더> --distance 2.0 --cycles 2 --mode both
    python tools/probe/straight_lidar.py --record-dir <폴더> --roll-sweep 0,2,4,6 --cycles 0   # 기울기만

기체 02 는 직진·후진 모두 오른쪽으로 휜다(09-22 실측 −2.8°/s, 출발 킥 2~3°)고, 정지 중 몸이
오른쪽으로 기운다(10-02 IMU roll +3.7°, 10-03 POSE roll 부호가 IMU 와 반대). 이 도구는 둘을 한
자리에서 잰다.

**재는 것 (구간마다)**
- 라이다: 옆벽(좌/우)에 직선을 맞춰 «벽 대비 방위» 와 «벽까지 거리» — 시작 대비 방위 변화(°)와
  옆 밀림(cm)이 곧 휘는 정도다. 전방(후진이면 후방) 벽 거리로 이동 거리를 잰다.
- IMU: yaw 변화, roll·pitch 평균/표준편차(몸 기울기).
- 명령: 보낸 step·angle.

**모드**
- `open`: angle = `--bias`(기본 0) 고정 — 자연스러운 휨을 잰다.
- `hold`: 옆벽 방위·거리를 시작값으로 유지하도록 매 회전 angle 을 고친다(직진 유지).
- `both`: 왕복마다 open 한 번, hold 한 번.

**기울기 스윕** `--roll-sweep 0,2,4,6`: 서 있는 채로 POSE roll 을 바꾸며 IMU roll 평균을 재고,
IMU roll 이 0 이 되는 POSE roll 을 선형 맞춤으로 권한다(설정은 사람이 바꾼다).

**안전**: 시작 전 두 번 묻는다. 전진은 전방, 후진은 후방이 `--clearance`(기본 0.5m) 안이면 정지.
라이다·텔레메트리가 1초 끊기거나, 로봇이 래치·장애물을 보고하면 정지. Ctrl+C 는 ESTOP 3회.
로봇 래치는 시작 때 RESET_SAFE 로 푼다. 걸음 크기는 `--step`(기본 60mm, 규약 상한 100) 으로 제한.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.lidar_link import ScanDecoder, scan_of  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402
from host.slam.scan_match import preprocess  # noqa: E402
from host.telemetry.lidar_feed import RevolutionAssembler  # noqa: E402
from host.telemetry.receiver import Reading, TelemetryDecoder  # noqa: E402

# ── 순수 계산 (시험 대상) ────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WallFit:
    """옆벽 직선. `heading_deg` = 벽 대비 로봇 방위(좌회전 +), `distance_m` = 벽까지 수직 거리."""

    side: str
    heading_deg: float
    distance_m: float
    inliers: int


def fit_side_wall(points: np.ndarray, side: str, *, reach_m: float = 2.5) -> WallFit | None:
    """로봇 기준 점(x 앞, y 왼쪽)에서 좌/우 옆벽 직선을 맞춘다. 점이 부족하면 None.

    가구 다리 같은 떨어진 점을 버리려고 잔차가 큰 점을 두 번 걸러 다시 맞춘다.
    """
    if points.size == 0:
        return None
    x, y = points[:, 0], points[:, 1]
    lateral = y if side == "left" else -y
    sel = (np.abs(x) <= 1.2) & (lateral >= 0.12) & (lateral <= reach_m)
    if int(sel.sum()) < 12:
        return None
    xs, ys = x[sel], y[sel]
    keep = np.ones(xs.size, bool)
    for _ in range(3):
        if int(keep.sum()) < 12:
            return None
        a, b = np.polyfit(xs[keep], ys[keep], 1)
        resid = np.abs(ys - (a * xs + b))
        keep = resid <= max(0.03, 2.5 * float(np.median(resid[keep])))
    a, b = np.polyfit(xs[keep], ys[keep], 1)
    # 벽이 y = a x + b 로 보이면 로봇은 벽 방향에서 −atan(a) 만큼 돌아 있다.
    heading = -math.degrees(math.atan(a))
    distance = abs(b) * math.cos(math.atan(a))
    return WallFit(side, heading, distance, int(keep.sum()))


def range_along(points: np.ndarray, forward: bool, *, half_width_m: float = 0.12) -> float | None:
    """앞(또는 뒤) 띠의 가장 가까운 점까지 거리 — 이동 거리와 정지 판정에 쓴다."""
    if points.size == 0:
        return None
    x, y = points[:, 0], points[:, 1]
    sel = (np.abs(y) <= half_width_m) & ((x > 0.05) if forward else (x < -0.05))
    if int(sel.sum()) < 3:
        return None
    values = np.abs(x[sel])
    return float(np.percentile(values, 10))


@dataclass(frozen=True, slots=True)
class HoldGains:
    heading: float = 0.8  # deg 명령 / deg 방위 오차
    lateral: float = 0.5  # deg 명령 / cm 옆 밀림
    limit_deg: float = 12.0


def hold_angle(
    reference: WallFit,
    now: WallFit,
    *,
    reverse: bool,
    bias_deg: float = 0.0,
    gains: HoldGains | None = None,
) -> float:
    """옆벽 방위·거리를 시작값으로 되돌리는 angle(양수 = 좌회전).

    방위: 오른쪽으로 돌았으면(heading 감소) 왼쪽으로 돈다. 옆 밀림: 오른쪽으로 밀렸으면 왼쪽으로
    가야 한다 — 전진은 코를 왼쪽으로, **후진은 코를 오른쪽으로** 돌려야 왼쪽으로 간다.
    """
    gains = gains or HoldGains()
    heading_err = now.heading_deg - reference.heading_deg  # + = 왼쪽으로 돌았다
    drift = now.distance_m - reference.distance_m
    # 왼벽에서 멀어지거나 오른벽에 가까워지면 오른쪽으로 밀린 것 (+ = 오른쪽 밀림, cm)
    right_cm = (drift if now.side == "left" else -drift) * 100.0
    lateral_turn = gains.lateral * right_cm * (-1.0 if reverse else 1.0)
    angle = bias_deg - gains.heading * heading_err + lateral_turn
    return max(-gains.limit_deg, min(gains.limit_deg, angle))


def trace_point(
    points: np.ndarray, refs: dict[str, WallFit], travelled_m: float
) -> dict[str, Any] | None:
    """한 바퀴의 좌우 벽 거리(cm)와 시작 대비 변화. 양쪽이 보이면 «오른쪽 밀림» 은 둘의 평균.

    오른쪽 밀림(+) = 왼벽에서 멀어짐 또는 오른벽에 가까워짐. 방위 변화(+) = 왼쪽으로 돎.
    """
    row: dict[str, Any] = {"travelled_m": round(travelled_m, 3)}
    shifts, turns = [], []
    for side, ref in refs.items():
        fit = fit_side_wall(points, side)
        if fit is None:
            continue
        delta = (fit.distance_m - ref.distance_m) * 100.0
        row[f"{side}_cm"] = round(fit.distance_m * 100.0, 1)
        row[f"{side}_delta_cm"] = round(delta, 1)
        shifts.append(delta if side == "left" else -delta)
        turns.append(fit.heading_deg - ref.heading_deg)
    if not shifts:
        return None
    row["right_shift_cm"] = round(statistics.fmean(shifts), 1)
    row["heading_change_deg"] = round(statistics.fmean(turns), 2)
    return row


def summarize_trace(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """한 구간의 벽 거리 기록 요약 — 끝 밀림, 가장 크게 벗어난 값, 1m 당 밀림(선형 맞춤)."""
    rows = [r for r in rows if "right_shift_cm" in r]
    if not rows:
        return None
    shift = np.array([r["right_shift_cm"] for r in rows], float)
    dist = np.array([r["travelled_m"] for r in rows], float)
    out: dict[str, Any] = {
        "samples": len(rows),
        "end_right_shift_cm": float(shift[-1]),
        "max_abs_shift_cm": round(float(np.max(np.abs(shift))), 1),
        "end_heading_change_deg": rows[-1]["heading_change_deg"],
    }
    if float(np.ptp(dist)) > 0.2:
        slope = float(np.polyfit(dist, shift, 1)[0])
        out["shift_cm_per_m"] = round(slope, 2)
    for side in ("left", "right"):
        values = [r[f"{side}_cm"] for r in rows if f"{side}_cm" in r]
        if values:
            out[f"{side}_wall_cm"] = {
                "start": values[0],
                "end": values[-1],
                "min": min(values),
                "max": max(values),
            }
    return out


def wrap_deg(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def fit_roll_offset(samples: list[tuple[float, float]]) -> dict[str, float] | None:
    """(POSE roll 명령, IMU roll 평균) 들로 IMU roll = 0 이 되는 명령을 선형으로 추정한다."""
    if len(samples) < 2:
        return None
    cmd = np.array([s[0] for s in samples])
    imu = np.array([s[1] for s in samples])
    if float(np.ptp(cmd)) == 0.0:
        return None
    slope, intercept = np.polyfit(cmd, imu, 1)
    if abs(slope) < 1e-6:
        return None
    zero = -intercept / slope
    resid = imu - (slope * cmd + intercept)
    return {
        "slope": round(float(slope), 3),
        "imu_at_zero_cmd": round(float(intercept), 2),
        "cmd_for_level": round(float(zero), 2),
        "fit_rms_deg": round(float(np.sqrt(np.mean(resid**2))), 2),
    }


# ── 장치 입출력 ──────────────────────────────────────────────


@dataclass
class Link:
    cmd: socket.socket
    lidar: socket.socket
    peer: tuple[str, int]
    commander: Commander
    telemetry: TelemetryDecoder
    scans: ScanDecoder
    assembler: RevolutionAssembler
    lidar_device: str
    range_m: tuple[float, float]
    log: Any
    reading: Reading | None = None
    reading_ms: int = 0
    points: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    points_ms: int = 0
    lidar_packets: int = 0

    def record(self, kind: str, **fields: Any) -> None:
        self.log.write(
            json.dumps({"t": system_clock_ms(), "kind": kind, **fields}, ensure_ascii=False) + "\n"
        )

    def send_due(self) -> None:
        now = system_clock_ms()
        for line in self.commander.tick(now):
            self.cmd.sendto(line.encode("utf-8"), self.peer)
            self.record("sent", line=line)

    def poll(self) -> None:
        """도착한 텔레메트리·라이다를 막지 않고 비운다."""
        while True:
            try:
                data, _ = self.cmd.recvfrom(4096)
            except (BlockingIOError, ConnectionResetError, OSError):
                break
            result = self.telemetry.decode(data)
            if result.accepted and result.message is not None and "imu" in result.message:
                self.reading = Reading.of(result.message)
                self.reading_ms = system_clock_ms()
                r = self.reading
                self.record(
                    "imu",
                    roll=r.roll,
                    pitch=r.pitch,
                    yaw=r.yaw,
                    state=r.state,
                    latched=r.safety_latched,
                    obstacle=r.obstacle,
                )
        while True:
            try:
                raw, _ = self.lidar.recvfrom(65535)
            except (BlockingIOError, ConnectionResetError, OSError):
                break
            scan = scan_of(self.scans.decode(raw))
            if scan is None or scan.device_id != self.lidar_device:
                continue
            self.lidar_packets += 1
            now = system_clock_ms()
            rev = self.assembler.add(scan, now)
            if rev is not None:
                self.points = preprocess(rev.points, *self.range_m)
                self.points_ms = now

    def pump(self, seconds: float) -> None:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            self.send_due()
            self.poll()
            time.sleep(0.01)

    def fresh(self, max_age_ms: int = 1000) -> str | None:
        now = system_clock_ms()
        if self.reading is None or now - self.reading_ms > max_age_ms:
            return "텔레메트리 끊김"
        if now - self.points_ms > max_age_ms:
            return "라이다 끊김"
        if self.reading.safety_latched:
            return "로봇 래치"
        if self.reading.obstacle:
            return "로봇 장애물 감지"
        return None

    def stop(self, estop: bool = False) -> None:
        if estop:
            line = self.commander.emergency_stop()
            for _ in range(3):
                self.cmd.sendto(line.encode("utf-8"), self.peer)
            self.record("estop")
        self.commander.halt()
        self.pump(0.6)


def leg(
    link: Link,
    *,
    forward: bool,
    mode: str,
    distance: float,
    step_mm: float,
    bias: float,
    clearance: float,
    speed_mm_s: float,
) -> dict[str, Any]:
    """한 방향 한 구간. 결과 요약을 돌려준다."""
    link.commander.halt()
    link.pump(1.0)  # 서서 새 스캔
    side_ref = None
    refs: dict[str, WallFit] = {}
    for side in ("left", "right"):
        fit = fit_side_wall(link.points, side)
        if fit is not None:
            refs[side] = fit
        if fit and (side_ref is None or fit.inliers > side_ref.inliers):
            side_ref = fit
    walls = " · ".join(
        f"{'왼' if k == 'left' else '오른'}벽 {v.distance_m * 100:.1f}cm" for k, v in refs.items()
    )
    print(f"   시작 {walls or '옆벽 못 찾음 — 밀림을 잴 수 없다'}")
    trace: list[dict[str, Any]] = []
    last_points_ms = link.points_ms
    last_print = 0.0
    start_range = range_along(link.points, forward)
    yaw0 = link.reading.yaw if link.reading else None
    rolls, pitches, angles = [], [], []
    deadline = time.perf_counter() + max(8.0, distance * 1000 / max(1.0, speed_mm_s) * 2.5)
    reason = "거리 도달"
    travelled = 0.0
    step = step_mm if forward else -step_mm
    while True:
        link.send_due()
        link.poll()
        problem = link.fresh()
        if problem:
            reason = problem
            break
        now_range = range_along(link.points, forward)
        if now_range is not None and now_range < clearance:
            reason = f"{'전방' if forward else '후방'} {now_range:.2f} m — 여유 {clearance} m 안"
            break
        if start_range is not None and now_range is not None:
            travelled = max(travelled, start_range - now_range)
            if travelled >= distance:
                break
        if time.perf_counter() > deadline:
            reason = "시간 초과(이동 거리 못 잼)" if start_range is None else "시간 초과"
            break
        if refs and link.points_ms != last_points_ms:
            last_points_ms = link.points_ms
            row = trace_point(link.points, refs, travelled)
            if row is not None:
                if link.reading is not None:
                    row["imu_yaw"] = link.reading.yaw
                    row["imu_roll"] = link.reading.roll
                trace.append(row)
                link.record(
                    "trace", direction="forward" if forward else "reverse", mode=mode, **row
                )
                if time.perf_counter() - last_print >= 0.5:
                    last_print = time.perf_counter()
                    sides = "  ".join(
                        f"{'왼' if k == 'left' else '오른'} {row[k + '_cm']:.1f}cm({row[k + '_delta_cm']:+.1f})"
                        for k in ("left", "right")
                        if k + "_cm" in row
                    )
                    print(
                        f"   {travelled:4.2f} m | {sides} | 오른쪽 밀림 {row['right_shift_cm']:+.1f} cm"
                        f" (최근5 중앙 {statistics.median(r['right_shift_cm'] for r in trace[-5:]):+.1f})"
                        f" | 방위 {row['heading_change_deg']:+.1f}°"
                    )
        angle = bias
        if mode == "hold" and side_ref is not None:
            now_fit = fit_side_wall(link.points, side_ref.side)
            if now_fit is not None:
                angle = hold_angle(side_ref, now_fit, reverse=not forward, bias_deg=bias)
        link.commander.drive(step, angle)
        angles.append(angle)
        if link.reading is not None:
            rolls.append(link.reading.roll)
            pitches.append(link.reading.pitch)
        time.sleep(0.02)
    link.commander.halt()
    link.pump(1.2)  # 서서 끝 스캔
    end_fit = fit_side_wall(link.points, side_ref.side) if side_ref else None
    if refs:
        final = trace_point(link.points, refs, travelled)
        if final is not None:
            final["settled"] = True
            trace.append(final)
    yaw1 = link.reading.yaw if link.reading else None

    def stats(values):
        values = [v for v in values if isinstance(v, int | float)]
        if not values:
            return None
        return {
            "mean": round(statistics.fmean(values), 2),
            "sd": round(statistics.pstdev(values), 2),
        }

    result = {
        "direction": "forward" if forward else "reverse",
        "mode": mode,
        "stop_reason": reason,
        "travelled_m": round(travelled, 3),
        "wall": None if side_ref is None else side_ref.side,
        "heading_change_deg": None
        if not (side_ref and end_fit)
        else round(end_fit.heading_deg - side_ref.heading_deg, 2),
        "right_drift_cm": None
        if not (side_ref and end_fit)
        else round(
            (
                (end_fit.distance_m - side_ref.distance_m)
                if side_ref.side == "left"
                else (side_ref.distance_m - end_fit.distance_m)
            )
            * 100,
            1,
        ),
        "imu_yaw_change_deg": None
        if yaw0 is None or yaw1 is None
        else round(wrap_deg(yaw1 - yaw0), 2),
        "roll": stats(rolls),
        "pitch": stats(pitches),
        "angle_cmd": stats(angles),
        "wall_trace": summarize_trace(trace),
    }
    link.record("leg", **result)
    result["_trace"] = trace
    return result


def plot_traces(path: Path, legs: list[dict[str, Any]]) -> None:
    """구간마다 «이동 거리 → 오른쪽 밀림(cm)» 선 그래프. 0 선 위가 오른쪽."""
    import cv2

    width, height, pad = 900, 520, 60
    img = np.full((height, width, 3), 255, np.uint8)
    series = [(leg_, leg_.get("_trace") or []) for leg_ in legs]
    shifts = [r["right_shift_cm"] for _, rows in series for r in rows if "right_shift_cm" in r] or [
        0.0
    ]
    span = max(5.0, max(abs(v) for v in shifts) * 1.15)
    max_d = max([r["travelled_m"] for _, rows in series for r in rows] or [2.0]) or 2.0

    def px(d: float, cm: float) -> tuple[int, int]:
        return int(pad + d / max_d * (width - 2 * pad)), int(
            height / 2 - cm / span * (height / 2 - pad)
        )

    cv2.line(img, px(0, 0), px(max_d, 0), (150, 150, 150), 1)
    for cm in (-span, span):
        cv2.putText(
            img,
            f"{cm:+.0f} cm",
            (5, px(0, cm)[1] + 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (90, 90, 90),
            1,
        )
    cv2.putText(
        img,
        f"{max_d:.1f} m",
        (px(max_d, 0)[0] - 30, height - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (90, 90, 90),
        1,
    )
    colors = {
        ("open", "forward"): (40, 40, 220),
        ("open", "reverse"): (40, 140, 240),
        ("hold", "forward"): (60, 160, 60),
        ("hold", "reverse"): (160, 120, 40),
    }
    for leg_, rows in series:
        pts = [px(r["travelled_m"], r["right_shift_cm"]) for r in rows if "right_shift_cm" in r]
        color = colors.get((leg_["mode"], leg_["direction"]), (0, 0, 0))
        for a_, b_ in zip(pts, pts[1:], strict=False):
            cv2.line(img, a_, b_, color, 2)
    y = 20
    for (mode, direction), color in colors.items():
        cv2.putText(
            img, f"{mode} {direction}", (width - 200, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2
        )
        y += 20
    cv2.putText(
        img,
        "right shift (cm) vs travelled (m), + = right",
        (pad, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (0, 0, 0),
        1,
    )
    cv2.imwrite(str(path), img)


def device_status(
    *,
    telemetry_age_ms: int | None,
    batt_v: float | None,
    latched: bool | None,
    packets_per_s: float,
    revs_per_s: float,
    battery_warn_v: float,
) -> tuple[bool, bool, list[str]]:
    """(로봇 준비, 라이다 준비, 경고들). 로봇은 1초 안 텔레메트리, 라이다는 초당 3바퀴 이상.

    패킷은 오는데 바퀴가 안 만들어지면 10-03/04 의 «347° 고착» 과 같은 모양이다 — 정상은 초당
    약 70패킷·10바퀴, 이상 때는 8패킷·0바퀴였다(tools/lidar/input_anomaly.py).
    """
    warnings = []
    robot_ok = telemetry_age_ms is not None and telemetry_age_ms <= 1000
    lidar_ok = revs_per_s >= 3.0  # 정상 LD19 ~10바퀴/s, 이상 때 0
    if packets_per_s > 0 and revs_per_s < 1.0:
        warnings.append(
            "라이다 패킷은 오는데 한 바퀴가 안 만들어짐 — 347° 이상 의심: 라이다·중계 전원 재시작"
        )
    elif 0 < packets_per_s < 40:
        warnings.append(f"라이다 패킷이 적다({packets_per_s:.0f}/s, 정상 ~70)")
    if robot_ok and batt_v is not None and batt_v < battery_warn_v:
        warnings.append(f"배터리 낮음 {batt_v:.2f} V (< {battery_warn_v} V)")
    if robot_ok and latched:
        warnings.append("로봇 래치 걸림 — 시작 때 RESET_SAFE 로 푼다")
    return robot_ok, lidar_ok, warnings


def wait_devices(link: Link, *, timeout_s: float, battery_warn_v: float) -> bool:
    """전원이 켜지기 전부터 돈다 — 정지 명령을 계속 보내 로봇이 켜지는 즉시 연결되고, 라이다를 듣는다.

    둘 다 2초 연속 준비되면 참. 1초마다 상태 한 줄. 시간 안에 안 되면 거짓.
    """
    print(
        "== 장치 대기 — 이제 로봇·라이다(·카메라) 전원을 켜세요. 준비되면 바로 묻습니다. (Ctrl+C 중단)"
    )
    link.commander.halt()
    started = time.perf_counter()
    last_report = started
    packets0, revs0 = link.lidar_packets, link.assembler.completed
    ready_since: float | None = None
    while time.perf_counter() - started < timeout_s:
        link.send_due()
        link.poll()
        time.sleep(0.01)
        now = time.perf_counter()
        if now - last_report < 1.0:
            continue
        dt = now - last_report
        last_report = now
        pps = (link.lidar_packets - packets0) / dt
        rps = (link.assembler.completed - revs0) / dt
        packets0, revs0 = link.lidar_packets, link.assembler.completed
        reading = link.reading
        age = None if reading is None else system_clock_ms() - link.reading_ms
        robot_ok, lidar_ok, warnings = device_status(
            telemetry_age_ms=age,
            batt_v=None if reading is None else reading.batt_v,
            latched=None if reading is None else reading.safety_latched,
            packets_per_s=pps,
            revs_per_s=rps,
            battery_warn_v=battery_warn_v,
        )
        robot = (
            f"로봇 연결 {reading.batt_v or 0:.2f}V · 기울기(roll) {reading.roll or 0:+.1f}° · {reading.state}"
            if robot_ok and reading is not None
            else "로봇 응답 없음"
        )
        lidar = f"라이다 {pps:.0f}패킷/s · {rps:.0f}바퀴/s" if pps else "라이다 수신 없음"
        print(
            f"  [{now - started:4.0f}s] {robot} | {lidar}"
            + "".join(f"\n    ⚠ {w}" for w in warnings)
        )
        link.record(
            "wait", robot_ok=robot_ok, lidar_ok=lidar_ok, pps=round(pps, 1), rps=round(rps, 1)
        )
        if robot_ok and lidar_ok:
            if ready_since is None:
                ready_since = now
            elif now - ready_since >= 2.0:
                return True
        else:
            ready_since = None
    return False


def roll_sweep(
    link: Link,
    values: list[float],
    pitch: float,
    settle_s: float,
    measure_s: float,
    restore_roll: float = 0.0,
) -> dict[str, Any]:
    samples = []
    for value in values:
        link.commander.once("POSE", pitch=pitch, roll=value, height=0, dur=300)
        link.pump(settle_s)
        start = len(samples)
        rolls = []
        end = time.perf_counter() + measure_s
        while time.perf_counter() < end:
            link.send_due()
            link.poll()
            if link.reading is not None and link.reading.roll is not None:
                rolls.append(link.reading.roll)
            time.sleep(0.05)
        if rolls:
            samples.append((value, statistics.fmean(rolls)))
            link.record(
                "roll_step", pose_roll=value, imu_roll_mean=round(samples[-1][1], 2), n=len(rolls)
            )
            print(
                f"  POSE roll {value:+.1f}° → IMU roll 평균 {samples[-1][1]:+.2f}° ({len(rolls)}표본)"
            )
        del start
    # 스윕이 끝나면 설정의 보정값으로 — 0 으로 두면 뒤따르는 걷기 시험이 보정 없이 돈다.
    link.commander.once("POSE", pitch=pitch, roll=restore_roll, height=0, dur=300)
    link.pump(1.0)
    return {"samples": samples, "fit": fit_roll_offset(samples)}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--device", default="mechdog-02")
    ap.add_argument("--robot-ip")
    ap.add_argument("--lidar-device", default="lidar-b03fd35ee950")
    ap.add_argument("--record-dir", type=Path, required=True)
    ap.add_argument("--distance", type=float, default=2.0)
    ap.add_argument("--cycles", type=int, default=2, help="앞뒤 왕복 횟수 (0 이면 걷지 않음)")
    ap.add_argument("--mode", choices=["open", "hold", "both"], default="both")
    ap.add_argument("--step", type=float, default=60.0, help="걸음 mm (상한 100)")
    ap.add_argument("--bias", type=float, default=0.0, help="직진 명령에 얹는 angle(°, 좌 +)")
    ap.add_argument("--clearance", type=float, default=0.5)
    ap.add_argument("--roll-sweep", default="", help="예: 0,2,4,6 — 서서 POSE roll 스윕")
    ap.add_argument("--pitch", type=float, default=0.0)
    ap.add_argument("--wait-timeout", type=float, default=900.0, help="장치 대기 상한(초)")
    ap.add_argument(
        "--no-wait", action="store_true", help="장치 대기 없이 바로 (이미 켜져 있을 때)"
    )
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # 현장에서 진행이 바로 보이게
    if not 0 < a.step <= 100:
        ap.error("--step 은 0 초과 100 이하")
    config = load_config(a.device)
    net = config["network"]
    host = a.robot_ip or net["mechdog_ip"]
    peer = (host, int(net["cmd_port"]))
    lidar_cfg = config["lidar"]
    range_m = (float(lidar_cfg["range_min_mm"]) / 1000, float(lidar_cfg["range_max_mm"]) / 1000)
    gait = config.get("gait_calibration") or {}
    speed = float(gait.get("forward_mm_per_sec") or 68.7) * a.step / 100.0
    a.record_dir.mkdir(parents=True, exist_ok=True)
    print(f"로봇 {host}:{peer[1]} · 라이다 {a.lidar_device} · 기록 {a.record_dir}")
    print(
        f"왕복 {a.cycles}회 × {a.distance} m · 모드 {a.mode} · 걸음 {a.step} mm · 여유 {a.clearance} m"
    )
    cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd.bind(("", int(net["telemetry_port"])))
    cmd.setblocking(False)
    lid = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    lid.bind(("", int(lidar_cfg["scan_port"])))
    lid.setblocking(False)
    period_ms = round(1000 / float(net["cmd_rate_hz"]))
    log = (a.record_dir / "straight.jsonl").open("a", encoding="utf-8")
    link = Link(
        cmd,
        lid,
        peer,
        Commander(CommandEncoder(), period_ms=period_ms),
        TelemetryDecoder(),
        ScanDecoder(
            float(lidar_cfg.get("mount_yaw_deg", 0.0)), int(lidar_cfg.get("angle_direction", 1))
        ),
        RevolutionAssembler(),
        a.lidar_device,
        range_m,
        log,
    )
    summary: dict[str, Any] = {"args": vars(a) | {"record_dir": str(a.record_dir)}, "legs": []}
    try:
        cmd.sendto(link.commander.open_session().encode("utf-8"), peer)
        if not a.no_wait and not wait_devices(
            link,
            timeout_s=a.wait_timeout,
            battery_warn_v=float(config["safety"].get("battery_warn_v", 7.0)),
        ):
            print("시간 안에 로봇·라이다가 준비되지 않았다 — 중단.")
            return 2
        print("\a== 준비 완료.")
        if (
            input(
                "  로봇이 바닥 선 위(앞이 왕복 방향)이고 앞 2.5m·뒤 0.5m 가 비었나? "
                "래치를 풀고 바로 시작한다 (정지 Ctrl+C) (y/N) "
            ).strip()
            != "y"
        ):
            print("중단.")
            return 1
        link.commander.once("RESET_SAFE")
        link.commander.halt()
        link.pump(1.5)
        problem = link.fresh(1500)
        if problem and problem != "로봇 래치":
            print(f"시작 불가 — {problem}")
            return 2
        roll_offset = float((config.get("posture") or {}).get("roll_offset_deg") or 0.0)
        if a.roll_sweep:
            values = [float(v) for v in a.roll_sweep.split(",") if v.strip()]
            print("== 기울기 스윕 (서 있음)")
            summary["roll_sweep"] = roll_sweep(
                link, values, a.pitch, 2.0, 6.0, restore_roll=roll_offset
            )
            print(f"  권장 POSE roll ≈ {summary['roll_sweep']['fit']}")
        else:
            link.commander.once("POSE", pitch=a.pitch, roll=roll_offset, height=0, dur=300)
            link.pump(1.0)
        print(f"== 걷기 시험은 설정 보정값 POSE roll {roll_offset:+.1f} 로")
        summary["walk_pose_roll"] = roll_offset
        modes = {"open": ["open"], "hold": ["hold"], "both": ["open", "hold"]}[a.mode]
        for cycle in range(a.cycles):
            for mode in modes:
                for forward in (True, False):
                    print(f"== {cycle + 1}회차 {mode} {'전진' if forward else '후진'}")
                    result = leg(
                        link,
                        forward=forward,
                        mode=mode,
                        distance=a.distance,
                        step_mm=a.step,
                        bias=a.bias,
                        clearance=a.clearance,
                        speed_mm_s=speed,
                    )
                    summary["legs"].append(result)
                    shown = {k: v for k, v in result.items() if k != "_trace"}
                    print("  ", json.dumps(shown, ensure_ascii=False))
                    if (
                        result["stop_reason"] not in ("거리 도달", "시간 초과")
                        and "여유" not in result["stop_reason"]
                    ):
                        print(f"중단 — {result['stop_reason']}")
                        raise KeyboardInterrupt
    except KeyboardInterrupt:
        link.stop(estop=True)
        summary["aborted"] = True
    finally:
        link.stop()
        try:
            plot_traces(a.record_dir / "straight_wall_trace.png", summary["legs"])
        except Exception as exc:  # noqa: BLE001 — 그래프 실패가 기록을 막으면 안 된다
            print(f"그래프 실패: {exc}")
        for leg_ in summary["legs"]:
            leg_.pop("_trace", None)
        (a.record_dir / "straight_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.close()
        cmd.close()
        lid.close()
    legs = summary["legs"]
    for mode in ("open", "hold"):
        for direction in ("forward", "reverse"):
            rows = [
                r
                for r in legs
                if r["mode"] == mode
                and r["direction"] == direction
                and r["heading_change_deg"] is not None
            ]
            if rows:
                print(
                    f"{mode:4s} {direction:7s}: 방위 변화 평균 {statistics.fmean(r['heading_change_deg'] for r in rows):+.1f}° · "
                    f"오른쪽 밀림 평균 {statistics.fmean(r['right_drift_cm'] for r in rows):+.1f} cm ({len(rows)}회)"
                )
    for r in legs:
        w = r.get("wall_trace")
        if w:
            per_m = w.get("shift_cm_per_m")
            print(
                f"  {r['mode']:4s} {r['direction']:7s}: 끝 오른쪽 밀림 {w['end_right_shift_cm']:+.1f} cm · "
                f"최대 {w['max_abs_shift_cm']:.1f} cm"
                + (f" · 1m 당 {per_m:+.2f} cm" if per_m is not None else "")
            )
    print(
        f"요약: {a.record_dir / 'straight_summary.json'} · 그래프: {a.record_dir / 'straight_wall_trace.png'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
