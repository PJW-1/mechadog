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
    budget_note,
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


# ── 예산 문구 ───────────────────────────────────────────────
def test_budget_note_does_not_claim_nfr_pass() -> None:
    """⚠️ **부분 구간에 통과 도장을 찍지 않는다.**

    예전 출력은 `NFR-1.1 예산 250ms 대비 최악값 … → 통과` 였다. 이 도구는 촬영→도착만
    재는데 NFR-1.1 정의가 `촬영 → 검출 → 명령 적용 ACK` 로 좁혀지면서, 부분 구간을
    전체 예산에 대고 합격시키는 문구가 됐다.
    """
    note = budget_note({"max_ms": 105}, 250)
    assert "통과" not in note
    assert "42.0%" in note, "예산의 몇 %를 쓰는지 보여야 한다"
    assert "포함되지 않았다" in note, "빠진 구간을 반드시 적는다"


def test_budget_note_still_fails_hard_when_segment_alone_exceeds() -> None:
    """구간 하나가 이미 전체 예산을 넘었다면 그것만으로 미달이 확정된다."""
    note = budget_note({"max_ms": 300}, 250)
    assert "미달 확정" in note


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
    assert "Date.now()" in page
    assert "requestAnimationFrame" in page


def test_counter_splits_coarse_digits_from_a_bar() -> None:
    """⚠️ **빨리 바뀌는 자리는 숫자로 읽을 수 없다.**

    화면이 60Hz 로만 갱신되고 카메라 노출이 두 갱신을 걸치므로 하위 자리가 뭉개진다
    — 실기에서 4·5번째 자리가 실제로 읽히지 않았다. 그래서 느린 숫자(100ms)와
    연속적인 막대(0~100ms)로 나눈다.
    """
    page = counter_page()
    assert "Math.floor(now / 100) % 1000" in page, "숫자는 100ms 단위"
    assert "(now % 100)" in page, "막대는 그 안의 0~100ms"
    assert 'padStart(3, "0")' in page, "자릿수가 흔들리면 오독한다"


# ── ⚠️ CLI 덮어쓰기도 network 절이다 ────────────────────────
def test_xiao_ip_override_goes_into_network() -> None:
    """**최상위에 넣으면 조용히 무시된다.**

    실제로 그렇게 만들어서 카메라를 향하게 두고 나서야 걸렸다. 같은 실수를
    프로파일 판독·런타임·이 도구 세 곳에서 했다.
    """
    from host.common.config import load_config
    from host.vision.stream_client import stream_endpoints
    from tools.latency_probe import with_xiao_ip

    config = with_xiao_ip(load_config("mechdog-01"), "192.168.1.100")
    assert config["network"]["xiao_ip"] == "192.168.1.100"
    assert stream_endpoints(config).stream == "http://192.168.1.100:81/stream"


def test_override_none_keeps_the_profile_value() -> None:
    from host.common.config import load_config
    from tools.latency_probe import with_xiao_ip

    config = with_xiao_ip(load_config("mechdog-01"), None)
    assert config["network"]["xiao_ip"] is None
