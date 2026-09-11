"""WBS 4.1.4 텔레메트리를 수신하고 부팅별 관측 주기를 확인한다.

    python tools/telemetry_probe.py --device mechdog-a --duration 30
    python tools/telemetry_probe.py --output measurements/telemetry.jsonl

명령을 보내지 않는 수신 전용 도구다. 다른 수신 프로그램이 5101 포트를 쓰면
먼저 종료하거나 --port를 바꿔야 한다. 출력 파일은 새 파일만 생성한다.
주기 판정은 수신한 첫~마지막 정상 패킷의 평균이다. 센서 정확도·전체 구간의
연속 송신·재부팅 동작 완료를 대신 검증하지 않으며 침묵/누락도 별도로 표시한다.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import TelemetryDecoder  # noqa: E402

RECV_BYTES = 65535
MIN_RATE_SAMPLES = 90
MIN_RATE_SPAN_S = 10.0
RATE_MIN_HZ = 9.0
RATE_MAX_HZ = 11.0
# Probe-only continuity limit: five expected 10 Hz intervals. This does not
# modify the robot's watchdog or host runtime safety policy.
MAX_CAPTURE_GAP_S = 0.5


@dataclass
class SessionStats:
    device_id: str
    boot_id: str
    first_seq: int
    last_seq: int
    first_received_s: float
    last_received_s: float
    new_boot_observed: bool
    accepted: int = 1
    sequence_gaps: int = 0
    max_interval_s: float = 0.0

    def add(self, seq: int, received_s: float) -> None:
        self.sequence_gaps += seq - self.last_seq - 1
        self.max_interval_s = max(self.max_interval_s, received_s - self.last_received_s)
        self.last_seq = seq
        self.last_received_s = received_s
        self.accepted += 1

    def summary(self, ended_s: float) -> dict[str, Any]:
        span = self.last_received_s - self.first_received_s
        hz = (self.accepted - 1) / span if span > 0 else None
        enough = self.accepted >= MIN_RATE_SAMPLES and span >= MIN_RATE_SPAN_S
        status = "unverified"
        if enough and hz is not None:
            status = "pass" if RATE_MIN_HZ <= hz <= RATE_MAX_HZ else "fail"
        return {
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "accepted": self.accepted,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            # Gaps include discarded/out-of-order records; not proven wire loss.
            "sequence_gaps": self.sequence_gaps,
            "first_received_monotonic_s": self.first_received_s,
            "last_received_monotonic_s": self.last_received_s,
            "observed_span_s": span,
            "receive_hz": hz,
            "max_interval_s": self.max_interval_s,
            "end_silence_s": max(0.0, ended_s - self.last_received_s),
            "rate_status": status,
            "first_seq_is_one": self.first_seq == 1,
            "new_boot_observed": self.new_boot_observed,
            "new_boot_seq1": (
                "observed"
                if self.new_boot_observed and self.first_seq == 1
                else "not_observed"
                if self.new_boot_observed
                else "not_applicable"
            ),
        }


def _reason_summary(reason: str) -> str:
    """Decoder 사유에 붙은 실제 값·미지 문자열을 로그에 복사하지 않는다."""
    if reason.startswith("알 수 없는 상태"):
        return "unknown_state"
    if reason.startswith("tipped 인데"):
        return "tipped_state_conflict"
    return reason.partition(":")[0]


def _known_payload(msg: dict[str, Any]) -> dict[str, Any]:
    """승인된 텔레메트리 필드만 보관한다. 임의의 추가 필드는 복사하지 않는다."""
    payload = {
        key: msg[key]
        for key in (
            "seq",
            "ts",
            "device_id",
            "boot_id",
            "state",
            "dist_cm",
            "batt_v",
            "last_cmd_age_ms",
        )
    }
    payload["imu"] = {key: msg["imu"][key] for key in ("pitch", "roll", "yaw")}
    payload["flags"] = {
        key: msg["flags"][key]
        for key in ("lowbatt", "tipped", "link_ok", "obstacle")
        if key in msg["flags"]
    }
    if isinstance(msg.get("safety_latched"), bool):
        payload["safety_latched"] = msg["safety_latched"]
    return payload


class ProbeStats:
    """소켓과 무관한 관측 누적기. (device_id, boot_id)별로 분리한다."""

    def __init__(self, device_id: str | None = None) -> None:
        self.device_id = device_id
        self.decoder = TelemetryDecoder()
        self.sessions: dict[tuple[str, str], SessionStats] = {}
        self.devices_seen: set[str] = set()
        self.last_device_received: dict[str, float] = {}
        self.max_device_interval: dict[str, float] = {}
        self.datagrams = 0
        self.accepted = 0
        self.filtered = 0
        self.discard_reasons: Counter[str] = Counter()

    def _discard(self, reason: str, received_s: float, byte_count: int) -> dict[str, Any]:
        safe_reason = _reason_summary(reason)
        self.discard_reasons[safe_reason] += 1
        return {
            "kind": "discarded",
            "received_monotonic_s": received_s,
            "reason": safe_reason,
            "datagram_bytes": byte_count,
        }

    def ingest(self, raw: bytes, received_s: float) -> dict[str, Any]:
        self.datagrams += 1
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            return self._discard("파싱 실패", received_s, len(raw))
        if not isinstance(msg, dict):
            return self._discard("최상위가 JSON 객체가 아님", received_s, len(raw))
        if (
            self.device_id is not None
            and isinstance(msg.get("device_id"), str)
            and msg["device_id"] != self.device_id
        ):
            self.filtered += 1
            return {"kind": "filtered", "received_monotonic_s": received_s}
        result = self.decoder.validate(msg)
        if not result.accepted or result.message is None:
            return self._discard(result.reason, received_s, len(raw))
        msg = result.message
        key = (msg["device_id"], msg["boot_id"])
        if key in self.sessions:
            self.sessions[key].add(msg["seq"], received_s)
        else:
            self.sessions[key] = SessionStats(
                device_id=key[0],
                boot_id=key[1],
                first_seq=msg["seq"],
                last_seq=msg["seq"],
                first_received_s=received_s,
                last_received_s=received_s,
                new_boot_observed=key[0] in self.devices_seen,
            )
            self.devices_seen.add(key[0])
        previous = self.last_device_received.get(key[0])
        if previous is not None:
            self.max_device_interval[key[0]] = max(
                self.max_device_interval.get(key[0], 0.0), received_s - previous
            )
        self.last_device_received[key[0]] = received_s
        self.accepted += 1
        return {
            "kind": "telemetry",
            "received_monotonic_s": received_s,
            "telemetry": _known_payload(msg),
        }

    def summary(self, started_s: float, ended_s: float, *, interrupted: bool = False) -> dict:
        sessions = [session.summary(ended_s) for session in self.sessions.values()]
        statuses = {session["rate_status"] for session in sessions}
        if not self.accepted or "fail" in statuses:
            status = "fail"
        elif interrupted or "unverified" in statuses:
            status = "unverified"
        else:
            status = "pass"
        device_windows = []
        for device_id, last in self.last_device_received.items():
            first = min(
                session.first_received_s
                for session in self.sessions.values()
                if session.device_id == device_id
            )
            start_silence = max(0.0, first - started_s)
            end_silence = max(0.0, ended_s - last)
            max_interval = self.max_device_interval.get(device_id, 0.0)
            device_windows.append(
                {
                    "device_id": device_id,
                    "start_silence_s": start_silence,
                    "end_silence_s": end_silence,
                    "max_receive_interval_s": max_interval,
                    "continuity_observed": max(start_silence, end_silence, max_interval)
                    <= MAX_CAPTURE_GAP_S,
                }
            )
        continuous = bool(device_windows) and all(
            window["continuity_observed"] for window in device_windows
        )
        capture_status = "fail" if status == "fail" or not continuous else status
        return {
            "kind": "summary",
            "device_filter": self.device_id,
            "capture_elapsed_s": max(0.0, ended_s - started_s),
            "interrupted": interrupted,
            "datagrams": self.datagrams,
            "accepted": self.accepted,
            "filtered": self.filtered,
            "discarded": sum(self.discard_reasons.values()),
            "discard_reasons": dict(self.discard_reasons),
            "rate_check": status,
            "rate_check_scope": "first-to-last accepted packet in each boot session",
            "capture_check": capture_status,
            "success": capture_status == "pass",
            "maximum_capture_gap_s": MAX_CAPTURE_GAP_S,
            "device_windows": device_windows,
            "minimum_samples": MIN_RATE_SAMPLES,
            "minimum_span_s": MIN_RATE_SPAN_S,
            "sessions": sessions,
        }


def _write_record(output: TextIO | None, record: dict) -> None:
    if output is not None:
        # The Python decoder accepts escaped surrogate code points in identity
        # strings. Escape them on disk as JSON too, avoiding UTF-8 write errors.
        output.write(json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n")


def capture(
    sock: socket.socket,
    stats: ProbeStats,
    duration_s: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    output: TextIO | None = None,
) -> dict:
    """이미 열린 수신 소켓을 사용한다. send/sendto를 호출하지 않는다."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("duration은 0보다 큰 유한한 초여야 합니다")
    started = clock()
    deadline = started + duration_s
    interrupted = False
    try:
        while (remaining := deadline - clock()) > 0:
            sock.settimeout(remaining)
            try:
                raw, _ = sock.recvfrom(RECV_BYTES)
            except TimeoutError:
                continue
            received = clock()
            if received > deadline:
                break
            _write_record(output, stats.ingest(raw, received))
    except KeyboardInterrupt:
        interrupted = True
    summary = stats.summary(started, clock(), interrupted=interrupted)
    _write_record(output, summary)
    return summary


