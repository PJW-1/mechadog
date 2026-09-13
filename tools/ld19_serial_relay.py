"""LD19 시리얼 → UDP 중계 — 중계 노드의 PC 판 대역 (WBS 6.1 · Phase 2).

`firmware_lidar_relay` 와 같은 일을 한다. LD19 를 ESP32 에 물릴 배선이
없을 때(또는 파서를 실물 스트림으로 교차검증할 때) USB-UART 어댑터로
PC 에 직결해 쓴다.

    python tools/ld19_serial_relay.py --serial COM10
    python tools/ld19_serial_relay.py --serial COM10 --host 127.0.0.1

파서·스캔 조립 로직은 `firmware_lidar_relay/src/ld19.cpp` 의 이식이다 —
같은 입력에 같은 결과를 내야 한다. 전선 형식은 `encode_scan` 을 쓰므로
규약(`docs/PROTOCOL_LIDAR.md`)과의 정합은 그쪽이 보장한다.

⚠️ 이 도구는 디버깅·대기용이다. 최종 배치는 ESP32 중계 노드다 — 이걸
   돌리는 동안은 라이다가 PC 에 묶여 이동 실측이 불가하다.
"""

from __future__ import annotations

import argparse
import contextlib
import secrets
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.console import survive_encoding_errors
from host.common.lidar_link import encode_scan
from host.common.protocol import system_clock_ms

# ── 프레임 상수 (firmware_lidar_relay/src/ld19.h 와 동일) ───────────
FRAME_BYTES = 47
HEADER = 0x54
VERLEN = 0x2C  # 타입 1, 12개 측정점
POINTS_PER_FRAME = 12
CDEG_PER_REV = 36000
MAX_SCAN_POINTS = 1200

# 한 데이터그램 점 수 상한 — 펌웨어 kChunkPoints 와 같다.
# 루프백 MTU 는 커도 같은 경로를 타게 하려고 맞춘다.
CHUNK_POINTS = 72


def crc8(data: bytes) -> int:
    """poly 0x4D, init 0, no reflect, xorout 0 — ld19.cpp 와 같은 절차."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x4D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class Ld19Parser:
    """바이트 스트림 → 검증된 프레임. C++ Ld19Parser 의 이식."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.frame: dict | None = None
        self.frames_ok = 0
        self.crc_failures = 0
        self.bad_verlen = 0
        self.resyncs = 0

    def feed(self, byte: int) -> dict | None:
        if not self.buf and byte != HEADER:
            return None
        self.buf.append(byte)
        if len(self.buf) < FRAME_BYTES:
            return None
        if self.buf[1] != VERLEN:
            self.bad_verlen += 1
            self._resync()
            return None
        if crc8(bytes(self.buf[: FRAME_BYTES - 1])) != self.buf[FRAME_BYTES - 1]:
            self.crc_failures += 1
            self._resync()
            return None
        b = self.buf
        self.frame = {
            "speed_dps": b[2] | (b[3] << 8),
            "start_cdeg": b[4] | (b[5] << 8),
            "points": [
                (b[6 + i * 3] | (b[7 + i * 3] << 8), b[8 + i * 3]) for i in range(POINTS_PER_FRAME)
            ],
            "end_cdeg": b[42] | (b[43] << 8),
            "stamp_ms": b[44] | (b[45] << 8),
        }
        self.frames_ok += 1
        self.buf = bytearray()
        return self.frame

    def _resync(self) -> None:
        self.resyncs += 1
        idx = self.buf.find(HEADER, 1)
        self.buf = self.buf[idx:] if idx > 0 else bytearray()


