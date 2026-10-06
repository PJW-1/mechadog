"""전이 가드 및 타이머 검증 (WBS 3.4.3 · FR-2.4 · FR-3.7).

가드도 타이머도 **표에 담겨 있어서 전수로 돌려볼 수 있다.** 조건을 `if` 로
흩어놓으면 "어떤 타이머가 존재하지 않는 전이를 겨누고 있는지" 를 세는 것 자체가
불가능해진다.
"""

from __future__ import annotations

from conftest import FakeClock

from host.behavior.commander import Commander
from host.behavior.fsm import (
    DIRECTIVES,
    LATCH_GUARDED,
    TIMERS,
    TRANSITIONS,
    Behavior,
    Event,
    Fsm,
    behavior_from_config,
)
from host.common import protocol as p


def _behavior(clock: FakeClock, cfg: dict) -> Behavior:
    return behavior_from_config(Commander(p.CommandEncoder(clock=clock)), cfg)


# ── 표 자체 ──────────────────────────────────────────────────
def test_every_timer_names_a_real_transition() -> None:
    """**타이머가 없는 전이를 겨누면 조용히 아무 일도 안 난다.**

    상태 이름을 오타 내거나 전이를 지웠을 때 여기서 걸린다.
    """
    pairs = {(t.src, t.event) for t in TRANSITIONS}
    for timer in TIMERS:
        assert timer.state in DIRECTIVES, f"{timer.state} 는 상태가 아니다"
        assert (timer.state, timer.event) in pairs, f"{timer.state} + {timer.event} 전이가 없다"


def test_every_timer_value_exists_in_config(cfg: dict) -> None:
    """타이머 임계는 코드가 아니라 `config.yaml` 에서 온다 (NFR-3①)."""
    for timer in TIMERS:
        node: object = cfg
        for key in timer.path:
            assert isinstance(node, dict) and key in node, f"설정에 {'.'.join(timer.path)} 가 없다"
            node = node[key]
        assert isinstance(node, int | float) and node > 0


def test_latch_guard_covers_only_recovery_events() -> None:
    """래치로 막을 것은 **해제 계열 사건뿐**이다.

    안전으로 *들어가는* 사건(`ESTOP`·`ONBOARD_FAILSAFE`)을 막으면 위험한 쪽으로
    틀린다. 막는 것은 나가는 문 하나여야 한다.
    """
    assert set(LATCH_GUARDED) == {Event.RESET_CONFIRMED}


# ── ⚠️ 로봇이 FAILSAFE 인 동안 호스트는 나올 수 없다 ──────────
def test_host_cannot_leave_failsafe_while_the_robot_reports_it(clock, cfg) -> None:
    """**이것이 없으면 관제 화면이 거짓을 말한다.**

    로봇이 전도로 `FAILSAFE` 를 10Hz 로 보고하는 중에 조작자가 리셋을 누르면
    호스트만 `IDLE` 로 나온다. 같은 상태의 반복 보고는 사건을 재발행하지 않으므로
    **그 뒤 어떤 텔레메트리도 어긋남을 고치지 못한다.**
    """
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.note_robot_latch(True)
    b.event(Event.ONBOARD_FAILSAFE, now_ms=clock.ms)
    assert b.state == "FAILSAFE"

    assert b.event(Event.RESET_CONFIRMED, now_ms=clock.ms) is False
    assert b.state == "FAILSAFE", "로봇의 래치가 아직 걸려 있다"


def test_reset_works_once_the_robot_reports_recovery(clock, cfg) -> None:
    """페일세이프를 벗어날지는 **로봇이 정하고 호스트가 따라간다** (아키텍처 1.2)."""
    b = _behavior(clock, cfg)
    b.note_robot_latch(True)
    b.event(Event.ONBOARD_FAILSAFE, now_ms=clock.ms)
    b.note_robot_latch(False)
    assert b.robot_latched is False
    assert b.event(Event.RESET_CONFIRMED, now_ms=clock.ms) is True
    assert b.state == "IDLE"


