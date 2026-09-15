"""실기 측정 하네스 — 번호 하나로 WBS 실측 항목을 실행하고 결과를 파일로 남긴다.

    python tools/field_measure.py --device mechdog-02 --host 192.168.0.18
    python tools/field_measure.py --host 127.0.0.1 --dry-run   # 목업 연습

**왜 만드는가.** 지금까지 실측은 "대화로 지시 → 사람이 실행 → 값을 대화로 회수" 였다.
항목이 늘수록 그 비용이 커지므로, 이 도구가 **안내·구동·측정·저장**을 한 번에 한다.
사람은 안내를 따라 위치 표시·줄자·버튼만 하면 되고, 결과는
`--out` 디렉터리에 `field_YYYYMMDD_HHMMSS.json` + 같은 이름의 `.md` 로 남는다.

**자동으로 재는 것과 사람에게 묻는 것을 구분한다.**
텔레메트리(IMU·래치·service 플래그, 10Hz)로 확인 가능한 것 — 명령 두절 정지
지연, 선회율(yaw 적분), 보행 진폭, 명령→구동 지연, 서비스 왕복 — 은 도구가 잰다.
오도메트리가 없어 **직선거리만은 줄자 입력**이 필요하다. 텔레메트리가 안 오면
자동 항목은 실행하지 않는다 — 빈 창을 쏘고 "쟀다" 가 되는 일이 없도록.

⚠️ **실행 전**: 관제 런타임(`host.runtime`)이 떠 있으면 명령 seq 경합으로 이 도구의
패킷이 폐기된다 — 측정 중에는 런타임을 내린다. 텔레메트리 포트(:5101)도 이
도구가 잡으므로 겹치면 bind 가 즉시 실패한다(그것이 의도된 거동).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import statistics
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gait_calibrate import Acks, discard_reason, drive_window, pump  # noqa: E402

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402
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

    def __init__(self, port: int, device_id: str) -> None:
        self._device_id = device_id
        self.samples: deque[Sample] = deque(maxlen=600)  # 10Hz × 60초
        self.latest: Reading | None = None
        self.received = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # ⚠️ SO_REUSEADDR 없음이 의도다 — 포트를 누가 잡고 있으면 조용히
        # 표본 0 개가 되는 대신 bind 에서 바로 터진다 (PR #162).
        self._sock.bind(("0.0.0.0", port))
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
            try:
                msg = json.loads(raw)
                reading = Reading.of(msg)
            except Exception:  # noqa: BLE001 — 깨진 패킷은 세지 않고 버린다
                continue
            # 다른 발신자(목업·다른 개체)의 패킷이 섞이면 판정이 오염된다.
            # device_id 가 우리 것이 아니어도 이름표 device 필드가 맞으면 받는다.
            if (
                reading.device_id
                and self._device_id not in (reading.device_id, msg.get("device", ""))
                and msg.get("device") != self._device_id
            ):
                continue
            self.received += 1
            self.latest = reading
            self.samples.append(Sample(time.perf_counter(), reading))

    def close(self) -> None:
        self._stop.set()
        self._sock.close()

    def window(self, seconds: float) -> list[Sample]:
        """최근 `seconds` 동안의 표본."""
        cut = time.perf_counter() - seconds
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
            r = self.latest
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


def _ask_number(prompt: str, *, allow_blank: bool = True) -> float | None:
    while True:
        raw = _ask(prompt)
        if not raw:
            return None if allow_blank else _ask_number(prompt, allow_blank=allow_blank)
        try:
            value = float(raw)
        except ValueError:
            print("    숫자를 입력한다")
            continue
        if value < 0:
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


def m_link(ctx: Ctx) -> Result:
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
    ok = rate >= 8.0
    print(f"    수신 {rate:.1f}Hz · state={r.state if r else '—'} · batt={r.batt_v if r else '—'}V")
    return Result(
        "텔레메트리 링크", "전제", "pass" if ok else "fail",
        f"{rate:.1f}Hz 수신" + (f" · {r.state} · 래치={r.safety_latched}" if r else " · 무수신"),
        data,
    )


def m_cmd_timeout(ctx: Ctx) -> Result:
    """1-7. 명령 스트림이 끊기면 다리가 서는가 — 3.2.1 타임아웃 감시기."""
    if not _require_telemetry(ctx):
        return Result("명령 두절 정지", "3.2.1", "skipped", "텔레메트리 없음", {})
    tap = ctx.tap
    assert tap is not None
    print("  정지 상태로 가라앉힌 뒤 2초간 전진하고, 스트림을 끊는다.")
    print("  ⚠️ 로봇이 실제로 걷는다 — 주변을 비워 둔다.")
    if not _ask_yn("  준비됐나"):
        return Result("명령 두절 정지", "3.2.1", "skipped", "사용자 건너뜀", {})
    drive_window(ctx.sock, ctx.peer, ctx.commander, step_mm=60, angle_deg=0, seconds=2.0, settle_s=1.0)
    moving = tap.motion()
    if moving is not True:
        return Result(
            "명령 두절 정지", "3.2.1", "fail",
            f"구동 창에 움직임이 감지되지 않았다(motion={moving}) — 두절 시험 무의미",
            {"motion_during_drive": moving},
        )
    # 스트림을 완전히 끊는다 — STOP 도 보내지 않는다. 펌웨어가 스스로 멈춰야 한다.
    t0 = time.perf_counter()
    latched_at: float | None = None
    still_at: float | None = None
    while time.perf_counter() - t0 < 4.0:
        r = tap.latest
        if latched_at is None and r is not None and r.safety_latched is True:
            latched_at = time.perf_counter() - t0
        quiet = tap.window(STILL_HOLD_S)
        if (
            still_at is None
            and len(quiet) >= 2
            and all(
                s.at > t0 + 0.15 for s in quiet
            )
            and tap.motion(STILL_HOLD_S) is False
        ):
            still_at = time.perf_counter() - t0 - STILL_HOLD_S
        if latched_at is not None and still_at is not None:
            break
        time.sleep(0.05)
    # 래치 확인 — 조용히 한 번 더 움직여 보내 적용되지 않는지 본다.
    _, _, acks = drive_window(ctx.sock, ctx.peer, ctx.commander, step_mm=60, angle_deg=0, seconds=0.5, settle_s=0.0)
    refused = acks.applied == 0 and acks.total > 0
    data = {
        "stop_latency_s": round(still_at, 3) if still_at is not None else None,
        "latched_at_s": round(latched_at, 3) if latched_at is not None else None,
        "post_silence_move_applied": acks.applied,
        "post_silence_acks": acks.total,
    }
    ok = still_at is not None and still_at < 1.0 and refused
    print(f"    정지까지 {still_at if still_at is not None else '미검출':}초 · 래치 {latched_at}초 · 후속 MOVE 적용 {acks.applied}건")
    seen = _ask_yn("  로봇 다리가 실제로 멈췄나")
    return Result(
        "명령 두절 정지", "3.2.1", "pass" if (ok and seen) else "fail",
        f"두절→정지 {still_at if still_at is not None else '?':}s · 래치 {latched_at}s · 후속 거부={'됨' if refused else '안 됨'} · 육안={'정지' if seen else '계속 움직임'}",
        data,
    )


def m_drive(ctx: Ctx, mode: str) -> Result:
    """2-2. 전진/후진 직선거리 — 줄자 입력. 구동 창의 IMU 도 같이 남긴다."""
    if ctx.tap is not None and not _require_telemetry(ctx):
        pass  # 거리는 줄자가 재므로 텔레메트리 없어도 진행은 한다
    label = "전진" if mode == "forward" else "후진"
    step = 60 if mode == "forward" else -60
    trials: list[dict[str, Any]] = []
    for index in range(1, 4):
        print(f"\n  [{index}/3] {label} 3초 — 시작 위치에 표시를 하고 준비되면 엔터")
        if _ask("  (엔터=실행 / s=건너뜀) ").lower() in ("s", "skip"):
            continue
        mark_t = time.perf_counter()
        actual, packets, acks = drive_window(
            ctx.sock, ctx.peer, ctx.commander, step_mm=step, angle_deg=0,
            seconds=3.0, settle_s=1.0,
        )
        yaw_drift = None
        if ctx.tap is not None:
            near = [s for s in ctx.tap.samples if s.at >= mark_t]
            if len(near) >= 5:
                yaw_drift = round(near[-1].reading.yaw - near[0].reading.yaw, 1) if near[0].reading.yaw is not None and near[-1].reading.yaw is not None else None
        print(f"    송신 창 {actual:.2f}초 · 패킷 {packets} · {acks.describe()}")
        reason = discard_reason(acks, ctx.host)
        if reason is not None:
            print(f"    ⚠️ {reason} — 이 시행은 버린다")
            continue
        mm = _ask_number(f"  [{index}/3] 시작점→끝점 직선거리(mm)? 엔터=버림: ")
        if mm is None:
            continue
        drift = _ask_number("       방향 변화(도, 0=직진)? 모르면 엔터: ")
        trials.append({
            "mm": mm, "actual_s": round(actual, 3), "packets": packets,
            "deg": drift, "imu_yaw_drift": yaw_drift, "acks": acks.describe(),
        })
    rates = [t["mm"] / t["actual_s"] for t in trials]
    mean = statistics.fmean(rates) if rates else None
    data = {"trials": trials, "mm_per_s": round(mean, 1) if mean else None}
    verdict = "measured" if len(trials) >= 3 else ("fail" if not trials else "skipped")
    spread = statistics.stdev(rates) / mean if len(rates) >= 2 and mean else None
    return Result(
        f"{label} 이동량", "2.2.3", verdict,
        f"평균 {mean:.1f} mm/s (n={len(trials)})" + (f" · 퍼짐 {spread * 100:.0f}%" if spread is not None else "")
        if mean else "유효 시행 없음",
        data,
    )


def m_turn(ctx: Ctx, mode: str) -> Result:
    """2-2. 선회율 — IMU yaw 적분으로 자동 계측. 각도기 대조는 선택 입력.

    ⚠️ angle 양수 = 반시계 = 좌회전(PROTOCOL). turn_right 는 음수를 보낸다.
    선회는 제자리 회전이 아니라 호를 그리며 앞으로도 간다 — 공간을 넓게 둔다.
    """
    if not _require_telemetry(ctx):
        return Result("선회율 " + mode, "2.2.3", "skipped", "텔레메트리 없음", {})
    tap = ctx.tap
    assert tap is not None
    angle = {"turn_left": 20.0, "turn_right": -20.0, "reverse_turn": -20.0}[mode]
    step = -60.0 if mode == "reverse_turn" else 60.0
    label = {"turn_left": "좌선회", "turn_right": "우선회", "reverse_turn": "후진 선회"}[mode]
    trials: list[dict[str, Any]] = []
    for index in range(1, 4):
        print(f"\n  [{index}/3] {label} 3초 — 주변 공간을 확인하고 엔터 (s=건너뜀)")
        if _ask("  ").lower() in ("s", "skip"):
            continue
        yaw0 = tap.latest.yaw if tap.latest else None
        actual, packets, acks = drive_window(
            ctx.sock, ctx.peer, ctx.commander, step_mm=step, angle_deg=angle,
            seconds=3.0, settle_s=1.0,
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
        deg = _ask_number("       각도기 대조값(도)? 안 쟀으면 엔터: ")
        mm = _ask_number("       시작점→끝점 직선거리(mm)? 안 쟀으면 엔터: ")
        trials.append({
            "imu_delta_deg": round(delta, 1), "deg_per_s": round(rate, 1),
            "actual_s": round(actual, 3), "protractor_deg": deg, "chord_mm": mm,
            "acks": acks.describe(),
        })
    rates = [t["deg_per_s"] for t in trials]
    mean = statistics.fmean(rates) if rates else None
    verdict = "measured" if len(trials) >= 3 else ("fail" if not trials else "skipped")
    return Result(
        f"선회율 {label}", "2.2.3", verdict,
        f"평균 {mean:+.1f} °/s (n={len(trials)})" if mean else "유효 시행 없음",
        {"trials": trials, "deg_per_s": round(mean, 1) if mean else None},
    )


def m_gait_imu(ctx: Ctx) -> Result:
    """2-3. 트롯 보행 중 몸통 피치·롤 진폭 — 구동 창의 텔레메트리만 모은다."""
    if not _require_telemetry(ctx):
        return Result("보행 진폭", "2.2.3③", "skipped", "텔레메트리 없음", {})
    tap = ctx.tap
    assert tap is not None
    print("  전진 3초 동안 IMU 를 모은다. ⚠️ 실제로 걷는다.")
    if not _ask_yn("  준비됐나"):
        return Result("보행 진폭", "2.2.3③", "skipped", "사용자 건너뜀", {})
    t0 = time.perf_counter()
    drive_window(ctx.sock, ctx.peer, ctx.commander, step_mm=60, angle_deg=0, seconds=3.0, settle_s=1.0)
    samples = [s for s in tap.samples if t0 <= s.at <= time.perf_counter()]
    pitches = [s.reading.pitch for s in samples if s.reading.pitch is not None]
    rolls = [s.reading.roll for s in samples if s.reading.roll is not None]
    if len(pitches) < 10:
        return Result("보행 진폭", "2.2.3③", "fail", f"표본 부족({len(pitches)}개)", {})
    data = {
        "samples": len(pitches),
        "pitch_max": round(max(map(abs, pitches)), 2),
        "pitch_p95": round(sorted(map(abs, pitches))[int(len(pitches) * 0.95)], 2),
        "roll_max": round(max(map(abs, rolls)), 2),
        "roll_p95": round(sorted(map(abs, rolls))[int(len(rolls) * 0.95)], 2),
    }
    print(f"    pitch 최대 {data['pitch_max']}° · p95 {data['pitch_p95']}° / roll 최대 {data['roll_max']}° · p95 {data['roll_p95']}°")
    return Result("보행 진폭", "2.2.3③", "measured", f"pitch p95 {data['pitch_p95']}° · roll p95 {data['roll_p95']}°", data)


def m_teleop(ctx: Ctx) -> Result:
    """2-4. 방향 매핑 — 각 키를 한 번씩 보내고 사람이 방향을 확인한다."""
    keys = [
        ("W 전진", 60, 0), ("S 후진", -60, 0),
        ("A 좌선회", 0, 20), ("D 우선회", 0, -20),
    ]
    outcomes: dict[str, str] = {}
    for label, step, angle in keys:
        print(f"\n  [{label}] 1.5초 구동 — 로봇이 어느 쪽으로 가는지 본다")
        if _ask("  엔터=실행 / s=건너뜀: ").lower() in ("s", "skip"):
            outcomes[label] = "skipped"
            continue
        drive_window(ctx.sock, ctx.peer, ctx.commander, step_mm=step, angle_deg=angle, seconds=1.5, settle_s=0.8)
        ok = _ask_yn(f"  로봇이 [{label.split()[0]}] 방향으로 움직였나")
        outcomes[label] = "ok" if ok else "WRONG"
    bad = [k for k, v in outcomes.items() if v == "WRONG"]
    return Result(
        "텔레옵 방향", "2-4", "fail" if bad else "pass",
        "전부 일치" if not bad else f"뒤바뀜: {', '.join(bad)}",
        {"keys": outcomes},
    )


def m_service(ctx: Ctx) -> Result:
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
    print(f"    명령 진입 {dt_in:.2f}s({'ok' if ok_in else '실패'}) · 해제 {dt_out:.2f}s({'ok' if ok_out else '실패'})")
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
    ok = ok_in and ok_out and (gpio is None or gpio.get("enter_s") is not None)
    return Result(
        "서비스 왕복", "확장팩 1-1/1-3", "pass" if ok else "fail",
        f"명령 enter={data['cmd_enter_s']}s exit={data['cmd_exit_s']}s · GPIO5={gpio}",
        data,
    )


def m_e2e(ctx: Ctx) -> Result:
    """3-3. 명령→실제 구동 지연 — 멈춰 있는 상태에서 MOVE 를 시작해 IMU 가
    흔들리기 시작하는 시각을 잰다."""
    if not _require_telemetry(ctx):
        return Result("E2E 구동 지연", "3.3.3", "skipped", "텔레메트리 없음", {})
    tap = ctx.tap
    assert tap is not None
    print("  로봇이 멈춰 있는 동안 MOVE 를 시작하고 반응 시각을 잰다. ⚠️ 걷는다.")
    if not _ask_yn("  준비됐나"):
        return Result("E2E 구동 지연", "3.3.3", "skipped", "사용자 건너뜀", {})
    # 정지 유지 1초 — 멈춘 것을 확인한다
    pump(ctx.sock, ctx.peer, ctx.commander, 1.0, Acks())
    if tap.motion() is True:
        print("    아직 움직이고 있다 — 1초 더 정지")
        pump(ctx.sock, ctx.peer, ctx.commander, 1.0, Acks())
    ctx.commander.once("RESET_SAFE")
    ctx.commander.drive(60, 0)
    t0 = time.perf_counter()
    acks = Acks()
    latency = None
    end = t0 + 3.0
    while time.perf_counter() < end:
        now = system_clock_ms()
        for line in ctx.commander.tick(now):
            _send(ctx, line)
        drain = ctx.sock
        with contextlib.suppress(BlockingIOError, TimeoutError, OSError):
            while True:
                raw, _ = drain.recvfrom(4096)
                with contextlib.suppress(Exception):
                    acks.note(json.loads(raw))
        if tap.motion(MOTION_WINDOW_S) is True:
            latency = time.perf_counter() - t0
            break
        due = ctx.commander.next_due_ms
        if due is not None:
            time.sleep(max(0.0, (due - system_clock_ms()) / 1000))
    ctx.commander.halt()
    for line in ctx.commander.tick(system_clock_ms()):
        _send(ctx, line)
    if latency is None:
        return Result("E2E 구동 지연", "3.3.3", "fail", "3초 안에 움직임 미검출", {"acks": acks.describe()})
    print(f"    명령→IMU 반응 {latency * 1000:.0f}ms")
    return Result(
        "E2E 구동 지연", "3.3.3", "measured",
        f"송신→구동 {latency * 1000:.0f}ms (명령 채널+펌웨어+서보 응답 합계)",
        {"latency_ms": round(latency * 1000, 1), "acks": acks.describe()},
    )


MENU: list[tuple[str, str, Callable[[Ctx], Result]]] = [
    ("0", "텔레메트리 링크 기저선 (자동)", m_link),
    ("1", "명령 두절 → 다리 정지 지연 [3.2.1] (자동)", m_cmd_timeout),
    ("2", "전진 이동량 — forward_mm_per_sec [2.2.3] (줄자)", lambda c: m_drive(c, "forward")),
    ("3", "후진 이동량 — reverse_mm_per_sec [2.2.3] (줄자)", lambda c: m_drive(c, "reverse")),
    ("4", "좌선회율 — turn_deg_per_sec [2.2.3] (IMU 자동)", lambda c: m_turn(c, "turn_left")),
    ("5", "우선회율 대조 [2.2.3] (IMU 자동)", lambda c: m_turn(c, "turn_right")),
    ("6", "후진 선회 — reverse_turn [2.2.3] (IMU 자동)", lambda c: m_turn(c, "reverse_turn")),
    ("7", "보행 피치·롤 진폭 [2.2.3③] (자동)", m_gait_imu),
    ("8", "텔레옵 방향 W/S/A/D [2-4] (눈으로 y/n)", m_teleop),
    ("9", "서비스 모드 왕복 + GPIO5 버튼 [확장팩] (자동 감지)", m_service),
    ("10", "명령→구동 지연 E2E [3.3.3] (자동)", m_e2e),
]


def save(results: list[Result], out_dir: Path, device: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"field_{stamp}.json"
    payload = {
        "tool": "field_measure",
        "device": device,
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


def main() -> int:
    survive_encoding_errors()
    ap = argparse.ArgumentParser(description="실기 측정 하네스 — WBS 실측 항목")
    ap.add_argument("--device", default="mechdog-02", help="개체 id")
    ap.add_argument("--host", required=True, help="로봇 IP")
    ap.add_argument("--out", default=".", help="결과 저장 디렉터리")
    ap.add_argument("--trials", type=int, default=3)
    args = ap.parse_args()

    config = load_config(args.device)
    network = config["network"]
    peer = (args.host, int(network["cmd_port"]))
    telemetry_port = int(network["telemetry_port"])
    period_ms = int(1000 / float(network.get("cmd_rate_hz", 10)))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    commander = Commander(CommandEncoder(clock=system_clock_ms), period_ms=period_ms)
    _send_raw = lambda packet: sock.sendto(packet.encode("utf-8"), peer)  # noqa: E731
    _send_raw(commander.open_session())

    tap: TelemetryTap | None = None
    try:
        tap = TelemetryTap(telemetry_port, args.device)
    except OSError as exc:
        print(f"⚠️ 텔레메트리 포트 :{telemetry_port} bind 실패 — {exc}")
        print("   다른 런타임/도구가 잡고 있다. 자동 항목은 못 재고 수동 항목만 진행한다.")

    ctx = Ctx(sock=sock, peer=peer, commander=commander, tap=tap, device=args.device, host=args.host)
    print(f"개체 {args.device} · {args.host}:{peer[1]} · 텔레메트리 :{telemetry_port}")
    print("⚠️ 관제 런타임이 떠 있으면 명령이 경합한다 — 측정 중에는 내려 둔다.\n")

    try:
        while True:
            print("─" * 60)
            for key, label, _ in MENU:
                print(f"  [{key:>2}] {label}")
            print("   [a] 전부 순서대로   [s] 저장하고 종료   [q] 저장 없이 종료")
            pick = _ask("선택: ").lower()
            if pick == "q":
                return 0
            if pick in ("s", ""):
                break
            todo = [fn for _k, _l, fn in MENU] if pick == "a" else None
            if todo is None:
                found = next((fn for k, _l, fn in MENU if k == pick), None)
                if found is None:
                    print("  목록에 없는 번호다")
                    continue
                todo = [found]
            for fn in todo:
                name = next(lbl for k, lbl, f2 in MENU if f2 is fn or (hasattr(f2, "__wrapped__") and f2.__wrapped__ is fn))
                print(f"\n▶ {name}")
                try:
                    result = fn(ctx)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001 — 한 항목 실패가 전체를 죽이지 않는다
                    result = Result(name, "—", "fail", f"도구 오류: {exc}", {"error": repr(exc)})
                result.at = time.strftime("%H:%M:%S")
                ctx.results.append(result)
                print(f"  → {result.verdict}: {result.summary}")
    except KeyboardInterrupt:
        print("\n중단됐다")
    finally:
        _send_raw(commander.emergency_stop())
        sock.close()
        if tap is not None:
            tap.close()

    if ctx.results:
        path = save(ctx.results, Path(args.out), args.device)
        print(f"\n저장: {path}")
        print(f"요약: {path.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
