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


# ── 후진 속도는 전진 속도가 아니다 (2026-09-11 실측) ────────
def test_reverse_uses_its_own_measured_speed(cfg) -> None:
    """⚠️ **실측이 코드의 가정을 깼다.** 전진 103.9 · 후진 78.0 mm/s — 25% 느리다.

    전진 값으로 후진 시간을 계산하면 200mm 목표에서 **50mm 가 부족해** 같은
    장애물에 다시 붙는다. 두 배터리 상태에서 같은 비율이 나와 전압 영향이 아니다.
    """
    merged = _cfg(cfg)
    merged["gait_calibration"] = dict(
        merged["gait_calibration"], forward_mm_per_sec=100.0, reverse_mm_per_sec=75.0
    )
    phases = avoid_phases(merged)
    assert phases is not None
    reverse = next(ph for ph in phases if ph.name == "reverse")
    # 200mm / 75mm/s = 2.667s — 전진 값으로 나눴다면 2.0s 였다
    assert reverse.duration_ms == 2666
    assert reverse.duration_ms > 2000, "전진 속도로 나누면 이 시험이 실패한다"


def test_reverse_falls_back_to_forward_when_unmeasured(cfg) -> None:
    """재지 않은 개체는 전진 값으로 돈다 — 회피를 아예 끄는 것보다 낫다.

    ⚠️ 다만 **그 개체의 후진량은 그만큼 틀린다.** 되돌아가는 것이 옳다는 뜻이
    아니라, 재기 전까지의 차악이라는 뜻이다.
    """
    merged = _cfg(cfg)
    merged["gait_calibration"] = dict(
        merged["gait_calibration"], forward_mm_per_sec=100.0, reverse_mm_per_sec=None
    )
    phases = avoid_phases(merged)
    assert phases is not None
    reverse = next(ph for ph in phases if ph.name == "reverse")
    assert reverse.duration_ms == 2000  # 200 / 100


# ── 후진하며 선회 (ADR-29 · 2026-09-11 실측이 설계를 바꿨다) ─
MEASURED = {
    "forward_mm_per_sec": 103.9,
    "reverse_mm_per_sec": 78.0,
    "turn_deg_per_sec": 6.8,
    "reverse_turn_deg_per_sec": 6.6,
    "reverse_turn_mm_per_sec": 69.7,
    "measured_on": "2026-09-11",
}


def _measured(cfg: dict, **override: float | None) -> dict:
    merged = dict(cfg)
    merged["gait_calibration"] = dict(MEASURED, **override)
    return merged


def test_reverse_turn_replaces_the_reverse_then_turn_pair(cfg) -> None:
    """⚠️ **전진하며 돌면 후진으로 번 여유를 되돌려 준다.**

    제자리 회전이 불가하므로(ADR-11) 선회가 반드시 이동을 동반하는데, 실측
    선회 속도가 6.8 도/s 라 30도에 4.4초가 걸리고 그 동안 84mm/s 로 **370mm 를
    전진했다** — 후진 200mm 를 다 먹고 **순 여유가 -170mm**, 즉 회피가 장애물에
    더 붙었다. 후진하며 돌면 한 구간으로 줄고 여유가 양수가 된다.
    """
    phases = avoid_phases(_measured(cfg))
    assert phases is not None
    assert [ph.name for ph in phases] == ["settle", "reverse_turn", "verify"]
    escape = phases[1]
    assert escape.step_mm < 0, "후진이어야 여유가 벌어진다"
    assert escape.angle_deg == cfg["gait"]["turn_angle_deg"], "부호는 그대로 좌선회"


def test_reverse_turn_gains_clearance_instead_of_losing_it(cfg) -> None:
    """**이 시험이 수정의 목적이다** — 순 여유가 목표(200mm)를 넘는지 본다."""
    phases = avoid_phases(_measured(cfg))
    assert phases is not None
    escape = next(ph for ph in phases if ph.name == "reverse_turn")
    retreat_mm = escape.duration_ms / 1000 * MEASURED["reverse_turn_mm_per_sec"]
    assert retreat_mm >= cfg["gait"]["reverse_distance_mm"]
    assert retreat_mm == pytest.approx(316.8, abs=1.0)


def test_reverse_turn_time_satisfies_the_slower_of_the_two_goals(cfg) -> None:
    """각도와 여유 **둘 다** 만족해야 한다.

    각도만 보면 여유가 모자랄 수 있고, 여유만 보면 방향 전환이 모자라 같은
    장애물을 다시 만난다. 그래서 오래 걸리는 쪽을 쓴다.
    """
    # 각도가 느린 쪽: 30도 / 6.6 = 4.545s > 200mm / 69.7 = 2.869s
    by_angle = avoid_phases(_measured(cfg))
    assert by_angle is not None
    assert by_angle[1].duration_ms == 4545

    # 여유가 느린 쪽: 30도 / 30 = 1.0s < 200mm / 50 = 4.0s
    by_clearance = avoid_phases(
        _measured(cfg, reverse_turn_deg_per_sec=30.0, reverse_turn_mm_per_sec=50.0)
    )
    assert by_clearance is not None
    assert by_clearance[1].duration_ms == 4000


@pytest.mark.parametrize(
    "override",
    [{"reverse_turn_deg_per_sec": None}, {"reverse_turn_mm_per_sec": None}],
)
def test_half_measured_reverse_turn_keeps_the_old_phases(cfg, override: dict) -> None:
    """⚠️ **한쪽만 재고 적용하면 시간을 아무 값으로나 계산하게 된다.**

    둘 다 있어야 각도·여유 두 목표를 비교할 수 있다. 없으면 옛 구간표로 돌아가고
    — 그 개체에서는 위의 여유 문제가 그대로 남으므로 `2.2.3` 을 먼저 재야 한다.
    """
    phases = avoid_phases(_measured(cfg, **override))
    assert phases is not None
    assert [ph.name for ph in phases] == ["settle", "reverse", "turn", "verify"]
