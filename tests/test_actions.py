"""상태별 모션 시퀀스 검증 (WBS 3.5.1 · FR-2.1/2.3).

**시간을 주입하므로 후진 4초·선회 3초를 실제로 기다리지 않는다.** 회피 시퀀스는
구간마다 다른 명령을 내므로, 가짜 시계로 구간 경계를 정확히 짚어 본다.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeClock

from host.behavior.actions import (
    AvoidSequence,
    PatrolSequence,
    avoid_phases,
    register_actions,
)
from host.behavior.commander import Commander
from host.behavior.fsm import Behavior, Event, behavior_from_config
from host.common import protocol as p

CALIBRATED = {"forward_mm_per_sec": 100.0, "turn_deg_per_sec": 15.0, "measured_on": "2026-09-08"}


def _cfg(cfg: dict, *, calibrated: bool = True) -> dict:
    merged = dict(cfg)
    merged["gait_calibration"] = (
        dict(CALIBRATED)
        if calibrated
        else {
            "forward_mm_per_sec": None,
            "turn_deg_per_sec": None,
            "measured_on": None,
        }
    )
    return merged


def _behavior(clock: FakeClock, config: dict) -> Behavior:
    return behavior_from_config(Commander(p.CommandEncoder(clock=clock)), config)


def sent(behavior: Behavior, now_ms: int) -> list[dict]:
    return [json.loads(line) for line in behavior.tick(now_ms)]


def moves(behavior: Behavior, now_ms: int) -> list[dict]:
    return [m for m in sent(behavior, now_ms) if m["type"] == "MOVE"]


# ── 순찰 ─────────────────────────────────────────────────────
def test_patrol_drives_forward_with_the_configured_stride(clock, cfg) -> None:
    b = _behavior(clock, cfg)
    register_actions(b, _cfg(cfg))
    b.event(Event.START_PATROL, now_ms=clock.ms)
    move = moves(b, clock.ms)[0]
    assert move["step"] == cfg["gait"]["step_length_mm"]
    assert move["angle"] == 0


def test_patrol_keeps_sending_every_tick(clock, cfg) -> None:
    """**10Hz 고정 송신이므로 변화가 없어도 계속 나간다** (FR-5.1)."""
    b = _behavior(clock, cfg)
    register_actions(b, _cfg(cfg))
    b.event(Event.START_PATROL, now_ms=clock.ms)
    for _ in range(20):
        assert moves(b, clock.advance(100)), "한 틱도 비어서는 안 된다"


def test_idle_still_stops(clock, cfg) -> None:
    """순찰을 붙였다고 기동만으로 걷지 않는다."""
    b = _behavior(clock, cfg)
    register_actions(b, _cfg(cfg))
    assert [m["type"] for m in sent(b, clock.ms)] == ["STATE", "STOP"]


# ── 회피 구간표 ──────────────────────────────────────────────
def test_avoid_phases_come_from_measured_speed(cfg) -> None:
    """거리·각도를 **실측 속도로 나눠** 시간이 된다 — 보폭은 거리가 아니다."""
    phases = avoid_phases(_cfg(cfg))
    assert phases is not None
    by_name = {ph.name: ph for ph in phases}
    assert [ph.name for ph in phases] == ["settle", "reverse", "turn", "verify"]
    # 200mm / 100mm/s = 2.0s
    assert by_name["reverse"].duration_ms == 2000
    assert by_name["reverse"].step_mm < 0, "후진이어야 한다"
    # 30° / 15°/s = 2.0s
    assert by_name["turn"].duration_ms == 2000
    assert by_name["turn"].angle_deg == cfg["gait"]["turn_angle_deg"]
    assert by_name["settle"].duration_ms == cfg["localization"]["settle_delay_ms"]


def test_avoid_is_not_built_without_calibration(cfg) -> None:
    """⚠️ **추정값으로 걷게 하지 않는다.** 후진이 모자라면 같은 장애물에 다시 붙는다."""
    assert avoid_phases(_cfg(cfg, calibrated=False)) is None


def test_missing_calibration_leaves_avoid_stopped(clock, cfg) -> None:
    """등록되지 않은 시퀀스 상태는 정지로 처리된다 — 기존 안전측 기본값이다."""
    config = _cfg(cfg, calibrated=False)
    b = _behavior(clock, config)
    result = register_actions(b, config)
    assert "실측" in result["AVOID"]
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.ONBOARD_AVOID, now_ms=clock.ms)
    assert b.state == "AVOID"
    assert [m["type"] for m in sent(b, clock.ms)][-1] == "STOP"


def test_registration_reports_what_it_skipped(clock, cfg) -> None:
    """**조용히 건너뛰지 않는다** — 회피가 없다는 것을 기동 시점에 알아야 한다."""
    b = _behavior(clock, cfg)
    assert register_actions(b, _cfg(cfg))["AVOID"] == "등록"
    b2 = _behavior(clock, cfg)
    assert register_actions(b2, _cfg(cfg, calibrated=False))["AVOID"] != "등록"


# ── 회피 진행 ────────────────────────────────────────────────
def test_avoid_walks_the_phases_in_order(clock, cfg) -> None:
    """정지 → 후진 → 선회 → 재확인. **구간 경계에서 명령이 바뀐다.**"""
    config = _cfg(cfg)
    b = _behavior(clock, config)
    register_actions(b, config)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.ONBOARD_AVOID, now_ms=clock.ms)

    settle_ms = int(cfg["localization"]["settle_delay_ms"])
    stride = float(cfg["gait"]["step_length_mm"])

    assert moves(b, clock.ms)[0]["step"] == 0, "온보드가 멈춘 직후는 정지 유지"
    assert moves(b, clock.advance(settle_ms))[0]["step"] == -stride, "후진"
    assert moves(b, clock.advance(2000))[0]["angle"] == cfg["gait"]["turn_angle_deg"], "선회"
    assert moves(b, clock.advance(2000))[0]["step"] == 0, "멈춰서 로봇 보고를 기다린다"


def test_leaving_avoid_returns_to_patrol_motion(clock, cfg) -> None:
    """**빠져나왔는지는 로봇이 알려준다** — 시퀀스가 판정하지 않는다."""
    config = _cfg(cfg)
    b = _behavior(clock, config)
    register_actions(b, config)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.ONBOARD_AVOID, now_ms=clock.ms)
    b.tick(clock.advance(1000))
    b.event(Event.AVOID_CLEARED, now_ms=clock.ms)
    assert b.state == "PATROL"
    assert moves(b, clock.advance(100))[0]["step"] == cfg["gait"]["step_length_mm"]


def test_reentering_avoid_starts_from_the_beginning(clock, cfg) -> None:
    """진행을 물려받으면 후진을 건너뛰고 선회부터 시작하는 일이 생긴다."""
    config = _cfg(cfg)
    b = _behavior(clock, config)
    register_actions(b, config)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.ONBOARD_AVOID, now_ms=clock.ms)
    b.tick(clock.advance(3000))  # 후진을 지나 선회 구간까지 진행
    b.event(Event.AVOID_CLEARED, now_ms=clock.ms)
    b.event(Event.ONBOARD_AVOID, now_ms=clock.ms)
    assert moves(b, clock.advance(100))[0]["step"] == 0, "다시 정지 구간부터"


def test_avoid_retries_the_configured_number_of_times(cfg) -> None:
    config = _cfg(cfg)
    phases = avoid_phases(config)
    assert phases is not None
    sequence = AvoidSequence(phases, attempts=int(cfg["fsm"]["avoid_attempts"]))
    cycle = sequence.cycle_ms
    seen: list[int] = []
    for step in range(0, cycle * int(cfg["fsm"]["avoid_attempts"]), 100):
        phase, attempt = sequence.phase_at(step)
        assert phase is not None
        seen.append(attempt)
    assert max(seen) == int(cfg["fsm"]["avoid_attempts"])


def test_avoid_stops_after_exhausting_attempts(clock, cfg) -> None:
    """⚠️ **갇혔으면 멈춘 채로 둔다.** 계속 흔들면 기어만 상하고 사람이 봐야 한다."""
    config = _cfg(cfg)
    b = _behavior(clock, config)
    register_actions(b, config)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.ONBOARD_AVOID, now_ms=clock.ms)
    phases = avoid_phases(config)
    assert phases is not None
    total = sum(ph.duration_ms for ph in phases) * int(cfg["fsm"]["avoid_attempts"])
    b.tick(clock.advance(total + 100))
    assert b.state == "AVOID", "상태는 그대로다 — 빠져나온 것이 아니다"
    assert moves(b, clock.advance(100))[0]["step"] == 0


def test_exhausted_is_reported(clock, cfg) -> None:
    config = _cfg(cfg)
    phases = avoid_phases(config)
    assert phases is not None
    sequence = AvoidSequence(phases, attempts=1)
    commander = Commander(p.CommandEncoder(clock=clock))
    sequence(commander, clock.ms)
    assert sequence.exhausted is False
    sequence(commander, clock.ms + sequence.cycle_ms)
    assert sequence.exhausted is True
    sequence.restart()
    assert sequence.exhausted is False, "재진입하면 다시 시도한다"


# ── 방어 ─────────────────────────────────────────────────────
def test_empty_phase_list_is_refused() -> None:
    with pytest.raises(ValueError):
        AvoidSequence((), attempts=1)


def test_zero_attempts_is_refused(cfg) -> None:
    phases = avoid_phases(_cfg(cfg))
    assert phases is not None
    with pytest.raises(ValueError):
        AvoidSequence(phases, attempts=0)


def test_patrol_sequence_ignores_the_clock(clock) -> None:
    """순찰은 시간에 따라 달라지지 않는다 — 달라지는 것은 전이가 만든다."""
    commander = Commander(p.CommandEncoder(clock=clock))
    sequence = PatrolSequence(60)
    sequence(commander, 0)
    first = commander.intent
    sequence(commander, 999_999)
    assert commander.intent == first


def test_avoid_only_registers_on_sequence_states(clock, cfg) -> None:
    """`register_sequence` 는 시퀀스를 쓰지 않는 상태를 거부한다 (3.4.2 계약)."""
    b = _behavior(clock, cfg)
    with pytest.raises(ValueError):
        b.register_sequence("FAILSAFE", PatrolSequence(60))
