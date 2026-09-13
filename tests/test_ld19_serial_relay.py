"""LD19 시리얼 중계 도구 시험 — 펌웨어 단위시험(test_lidar_relay.cpp)의 파이썬 대역.

파서·스캔 조립기는 ld19.cpp 의 이식이므로 같은 골든 프레임·같은 케이스로
검증한다. 시리얼·소켓은 가짜로 주입하며 실제 장치 포트는 열지 않는다.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from host.common.lidar_link import ScanDecoder, scan_of
from tools.ld19_serial_relay import (
    CDEG_PER_REV,
    CHUNK_POINTS,
    FRAME_BYTES,
    HEADER,
    VERLEN,
    Ld19Parser,
    ScanAssembler,
    build_parser,
    crc8,
    run,
)

# LD19 개발 매뉴얼의 예제 프레임 — 펌웨어 시험의 kGoldenFrame 과 동일.
# speed=2152 deg/s, start=324.27°, end=334.70°, ts=6714ms.
GOLDEN_FRAME = bytes(
    [
        0x54, 0x2C, 0x68, 0x08, 0xAB, 0x7E, 0xE0, 0x00, 0xE4, 0xDC, 0x00, 0xE2, 0xD9, 0x00, 0xE5,
        0xD5, 0x00, 0xE3, 0xD3, 0x00, 0xE4, 0xD0, 0x00, 0xE9, 0xCD, 0x00, 0xE4, 0xCA, 0x00, 0xE2,
        0xC7, 0x00, 0xE9, 0xC5, 0x00, 0xE5, 0xC2, 0x00, 0xE5, 0xC0, 0x00, 0xE5, 0xBE, 0x82, 0x3A,
        0x1A, 0x50,
    ]
)


def make_frame(
    speed_dps: int,
    start_cdeg: int,
    end_cdeg: int,
    stamp_ms: int,
    base_dist: int,
    base_intensity: int,
) -> bytes:
    """CRC 까지 계산해 완성된 47바이트 프레임 — C++ MakeFrame 의 이식."""
    f = bytearray(FRAME_BYTES)
    f[0] = HEADER
    f[1] = VERLEN
    f[2] = speed_dps & 0xFF
    f[3] = speed_dps >> 8
    f[4] = start_cdeg & 0xFF
    f[5] = start_cdeg >> 8
    for i in range(12):
        f[6 + i * 3] = (base_dist + i) & 0xFF
        f[7 + i * 3] = (base_dist + i) >> 8
        f[8 + i * 3] = (base_intensity + i) & 0xFF
    f[42] = end_cdeg & 0xFF
    f[43] = end_cdeg >> 8
    f[44] = stamp_ms & 0xFF
    f[45] = stamp_ms >> 8
    f[46] = crc8(bytes(f[: FRAME_BYTES - 1]))
    return bytes(f)


def feed_all(parser: Ld19Parser, data: bytes) -> int:
    return sum(1 for b in data if parser.feed(b) is not None)


def revolution_frames(revolutions: int, base_dist: int = 1000) -> bytes:
    """5° 간격 프레임으로 revolutions 바퀴 분량의 바이트 스트림."""
    out = bytearray()
    for i in range(72 * revolutions + 1):
        start = (i * 500) % CDEG_PER_REV
        out += make_frame(3580, start, (start + 400) % CDEG_PER_REV, i * 10, base_dist, 50)
    return bytes(out)


class FakePort:
    """read() 가 준비된 청크를 순서대로 내리고, 다 쓰면 KeyboardInterrupt."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __enter__(self) -> FakePort:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def read(self, _n: int) -> bytes:
        if not self._chunks:
            raise KeyboardInterrupt
        return self._chunks.pop(0)


class FakeUdpSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, peer: tuple[str, int]) -> int:
        self.sent.append((data, peer))
        return len(data)

    def close(self) -> None:
        self.closed = True


def install_fakes(monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]) -> FakeUdpSocket:
    port = FakePort(chunks)
    sock = FakeUdpSocket()

    def open_serial(*_args: object, **_kwargs: object) -> FakePort:
        return port

    def open_socket(*_args: object, **_kwargs: object) -> FakeUdpSocket:
        return sock

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=open_serial))
    monkeypatch.setattr(
        "tools.ld19_serial_relay.socket",
        SimpleNamespace(socket=open_socket, AF_INET=2, SOCK_DGRAM=2),
    )
    return sock


def args_for(**overrides: Any) -> Any:
    argv = ["--serial", "COM99"]
    for key, value in overrides.items():
        if value is None:
            continue
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return build_parser().parse_args(argv)


# ── CRC ──────────────────────────────────────────────────────


def test_crc8_golden_frame() -> None:
    assert crc8(GOLDEN_FRAME[: FRAME_BYTES - 1]) == GOLDEN_FRAME[FRAME_BYTES - 1]
    assert crc8(b"") == 0


