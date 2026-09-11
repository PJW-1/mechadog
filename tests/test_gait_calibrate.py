"""보행 실측 도구 검증 (WBS 2.2.3 · FR-6.4).

**측정 자체는 시험할 수 없다** — 사람이 줄자로 재는 값이다. 시험할 수 있는 것은
*그 값을 무엇으로 나누는가*, *몇 회를 요구하는가*, *프로파일에 무엇을 적는가* 다.
그 셋이 틀리면 실측값이 맞아도 결과가 틀린다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.gait_calibrate import (
    Trial,
    build_parser,
    report_avoid_clearance,
    summarize,
    write_profile,
)


def _trial(
    index: int,
    *,
    actual_s: float,
    measured: float,
    requested_s: float = 3.0,
    secondary: float | None = None,
) -> Trial:
    return Trial(
        index=index,
        requested_s=requested_s,
        actual_s=actual_s,
        packets=int(actual_s * 10) + 1,
        measured=measured,
        secondary=secondary,
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
  reverse_mm_per_sec: null
  turn_deg_per_sec: null
  reverse_turn_deg_per_sec: null
  reverse_turn_mm_per_sec: null
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
    path = write_profile("mechdog-01", "turn_left", 42.0, root=root)
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


# ── 네 모드 (2.2.3 ①~④) ───────────────────────────────────
def test_all_modes_are_offered() -> None:
    """전진·후진·좌선회·우선회 + **후진 선회**(회피 개선안).

    `forward` 와 `turn_left` 만 설정에 들어가고 나머지는 **가정 확인용**이다.
    """
    for mode in ("forward", "reverse", "turn_right", "turn_left", "reverse_turn"):
        args = build_parser().parse_args(["--host", "1.2.3.4", "--mode", mode])
        assert args.mode == mode


def test_only_the_configured_directions_have_profile_keys() -> None:
    """⚠️ **우선회는 적을 곳이 없다 — 그래서 적지 않는다.**

    회피는 `+turn_angle_deg`(**좌회전**)만 쓰고 설정에 방향별 선회 키가 없다.
    값을 임의로 적으면 *"쟀다"* 로 보이지만 어느 방향의 값인지 알 수 없게 된다.

    나머지 넷은 **실측이 키를 만들어서** 적을 곳이 생겼다 — 후진이 25% 느리고
    전진 선회가 회피 여유를 다 먹는 것이 드러났기 때문이다(ADR-29).
    """
    from tools.gait_calibrate import PROFILE_KEYS

    assert set(PROFILE_KEYS) == {"forward", "turn_left", "reverse", "reverse_turn"}
    assert "turn_right" not in PROFILE_KEYS


def test_the_unmeasured_direction_refuses_to_write(tmp_path: Path, capsys) -> None:
    root = _profile(tmp_path)
    assert write_profile("mechdog-01", "turn_right", 123.4, root=root) is None
    assert "적을 키가 없다" in capsys.readouterr().err
    assert "null" in (root / "mechdog-01.yaml").read_text(encoding="utf-8")


def test_reverse_turn_refuses_to_write_only_half(tmp_path: Path, capsys) -> None:
    """⚠️ **반만 적히면 개체가 조용히 옛 구간표로 돌아간다.**

    회피는 각도 목표와 여유 목표 중 느린 쪽으로 시간을 잡으므로 두 키가 다
    있어야 새 구간이 켜진다. 하나만 적으면 *"적었는데 안 쓰인다"* 는, 가장
    알아채기 어려운 상태가 된다.
    """
    root = _profile(tmp_path)
    assert write_profile("mechdog-01", "reverse_turn", 6.6, root=root) is None
    assert "둘 다" in capsys.readouterr().err


def test_reverse_turn_writes_both_keys(tmp_path: Path) -> None:
    root = _profile(tmp_path)
    path = write_profile("mechdog-01", "reverse_turn", 6.6, secondary=69.7, root=root)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "reverse_turn_deg_per_sec: 6.6" in text
    assert "reverse_turn_mm_per_sec: 69.7" in text


@pytest.mark.parametrize(
    "mode,unit", [("forward", "mm/s"), ("reverse", "mm/s"), ("turn_left", "도/s")]
)
def test_units_follow_the_mode(capsys, mode: str, unit: str) -> None:
    """후진은 거리(mm)고 선회는 각도(도)다 — 단위가 섞이면 값이 조용히 틀린다."""
    summarize([_trial(1, actual_s=3.0, measured=300.0)], mode)
    assert unit in capsys.readouterr().out


# ── 부 측정 (사용자 지적으로 추가) ─────────────────────────
def test_secondary_is_none_when_not_measured() -> None:
    """⚠️ **안 잰 것을 0 으로 채우면 *"쟀다"* 가 된다.**"""
    t = _trial(1, actual_s=3.0, measured=300.0)
    assert t.secondary is None
    assert t.secondary_rate is None


def test_secondary_rate_uses_the_same_window() -> None:
    t = _trial(1, actual_s=3.0, measured=45.0, secondary=150.0)
    assert t.rate == pytest.approx(15.0)  # 도/s
    assert t.secondary_rate == pytest.approx(50.0)  # mm/s


def test_every_mode_defines_both_measurements() -> None:
    """모드마다 주·부 측정이 정의돼 있어야 입력 안내가 틀리지 않는다."""
    from tools.gait_calibrate import MEASURES, PROFILE_KEYS

    assert set(MEASURES) == {
        "forward",
        "reverse",
        "turn_right",
        "turn_left",
        "reverse_turn",
    }
    assert set(PROFILE_KEYS) <= set(MEASURES)
    for mode, (what, unit, second_what, second_unit) in MEASURES.items():
        assert unit != second_unit, f"{mode}: 주·부 단위가 같으면 뒤바뀌어도 모른다"
        assert what and second_what


# ── 회피 여유 산수 (ADR-11 의 결과) ────────────────────────
def test_avoid_clearance_warns_when_the_turn_eats_the_gap(cfg: dict, capsys) -> None:
    """⚠️ **제자리 회전이 불가하므로 선회가 앞으로 간다.**

    후진으로 확보한 여유를 선회가 되돌려 주며, 되돌려 주는 양이 더 크면 **같은
    장애물에 다시 붙는다.** `avoid_phases` 는 시간만 계산하므로 이 산수를 하지
    않는다 — 그래서 측정 도구가 한다.
    """
    # 30도 선회에 3초가 걸리고 그 동안 100mm/s 로 가면 300mm — 후진 200mm 를 넘는다
    report_avoid_clearance(10.0, 100.0, cfg)
    out = capsys.readouterr().out
    assert "순 여유" in out
    assert "여유가 음수다" in out


def test_avoid_clearance_is_quiet_when_the_gap_holds(cfg: dict, capsys) -> None:
    # 30도 선회에 0.5초, 그 동안 40mm — 200mm 중 160mm 가 남는다
    report_avoid_clearance(60.0, 80.0, cfg)
    out = capsys.readouterr().out
    assert "순 여유" in out
    assert "여유가 음수다" not in out
    assert "여유가 얇다" not in out


# ── 명령 부호 (두 번 틀렸던 자리) ──────────────────────────
def test_command_signs_follow_the_convention(cfg: dict) -> None:
    """⚠️ **양수 = 반시계 = 왼쪽.** 이 표가 뒤집혀서 두 번 고쳤다.

    `teleop` 의 좌우가 실제로 뒤바뀐 상태였고 그 값을 근거로 삼았다. 그래서
    부호를 주석이 아니라 **시험**에 적는다.
    """
    from tools.gait_calibrate import command_for

    turn = float(cfg["gait"]["turn_angle_deg"])
    step = float(cfg["gait"]["step_length_mm"])
    assert command_for("forward", cfg["gait"]) == (step, 0.0)
    assert command_for("reverse", cfg["gait"]) == (-step, 0.0)
    assert command_for("turn_left", cfg["gait"]) == (step, turn)
    assert command_for("turn_right", cfg["gait"]) == (step, -turn)
    # 후진 선회는 **후진 + 좌선회와 같은 부호** — 요가 `angle` 단독이기 때문이다.
    assert command_for("reverse_turn", cfg["gait"]) == (-step, turn)


def test_bias_is_added_to_every_mode(cfg: dict) -> None:
    """직진 보정 실측 — `forward` 에 반대 각도를 실어 편향이 0 이 되는 값을 찾는다."""
    from tools.gait_calibrate import command_for

    step = float(cfg["gait"]["step_length_mm"])
    assert command_for("forward", cfg["gait"], bias_deg=-3.0) == (step, -3.0)
    turn = float(cfg["gait"]["turn_angle_deg"])
    assert command_for("turn_left", cfg["gait"], bias_deg=-3.0) == (step, turn - 3.0)


def test_bias_defaults_to_zero() -> None:
    """⚠️ 기본이 0 이어야 한다 — 보정이 섞인 값을 프로파일에 적으면 이름과 내용이 어긋난다."""
    args = build_parser().parse_args(["--host", "1.2.3.4", "--mode", "forward"])
    assert args.bias_deg == 0.0
