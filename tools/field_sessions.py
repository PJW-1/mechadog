"""Case-specific measurement execution. GUI worker; no hardware activity on import."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import math
import socket
import statistics
import threading
import time
from dataclasses import asdict
from pathlib import Path

from tools import field_measure as fm


class MeasurementCancelledError(Exception):
    """Operator cancelled the current measurement."""


ROUTES = {
    "HW-001": ("identity",),
    "HW-002": ("imu",),
    "HW-003": ("battery",),
    "HW-004": ("service",),
    "HW-006": ("link",),
    "HW-007": ("posture",),
    "HW-008": ("forward", "reverse"),
    "HW-009": ("left", "right", "reverse_turn"),
    "HW-011": ("timeout",),
    "TC-F-001": ("directions",),
    "TC-F-002": ("timeout",),
    "TC-F-003": ("link",),
    "TC-N-008": ("timeout",),
    "TC-N-011": ("battery",),
}
PASSIVE = {"identity", "imu", "battery", "link"}
LABELS = {
    "identity": "기체 신원 수신 확인",
    "imu": "정지 IMU 30초 수집·통계",
    "battery": "전압 수집·멀티미터 비교",
    "link": "통신 30초 수신·누락·간격 분석",
    "service": "SERVICE·GPIO 왕복 시험",
    "posture": "중립·±10도 자세 시험",
    "forward": "전진 3회·거리 입력",
    "reverse": "후진 3회·거리 입력",
    "left": "좌선회 3회·외부 각도 입력",
    "right": "우선회 3회·외부 각도 입력",
    "reverse_turn": "후진 선회 3회·외부 각도 입력",
    "directions": "4방향 구동 확인",
    "timeout": "명령 송신 중단·래치 관측 (물리 정지 별도 확인)",
}


def route(case: dict) -> tuple[str, ...]:
    if case["phase"] == "제외" or case["phase"].startswith("6"):
        return ()
    return ROUTES.get(case["id"], ("guided",))


def movement_cases(cases: list[dict]) -> list[dict]:
    """Floor-drive cases with an automatic route, in catalog order."""
    return [
        case
        for case in cases
        if case["phase"].startswith("2") and route(case) not in ((), ("guided",))
    ]


def describe(case: dict) -> str:
    actions = route(case)
    if not actions:
        return "실행 제외/선행 조건 대기"
    if actions == ("guided",):
        return "수동 계측 진행: 단계별 안내·관측 입력·증거 첨부·자동 저장 (자동 구동 없음)"
    return " → ".join(LABELS[a] for a in actions)


def metrics(samples: list[fm.Sample]) -> dict:
    """Host receive intervals, not one-way latency or physical actuation time."""
    if len(samples) < 2:
        raise ValueError("유효 표본 부족 — 측정 결과를 계산할 수 없습니다.")
    intervals = [b.at - a.at for a, b in zip(samples, samples[1:], strict=False)]
    gaps = [
        max(0, b.reading.seq - a.reading.seq - 1)
        for a, b in zip(samples, samples[1:], strict=False)
        if a.reading.boot_id == b.reading.boot_id
    ]
    result = {
        "samples": len(samples),
        "elapsed_s": samples[-1].at - samples[0].at,
        "seq_missing_same_boot": sum(gaps),
        "boot_ids": sorted({s.reading.boot_id for s in samples}),
        "max_receive_gap_ms": max(intervals) * 1000,
        "interval_median_ms": statistics.median(intervals) * 1000,
    }
    result["rate_hz"] = (
        (len(samples) - 1) / result["elapsed_s"] if result["elapsed_s"] > 0 else None
    )
    for axis in ("pitch", "roll", "yaw", "batt_v", "dist_cm"):
        values = [getattr(s.reading, axis) for s in samples if getattr(s.reading, axis) is not None]
        if values:
            result[axis] = {
                "n": len(values),
                "mean": statistics.fmean(values),
                "min": min(values),
                "max": max(values),
                "stddev": statistics.pstdev(values),
            }
    result["note"] = (
        "Host"
        + " 수신간격. 단방향 지연·물리 정지시간·보행 진폭이 아님. yaw 통계는 0/360 경계 해석 주의."
    )
    return result


class CommandGuard:
    """Cancel active pump loops before they send another MOVE."""

    def __init__(self, commander, cancel: threading.Event):
        self.commander = commander
        self.cancel = cancel

    def __getattr__(self, name):
        return getattr(self.commander, name)

    def tick(self, now):
        if self.cancel.is_set():
            raise MeasurementCancelledError("측정 중단")
        return self.commander.tick(now)


class MeasurementSession:
    """Owns sockets for one selected case; injected prompts run on the GUI thread."""

    def __init__(self, device: str, host: str, ask, notify, cancel: threading.Event):
        self.device, self.host = device, host
        self.ask, self.notify, self.cancel = ask, notify, cancel

    def check(self):
        if self.cancel.is_set():
            raise MeasurementCancelledError("사용자 중단")

    def prompt(self, text: str) -> str:
        self.check()
        result = self.ask(text)
        self.check()
        return result

    def capture(
        self, tap, seconds: float = 30
    ) -> list[fm.Sample]:  # pragma: no cover - 실기 측정용
        start = time.perf_counter()
        end = start + seconds
        last_second = -1
        while time.perf_counter() < end:
            self.check()
            remain = max(0, math.ceil(end - time.perf_counter()))
            if remain != last_second:
                self.notify(f"수집 중 · 남은 {remain}초 · 현재 {tap.received}개 수신")
                last_second = remain
            self.cancel.wait(min(0.1, max(0, end - time.perf_counter())))
        return [s for s in tap.window(seconds + 1) if s.at >= start]

    def passive(self, action, ctx) -> fm.Result:  # pragma: no cover - 실기 측정용
        samples = self.capture(ctx.tap, 3 if action == "identity" else 30)
        values = metrics(samples)
        if action == "identity":
            values["received_device_ids"] = sorted({s.reading.device_id for s in samples})
        if action == "battery":
            value = fm._ask_number("같은 시점의 멀티미터 전압(V)? 안 재었으면 빈칸: ")
            values["multimeter_v"] = value
            values["voltage_error_v"] = (
                values["batt_v"]["mean"] - value if value is not None else None
            )
        values["raw_samples"] = [asdict(s) for s in samples]
        return fm.Result(
            LABELS[action],
            "선택 항목",
            "measured",
            f"{len(samples)}개 · {values['rate_hz']:.2f}Hz · 최대 수신간격 {values['max_receive_gap_ms']:.1f}ms",
            values,
        )

    def timeout(self, ctx) -> fm.Result:  # pragma: no cover - 실기 측정용
        if (
            self.prompt(
                "전진 2초 뒤 송신을 1.2초 끊고 ESTOP을 보냅니다. 바닥/중지 수단 준비 후 '준비완료' 입력: "
            )
            != "준비완료"
        ):
            raise MeasurementCancelledError("구동 준비 취소")
        ctx.commander.once("RESET_SAFE")
        ctx.commander.halt()
        fm.pump(ctx.sock, ctx.peer, ctx.commander, 0.5, fm.Acks())
        ctx.commander.drive(60, 0)
        acks = fm.Acks()
        fm.pump(ctx.sock, ctx.peer, ctx.commander, 2.0, acks)
        reason = fm.discard_reason(acks, ctx.host)
        if reason:
            raise ValueError(f"구동 ACK 불충분: {reason}")
        cutoff = time.perf_counter()
        # Do not call drive_window: its terminal HALT invalidates command-loss testing.
        self.notify("명령 송신 중단 중 — HALT/RESET_SAFE를 보내지 않음")
        samples = self.capture(ctx.tap, 1.2)
        ctx.sock.sendto(ctx.commander.emergency_stop().encode(), ctx.peer)
        latched = next((s for s in samples if s.reading.safety_latched), None)
        observed = self.prompt("실제로 다리가 멈췄는지, 영상 계측값/미측정 여부를 입력하세요: ")
        return fm.Result(
            "명령 두절",
            "3.2.1",
            "measured",
            "송신중단 후 래치 관측; 300ms/물리 정지 합격은 외부 계측 필요",
            {
                "cutoff_host_monotonic": cutoff,
                "latch_observed_after_ms": (latched.at - cutoff) * 1000 if latched else None,
                "physical_observation": observed,
                "acks": acks.describe(),
                "raw_samples": [asdict(s) for s in samples],
            },
        )

    def posture(self, ctx) -> fm.Result:  # pragma: no cover - 실기 측정용
        stages = []
        for angle in (0, -10, 0, 10, 0):
            if (
                self.prompt(
                    f"pitch {angle:+}° 명령을 보낼 준비가 되었으면 '준비완료' 입력 (다리/자세 실제 변경): "
                )
                != "준비완료"
            ):
                raise MeasurementCancelledError("자세 단계 취소")
            self.check()
            ctx.commander.once("RESET_SAFE")
            ctx.commander.halt()
            fm.pump(ctx.sock, ctx.peer, ctx.commander, 0.5, fm.Acks())
            ctx.commander.once("POSE", pitch=angle, roll=0, height=0, dur=1500)
            acks = fm.Acks()
            start = time.perf_counter()
            fm.pump(ctx.sock, ctx.peer, ctx.commander, 4.0, acks)
            samples = [s for s in ctx.tap.window(2) if s.at >= start + 2]
            actual = fm._ask_number(
                "각도기로 잰 실제 pitch(도, 부호 포함)? 미측정은 빈칸: ", signed=True
            )
            stages.append(
                {
                    "requested_pitch": angle,
                    "external_pitch": actual,
                    "statistics": metrics(samples),
                    "acks": acks.describe(),
                    "raw_samples": [asdict(s) for s in samples],
                }
            )
        return fm.Result(
            "자세 단계",
            "2.1.4",
            "measured",
            "5단계 명령/외부 각도/IMU 대조. ACK 집계는 실제 자세 성공 판정이 아님.",
            {"stages": stages},
        )

    def run(
        self, case: dict, approved: bool = False
    ) -> list[fm.Result]:  # pragma: no cover - 실기 측정용
        actions = route(case)
        if not actions or actions == ("guided",):
            raise ValueError("이 항목은 전용 자동 구동 경로가 없습니다.")
        active = any(a not in PASSIVE for a in actions)
        if active and not approved:
            raise ValueError("구동/상태 변경 준비 확인이 필요합니다.")
        ipaddress.IPv4Address(self.host)
        config = fm.load_config(self.device)
        net = config["network"]
        identity = config["telemetry_device_id"]
        tap = fm.TelemetryTap(int(net["telemetry_port"]), identity, self.host)
        tap.cancel_check = self.check
        sock = None
        started = False
        results = []
        real = fm.Commander(
            fm.CommandEncoder(clock=fm.system_clock_ms),
            period_ms=int(1000 / net.get("cmd_rate_hz", 10)),
        )
        peer = (self.host, int(net["cmd_port"]))
        token = fm.ASK_HANDLER.set(self.prompt)
        try:
            if not tap.wait_flag(lambda _r: True, timeout_s=3)[0]:
                raise ValueError("해당 기체의 유효한 새 표본이 없습니다. 명령을 보내지 않았습니다.")
            self.check()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            ctx = fm.Ctx(sock, peer, CommandGuard(real, self.cancel), tap, self.device, self.host)
            if active:
                sock.sendto(real.open_session().encode(), peer)
                started = True
            for action in actions:
                self.check()
                self.notify("실행: " + LABELS[action])
                if action in PASSIVE:
                    result = self.passive(action, ctx)
                elif action in {"forward", "reverse"}:
                    result = fm.m_drive(ctx, action)
                elif action in {"left", "right", "reverse_turn"}:
                    result = fm.m_turn(
                        ctx, {"left": "turn_left", "right": "turn_right"}.get(action, action)
                    )
                elif action == "posture":
                    result = self.posture(ctx)
                elif action == "timeout":
                    result = self.timeout(ctx)
                elif action == "service":
                    result = fm.m_service(ctx)
                else:
                    result = fm.m_teleop(ctx)
                result.data.update(
                    telemetry_device_id=identity, boot_id=tap.latest.boot_id if tap.latest else None
                )
                result.data.setdefault("raw_samples", [asdict(s) for s in tap.window(60)])
                result.at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                results.append(result)
                self.notify({"result": asdict(result)})  # persist each completed step immediately
            return results
        finally:
            if sock is not None:
                if started:
                    with contextlib.suppress(OSError):
                        sock.sendto(real.emergency_stop().encode(), peer)
                sock.close()
            tap.close()
            fm.ASK_HANDLER.reset(token)


def write_session(path: Path, payload: dict) -> None:
    """Atomic progress checkpoint, including interrupted sessions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
