"""실측 보조 수집기. 전체 계획/기체별 증거 기록은 tools/field_plan.py.

기본 실행은 명령을 전송하지 않는다. 항목 0은 수동 수신만 한다.
구동 항목은 --allow-motion 및 현장 준비 확인이 필요하다.
10Hz IMU로 물리 정지 시간/50ms 서보 지연/주기별 진폭을 판정하지 않는다.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import socket
import statistics
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gait_calibrate import Acks, discard_reason, drive_window, pump  # noqa: E402

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, TelemetryDecoder, system_clock_ms  # noqa: E402
from host.telemetry.receiver import Reading  # noqa: E402

#: IMU 가 "움직이고 있다" 로 볼 pitch/roll 표준편차(도). 선 상태의 노이즈는
#: ~0.1° 이고 트롯은 수 °씩 흔들린다. 둘 사이 아무데나 놓으면 되지만 **너무 낮으면
#: 서 있는 것도 움직임으로 읽으므로** 보수적으로 둔다.
MOTION_STDDEV_DEG = 0.4
#: 판정 창. 10Hz 텔레메트리 기준으로 최소 표본이 모이는 길이다.
MOTION_WINDOW_S = 0.4
#: 1-7 에서 "멈췄다" 로 인정할 연속 정지 길이 — 순간 노이즈로 조기 판정하지 않게.
STILL_HOLD_S = 0.3
#: 항목들의 공통 상한. 로봇 응답이 없으면 기다리다 끝나는 게 아니라 실패로 남긴다.
WAIT_TIMEOUT_S = 8.0
ASK_HANDLER: ContextVar[Callable[[str], str] | None] = ContextVar("measurement_ask", default=None)


@dataclass(slots=True)
class Sample:
    """수신 시각(호스트 `perf_counter`)을 붙인 텔레메트리 하나."""

    at: float
    reading: Reading


class TelemetryTap:
    """백그라운드 수신기. 명령 소켓과 **별개** — 로봇→호스트 방향은 항상 흐른다.

    명령 스트림을 끊는 시험(1-7)에서도 텔레메트리는 계속 오므로 정지 판정에
    쓸 수 있다.
    """

    def __init__(self, port: int, device_id: str, source_ip: str | None = None) -> None:
        self.cancel_check = lambda: None
        self._device_id = device_id
        self._source_ip = source_ip
        self._decoder = TelemetryDecoder()
        self._lock = threading.Lock()
        self.samples: deque[Sample] = deque(maxlen=600)  # 10Hz × 60초
        self.latest: Reading | None = None
        self.received = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # ⚠️ SO_REUSEADDR 없음이 의도다 — 포트를 누가 잡고 있으면 조용히
        # 표본 0 개가 되는 대신 bind 에서 바로 터진다 (PR #162).
        try:
            self._sock.bind(("0.0.0.0", port))
        except OSError:
            self._sock.close()
            raise
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _addr = self._sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                continue
            if self._source_ip is not None and _addr[0] != self._source_ip:
                continue
            self.accept(raw)

    def accept(self, raw: bytes) -> bool:
        """Reject another board, malformed packets and duplicate/out-of-order seqs."""
        try:
            msg = json.loads(raw)
        except (ValueError, UnicodeError):
            return False
        if not isinstance(msg, dict) or msg.get("device_id") != self._device_id:
            return False
        decoded = self._decoder.validate(msg)
        if not decoded.accepted or decoded.message is None:
            return False
        reading = Reading.of(decoded.message)
        with self._lock:
            self.received += 1
            self.latest = reading
            self.samples.append(Sample(time.perf_counter(), reading))
        return True

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=1.0)

    def window(self, seconds: float) -> list[Sample]:
        """최근 `seconds` 동안의 표본."""
        cut = time.perf_counter() - seconds
        with self._lock:
            return [s for s in self.samples if s.at >= cut]

    def motion(self, window_s: float = MOTION_WINDOW_S) -> bool | None:
        """최근 창의 IMU 흔들림. 표본이 모자라면 `None`(모름) — 모르는 것을
        '멈춤' 으로 읽으면 1-7 이 가짜 통과를 한다."""
        samples = self.window(window_s)
        pitches = [s.reading.pitch for s in samples if s.reading.pitch is not None]
        rolls = [s.reading.roll for s in samples if s.reading.roll is not None]
        if len(pitches) < 3:
            return None
        shake = max(
            statistics.stdev(pitches) if len(pitches) > 1 else 0.0,
            statistics.stdev(rolls) if len(rolls) > 1 else 0.0,
        )
        return shake > MOTION_STDDEV_DEG

    def wait_flag(
        self, pred: Callable[[Reading], bool], timeout_s: float = WAIT_TIMEOUT_S
    ) -> tuple[bool, float]:
        """`pred` 를 만족하는 최신 레코드를 기다린다. (성공, 경과초)."""
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout_s:
            self.cancel_check()
            fresh = self.window(0.5)
            r = fresh[-1].reading if fresh and fresh[-1].at >= t0 else None
            if r is not None and pred(r):
                return True, time.perf_counter() - t0
            time.sleep(0.05)
        return False, time.perf_counter() - t0


@dataclass(slots=True)
class Result:
    """한 측정 항목의 기록 — md 요약과 json 원본에 같이 들어간다."""

    item: str
    wbs: str
    verdict: str  # pass | fail | skipped | measured
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    at: str = ""


def _ask(prompt: str) -> str:
    handler = ASK_HANDLER.get()
    if handler is not None:
        return handler(prompt).strip()
    return input(prompt).strip()


def _ask_yn(prompt: str) -> bool:
    while True:
        raw = _ask(prompt + " [y/n/s(건너뜀)]: ").lower()
        if raw in ("y", "yes", "ㅛ"):
            return True
        if raw in ("n", "no"):
            return False
        if raw in ("s", "skip", ""):
            return False
        print("    y / n / s 중 하나로 답한다")


def _ask_number(prompt: str, *, allow_blank: bool = True, signed: bool = False) -> float | None:
    while True:
        raw = _ask(prompt)
        if not raw:
            return None if allow_blank else _ask_number(prompt, allow_blank=allow_blank)
        try:
            value = float(raw)
        except ValueError:
            print("    숫자를 입력한다")
            continue
        if not math.isfinite(value):
            print("    유한한 숫자만 입력한다")
            continue
        if value < 0 and not signed:
            print("    음수는 안 된다")
            continue
        return value


@dataclass(slots=True)
class Ctx:
    sock: socket.socket
    peer: tuple[str, int]
    commander: Commander
    tap: TelemetryTap | None
    device: str
    host: str
    results: list[Result] = field(default_factory=list)


def _require_telemetry(ctx: Ctx) -> bool:
    """자동 판정의 전제 — 텔레메트리가 흐르는지 본다."""
    if ctx.tap is None:
        return False
    ok, _ = ctx.tap.wait_flag(lambda _r: True, timeout_s=3.0)
    if not ok:
        print("    ⚠️ 텔레메트리가 오지 않는다 — 자동 판정 불가, 이 항목은 건너뛴다")
    return ok


def _send(ctx: Ctx, packet: str) -> None:
    ctx.sock.sendto(packet.encode("utf-8"), ctx.peer)


def m_link(ctx: Ctx) -> Result:  # pragma: no cover - 실기 측정용
    """0. 텔레메트리 링크 기저선 — 이게 없으면 아래 자동 항목이 전부 무의미하다."""
    print("  3초 동안 수신률을 잰다…")
    t0 = time.perf_counter()
    n0 = ctx.tap.received if ctx.tap else 0
    time.sleep(3.0)
    n = (ctx.tap.received if ctx.tap else 0) - n0
    rate = n / (time.perf_counter() - t0)
    r = ctx.tap.latest if ctx.tap else None
    data: dict[str, Any] = {"rate_hz": round(rate, 1), "samples": n}
    if r is not None:
        data.update(
            batt_v=r.batt_v,
            state=r.state,
            safety_latched=r.safety_latched,
            service=r.service,
            last_cmd_age_ms=r.last_cmd_age_ms,
        )
    ok = 9.0 <= rate <= 11.0
    data["within_10hz_plus_minus_1"] = ok
    data["observation_seconds"] = 3.0
    print(f"    수신 {rate:.1f}Hz · state={r.state if r else '—'} · batt={r.batt_v if r else '—'}V")
    return Result(
        "텔레메트리 링크",
        "전제",
        "measured" if n else "fail",
        f"3초 예비 관찰 {rate:.1f}Hz 수신"
        + (f" · {r.state} · 래치={r.safety_latched}" if r else " · 무수신"),
        data,
    )


def m_cmd_timeout(_ctx: Ctx) -> Result:  # pragma: no cover - 실기 측정용
    """Invalid legacy measurement intentionally cannot actuate the robot."""
    return Result(
        "외부 계측 필요",
        "측정계획 참조",
        "skipped",
        "기존 drive_window는 HALT/RESET_SAFE를 보내므로 명령 두절/래치 시험이 아님. HW-011 절차로 외부 계측 필요.",
    )


def m_drive(ctx: Ctx, mode: str) -> Result:  # pragma: no cover - 실기 측정용
    """2-2. 전진/후진 직선거리 — 줄자 입력. 구동 창의 IMU 도 같이 남긴다."""
    if ctx.tap is not None and not _require_telemetry(ctx):
        return Result("이동량", "2.2.3", "skipped", "새 텔레메트리 없음 — 구동 안 함")
    label = "전진" if mode == "forward" else "후진"
    step = 60 if mode == "forward" else -60
    trials: list[dict[str, Any]] = []
    for index in range(1, 4):
        if ctx.tap is not None and not _require_telemetry(ctx):
            break
        print(f"\n  [{index}/3] {label} 3초 — 시작 위치에 표시를 하고 준비되면 엔터")
        if _ask("  (엔터=실행 / s=건너뜀) ").lower() in ("s", "skip"):
            continue
        mark_t = time.perf_counter()
        actual, packets, acks = drive_window(
            ctx.sock,
            ctx.peer,
            ctx.commander,
            step_mm=step,
            angle_deg=0,
            seconds=3.0,
            settle_s=1.0,
        )
        yaw_drift = None
        if ctx.tap is not None:
            near = ctx.tap.window(time.perf_counter() - mark_t)
            if len(near) >= 5:
                yaw_drift = (
                    round(near[-1].reading.yaw - near[0].reading.yaw, 1)
                    if near[0].reading.yaw is not None and near[-1].reading.yaw is not None
                    else None
                )
        print(f"    송신 창 {actual:.2f}초 · 패킷 {packets} · {acks.describe()}")
        reason = discard_reason(acks, ctx.host)
        if reason is not None:
            print(f"    ⚠️ {reason} — 이 시행은 버린다")
            continue
        mm = _ask_number(f"  [{index}/3] 시작점→끝점 직선거리(mm)? 엔터=버림: ")
        if mm is None:
            continue
        drift = _ask_number("       방향 변화(도, 좌+/우-, 0=직진)? 모르면 엔터: ", signed=True)
        trials.append(
            {
                "mm": mm,
                "actual_s": round(actual, 3),
                "packets": packets,
                "deg": drift,
                "imu_yaw_drift": yaw_drift,
                "acks": acks.describe(),
            }
        )
    rates = [t["mm"] / t["actual_s"] for t in trials]
    mean = statistics.fmean(rates) if rates else None
    data = {"trials": trials, "mm_per_s": round(mean, 1) if mean is not None else None}
    verdict = "measured" if len(trials) >= 3 else ("fail" if not trials else "skipped")
    # ⚠️ `mean` 의 falsy 검사가 아니라 `is not None` 이다. **0.0 은 빈 값이 아니라
    # 관측값이다** — 안 움직이는 기체를 "측정 안 됨" 으로 적으면 가장 중요한 결과가
    # 지워지고, 사람은 도구가 고장난 줄 알고 다시 잰다.
    # 퍼짐은 평균 대비 비율이라 0 에서는 정의되지 않는다(0 나누기). 거기만 falsy 검사다.
    spread = statistics.stdev(rates) / mean if len(rates) >= 2 and mean else None
    return Result(
        f"{label} 이동량",
        "2.2.3",
        verdict,
        f"평균 {mean:.1f} mm/s (n={len(trials)})"
        + (f" · 퍼짐 {spread * 100:.0f}%" if spread is not None else "")
        if mean is not None
        else "유효 시행 없음",
        data,
    )


#: 모드 → `move(step, angle)`. **부호는 `gait_calibrate.command_for` 와 같아야 한다**
#: — 두 도구가 같은 모드명으로 반대 방향을 보내면, 잰 값이 회피가 실제 쓰는 구동의
#: 반대 방향 측정이 된다(2026-09-21 실수정: 여기 `reverse_turn` 이 -20 이었다).
#: ⚠️ `reverse_turn` 은 **+20 (후진+좌선회)** 다 — 회피 시퀀스가 실제 보내는
#: `Phase("reverse_turn", -step, +turn_deg)` (actions.avoid_phases · ADR-29)와
#: `gait_calibrate.command_for` 의 부호를 따른다.
TURN_COMMANDS: dict[str, tuple[float, float]] = {
    "turn_left": (60.0, 20.0),
    "turn_right": (60.0, -20.0),
    "reverse_turn": (-60.0, 20.0),
}

TURN_LABELS: dict[str, str] = {
    "turn_left": "좌선회",
    "turn_right": "우선회",
    "reverse_turn": "후진 선회",
}


def m_turn(ctx: Ctx, mode: str) -> Result:  # pragma: no cover - 실기 측정용
    """2-2. 선회율 — 외부 각도/시간으로 산출. IMU 차이는 참고값만 기록.

    ⚠️ angle 양수 = 반시계 = 좌회전(PROTOCOL). turn_right 는 음수를 보낸다.
    선회는 제자리 회전이 아니라 호를 그리며 앞으로도 간다 — 공간을 넓게 둔다.
    """
    if not _require_telemetry(ctx):
        return Result("IMU 보조 선회율 " + mode, "2.2.3", "skipped", "텔레메트리 없음", {})
    tap = ctx.tap
    assert tap is not None
    step, angle = TURN_COMMANDS[mode]
    label = TURN_LABELS[mode]
    trials: list[dict[str, Any]] = []
    for index in range(1, 4):
        if ctx.tap is not None and not _require_telemetry(ctx):
            break
        print(f"\n  [{index}/3] {label} 3초 — 주변 공간을 확인하고 엔터 (s=건너뜀)")
        if _ask("  ").lower() in ("s", "skip"):
            continue
        yaw0 = tap.latest.yaw if tap.latest else None
        actual, packets, acks = drive_window(
            ctx.sock,
            ctx.peer,
            ctx.commander,
            step_mm=step,
            angle_deg=angle,
            seconds=3.0,
            settle_s=1.0,
        )
        time.sleep(0.4)  # 마지막 텔레메트리가 도착할 시간
        yaw1 = tap.latest.yaw if tap.latest else None
        reason = discard_reason(acks, ctx.host)
        if reason is not None:
            print(f"    ⚠️ {reason} — 이 시행은 버린다")
            continue
        if yaw0 is None or yaw1 is None:
            print("    yaw 를 못 읽었다 — 시행 버림")
            continue
        delta = (yaw1 - yaw0 + 540.0) % 360.0 - 180.0
        rate = delta / actual
        print(f"    IMU 방향 변화 {delta:+.1f}° / {actual:.2f}s = {rate:+.1f}°/s")
        deg = _ask_number("       각도기 대조값(도, 좌+/우-)? 안 쟀으면 엔터: ", signed=True)
        mm = _ask_number("       시작점→끝점 직선거리(mm)? 안 쟀으면 엔터: ")
        trials.append(
            {
                "imu_delta_deg": round(delta, 1),
                "deg_per_s": round(deg / actual, 1) if deg is not None else None,
                "imu_deg_per_s": round(rate, 1),
                "actual_s": round(actual, 3),
                "protractor_deg": deg,
                "chord_mm": mm,
                "acks": acks.describe(),
            }
        )
    rates = [t["deg_per_s"] for t in trials if t["deg_per_s"] is not None]
    mean = statistics.fmean(rates) if rates else None
    verdict = "measured" if len(rates) >= 3 else "skipped"
    return Result(
        f"외부 각도 선회율 {label} (IMU 별도 참고)",
        "2.2.3",
        verdict,
        # 0.0 °/s 는 "안 돌았다" 는 관측값이다 — `m_drive` 와 같은 이유로 `is not None`.
        f"평균 {mean:+.1f} °/s (n={len(trials)})" if mean is not None else "유효 시행 없음",
        {"trials": trials, "deg_per_s": round(mean, 1) if mean is not None else None},
    )


def m_gait_imu(_ctx: Ctx) -> Result:  # pragma: no cover - 실기 측정용
    """Invalid legacy measurement intentionally cannot actuate the robot."""
    return Result(
        "외부 계측 필요",
        "측정계획 참조",
        "skipped",
        "3초·10Hz의 절대 자세값은 주기별 보행 진폭과 WBS 연속10초를 검증하지 못함. HW-010 원본 분석 사용.",
    )


def m_teleop(ctx: Ctx) -> Result:  # pragma: no cover - 실기 측정용
    """2-4. 방향 매핑 — 각 키를 한 번씩 보내고 사람이 방향을 확인한다."""
    keys = [
        ("W 전진", 60, 0),
        ("S 후진", -60, 0),
        ("A 좌선회", 60, 20),
        ("D 우선회", 60, -20),
    ]
    outcomes: dict[str, str] = {}
    for label, step, angle in keys:
        print(f"\n  [{label}] 1.5초 구동 — 로봇이 어느 쪽으로 가는지 본다")
        if _ask("  엔터=실행 / s=건너뜀: ").lower() in ("s", "skip"):
            outcomes[label] = "skipped"
            continue
        _actual, _packets, acks = drive_window(
            ctx.sock,
            ctx.peer,
            ctx.commander,
            step_mm=step,
            angle_deg=angle,
            seconds=1.5,
            settle_s=0.8,
        )
        if discard_reason(acks, ctx.host) is not None:
            outcomes[label] = "WRONG"
            continue
        ok = _ask_yn(f"  로봇이 [{label.split()[0]}] 방향으로 움직였나")
        outcomes[label] = "ok" if ok else "WRONG"
    bad = [k for k, v in outcomes.items() if v == "WRONG"]
    return Result(
        "텔레옵 방향",
        "2-4",
        "fail" if bad else ("pass" if all(v == "ok" for v in outcomes.values()) else "skipped"),
        "시행별 결과 확인 (건너뜀은 통과 아님)" if not bad else f"뒤바뀜: {', '.join(bad)}",
        {"keys": outcomes},
    )


def m_service(ctx: Ctx) -> Result:  # pragma: no cover - 실기 측정용
    """1-1/1-3. SERVICE 명령 왕복 + GPIO5 버튼 — 플래그 반전을 텔레메트리로 본다."""
    if not _require_telemetry(ctx):
        return Result("서비스 왕복", "확장팩", "skipped", "텔레메트리 없음", {})
    tap = ctx.tap
    assert tap is not None
    data: dict[str, Any] = {}
    # 명령 왕복
    ctx.commander.once("SERVICE", mode="enter")
    for line in ctx.commander.tick(system_clock_ms()):
        _send(ctx, line)
    ok_in, dt_in = tap.wait_flag(lambda r: r.service is True, timeout_s=5.0)
    pump(ctx.sock, ctx.peer, ctx.commander, 0.5, Acks())
    ctx.commander.once("SERVICE", mode="exit")
    for line in ctx.commander.tick(system_clock_ms()):
        _send(ctx, line)
    ok_out, dt_out = tap.wait_flag(lambda r: r.service is False, timeout_s=5.0)
    data["cmd_enter_s"] = round(dt_in, 2) if ok_in else None
    data["cmd_exit_s"] = round(dt_out, 2) if ok_out else None
    print(
        f"    명령 진입 {dt_in:.2f}s({'ok' if ok_in else '실패'}) · 해제 {dt_out:.2f}s({'ok' if ok_out else '실패'})"
    )
    # GPIO5 — 사람이 누르고 도구가 감지한다
    gpio = None
    if _ask_yn("  GPIO5 흰색 버튼을 지금 눌러볼 수 있나"):
        print("    지금 누른다 — 진입 신호를 기다린다 (최대 15초)")
        btn_in, dt_btn = tap.wait_flag(lambda r: r.service is True, timeout_s=15.0)
        if btn_in:
            print(f"    진입 감지 ({dt_btn:.1f}s) — 한 번 더 눌러 해제를 본다 (최대 15초)")
            btn_out, dt_btn2 = tap.wait_flag(lambda r: r.service is False, timeout_s=15.0)
            gpio = {"enter_s": round(dt_btn, 1), "exit_s": round(dt_btn2, 1) if btn_out else None}
        else:
            gpio = {"enter_s": None}
    data["gpio5"] = gpio
    ok = (
        ok_in
        and ok_out
        and (
            gpio is not None and gpio.get("enter_s") is not None and gpio.get("exit_s") is not None
        )
    )
    return Result(
        "서비스 왕복",
        "확장팩 1-1/1-3",
        "measured" if ok else "skipped",
        f"명령 enter={data['cmd_enter_s']}s exit={data['cmd_exit_s']}s · GPIO5={gpio}",
        data,
    )


def m_e2e(_ctx: Ctx) -> Result:  # pragma: no cover - 실기 측정용
    """Invalid legacy measurement intentionally cannot actuate the robot."""
    return Result(
        "외부 계측 필요",
        "측정계획 참조",
        "skipped",
        "10Hz IMU는 50ms 물리 구동 지연을 분해할 수 없음. TC-N-005/HW-011 외부 계측 필요.",
    )


MENU: list[tuple[str, str, Callable[[Ctx], Result]]] = [
    ("0", "텔레메트리 링크 기저선 (자동)", m_link),
    ("1", "명령 두절 시험 — 외부 계측 절차 안내", m_cmd_timeout),
    ("2", "전진 이동량 — forward_mm_per_sec [2.2.3] (줄자)", lambda c: m_drive(c, "forward")),
    ("3", "후진 이동량 — reverse_mm_per_sec [2.2.3] (줄자)", lambda c: m_drive(c, "reverse")),
    (
        "4",
        "좌선회율 — turn_deg_per_sec [2.2.3] (IMU 보조값; 외부 각도 확인 필수)",
        lambda c: m_turn(c, "turn_left"),
    ),
    (
        "5",
        "우선회율 대조 [2.2.3] (IMU 보조값; 외부 각도 확인 필수)",
        lambda c: m_turn(c, "turn_right"),
    ),
    (
        "6",
        "후진 선회 — reverse_turn [2.2.3] (IMU 보조값; 외부 각도 확인 필수)",
        lambda c: m_turn(c, "reverse_turn"),
    ),
    ("7", "보행 진폭 — 폰 원본 분석 절차 안내", m_gait_imu),
    ("8", "텔레옵 방향 W/S/A/D [2-4] (눈으로 y/n)", m_teleop),
    ("9", "서비스 모드 왕복 + GPIO5 버튼 [확장팩] (자동 감지)", m_service),
    ("10", "물리 구동 지연 — 고속 계측 절차 안내", m_e2e),
]


def save(results: list[Result], out_dir: Path, device: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
    path = out_dir / f"field_{stamp}.json"
    payload = {
        "tool": "field_measure",
        "device": device,
        "evidence_scope": "보조 관찰 — 기체별 계획 도구에서 조건·근거 확인 후 판정",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = out_dir / f"field_{stamp}.md"
    lines = [
        f"# 실기 측정 — {device} · {payload['at']}",
        "",
        "| 항목 | WBS | 판정 | 결과 |",
        "|---|---|---|---|",
    ]
    for r in results:
        mark = {"pass": "✅", "fail": "❌", "measured": "📏", "skipped": "⏭️"}.get(r.verdict, "?")
        lines.append(f"| {r.item} | {r.wbs} | {mark} {r.verdict} | {r.summary} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:  # pragma: no cover - 실기 측정용
    survive_encoding_errors()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="mechdog-02")
    ap.add_argument("--host", help="로봇 IPv4 주소")
    ap.add_argument("--out", default=".")
    ap.add_argument("--item", choices=[k for k, _, _ in MENU])
    ap.add_argument("--allow-motion", action="store_true", help="현장 구동 시험 명시적 선택")
    ap.add_argument("--plan", action="store_true", help="명령 없이 기체별 계획 GUI 열기")
    args = ap.parse_args()
    if args.plan:
        from field_plan import gui

        gui(Path(args.out), args.device)
        return 0
    if args.item is None:
        for key, label, _ in MENU:
            print(f"{key}: {label}")
        print("전체 기록: --plan / 한 항목: --item 번호 --host IP. 구동은 --allow-motion 필요.")
        return 0
    fn = next(f for k, _, f in MENU if k == args.item)
    if args.item in {"1", "7", "10"}:
        print(fn(None).summary)
        return 0
    active = args.item != "0"
    if active and not args.allow_motion:
        ap.error(
            "구동/상태 변경 시험에는 --allow-motion이 필요합니다. 아직 패킷을 보내지 않았습니다."
        )
    if not args.host:
        ap.error("수집할 기체의 --host IPv4 주소가 필요합니다.")
    import ipaddress

    ipaddress.IPv4Address(args.host)
    config = load_config(args.device)
    network = config["network"]
    identity = config["telemetry_device_id"]
    if active:
        print("바닥·주변 공간 확보, 관제 및 다른 명령 송신기 종료, 독립 중지 수단 확인.")
        print("이 도구의 보행 시험은 RESET_SAFE 후 구동합니다. 기체/자세를 먼저 확인하세요.")
        if (
            _ask(f"실제 기체 {identity}에서 이 항목 구동을 준비했으면 '준비완료' 입력: ")
            != "준비완료"
        ):
            return 0
    # Receive ownership and fresh exact identity must be verified before any command.
    try:
        tap = TelemetryTap(int(network["telemetry_port"]), identity, args.host)
    except OSError as exc:
        print(f"수신 포트 확보 실패. 명령 송신 없이 종료: {exc}")
        return 2
    sock = None
    started = False
    commander = Commander(
        CommandEncoder(clock=system_clock_ms), period_ms=int(1000 / network.get("cmd_rate_hz", 10))
    )
    try:
        if not tap.wait_flag(lambda _r: True, 3)[0]:
            print("해당 기체의 유효한 새 텔레메트리 없음. 명령 송신 없이 종료.")
            return 2
        peer = (args.host, int(network["cmd_port"]))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        ctx = Ctx(sock, peer, commander, tap, args.device, args.host)
        if active:
            sock.sendto(commander.open_session().encode("utf-8"), peer)
            started = True
        try:
            result = fn(ctx)
        except (KeyboardInterrupt, Exception) as exc:
            if started:
                with contextlib.suppress(OSError):
                    sock.sendto(commander.emergency_stop().encode("utf-8"), peer)
            result = Result(
                f"항목 {args.item} 중단",
                "미판정",
                "skipped",
                "실행 중단/오류 — 실측 통과 아님",
                {"error": type(exc).__name__, "detail": str(exc)},
            )
        result.at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        result.data["telemetry_device_id"] = identity
        result.data["boot_id"] = tap.latest.boot_id if tap.latest else None
        result.data["raw_samples"] = [asdict(sample) for sample in tap.window(60)]
        path = save([result], Path(args.out), args.device)
        print(f"{result.verdict}: {result.summary}\n저장: {path}")
        print(
            "기체별 계획 GUI에서 이 JSON을 증거로 가져오고 실제 설치 버전·환경·관찰을 추가하세요."
        )
    finally:
        if sock is not None:
            try:
                if started:
                    with contextlib.suppress(OSError):
                        sock.sendto(commander.emergency_stop().encode("utf-8"), peer)
            finally:
                sock.close()
        tap.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