def test_echoed_failsafe_state_does_not_lock_the_host(clock, cfg) -> None:
    """⚠️ **`state` 로 막으면 호스트가 스스로를 영구히 잠근다.**

    `FAILSAFE` 는 `STATE` 명령으로도 내려간다. 그래서 로봇이 되돌려준
    `state="FAILSAFE"` 는 *우리가 알려준 것의 반향*일 수 있고 그것과 로봇 자신의
    판정을 구분할 수 없다. 반향을 근거로 잠그면 해제 수단이 사라진다.

    래치는 반향되지 않으므로 이 함정을 피한다 (PROTOCOL 2절).
    """
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.LINK_LOST, now_ms=clock.ms)  # 링크 두절로 호스트가 FAILSAFE
    assert b.state == "FAILSAFE"

    # 링크가 살아났고 로봇은 래치가 없다고 보고한다. 다만 `state` 는 우리가 알려준
    # FAILSAFE 를 그대로 되돌려준다.
    b.note_onboard_state("FAILSAFE")
    b.note_robot_latch(False)
    assert b.event(Event.RESET_CONFIRMED, now_ms=clock.ms) is True
    assert b.state == "IDLE", "반향은 잠금의 근거가 아니다"


def test_unreported_latch_does_not_block(clock, cfg) -> None:
    """래치를 보고하지 않는 구형 펌웨어에서는 막지 않는다.

    알 수 없는 것을 근거로 잠그면 해제 수단이 없어진다. 이 조합은 규약이
    *"실기 운용 전 송수신을 함께 갱신한다"* 로 이미 금지했다.
    """
    b = _behavior(clock, cfg)
    b.event(Event.ONBOARD_FAILSAFE, now_ms=clock.ms)
    assert b.robot_latched is None
    assert b.event(Event.RESET_CONFIRMED, now_ms=clock.ms) is True


def test_host_only_state_is_not_taken_as_an_onboard_report(clock, cfg) -> None:
    """`ALERT` 는 우리가 내려보낸 값이 되돌아온 것이라 가드의 근거가 아니다."""
    b = _behavior(clock, cfg)
    b.note_onboard_state("FAILSAFE")
    b.note_onboard_state("ALERT")
    assert b.onboard_state == "FAILSAFE"


def test_estop_is_not_blocked_by_any_guard(clock, cfg) -> None:
    """사람이 누른 비상정지는 어떤 가드도 막지 않는다 (NFR-2.7)."""
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.note_robot_latch(True)
    assert b.event(Event.ESTOP, now_ms=clock.ms) is True
    assert b.state == "FAILSAFE"


# ── 상태 타이머 ──────────────────────────────────────────────
def test_patrol_timer_opens_a_scan(clock, cfg) -> None:
    """순찰 10초마다 정지해 상체 스캔 (FR-2.4)."""
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    wait = int(cfg["fsm"]["patrol_scan_interval_s"]) * 1000
    b.tick(clock.advance(wait - 100))
    assert b.state == "PATROL", "아직 이르다"
    b.tick(clock.advance(100))
    assert b.state == "SCAN"


def test_scan_timer_returns_to_patrol(clock, cfg) -> None:
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.SCAN_DUE, now_ms=clock.ms)
    assert b.state == "SCAN"
    b.tick(clock.advance(int(cfg["fsm"]["scan_duration_s"]) * 1000))
    assert b.state == "PATROL"


def test_timer_restarts_on_reentry(clock, cfg) -> None:
    """떠났다 돌아오면 처음부터 다시 센다 — 남은 시간을 물려받지 않는다."""
    b = _behavior(clock, cfg)
    interval = int(cfg["fsm"]["patrol_scan_interval_s"]) * 1000
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.tick(clock.advance(interval - 500))
    b.event(Event.ONBOARD_AVOID, now_ms=clock.ms)  # 잠깐 회피로 빠진다
    b.event(Event.AVOID_CLEARED, now_ms=clock.ms)
    assert b.state == "PATROL"
    b.tick(clock.advance(600))
    assert b.state == "PATROL", "재진입했으므로 남은 500ms 를 물려받지 않는다"
    b.tick(clock.advance(interval))
    assert b.state == "SCAN"


