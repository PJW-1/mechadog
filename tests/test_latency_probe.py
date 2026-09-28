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


def test_counter_splits_coarse_digits_from_countable_cells() -> None:
    """⚠️ **빨리 바뀌는 자리는 숫자로 읽을 수 없다.**

    화면이 60Hz 로만 갱신되고 카메라 노출이 두 갱신을 걸치므로 하위 자리가 뭉개진다
    — 실기에서 4·5번째 자리가 실제로 읽히지 않았다. 그래서 느린 숫자(100ms)와
    하위 자리를 나눈다.

    ⚠️ **하위 자리를 연속 막대로 두지 않는다.** 가장자리를 읽으려면 막대의 양 끝이
    프레임 안에 있고 포화되지 않아야 하는데 실기에서 둘 다 깨졌다(2026-09-15) —
    화면을 크게 대면 끝이 잘리고, 꽉 채우면 하얗게 떠서 가장자리가 사라졌다.
    **칸은 세기만 하면 되므로** 잘려도 남은 칸을 세고 떠도 칸 사이 틈이 남는다.
    """
    page = counter_page()
    assert "Math.floor(now / 100) % 1000" in page, "숫자는 100ms 단위"
    assert "Math.floor((now % 100) / 10)" in page, "칸은 10ms 단위"
    assert 'padStart(3, "0")' in page, "자릿수가 흔들리면 오독한다"
    assert page.count('className = "cell"') == 1 and "for (let i = 0; i < 10; i++)" in page, (
        "칸은 10개다 — 개수가 바뀌면 읽는 규칙도 바뀐다"
    )


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


def test_override_none_keeps_the_profile_value(committed_devices_dir: Path) -> None:
    """`--xiao-ip` 없이 부르면 프로파일 값이 그대로 남는다.

    커밋본을 봐야 한다 — `*.local.yaml` 오버레이를 타면 이 PC 의 실측 주소가
    섞여 들어온다 (`conftest.py` 의 `committed_devices_dir`).
    """
    from host.common.config import load_config
    from tools.latency_probe import with_xiao_ip

    config = with_xiao_ip(load_config("mechdog-01", devices_dir=committed_devices_dir), None)
    assert config["network"]["xiao_ip"] is None


# ── 전 구간 사슬 (chain) ──────────────────────────────────────


def test_chain_sums_each_frame_before_taking_statistics() -> None:
    """**구간별 p95 를 더하지 않는다.**

    서로 다른 프레임의 최악값을 합치면 실제로는 일어나지 않는 경로가 만들어진다.
    2026-09-14 기록이 그렇게 278ms 를 냈고, 그중 한 값은 시작점이 달라 구간이
    겹치기까지 했다. 아래 두 프레임은 각 구간의 최악이 서로 다른 프레임에 있다.
    """
    from tools.latency_probe import chain_stats

    rows = [
        {"capture_ms": 150, "decode_infer_ms": 10, "ack_ms_from_detect": 10},
        {"capture_ms": 90, "decode_infer_ms": 40, "ack_ms_from_detect": 60},
    ]
    stats = chain_stats(rows)
    # 구간별 최악을 더하면 150+40+60 = 250 이지만 실제 프레임 합은 170·190 이다.
    assert stats["합계(프레임별)"]["max_ms"] == 190
    assert stats["n"] == 2


def test_chain_report_skips_rows_without_an_ack(tmp_path: Path, capsys) -> None:
    """ACK 이 없거나 숫자를 안 적은 행은 사슬이 끊긴 것이다 — 합에 넣으면 안 된다."""
    from types import SimpleNamespace

    import tools.latency_probe as probe

    readings = tmp_path / "chain_readings.csv"
    with readings.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "arrival_ms", "completed_ms", "ack_ms", "applied", "shown_ms"])
        writer.writerow(["a.jpg", 1000, 1030, 1070, True, 900])
        writer.writerow(["b.jpg", 2000, 2030, "", False, 1900])  # ACK 미수신
        writer.writerow(["c.jpg", 3000, 3030, 3070, True, ""])  # 숫자 미기입
    code = probe.cmd_chain_report(
        SimpleNamespace(readings=str(readings), refresh_hz=60.0, budget_ms=250)
    )
    assert code == 0
    assert "표본 1" in capsys.readouterr().out, "쓸 수 있는 행은 a.jpg 하나뿐이다"


def test_ack_matching_ignores_telemetry_and_other_sequences() -> None:
    """같은 소켓으로 10Hz 텔레메트리가 함께 들어온다.

    아무 전문이나 첫 번째를 ACK 로 세면 **텔레메트리 도착 시각을 지연으로 적게 된다.**
    """
    import json as _json

    from tools.latency_probe import _await_ack

    packets = [
        _json.dumps({"seq": 7, "state": "IDLE", "batt_v": 8.0}).encode(),  # 텔레메트리
        _json.dumps({"seq": 41, "verdict": "Accept", "applied": True}).encode(),  # 앞 명령
        _json.dumps({"seq": 42, "verdict": "Accept", "applied": True}).encode(),  # 우리 것
    ]

    class FakeSocket:
        def settimeout(self, _value):
            pass

        def recvfrom(self, _size):
            return packets.pop(0), ("10.0.0.1", 5001)

    ack_ms, applied = _await_ack(FakeSocket(), 42, deadline_ms=2**62)
    assert ack_ms is not None and applied is True
    assert packets == [], "세 전문을 모두 소비하고 42 번에서 멈춘다"
