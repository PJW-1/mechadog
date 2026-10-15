"""쓰러짐 감시 단독 검증 — `FallMonitor` (ADR-35 · ADR-42).

틱을 거치는 시나리오(의심·확정·제한 시간·쿨다운·경보 확인)는 `test_runtime.py` 에 있다.
여기서는 런타임 없이 닿기 어려운 분기만 본다 — 판독 버림, 워커 사망, 상실 종료, 워커 중재.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from host.behavior.commander import Commander
from host.behavior.escalation import Escalation, Level
from host.behavior.fall_monitor import FallMonitor
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.mission import Mission
from host.vision.vlm_reader import Answer, Reading

T0 = 1_000_000
#: 공장 모드의 선행 기능 검사를 연다 — 쓰러짐 감시는 공장 모드에서만 돈다.
pytestmark = pytest.mark.usefixtures("unlock_modes")


def _down(value: bool = True) -> Reading:
    answer = Answer("person_down", value, "yes" if value else "no", 1)
    return Reading(answers=(answer,), degraded=False, reason=None, taken_at_ms=0)


class OneSlotVlm:
    """`VlmWorker` 대역 — 건 판독은 `slot` 에 넣어 둔 것이 곧바로 나온다."""

    def __init__(self) -> None:
        self.busy = False
        self.slot: Reading | None = None
        self.submitted: list[int] = []

    def submit(self, _image, *, now_ms: int, **_keys) -> bool:
        self.submitted.append(now_ms)
        return True

    def take(self) -> Reading | None:
        reading, self.slot = self.slot, None
        return reading


@pytest.fixture
def parts(cfg: dict):
    """순찰 중인 공장 모드의 감시 하나. `zone` 은 구역 판독이 기다리는 중인지를 흉내 낸다."""
    behavior = behavior_from_config(Commander(), cfg)
    behavior.event(Event.START_PATROL, now_ms=T0)
    escalation = Escalation(cfg)
    vlm = OneSlotVlm()
    zone = SimpleNamespace(waiting=False)
    applied: list[Event] = []

    def apply(event: Event, now_ms: int) -> bool:
        applied.append(event)
        return behavior.event(event, now_ms=now_ms)

    monitor = FallMonitor(
        cfg,
        behavior=behavior,
        mission=Mission(cfg, mode="factory"),
        escalation=escalation,
        vlm=vlm,
        apply=apply,
        record=lambda *_: None,
        zone_waiting=lambda: zone.waiting,
    )
    return SimpleNamespace(
        monitor=monitor,
        behavior=behavior,
        escalation=escalation,
        vlm=vlm,
        zone=zone,
        applied=applied,
    )


FRAME = SimpleNamespace(jpeg=b"jpeg")


def test_a_late_answer_asked_during_an_ended_suspicion_is_dropped(parts) -> None:
    """의심 중에 건 판독이 의심이 끝난 뒤 «예» 로 와도 다시 의심하지 않는다."""
    fall = parts.monitor
    fall.suspect("yolox", T0)
    fall.ask(FRAME, T0)  # 의심 중에 건다
    assert fall.waiting
    parts.behavior.event(Event.MANUAL_ON, now_ms=T0 + 100)
    fall.watch(T0 + 100)  # 추종을 떠나 의심이 끝난다
    parts.vlm.slot = _down()
    fall.take_reading(T0 + 200)
    assert not fall.waiting
    assert not fall.suspected, "끝난 의심의 늦은 답이 다시 의심을 켰다"


def test_a_yes_asked_before_the_suspicion_is_not_counted(parts, cfg: dict) -> None:
    """의심에 들기 전에 건 판독의 «예» 는 확정에 세지 않는다 — 순찰 판독과 누움이 겹친 경우."""
    fall = parts.monitor
    every = int(cfg["vision"]["vlm"]["patrol_interval_ms"])
    fall.ask(FRAME, T0)  # 첫 순찰 판독 시각을 잡는다
    fall.ask(FRAME, T0 + every)  # 순찰 판독을 건다
    assert fall.waiting
    since = T0 + every + 100
    fall.suspect("yolox", since)  # 판독이 도는 사이 누움으로 의심에 든다
    parts.vlm.slot = _down()
    fall.take_reading(since + 100)  # 의심 전에 건 «예» — 세지 않는다
    gap = int(cfg["fsm"]["fall_confirm_gap_ms"])
    # 뒤이은 «예» 는 간격을 지켜 하나 모자라게만 받는다 — 앞의 «예» 를 셌다면 확정이다.
    for n in range(1, int(cfg["fsm"]["fall_confirm_vlm_yes"])):
        fall.ask(FRAME, T0 + every + n * gap)
        parts.vlm.slot = _down()
        fall.take_reading(T0 + every + n * gap + 100)
    assert not fall.confirmed
    assert Event.PERSON_DOWN not in parts.applied


def test_a_dead_worker_frees_the_slot_without_a_verdict(parts, cfg: dict) -> None:
    """워커가 죽어 `take()` 가 `None` 이면 기다림만 풀고 아무 판정도 하지 않는다."""
    fall = parts.monitor
    every = int(cfg["vision"]["vlm"]["patrol_interval_ms"])
    fall.ask(FRAME, T0)
    fall.ask(FRAME, T0 + every)
    assert fall.waiting
    fall.take_reading(T0 + every + 100)
    assert not fall.waiting
    assert not fall.suspected


def test_losing_the_target_ends_the_suspicion_without_a_cooldown(parts) -> None:
    """`ALERT`·`TRACK` 을 떠나 끝난 의심에는 쿨다운이 없다 — 곧바로 다시 의심할 수 있다."""
    fall = parts.monitor
    fall.suspect("yolox", T0)
    assert parts.escalation.level is Level.L1
    parts.behavior.event(Event.TARGET_LOST, now_ms=T0 + 100)
    fall.watch(T0 + 100)
    assert not fall.suspected
    assert Event.FALL_RESOLVED not in parts.applied
    fall.suspect("yolox", T0 + 200)
    assert fall.suspected, "상실로 끝난 의심 뒤에 쿨다운이 걸렸다"


def test_no_fall_reading_is_asked_while_the_zone_reading_waits(parts) -> None:
    """워커는 하나다 — 구역 판독이 기다리는 동안에는 쓰러짐 판독을 걸지 않는다."""
    fall = parts.monitor
    fall.suspect("yolox", T0)
    parts.zone.waiting = True
    fall.ask(FRAME, T0)
    assert parts.vlm.submitted == []
    assert not fall.waiting
    parts.zone.waiting = False
    fall.ask(FRAME, T0 + 100)
    assert parts.vlm.submitted == [T0 + 100]
    assert fall.waiting
