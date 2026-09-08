"""E2E 지연 하네스 검증 (WBS 6.1.2 · NFR-1.1).

**되돌아옴 보정과 통계가 핵심이다.** 화면이 하위 5자리만 보여주므로 100초마다 0으로
돌아가고, 그 경계에서 음수가 나오면 지연이 -99초로 찍힌다.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from tools.latency_probe import (
    MODULUS,
    counter_page,
    latency_ms,
    read_readings,
    summarize,
)


# ── 되돌아옴 보정 ────────────────────────────────────────────
def test_plain_case() -> None:
    assert latency_ms(1_788_000_012_340, 12_200) == 140


def test_wrap_is_corrected() -> None:
    """⚠️ **촬영이 되돌아옴 직전, 도착이 직후인 경우.**

    보정하지 않으면 지연이 -99,880ms 로 찍힌다.
    """
    arrival = 1_788_000_000_120  # 하위 5자리 = 00120
    shown = 99_990
    assert latency_ms(arrival, shown) == 130


def test_reading_out_of_range_is_refused() -> None:
    """5자리를 벗어난 값은 잘못 읽은 것이다 — 조용히 계산하면 안 된다."""
    with pytest.raises(ValueError):
        latency_ms(1_788_000_000_000, MODULUS)
    with pytest.raises(ValueError):
        latency_ms(1_788_000_000_000, -1)


# ── 통계 ────────────────────────────────────────────────────
def test_summary_reports_worst_case() -> None:
    """**평균만 보지 않는다** — 예산을 정하는 것은 최악값이다."""
    stats = summarize([100, 110, 120, 130, 400])
    assert stats["n"] == 5
    assert stats["min_ms"] == 100
    assert stats["max_ms"] == 400
    assert stats["mean_ms"] == 172.0
    assert stats["p95_ms"] == 400


def test_quantization_is_reported_not_hidden() -> None:
    """모니터 주기 오차를 몰래 보정하지 않고 함께 적는다."""
    assert summarize([100], refresh_hz=60.0)["quantization_ms"] == 16.7
    assert summarize([100], refresh_hz=120.0)["quantization_ms"] == 8.3


def test_empty_samples_refused() -> None:
    with pytest.raises(ValueError):
        summarize([])


# ── CSV ─────────────────────────────────────────────────────
def test_unfilled_rows_are_skipped(tmp_path: Path) -> None:
    """숫자를 못 읽은 프레임은 건너뛴다 — 0 으로 채우면 통계가 망가진다."""
    path = tmp_path / "readings.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "arrival_ms", "shown_ms"])
        writer.writerow(["a.jpg", 1_788_000_012_340, 12_200])
        writer.writerow(["b.jpg", 1_788_000_012_500, ""])
        writer.writerow(["c.jpg", 1_788_000_012_600, 12_400])
    assert read_readings(path) == [140, 200]


# ── 카운터 페이지 ───────────────────────────────────────────
def test_counter_uses_the_host_clock() -> None:
    """⚠️ **`Date.now()` 여야 한다.** 다른 시계를 쓰면 동기 문제가 되살아난다."""
    page = counter_page()
    assert "Date.now() % 100000" in page
    assert "requestAnimationFrame" in page


def test_counter_pads_to_five_digits() -> None:
    """자릿수가 흔들리면 프레임에서 읽을 때 오독한다."""
    assert 'padStart(5, "0")' in counter_page()