def test_blocked_timer_fires_only_once(clock, cfg) -> None:
    """**전이가 막혀 있어도 매 틱 재시도하지 않는다.**

    재시도하면 로그가 초당 10건씩 쌓인다 — 같은 사건을 반복 발행하는 것이 문제라는
    것을 수신기 쪽에서 이미 겪었다.
    """
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.PERSON_FOUND, now_ms=clock.ms)
    b.event(Event.AUTH_REQUIRED, now_ms=clock.ms)
    assert b.state == "AUTH_WAIT"

    seen: list[Event] = []
    original = b.fsm.handle

    def spy(event: Event) -> bool:
        seen.append(event)
        return original(event)

    b.fsm.handle = spy  # type: ignore[method-assign]
    # 인증 실패는 ALERT 로 가고, ALERT 에서는 이 타이머가 없다.
    # 그래도 20틱 동안 사건이 두 번 이상 나가면 안 된다.
    timeout = int(cfg["auth"]["timeout_s"]) * 1000
    for _ in range(20):
        b.tick(clock.advance(timeout))
    assert seen.count(Event.AUTH_FAILED) == 1, "한 번만 발화한다"


# ── 타이머 유예 (ADR-37 · FR-10.3) ────────────────────────────
#
# 음성 인증은 «말이 끝난 시각» 과 «판정이 도착한 시각» 이 다르다 — 녹음(최대
# 15초)·무음 1초·전사가 직렬이라, 창 안에서 말해도 판정이 `timeout_s` 를 넘겨
# 도착할 수 있다 (2026-09-22 실기: 통과 2건이 24초·18초를 썼고 창은 30초였다).
# 그래서 «판정이 오는 중» 을 받아 마감을 **상한 안에서** 미룬다.
#
# ⚠️ 엔진은 **무엇을 왜 미루는지 모른다.** 그 정책은 런타임에 있다
# (`test_command_api.py` 의 인증 유예 절).


def _auth_wait(clock, cfg) -> Behavior:
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.PERSON_FOUND, now_ms=clock.ms)
    b.event(Event.AUTH_REQUIRED, now_ms=clock.ms)
    assert b.state == "AUTH_WAIT"
    return b


def test_deferring_pushes_the_deadline_back(clock, cfg) -> None:
    """미룬 만큼 **늦게** 발화한다 — 마감을 없애는 것이 아니라 옮기는 것이다."""
    b = _auth_wait(clock, cfg)
    timeout = int(cfg["auth"]["timeout_s"]) * 1000

    assert b.defer_timer(by_ms=5000, cap_ms=5000) == 5000

    b.tick(clock.advance(timeout))
    assert b.state == "AUTH_WAIT", "원래 마감에는 아직 울리지 않는다"
    b.tick(clock.advance(5000))
    assert b.state == "ALERT", "미룬 시간이 지나면 울린다"


def test_deferring_is_capped(clock, cfg) -> None:
    """**상한이 곧 안전장치다.** 계속 미루라고 해도 상한까지만 준다 —
    아니면 신호만 보내 경보를 영영 재울 수 있다."""
    b = _auth_wait(clock, cfg)

    assert b.defer_timer(by_ms=4000, cap_ms=6000) == 4000
    assert b.defer_timer(by_ms=4000, cap_ms=6000) == 2000, "남은 몫만 준다"
    assert b.defer_timer(by_ms=4000, cap_ms=6000) == 0, "상한에 닿으면 더 없다"
    assert b.timer_deferred_ms == 6000

    timeout = int(cfg["auth"]["timeout_s"]) * 1000
    b.tick(clock.advance(timeout + 6000))
    assert b.state == "ALERT", "상한을 넘겨 미뤄지지 않는다"