def exit_code(summary: dict) -> int:
    """0=주기·관측 연속성 통과, 1=수신/주기/연속성 실패, 2=표본·기간 미검증."""
    return {"pass": 0, "fail": 1, "unverified": 2}[summary["capture_check"]]


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("숫자 초를 입력하세요") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 유한한 초를 입력하세요")
    return seconds


def main(argv: Sequence[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0", help="로컬 수신 주소")
    parser.add_argument("--port", type=int, default=5101, help="로컬 UDP 포트 (기본 5101)")
    parser.add_argument("--duration", type=_positive_seconds, default=30.0, help="수신 시간(초)")
    parser.add_argument("--device", help="수신할 device_id 하나만 선택")
    parser.add_argument("--output", type=Path, help="새 JSONL 파일 경로 (기존 파일 덮어쓰기 금지)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port는 1~65535 범위여야 합니다")
    if args.device == "":
        parser.error("device_id는 비어 있을 수 없습니다")
    try:
        with ExitStack() as stack:
            output = None
            if args.output is not None:
                output = stack.enter_context(args.output.open("x", encoding="utf-8"))
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((args.bind, args.port))
            print(f"수신 전용: {args.bind}:{args.port}, {args.duration:g}초")
            summary = capture(sock, ProbeStats(args.device), args.duration, output=output)
    except OSError as exc:
        print(f"수신/파일 오류: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    print("주기 판정은 관측 구간 평균입니다. 센서 정확도·연속 송신·재부팅 검증은 별도입니다.")
    return exit_code(summary)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
