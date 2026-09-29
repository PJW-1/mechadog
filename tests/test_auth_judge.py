"""경비 인증 판정 단독 검증 — `AuthJudge` (FR-10 · ADR-28 · ADR-37).

암구호와 함께 가는 시나리오는 `test_voice_auth.py`, 틱을 거치는 단계 축 시나리오는
`test_runtime.py` 에 있다. 여기서는 공개 API 로만 닿는 조각 — 사원증 통과 뒤 순찰 재개
대기, 미등록 사원증 소진, 모드 게이트, L2 에서의 인증 요구, 세션 초기화 — 를 본다.
"""

from __future__ import annotations

from types import SimpleNamespace

from host.behavior.auth_judge import AuthJudge
from host.behavior.commander import Commander
from host.behavior.escalation import Escalation, Level
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.mission import Mission
from host.behavior.voice_auth import VoiceAuthWindow
from host.vision.badge import Marker


def _judge(cfg, clock, *, mode="guard", escalation=None, route=()):
    """암구호 단독 · 현장 인증 설정의 판정기 — `apply` 가 받은 사건을 함께 돌려준다.

    `cfg` 는 세션 범위라 다른 시험이 바꿔 두기도 한다 — 쓰는 키는 여기서 못 박는다.
    """
    cfg = dict(cfg, auth=dict(cfg["auth"], require_both=False, bind_to_track_id=False))
    behavior = behavior_from_config(Commander(), cfg)
    mission = Mission(cfg, mode=mode)
    applied: list[Event] = []

    def apply(event: Event, now_ms: int) -> bool:
        applied.append(event)
        return behavior.event(event, now_ms=now_ms)

    voice = VoiceAuthWindow(cfg, behavior=behavior, mission=mission, apply=apply, clock=clock)
    judge = AuthJudge(
        cfg,
        behavior=behavior,
        mission=mission,
        escalation=escalation if escalation is not None else Escalation(cfg),
        voice=voice,
        apply=apply,
    )
    for event in route:
        behavior.event(event, now_ms=clock.ms)
    return judge, behavior, applied


AUTH_WAIT_ROUTE = (Event.START_PATROL, Event.PERSON_FOUND, Event.AUTH_REQUIRED)


def _frame(*marker_ids: int) -> SimpleNamespace:
    markers = tuple(Marker(marker_id=m, center=(320.0, 300.0)) for m in marker_ids)
    return SimpleNamespace(tracks=(), markers=markers)


def _badge(cfg) -> int:
    return int(next(iter(cfg["auth"]["badge_marker_map"])))


def test_badge_grants_and_holds_patrol_for_resume_delay(cfg, clock):
    """등록 사원증 → `AUTH_OK` · 순찰 복귀, 그 뒤 `resume_delay_ms` 동안 순찰을 멈춰 둔다."""
    judge, behavior, applied = _judge(cfg, clock, route=AUTH_WAIT_ROUTE)
    delay_ms = int(cfg["auth"].get("resume_delay_ms", 3500))
    now = clock.ms

    judge.judge(_frame(_badge(cfg)), now)

    assert applied == [Event.AUTH_OK]
    assert behavior.state == "PATROL"
    assert judge.authenticator.holder(0, now) is not None
    assert judge.holds_patrol(now + delay_ms - 1)
    assert not judge.holds_patrol(now + delay_ms)


def test_no_resume_wait_before_any_grant(cfg, clock):
    judge, _behavior, _applied = _judge(cfg, clock)
    assert not judge.holds_patrol(clock.ms)


def test_two_unknown_badges_exhaust_attempts(cfg, clock):
    """미등록 사원증 두 장(각각 안정 검출) → `AUTH_FAILED` (FR-10.3)."""
    judge, _behavior, applied = _judge(cfg, clock, route=AUTH_WAIT_ROUTE)
    for marker_id in (90, 91):
        for _ in range(int(cfg["auth"].get("unknown_marker_min_frames", 3))):
            judge.judge(_frame(marker_id), clock.advance(100))

    assert applied == [Event.AUTH_FAILED]


def test_factory_mode_never_judges(cfg, clock):
    """인증이 없는 모드에서는 사원증이 스쳐도 사건도 세션도 없다 (FR-11.1)."""
    judge, _behavior, applied = _judge(cfg, clock, mode="factory", route=AUTH_WAIT_ROUTE)

    judge.judge(_frame(_badge(cfg)), clock.ms)

    assert applied == []
    assert judge.authenticator.holder(0, clock.ms) is None


def test_request_only_at_l2_and_only_once(cfg, clock):
    """L2 에서만 `AUTH_REQUIRED` 를 내고, 이미 `AUTH_WAIT` 면 다시 내지 않는다."""
    escalation = SimpleNamespace(level=Level.L1)
    judge, behavior, applied = _judge(
        cfg, clock, escalation=escalation, route=(Event.START_PATROL, Event.PERSON_FOUND)
    )

    judge.request(clock.ms)
    assert applied == []

    escalation.level = Level.L2
    judge.request(clock.ms)
    judge.request(clock.ms)
    assert applied == [Event.AUTH_REQUIRED]
    assert behavior.state == "AUTH_WAIT"


def test_reset_forgets_the_badge_session(cfg, clock):
    judge, _behavior, _applied = _judge(cfg, clock, route=AUTH_WAIT_ROUTE)
    judge.judge(_frame(_badge(cfg)), clock.ms)
    assert judge.authenticator.holder(0, clock.ms) is not None

    judge.reset()

    assert judge.authenticator.holder(0, clock.ms) is None