class ScanAssembler:
    """프레임 → 한 바퀴 점 목록. C++ ScanAssembler 의 이식."""

    def __init__(self) -> None:
        self.points: list[tuple[int, int]] = []  # (angle_cdeg, dist_mm)
        self.last_start: int | None = None
        self.scans_completed = 0
        self.points_dropped = 0

    def add_frame(self, f: dict) -> list[tuple[int, int]] | None:
        """한 바퀴가 완성되면 점 목록을 돌려주고 버퍼를 비운다."""
        emitted = None
        if self.last_start is not None and f["start_cdeg"] < self.last_start:
            emitted, self.points = self.points[:MAX_SCAN_POINTS], []
            self.scans_completed += 1
        self.last_start = f["start_cdeg"]

        end = f["end_cdeg"]
        if end < f["start_cdeg"]:
            end += CDEG_PER_REV  # 이 프레임이 0° 를 걸친다
        span = end - f["start_cdeg"]

        for i, (dist_mm, _intensity) in enumerate(f["points"]):
            if len(self.points) >= MAX_SCAN_POINTS:
                self.points_dropped += 1
                continue
            angle_cdeg = (f["start_cdeg"] + span * i // 11) % CDEG_PER_REV
            self.points.append((angle_cdeg, dist_mm))
        return emitted


def run(args: argparse.Namespace) -> int:
    import serial  # pyserial — 없으면 여기서야 죽는다

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    if args.fwd_host:
        # ── 생 바이트 전달 모드 — 파싱 없이 ESP32 udp_feed 스케치로 흘린다.
        # LD19→ESP32 물리 배선 없이 노드 처리 체인을 실물 바이트로 시험한다.
        feed = (args.fwd_host, args.fwd_port)
        pkts = bytes_sent = 0
        started = time.monotonic()
        print(
            f"[ld19-relay] {args.serial} @{args.baud} →UDP {feed[0]}:{feed[1]} (raw feed)",
            flush=True,
        )
        try:
            with serial.Serial(args.serial, args.baud, timeout=1) as port:
                while True:
                    data = port.read(1024)
                    if data:
                        with contextlib.suppress(OSError):
                            sock.sendto(data, feed)
                        pkts += 1
                        bytes_sent += len(data)
                    if time.monotonic() - started >= 5.0:
                        started = time.monotonic()
                        print(f"[ld19-relay] fwd pkts={pkts} bytes={bytes_sent}", flush=True)
        except KeyboardInterrupt:
            print(f"[ld19-relay] 종료 — {bytes_sent}B 전달", flush=True)
        finally:
            sock.close()
        return 0

    peer = (args.host, args.port)
    boot_id = secrets.token_hex(8)
    parser, assembler = Ld19Parser(), ScanAssembler()
    seq = 0
    scans_sent = 0

    def send(points: list[tuple[int, int]]) -> None:
        nonlocal seq, scans_sent
        for off in range(0, len(points), CHUNK_POINTS):
            chunk = points[off : off + CHUNK_POINTS]
            seq += 1
            wire = [[a / 100.0, d] for a, d in chunk]
            line = encode_scan(
                seq=seq,
                ts_ms=system_clock_ms(),
                device_id=args.device,
                boot_id=boot_id,
                points_wire=wire,
            )
            with contextlib.suppress(OSError):
                sock.sendto(line.encode("utf-8"), peer)
                scans_sent += 1

    print(
        f"[ld19-relay] {args.serial} @{args.baud} → {peer[0]}:{peer[1]} · device={args.device} boot={boot_id}",
        flush=True,
    )
    stats_at = time.monotonic()
    try:
        with serial.Serial(args.serial, args.baud, timeout=1) as port:
            while True:
                data = port.read(4096)
                for byte in data:
                    frame = parser.feed(byte)
                    if frame is None:
                        continue
                    done = assembler.add_frame(frame)
                    if done:
                        send(done)
                    while len(assembler.points) >= CHUNK_POINTS:
                        send(assembler.points[:CHUNK_POINTS])
                        del assembler.points[:CHUNK_POINTS]
                now = time.monotonic()
                if now - stats_at >= 5.0:
                    stats_at = now
                    print(
                        f"[ld19-relay] frames={parser.frames_ok} crc_fail={parser.crc_failures} "
                        f"bad_verlen={parser.bad_verlen} resync={parser.resyncs} "
                        f"scans={assembler.scans_completed} sent={scans_sent} "
                        f"drop_pts={assembler.points_dropped}",
                        flush=True,
                    )
    except KeyboardInterrupt:
        print(f"[ld19-relay] 종료 — {scans_sent} 데이터그램 송신")
    finally:
        sock.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LD19 시리얼 → SCAN UDP 중계 (ESP32 노드의 PC 대역)")
    p.add_argument("--serial", required=True, help="COM 포트 (예: COM10)")
    p.add_argument("--baud", type=int, default=230400)
    p.add_argument("--host", default="127.0.0.1", help="SCAN 수신 호스트")
    p.add_argument("--port", type=int, default=5201, help="SCAN 수신 포트 (lidar.scan_port)")
    p.add_argument("--device", default="lidar-pc-relay", help="device_id")
    p.add_argument(
        "--fwd-host",
        default=None,
        help="설정 시 파싱 없이 생 바이트를 이 IP 로 UDP 전달 (udp_feed 스케치)",
    )
    p.add_argument("--fwd-port", type=int, default=5202)
    return p


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
