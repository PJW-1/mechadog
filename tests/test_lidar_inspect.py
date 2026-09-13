"""lidar_inspect 실측 통계 도구 시험 — 가짜 시리얼로 합성 스트림을 먹인다.

실제 장치 포트는 열지 않는다. 프레임 합성은 test_ld19_serial_relay 의
도우미를 쓴다.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from test_ld19_serial_relay import make_frame

from tools import lidar_inspect


class FakePort:
    """준비된 청크를 내리고 다 쓰면 빈 바이트를 돌려주는 가짜 포트."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __enter__(self) -> FakePort:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def install_serial(monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]) -> None:
    port = FakePort(chunks)

    def open_serial(*_args: object, **_kwargs: object) -> FakePort:
        return port

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=open_serial))


def args_for(**overrides: Any) -> Any:
    argv = ["--serial", "COM99"]
    for key, value in overrides.items():
        if value is None:
            continue
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return lidar_inspect.build_parser().parse_args(argv)


def revolutions(count: int, dist_of) -> bytes:
    """5° 간격 프레임으로 count 바퀴 + wrap 용 1프레임."""
    out = bytearray()
    for i in range(72 * count + 1):
        start = (i * 500) % 36000
        dist = dist_of(start)
        out += make_frame(3580, start, (start + 400) % 36000, i * 10, dist, 50)
    return bytes(out)


def test_collects_and_reports(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # 100°~110° 대역만 2000mm, 나머지는 1000mm — 빈 분리가 보여야 한다.
    stream = revolutions(3, lambda cdeg: 2000 if 10000 <= cdeg < 11000 else 1000)
    install_serial(monkeypatch, [stream])
    out = tmp_path / "report.json"

    assert lidar_inspect.run(args_for(scans=2, out=str(out))) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["scans"] == 3  # 한 번의 read 로 스트림 전부 소비 — 3바퀴 완성
    assert report["frames_ok"] == 72 * 3 + 1  # 세 바퀴 분량 전부 파싱됨
    assert report["crc_fail"] == 0
    assert report["resync"] == 0
    assert report["speed_median_raw"] == 3580
    assert report["pts_per_scan_median"] == 864
    assert report["dist_min_mm"] == 1000
    assert report["dist_max_mm"] == 2011  # base_dist + 11 (프레임 내 마지막 점)

    by_deg = {row["deg"]: row for row in report["bins"]}
    assert by_deg[100.0]["median_mm"] == pytest.approx(2000, abs=11)
    assert by_deg[50.0]["median_mm"] == pytest.approx(1000, abs=11)


def test_no_scans_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install_serial(monkeypatch, [])  # 포트가 열리지만 데이터가 없다

    # 60초 실시간 대기를 시험에서 기다리지 않는다 — 시계를 크게 진행시킨다.
    ticks = iter([0.0] + [61.0 + i for i in range(100)])
    monkeypatch.setattr(lidar_inspect, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    assert lidar_inspect.run(args_for(scans=1)) == 1
