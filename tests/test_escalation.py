"""대응 에스컬레이션 검증 (WBS 3.8.3 · 아키텍처 3.1).

**이 파일이 지키는 것은 정책이다.** 단계가 오르는 것은 여러 경로로 쉽게 확인되지만
*"내려오는 길이 있는가"* 는 시험하지 않으면 알 수 없다 — 그리고 그것이 없으면
시연이 그 자리에서 끝난다. 그래서 전 단계의 **해제 경로**를 전수로 확인하고,
반대로 **자동으로 풀려서는 안 되는 것**(L3·F)도 함께 못 박는다.

시간은 전부 주입한다. 10초 승격과 5초 상실을 실제로 기다리지 않는다.
"""

from __future__ import annotations

import pytest

from host.behavior.escalation import (
    LATCHED,
    LED_KEYS,
    RAISED_BY,
    Escalation,
    Level,
)

T0 = 1_000_000


@pytest.fixture
def esc(cfg: dict) -> Escalation:
    return Escalation(cfg)


def _see(esc: Escalation, now_ms: int, *, present: bool = True) -> None:
    """사람이 보이는 상태로 한 번 관측한다."""
    esc.note_person(present=present, last_seen_ms=now_ms, now_ms=now_ms)


def _stand_still(esc: Escalation, cfg: dict, *, start_ms: int = T0) -> int:
    """사람이 **계속 보이는 채로** 승격 시간을 흘려 L2 까지 올린다. 끝난 시각을 준다.

    ⚠️ 한 번만 관측하고 시간을 점프하면 승격(10초)보다 대상 상실(5초)이 먼저 걸려
    L0 으로 내려간다. 서 있는 사람과 지나간 사람은 다른 상황이며, 그 차이가 여기서
    드러난다.
    """
    hold_ms = cfg["escalation"]["l1_to_l2_hold_s"] * 1000
    now = start_ms
    for i in range(hold_ms // 500 + 1):
        now = start_ms + i * 500
        _see(esc, now)
        esc.tick(now)
    return now


# ── 표 자체 ────────────────────────────────────────────────
def test_led_covers_every_level(cfg: dict) -> None:
    """LED 매핑에 빠진 단계가 있으면 그 단계에서 표현이 죽는다."""
    assert set(LED_KEYS) == set(Level)
    leds = cfg["escalation"]["led"]
    for key in LED_KEYS.values():
        assert key in leds, f"config 에 없는 LED 키: {key}"


def test_only_l3_and_f_are_latched() -> None:
    """**자동 해제 금지는 두 단계뿐이다.** L1·L2 까지 래치되면 순찰이 멈춘다."""
    assert set(LATCHED) == {Level.L3, Level.F}


def test_event_table_raises_only_to_alarm_or_failsafe() -> None:
    """사건으로 올라가는 곳은 L3·F 다. L1·L2 는 **관측과 시간**이 정한다."""
    assert set(RAISED_BY.values()) == {Level.L3, Level.F}


def test_failsafe_outranks_alarm() -> None:
    """안전이 대응보다 앞선다 — 경보 중에도 페일세이프로 들어가야 한다."""
    assert Level.F.rank > Level.L3.rank > Level.L2.rank > Level.L1.rank > Level.L0.rank


def test_thresholds_come_from_config_not_code(cfg: dict) -> None:
    """설정을 고치면 동작이 바뀌어야 한다 (NFR-3①)."""
    tweaked = dict(cfg)
    tweaked["escalation"] = dict(cfg["escalation"], l1_to_l2_hold_s=1)
    esc = Escalation(tweaked)
    _see(esc, T0)
    esc.tick(T0 + 999)
    assert esc.level is Level.L1
    esc.tick(T0 + 1000)
    assert esc.level is Level.L2


# ── L0 → L1 ────────────────────────────────────────────────
def test_starts_at_patrol(esc: Escalation, cfg: dict) -> None:
    assert esc.level is Level.L0
    assert esc.presentation().led == cfg["escalation"]["led"]["l0_patrol"]
    assert esc.presentation().blink_hz is None


def test_confirmed_person_enters_observe(esc: Escalation, cfg: dict) -> None:
    _see(esc, T0)
    assert esc.level is Level.L1
    assert esc.presentation().led == cfg["escalation"]["led"]["l1_observe"]


def test_unconfirmed_person_does_not_raise(esc: Escalation) -> None:
    """게이트가 확정하지 않은 검출로는 올라가지 않는다 — 그것이 FR-3.2 의 목적이다."""
    esc.note_person(present=False, last_seen_ms=T0, now_ms=T0)
    assert esc.level is Level.L0


def test_tick_without_any_sighting_does_nothing(esc: Escalation) -> None:
    """기동 직후를 대상 상실로 보면 아무 일도 없이 단계가 내려갔다는 로그가 남는다."""
    esc.tick(T0 + 60_000)
    assert esc.level is Level.L0


# ── L1 해제 ────────────────────────────────────────────────
def test_observe_releases_after_target_lost(esc: Escalation, cfg: dict) -> None:
    lost_ms = cfg["fsm"]["target_lost_timeout_s"] * 1000
    _see(esc, T0)
    esc.tick(T0 + lost_ms - 1)
    assert esc.level is Level.L1
    esc.tick(T0 + lost_ms)
    assert esc.level is Level.L0


def test_continued_sighting_keeps_observe_alive(esc: Escalation) -> None:
    """계속 보이는 동안에는 5초가 지나도 해제되지 않는다."""
    for i in range(20):
        now = T0 + i * 500
        _see(esc, now)
        esc.tick(now)
    assert esc.level is Level.L1


# ── L1 → L2 ────────────────────────────────────────────────
def test_unauthenticated_hold_escalates_to_auth_request(esc: Escalation, cfg: dict) -> None:
    _stand_still(esc, cfg)
    assert esc.level is Level.L2
    assert esc.presentation().led == cfg["escalation"]["led"]["l2_auth_request"]


def test_authenticated_person_never_escalates(esc: Escalation, cfg: dict) -> None:
    """인증된 사람 앞에서 경보로 올라가면 시연이 못 진행된다."""
    _see(esc, T0)
    esc.note_event("AUTH_OK", T0)
    _stand_still(esc, cfg)
    assert esc.level is Level.L1  # 관찰은 계속하되 승격은 없다
    assert esc.authenticated is True


def test_authentication_releases_auth_request(esc: Escalation, cfg: dict) -> None:
    at = _stand_still(esc, cfg)
    assert esc.level is Level.L2
    esc.note_event("AUTH_OK", at)
    assert esc.level is Level.L0


# ── L2 는 함정이 아니다 ────────────────────────────────────
def test_walking_away_unauthenticated_becomes_alarm(esc: Escalation, cfg: dict) -> None:
    """**미인증 통과는 경비 대응 대상이다.** L0 으로 내리면 무시가 가장 이득이 된다."""
    lost_ms = cfg["fsm"]["target_lost_timeout_s"] * 1000
    at = _stand_still(esc, cfg)
    assert esc.level is Level.L2
    esc.tick(at + lost_ms - 1)
    assert esc.level is Level.L2
    esc.tick(at + lost_ms)
    assert esc.level is Level.L3


def test_auth_timeout_becomes_alarm(esc: Escalation) -> None:
    """`AUTH_WAIT` 30초 만료가 내는 사건이다 (FR-10.3 · fsm TIMERS)."""
    _see(esc, T0)
    esc.note_event("AUTH_FAILED", T0 + 1000)
    assert esc.level is Level.L3


# ── L3 ────────────────────────────────────────────────────
@pytest.mark.parametrize("event", ["AUTH_FAILED", "PPE_VIOLATION", "ZONE_CHANGED"])
def test_every_alarm_cause_reaches_l3(esc: Escalation, cfg: dict, event: str) -> None:
    """`ALERT` 에서의 PPE 위반처럼 **상태가 그대로인 사건도** 올라가야 한다."""
    esc.note_event(event, T0)
    assert esc.level is Level.L3
    view = esc.presentation()
    assert view.led == cfg["escalation"]["led"]["l3_alarm"]
    assert view.blink_hz == cfg["escalation"]["led"]["l3_blink_hz"]


def test_alarm_does_not_release_when_person_leaves(esc: Escalation, cfg: dict) -> None:
    """**여기가 이 파일의 핵심이다.** 사람이 사라졌다고 경보가 풀리면 규칙이 없는 것이다."""
    lost_ms = cfg["fsm"]["target_lost_timeout_s"] * 1000
    esc.note_event("AUTH_FAILED", T0)
    esc.note_person(present=False, last_seen_ms=T0, now_ms=T0 + lost_ms)
    esc.tick(T0 + lost_ms * 10)
    assert esc.level is Level.L3


def test_alarm_does_not_release_by_authentication(esc: Escalation) -> None:
    """원인별 해제를 만들지 않는다 — PPE·물체 변화에는 인증할 대상이 없다."""
    esc.note_event("AUTH_FAILED", T0)
    esc.note_event("AUTH_OK", T0 + 1000)
    assert esc.level is Level.L3
    assert esc.authenticated is True  # 표시는 하되 해제하지 않는다


def test_manual_confirmation_releases_alarm(esc: Escalation) -> None:
    esc.note_event("PPE_VIOLATION", T0)
    assert esc.confirm_alarm(T0 + 5000) is True
    assert esc.level is Level.L0


def test_confirming_alarm_forgets_authentication(esc: Escalation) -> None:
    """다음 사람은 다시 인증해야 한다."""
    esc.note_event("AUTH_OK", T0)
    esc.note_event("PPE_VIOLATION", T0)
    esc.confirm_alarm(T0 + 1)
    assert esc.authenticated is False


def test_confirming_alarm_is_a_no_op_elsewhere(esc: Escalation) -> None:
    """확인 키를 아무 때나 눌러도 단계가 흐트러지지 않는다."""
    _see(esc, T0)
    assert esc.confirm_alarm(T0) is False
    assert esc.level is Level.L1


# ── F ─────────────────────────────────────────────────────
@pytest.mark.parametrize("event", ["ESTOP", "LINK_LOST", "ONBOARD_FAILSAFE"])
def test_every_failsafe_cause_reaches_f(esc: Escalation, cfg: dict, event: str) -> None:
    esc.note_event(event, T0)
    assert esc.level is Level.F
    assert esc.presentation().led == cfg["escalation"]["led"]["failsafe"]
    assert esc.presentation().blink_hz is None


def test_failsafe_wins_over_alarm(esc: Escalation) -> None:
    esc.note_event("AUTH_FAILED", T0)
    esc.note_event("ESTOP", T0 + 100)
    assert esc.level is Level.F


def test_alarm_event_during_failsafe_is_ignored(esc: Escalation) -> None:
    """로봇이 멈춰 있는 동안 대응 단계를 내리는 사건은 없어야 한다."""
    esc.note_event("ESTOP", T0)
    esc.note_event("AUTH_FAILED", T0 + 100)
    assert esc.level is Level.F


def test_failsafe_does_not_release_automatically(esc: Escalation) -> None:
    esc.note_event("LINK_LOST", T0)
    esc.tick(T0 + 600_000)
    assert esc.level is Level.F


def test_robot_unlatch_releases_failsafe(esc: Escalation) -> None:
    """`RESET_CONFIRMED` 는 로봇이 실제로 래치를 풀었다는 확인이다 (PROTOCOL 2절)."""
    esc.note_event("ESTOP", T0)
    esc.note_event("RESET_CONFIRMED", T0 + 5000)
    assert esc.level is Level.L0


def test_confirming_alarm_does_not_release_failsafe(esc: Escalation) -> None:
    """경보를 끄려는 조작이 페일세이프까지 풀면 로봇이 넘어진 채로 다시 움직인다."""
    esc.note_event("ESTOP", T0)
    assert esc.confirm_alarm(T0 + 100) is False
    assert esc.level is Level.F


def test_estop_cannot_be_used_to_clear_an_alarm(esc: Escalation) -> None:
    """**비상정지 → 해제로 경보를 지우는 길을 막는다.** F 를 풀면 L3 로 돌아온다."""
    esc.note_event("AUTH_FAILED", T0)
    esc.note_event("ESTOP", T0 + 100)
    assert esc.alarm_pending is True
    esc.note_event("RESET_CONFIRMED", T0 + 5000)
    assert esc.level is Level.L3
    assert esc.confirm_alarm(T0 + 6000) is True
    assert esc.level is Level.L0


def test_failsafe_without_alarm_returns_to_patrol(esc: Escalation) -> None:
    """경보가 없었으면 L3 로 되돌리지 않는다 — 없던 경보를 만들어내면 안 된다."""
    _see(esc, T0)
    esc.note_event("LINK_LOST", T0 + 100)
    assert esc.alarm_pending is False
    esc.note_event("RESET_CONFIRMED", T0 + 5000)
    assert esc.level is Level.L0


# ── 표현 ───────────────────────────────────────────────────
def test_sound_ids_are_absent_until_flashed(esc: Escalation) -> None:
    """WonderEcho 문구 ID 는 플래싱 후에 채운다 (OI-10/11). **없는 것을 없다고 말한다.**"""
    esc.note_event("AUTH_FAILED", T0)
    assert esc.presentation().sound_id is None


def test_unknown_event_is_ignored(esc: Escalation) -> None:
    """사건은 바깥에서 온다 — 표에 없는 것이 오는 것은 정상이다."""
    esc.note_event("SCAN_DUE", T0)
    assert esc.level is Level.L0


def test_rejected_reset_does_not_release_failsafe(esc: Escalation) -> None:
    """⚠️ **로봇 래치가 걸려 있으면 FSM 이 `RESET_CONFIRMED` 를 거부한다**
    (`LATCH_GUARDED`). 그때 F 를 풀면 로봇은 잠긴 채 호스트만 풀린다."""
    esc.note_event("ESTOP", T0)
    esc.note_event("RESET_CONFIRMED", T0 + 1000, accepted=False)
    assert esc.level is Level.F


def test_rejected_event_still_raises(esc: Escalation) -> None:
    """`ALERT` 에서의 PPE 위반은 전이표에 없다 — 그것이 에스컬레이션 소관인 이유다."""
    esc.note_event("PPE_VIOLATION", T0, accepted=False)
    assert esc.level is Level.L3
