"""보행 실측 도구 검증 (WBS 2.2.3 · FR-6.4).

**측정 자체는 시험할 수 없다** — 사람이 줄자로 재는 값이다. 시험할 수 있는 것은
*그 값을 무엇으로 나누는가*, *몇 회를 요구하는가*, *프로파일에 무엇을 적는가* 다.
그 셋이 틀리면 실측값이 맞아도 결과가 틀린다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.gait_calibrate import Trial, build_parser, summarize, write_profile


def _trial(index: int, *, actual_s: float, measured: float, requested_s: float = 3.0) -> Trial:
    return Trial(
        index=index,
        requested_s=requested_s,
        actual_s=actual_s,
        packets=int(actual_s * 10) + 1,
        measured=measured,
    )


# ── 비율 계산 ──────────────────────────────────────────────
def test_rate_divides_by_the_actual_window_not_the_request() -> None:
    """⚠️ **이것이 이 도구의 존재 이유다.**

    로봇은 명령이 300ms 끊기면 스스로 멈추므로 실제 구동 시간은 송신 창의
    길이다. 요청값(3초)으로 나누면 창이 3.2초였을 때 값이 6% 틀린다.
    """
    trial = _trial(1, requested_s=3.0, actual_s=3.2, measured=480.0)
    assert trial.rate == pytest.approx(150.0)  # 480 / 3.2
    assert trial.measured / trial.requested_s == pytest.approx(160.0)  # 요청으로 나누면 틀린다


def test_mean_is_over_trials() -> None:
    trials = [
        _trial(1, actual_s=3.0, measured=300.0),
        _trial(2, actual_s=3.0, measured=330.0),
        _trial(3, actual_s=3.0, measured=270.0),
    ]
    assert summarize(trials, "forward") == pytest.approx(100.0)


def test_no_trials_yields_nothing() -> None:
    """버린 시행만 남았으면 값을 만들어 내지 않는다."""
    assert summarize([], "forward") is None


# ── 퍼짐 경고 ──────────────────────────────────────────────
def test_wide_spread_warns_about_the_floor(capsys) -> None:
    """⚠️ 퍼짐이 크다는 것은 **그 바닥에서 회피량을 신뢰할 수 없다**는 뜻이다."""
    trials = [
        _trial(1, actual_s=3.0, measured=300.0),  # 100 mm/s
        _trial(2, actual_s=3.0, measured=600.0),  # 200 mm/s
        _trial(3, actual_s=3.0, measured=450.0),  # 150 mm/s
    ]
    summarize(trials, "forward")
    assert "바닥이 일정하지 않다" in capsys.readouterr().out


def test_tight_spread_does_not_warn(capsys) -> None:
    trials = [
        _trial(1, actual_s=3.0, measured=300.0),
        _trial(2, actual_s=3.0, measured=306.0),
        _trial(3, actual_s=3.0, measured=294.0),
    ]
    summarize(trials, "forward")
    assert "바닥이 일정하지 않다" not in capsys.readouterr().out


def test_fewer_than_three_trials_is_flagged(capsys) -> None:
    """WBS 2.2.3 은 3회 이상 평균을 요구한다 — 조용히 넘기지 않는다."""
    summarize([_trial(1, actual_s=3.0, measured=300.0)], "forward")
    assert "3회 이상" in capsys.readouterr().out


# ── 프로파일 기록 ──────────────────────────────────────────
PROFILE = """device_id: mechdog-01

gait_calibration:
  forward_mm_per_sec: null
  turn_deg_per_sec: null
  measured_on: null
"""


def _profile(tmp_path: Path) -> Path:
    root = tmp_path / "devices"
    root.mkdir()
    (root / "mechdog-01.yaml").write_text(PROFILE, encoding="utf-8")
    return root


def test_write_records_the_value_and_the_date(tmp_path: Path) -> None:
    """⚠️ **`measured_on` 없이 숫자만 넣으면 언제 어느 바닥에서 잰 값인지 잃는다.**

    그러면 다시 재야 하는지 알 수 없고, `prod` 프로파일은 그 날짜를 요구한다.
    """
    root = _profile(tmp_path)
    path = write_profile("mechdog-01", "forward", 148.6, root=root)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "forward_mm_per_sec: 148.6" in text
    assert "measured_on: null" not in text
    assert "turn_deg_per_sec: null" in text, "다른 값을 건드리면 안 된다"


def test_write_keeps_indentation(tmp_path: Path) -> None:
    """들여쓰기를 잃으면 YAML 이 깨져 기동이 안 된다."""
    root = _profile(tmp_path)
    path = write_profile("mechdog-01", "turn", 42.0, root=root)
    assert path is not None
    assert "  turn_deg_per_sec: 42.0\n" in path.read_text(encoding="utf-8")


def test_write_reports_when_the_key_is_missing(tmp_path: Path, capsys) -> None:
    """키가 없으면 **조용히 성공하지 않는다** — 안 적힌 것을 적힌 줄 알면 안 된다."""
    root = tmp_path / "devices"
    root.mkdir()
    (root / "mechdog-01.yaml").write_text("device_id: mechdog-01\n", encoding="utf-8")
    assert write_profile("mechdog-01", "forward", 100.0, root=root) is None
    assert "찾지 못했다" in capsys.readouterr().err


# ── CLI ────────────────────────────────────────────────────
def test_mode_is_required() -> None:
    """모드를 빼면 어느 값을 재는지 알 수 없다 — 기본값을 두지 않는다."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--host", "127.0.0.1"])


def test_defaults_meet_the_three_trial_rule() -> None:
    args = build_parser().parse_args(["--host", "127.0.0.1", "--mode", "forward"])
    assert args.trials >= 3
    assert args.seconds > 0