def test_deferring_resets_when_the_state_changes(clock, cfg) -> None:
    """유예는 **그 체류 한 번의 것**이다 — 다시 들어오면 0 에서 시작한다.

    남겨 두면 앞 대기에서 산 유예로 새 대기의 마감이 밀린다.
    """
    b = _auth_wait(clock, cfg)
    b.defer_timer(by_ms=9000, cap_ms=9000)
    assert b.timer_deferred_ms == 9000

    b.event(Event.AUTH_OK, now_ms=clock.ms)
    assert b.state == "PATROL"
    assert b.timer_deferred_ms == 0

    b.event(Event.PERSON_FOUND, now_ms=clock.ms)
    b.event(Event.AUTH_REQUIRED, now_ms=clock.ms)
    timeout = int(cfg["auth"]["timeout_s"]) * 1000
    b.tick(clock.advance(timeout))
    assert b.state == "ALERT", "새 대기는 제 시각에 울린다"


def test_deferring_nothing_is_a_no_op(clock, cfg) -> None:
    """0 이나 음수는 **조용히 아무 일도 하지 않는다** — 설정이 비어도 터지지 않게."""
    b = _auth_wait(clock, cfg)
    assert b.defer_timer(by_ms=0, cap_ms=5000) == 0
    assert b.defer_timer(by_ms=5000, cap_ms=0) == 0
    assert b.defer_timer(by_ms=-1000, cap_ms=5000) == 0
    assert b.timer_deferred_ms == 0


# ── 대상 상실 감시 ───────────────────────────────────────────
def test_target_lost_after_the_configured_timeout(clock, cfg) -> None:
    """5초간 미검출이면 순찰로 복귀 (FR-3.7)."""
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.note_target(clock.ms)
    b.event(Event.PERSON_FOUND, now_ms=clock.ms)
    assert b.state == "ALERT"
    b.tick(clock.advance(int(cfg["fsm"]["target_lost_timeout_s"]) * 1000))
    assert b.state == "PATROL"


def test_seeing_the_target_again_refreshes_the_watch(clock, cfg) -> None:
    """**사람이 계속 서 있으면 ALERT 를 떠나지 않는다.**

    상태 타이머로 만들면 5초마다 떠난다 — 그래서 이것만 감시로 두었다.
    """
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.note_target(clock.ms)
    b.event(Event.PERSON_FOUND, now_ms=clock.ms)
    for _ in range(30):
        b.note_target(clock.advance(500))
        b.tick(clock.ms)
    assert b.state == "ALERT"


def test_target_watch_is_silent_where_the_table_has_no_transition(clock, cfg) -> None:
    """`PATROL` 에는 `TARGET_LOST` 전이가 없다 — 감시가 헛발질하지 않는다."""
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.note_target(clock.ms)
    assert b.fsm.can(Event.TARGET_LOST) is False
    b.tick(clock.advance(int(cfg["fsm"]["target_lost_timeout_s"]) * 1000 * 3))
    assert b.state in {"PATROL", "SCAN"}


def test_target_watch_needs_a_first_sighting(clock, cfg) -> None:
    """한 번도 못 봤으면 감시하지 않는다 — 켜는 순간 상실로 보면 안 된다."""
    b = _behavior(clock, cfg)
    b.event(Event.START_PATROL, now_ms=clock.ms)
    b.event(Event.PERSON_FOUND, now_ms=clock.ms)
    b.tick(clock.advance(int(cfg["fsm"]["target_lost_timeout_s"]) * 1000 * 2))
    assert b.state == "ALERT"


# ── can() 은 handle() 과 같은 답을 준다 ──────────────────────
def test_can_agrees_with_handle_for_every_state_and_event() -> None:
    """어긋나면 감시자가 잘못된 판단을 한다 — 조회를 한 곳에 모은 이유다."""
    for state in DIRECTIVES:
        for event in Event:
            probe = Fsm(initial=state)
            expected = probe.can(event)
            assert probe.handle(event) is expected, f"{state} + {event.name}"


def test_can_respects_the_failsafe_seal() -> None:
    f = Fsm(initial="FAILSAFE")
    assert f.can(Event.RESET_CONFIRMED) is True
    assert f.can(Event.START_PATROL) is False
