"""고정 주기 송신기 검증 (WBS 4.3.2 · FR-5.1).

가짜 시계를 주입하므로 **10초짜리 주기 시험이 0초에 끝난다.** 실제로 기다리면
100틱 시험 하나에 10초가 들고, 그러면 아무도 자주 돌리지 않는다.
"""

import json

import pytest
from conftest import FakeClock

from host.behavior.commander import HALT, Commander, Intent
from host.common.protocol import CommandDecoder, CommandEncoder


def make(clock: FakeClock, period_ms: int = 100) -> Commander:
    return Commander(CommandEncoder(clock=clock), period_ms=period_ms)


def types_of(lines: list[str]) -> list[str]:
    return [json.loads(line)["type"] for line in lines]


def test_sends_even_when_nothing_changed(clock: FakeClock) -> None:
    """**변화가 없어도 매 주기 보낸다.** 이것이 이 모듈의 존재 이유다.

    수신측 300ms 타임아웃을 갱신하는 것이 곧 하트비트이므로, 조용해지면
    로봇이 스스로 멈춘다 (PROTOCOL.md 1절).
    """
    c = make(clock)
    c.drive(step=40, angle=0)
    seen = []
    for _ in range(30):
        seen += c.tick(clock.ms)
        clock.advance(100)
    assert len(seen) == 30, "주기마다 정확히 한 번 나가야 한다"
    assert types_of(seen) == ["MOVE"] * 30


def test_does_not_fire_before_the_period_elapses(clock: FakeClock) -> None:
    c = make(clock)
    assert c.tick(clock.ms), "첫 틱은 즉시 발사한다"
    clock.advance(99)
    assert c.tick(clock.ms) == [], "99ms 에는 아직 아니다"
    clock.advance(1)
    assert c.tick(clock.ms), "100ms 에 발사한다"


def test_period_does_not_drift_over_a_hundred_ticks(clock: FakeClock) -> None:
    """**주기가 누적으로 밀리지 않아야 한다.**

    `sleep(100ms)` 방식은 처리 시간이 매 주기 더해져 실제 주기가 느려진다.
    첫 안전 시험에서 송신기가 그렇게 만들어져 있었고, 실제 주기가 6.4Hz 로
    떨어져 **로봇의 300ms 타임아웃이 의도보다 먼저 걸렸다.**
    """
    c = make(clock)
    start = clock.ms
    ticks = 0
    while clock.ms - start < 10_000:
        ticks += len(c.tick(clock.ms))
        clock.advance(7)  # 시계가 주기와 안 맞아떨어져도 밀리지 않아야 한다
    assert ticks == pytest.approx(100, abs=1), f"10초에 100틱이어야 하는데 {ticks}틱"


def test_a_long_stall_resynchronises_instead_of_bursting(clock: FakeClock) -> None:
    """오래 멈췄다가 돌아오면 **밀린 만큼 몰아 보내지 않는다.**

    과거를 따라잡으면 순간 폭주가 되고, 로봇에게는 오래된 명령이 줄줄이
    들어오는 것이라 더 나쁘다.
    """
    c = make(clock)
    c.tick(clock.ms)
    clock.advance(5_000)
    assert len(c.tick(clock.ms)) == 1, "5초 공백 뒤에도 한 번만"
    clock.advance(99)
    assert c.tick(clock.ms) == [], "재동기 후 주기가 지금 기준으로 다시 잡힌다"


def test_out_of_range_values_are_clamped_before_sending(clock: FakeClock) -> None:
    """규약 범위를 넘는 값은 **보내기 전에** 자른다 (PROTOCOL 규칙 ②)."""
    c = make(clock)
    c.drive(step=999, angle=-999)
    msg = json.loads(c.tick(clock.ms)[0])
    assert (msg["step"], msg["angle"]) == (100, -30)


def test_sequence_numbers_increase_monotonically(clock: FakeClock) -> None:
    """수신측 순서 게이트가 이것만 보고 역전·중복을 판정한다."""
    c = make(clock)
    c.drive(step=10, angle=0)
    seqs = []
    for _ in range(5):
        seqs += [json.loads(x)["seq"] for x in c.tick(clock.ms)]
        clock.advance(100)
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_state_is_announced_once_per_change(clock: FakeClock) -> None:
    """`STATE` 는 **바뀔 때만** 나간다. 매 틱 보내면 대역만 먹는다."""
    c = make(clock)
    c.announce("IDLE")
    assert types_of(c.tick(clock.ms)) == ["STATE", "STOP"]
    clock.advance(100)
    c.announce("IDLE")
    assert types_of(c.tick(clock.ms)) == ["STOP"], "같은 상태를 다시 알리지 않는다"
    clock.advance(100)
    c.announce("MANUAL")
    assert types_of(c.tick(clock.ms)) == ["STATE", "STOP"]
    assert c.announced_state == "MANUAL"


def test_one_shot_commands_are_sent_exactly_once(clock: FakeClock) -> None:
    c = make(clock)
    c.once("LED", color="red", blink_hz=2)
    assert types_of(c.tick(clock.ms)) == ["LED", "STOP"]
    clock.advance(100)
    assert types_of(c.tick(clock.ms)) == ["STOP"]


def test_emergency_stop_bypasses_the_tick_and_forces_halt(clock: FakeClock) -> None:
    """**비상정지만 틱을 기다리지 않는다.** 100ms 를 기다리게 하면 안 된다."""
    c = make(clock)
    c.drive(step=80, angle=10)
    wire = c.emergency_stop()
    assert json.loads(wire)["type"] == "ESTOP"
    assert c.intent == HALT, "래치 뒤에 MOVE 는 어차피 차단되므로 의도도 정지여야 한다"


def test_clear_safe_also_returns_wire_and_halts(clock: FakeClock) -> None:
    c = make(clock)
    c.drive(step=50, angle=0)
    assert json.loads(c.clear_safe())["type"] == "RESET_SAFE"
    assert c.intent == HALT, "해제 직후 이전 보행 의도가 되살아나면 안 된다"


def test_everything_it_emits_is_accepted_by_the_receiver(clock: FakeClock) -> None:
    """송신기가 만든 것을 **규약 수신기가 그대로 받아들여야 한다.**

    송신·수신을 각각 시험해도 둘이 맞물리는지는 알 수 없다. 여기서 한 바퀴를
    닫는다 — 로봇이 실제로 보게 되는 것과 같은 판정을 거친다.
    """
    c = make(clock)
    decoder = CommandDecoder()
    c.announce("MANUAL")
    c.drive(step=60, angle=20)
    for _ in range(20):
        for line in c.tick(clock.ms):
            result = decoder.decode(line)
            assert result.accepted, f"수신측이 거부했다: {result.reason} / {line}"
        clock.advance(100)


def test_period_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Commander(period_ms=0)


def test_intent_is_immutable() -> None:
    """의도를 나중에 몰래 바꾸면 무엇을 보냈는지 추적할 수 없다."""
    with pytest.raises(AttributeError):
        Intent("STOP").type_ = "MOVE"  # type: ignore[misc]