# ── Ld19Parser ───────────────────────────────────────────────


def test_golden_frame() -> None:
    p = Ld19Parser()
    got = None
    for b in GOLDEN_FRAME:
        got = p.feed(b)
    assert got is not None
    assert got["speed_dps"] == 2152
    assert got["start_cdeg"] == 32427
    assert got["end_cdeg"] == 33470
    assert got["stamp_ms"] == 6714
    assert got["points"][0] == (224, 228)
    assert got["points"][11] == (192, 229)
    assert p.frames_ok == 1
    assert p.crc_failures == 0


def test_crc_reject_then_recover() -> None:
    bad = bytearray(make_frame(3600, 0, 500, 0, 1000, 100))
    bad[10] ^= 0xFF  # 측정점 하나를 뭉갠다 — CRC 불일치
    p = Ld19Parser()
    assert feed_all(p, bytes(bad)) == 0
    assert p.crc_failures == 1

    good = make_frame(3600, 500, 1000, 0, 2000, 150)
    assert feed_all(p, good) == 1  # 재동기화 후 정상 수신


def test_garbage_and_fake_header_resync() -> None:
    p = Ld19Parser()
    # 쓰레기 바이트 + 0x54 낚시 + 쓰레기 → 정상 프레임
    junk = bytes([0x00, 0xFF, 0x13, 0x54, 0xAA, 0x55, 0x00])
    for b in junk:
        p.feed(b)
    assert feed_all(p, make_frame(3600, 100, 600, 0, 3000, 200)) == 1


def test_bad_verlen() -> None:
    bad = bytearray(make_frame(3600, 0, 500, 0, 1000, 100))
    bad[1] = 0x10  # VerLen 깨짐 — CRC 도 같이 깨지지만 VerLen 검사가 먼저다
    p = Ld19Parser()
    assert feed_all(p, bytes(bad)) == 0
    assert p.bad_verlen == 1


# ── ScanAssembler ────────────────────────────────────────────


def _frame_dict(start_cdeg: int, end_cdeg: int, dist: int = 1000) -> dict:
    return {
        "speed_dps": 3580,
        "start_cdeg": start_cdeg,
        "points": [(dist, 50)] * 12,
        "end_cdeg": end_cdeg,
        "stamp_ms": 0,
    }


def test_frame_straddles_zero() -> None:
    a = ScanAssembler()
    assert a.add_frame(_frame_dict(35950, 60)) is None
    emitted = a.add_frame(_frame_dict(200, 700))
    assert emitted is not None and len(emitted) == 12
    assert emitted[0][0] == 35950
    assert emitted[11][0] == 60  # 0° 를 걸친 끝 점은 mod 360 으로 돌아온다


def test_full_revolution() -> None:
    a = ScanAssembler()
    emitted_total = 0
    for i in range(73):
        start = (i * 500) % CDEG_PER_REV
        emitted = a.add_frame(_frame_dict(start, (start + 400) % CDEG_PER_REV))
        if emitted:
            emitted_total += len(emitted)
    assert emitted_total == 72 * 12
    assert a.scans_completed == 1


def test_point_cap() -> None:
    a = ScanAssembler()
    # start 가 1씩만 증가하면 wrap 이 걸리지 않는다 — 상한 초과분은 드롭.
    for i in range(101):
        a.add_frame(_frame_dict(i, i + 1))
    assert a.points_dropped == 12
    assert a.scans_completed == 0


# ── run() — 가짜 시리얼/소켓으로 종단간 경로 ────────────────


def test_run_streams_decodable_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = install_fakes(monkeypatch, [revolution_frames(2)])
    assert run(args_for(device="lidar-test")) == 0
    assert sock.closed

    # 한 바퀴 864점은 CHUNK_POINTS 단위로 끊겨야 한다 — 12개 데이터그램.
    assert len(sock.sent) == 864 // CHUNK_POINTS * 2
    decoder = ScanDecoder()
    seqs: list[int] = []
    for data, peer in sock.sent:
        assert peer == ("127.0.0.1", 5201)
        result = decoder.decode(data)
        assert result.accepted, result.reason
        scan = scan_of(result)
        assert scan is not None
        seqs.append(scan.seq)
        assert 0 < len(scan.points) <= CHUNK_POINTS
        assert scan.device_id == "lidar-test"
    assert seqs == sorted(seqs)  # seq 는 단조 증가


def test_run_forward_mode_sends_raw_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = revolution_frames(1)
    chunks = [raw[:1000], raw[1000:2000]]
    sock = install_fakes(monkeypatch, chunks)
    assert run(args_for(fwd_host="192.168.0.45")) == 0

    # 파싱 없이 받은 바이트가 그대로 UDP 로 나간다.
    assert [data for data, _ in sock.sent] == chunks
    assert all(peer == ("192.168.0.45", 5202) for _, peer in sock.sent)
