"""음성 암구호 인증 창 검증 — `VoiceAuthWindow` (FR-10.2.4 · FR-10.3 · ADR-37).

판정은 대시보드 명령(`CommandService.auth`)으로 넣고, 창을 여닫는 전이는 런타임을
거친다 — 실제 배선(`dashboard_wiring`)과 같은 조합이다.
"""

from __future__ import annotations

from types import SimpleNamespace

from host.behavior.commander import Commander
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.mission import Mission
from host.behavior.voice_auth import VoiceAuthWindow
from host.dashboard.commands import CommandService
from host.runtime import Runtime
from host.vision.badge import Marker

AUTH_WAIT_ROUTE: tuple[Event, ...] = (
    Event.START_PATROL,
    Event.PERSON_FOUND,
    Event.AUTH_REQUIRED,
)


def _window(cfg, clock):
    """런타임 없이 창 하나(암구호 단독) — `apply` 가 받은 사건을 함께 돌려준다."""
    cfg = dict(cfg, auth=dict(cfg["auth"], require_both=False))
    behavior = behavior_from_config(Commander(), cfg)
    applied: list[Event] = []

    def apply(event: Event, now_ms: int) -> bool:
        applied.append(event)
        return behavior.event(event, now_ms=now_ms)

    window = VoiceAuthWindow(cfg, behavior=behavior, mission=Mission(cfg), apply=apply, clock=clock)
    for event in AUTH_WAIT_ROUTE:
        behavior.event(event, now_ms=clock.ms)
    window.open(clock.ms)
    return window, behavior, applied


def test_window_counts_attempts_and_raises_auth_failed_when_exhausted(cfg, clock):
    """창 단독으로도 `max_attempts` 를 채울 때만 `AUTH_FAILED` 를 낸다."""
    window, behavior, applied = _window(cfg, clock)

    assert window.note_verdict(False) == (True, "암구호가 일치하지 않는다 — 1회 남았다")
    assert applied == []
    accepted, detail = window.note_verdict(False)

    assert applied == [Event.AUTH_FAILED]
    assert accepted is True and detail == "시도 횟수를 소진했다"
    assert behavior.state == "ALERT"


class _ClosesAfterFirstRead(VoiceAuthWindow):
    """`opened_ms` 를 처음 한 번 읽은 뒤 대시보드 스레드가 창을 닫은 것처럼 보인다."""

    @property
    def opened_ms(self) -> int | None:
        value, self._next = self._next, None
        return value

    @opened_ms.setter
    def opened_ms(self, value: int | None) -> None:
        self._next = value


def test_stale_utterance_survives_the_window_closing_mid_check(cfg, clock):
    """창보다 앞선 발화를 판정한 직후 창이 닫혀도 **예외 없이** 버린다.

    열린 시각을 판정과 로그에서 따로 읽으면, 그 사이에 닫힌 창의 `None` 에서
    `behind_ms` 를 빼다 `TypeError` 가 난다. 한 번 읽은 값으로 판정하고 기록한다.
    """
    cfg = dict(cfg, auth=dict(cfg["auth"], require_both=False))
    behavior = behavior_from_config(Commander(), cfg)
    for event in AUTH_WAIT_ROUTE:
        behavior.event(event, now_ms=clock.ms)
    window = _ClosesAfterFirstRead(
        cfg, behavior=behavior, mission=Mission(cfg), apply=behavior.event, clock=clock
    )
    window.open(clock.ms)

    assert window.note_verdict(True, captured_at_ms=clock.ms - 1) == (
        True,
        "인증 창이 열리기 전에 녹음된 발화다 — 다시 말해 주세요",
    )
    window.opened_ms = clock.ms
    assert window.note_listening(captured_at_ms=clock.ms - 1)[1] == (
        "인증 창이 열리기 전에 시작된 발화다 — 유예하지 않는다"
    )


def test_window_grants_for_session_valid_s_and_close_clears_open_time(cfg, clock):
    """일치가 받아들여지면 `session_valid_s` 동안 허가다. 닫으면 열린 시각을 지운다."""
    valid_ms = int(cfg["auth"]["session_valid_s"]) * 1000
    window, _behavior, applied = _window(cfg, clock)

    assert window.note_verdict(True, captured_at_ms=clock.ms) == (True, "인증 결과를 반영했다")
    assert applied == [Event.AUTH_OK]
    assert window.granted(clock.ms + valid_ms - 1) is True
    assert window.granted(clock.ms + valid_ms) is False

    window.close()
    assert window.opened_ms is None


# ── 음성 암구호 시도 횟수 (FR-10.3 · 2026-09-21 실기) ──────────────
#
# 실기 3라운드에서 **암구호를 한 번 틀리자 곧바로 L3 경보**가 됐다. 설정은
# `auth.max_attempts: 2` 인데 그 값을 읽는 곳이 사원증 인증기뿐이어서, 음성
# 경로는 첫 불일치가 그대로 `AUTH_FAILED` 로 나갔다. 말은 사원증과 달리 잘못
# 들릴 수 있으므로 재시도 여유가 있어야 한다 — 그것이 이 값의 존재 이유다.


def _voice_auth_service(cfg, clock, *, require_both=False):
    """런타임이 붙은 서비스 — 창을 여닫는 것이 `Runtime._apply` 라 이 조합이어야 한다."""
    config = dict(cfg, auth=dict(cfg["auth"], require_both=require_both))
    runtime = Runtime(config, device_id="mechdog-01", clock=clock)
    svc = CommandService(
        runtime.behavior,
        runtime.commander,
        lambda _line: None,
        apply_event=runtime.apply_external,
        note_voice_auth=runtime.note_voice_auth,
        note_voice_listening=runtime.note_voice_listening,
    )
    for event in AUTH_WAIT_ROUTE:
        runtime.apply_external(event)
    assert runtime.behavior.state == "AUTH_WAIT"
    return svc, runtime


def test_voice_auth_first_mismatch_keeps_waiting(cfg, clock):
    """**첫 불일치로 경보를 울리지 않는다.** 아직 한 번 남았다."""
    svc, runtime = _voice_auth_service(cfg, clock)

    result = svc.auth("fail")

    assert runtime.behavior.state == "AUTH_WAIT", "재시도 여유가 남아 있으면 대기를 유지한다"
    # 단계는 사건으로만 움직이므로 이 경로에서는 올라가지 않는다 — 중요한 것은
    # **L3 로 뛰지 않았다**는 것이다.
    assert runtime.escalation.level.value != "L3", "L3 경보는 소진 뒤에만 울린다"
    # 전달은 성공했다 — 거짓으로 돌려주면 파이프라인이 "전달하지 못했습니다" 라고
    # 말해, 사람이 다시 말할 이유를 잃는다.
    assert result.accepted is True
    assert "1회 남았다" in result.detail


def test_voice_auth_exhausts_at_max_attempts(cfg, clock):
    """`max_attempts` 를 채우면 그때 `AUTH_FAILED` — L3 경보다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    assert cfg["auth"]["max_attempts"] == 2

    svc.auth("fail")
    result = svc.auth("fail")

    assert runtime.behavior.state == "ALERT"
    assert runtime.escalation.level.value == "L3"
    assert result.accepted is True


def test_voice_auth_retry_can_still_pass(cfg, clock):
    """틀린 뒤 **다시 말해서 통과**할 수 있어야 한다 — 이것이 재시도의 목적이다."""
    svc, runtime = _voice_auth_service(cfg, clock)

    svc.auth("fail")
    result = svc.auth("ok")

    assert result.accepted is True
    assert runtime.behavior.state == "PATROL"
    assert runtime.escalation.level.value == "L0"


def test_voice_auth_attempts_reset_on_each_auth_wait(cfg, clock):
    """**다음 대기는 0 부터 센다.** 앞사람의 실패가 넘어오면 처음 말하는 사람이
    한 마디에 소진된다."""
    svc, runtime = _voice_auth_service(cfg, clock)

    svc.auth("fail")  # 1회 소모하고
    svc.auth("ok")  # 통과해서 AUTH_WAIT 를 떠난다
    assert runtime.behavior.state == "PATROL"

    runtime.apply_external(Event.PERSON_FOUND)
    runtime.apply_external(Event.AUTH_REQUIRED)
    assert runtime.behavior.state == "AUTH_WAIT"

    result = svc.auth("fail")
    assert runtime.behavior.state == "AUTH_WAIT", "새 대기의 첫 실패가 소진이 되면 안 된다"
    assert "1회 남았다" in result.detail


def test_voice_auth_outside_auth_wait_is_not_counted(cfg, clock):
    """대기 중이 아닌 실패는 **세지도 않는다** — 세면 엉뚱한 대기에 쌓인다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    svc.auth("ok")
    assert runtime.behavior.state == "PATROL"

    result = svc.auth("fail")
    assert result.accepted is False
    assert "AUTH_WAIT" in result.detail

    runtime.apply_external(Event.PERSON_FOUND)
    runtime.apply_external(Event.AUTH_REQUIRED)
    svc.auth("fail")
    assert runtime.behavior.state == "AUTH_WAIT", "밖에서 온 실패가 시도로 쌓이면 안 된다"


# ── 음성 인증 유효 시간 (FR-10.2.4 · 2026-09-21 실기) ──────────────
#
# 실기에서 암구호로 통과한 직후 **다시 인증을 요구**했다. 음성은 `Authenticator`
# 세션을 만들지 않는데 `AuthJudge.judge` 는 세션이 붙은 트랙만 보고 인증 여부를
# 판정해서, 통과한 다음 틱에 `note_authentication_lost()` 가 불렸다.
# `session_valid_s` 가 음성 경로에 적용된 적이 한 번도 없었다.
#
# 허가를 트랙에 붙이지 않는 것은 타협이 아니라 판단이다 — 마이크는 로봇 몸통에
# 하나뿐이라 **그 소리가 누구 목소리인지 모른다**. 없는 근거로 트랙을 고르면
# 틀렸을 때 엉뚱한 사람이 허가를 받는다.


def _frame(tracks: tuple = (), markers: tuple = ()) -> SimpleNamespace:
    """`AuthJudge.judge` 가 만지는 두 칸만 있는 가짜 프레임."""
    return SimpleNamespace(tracks=tuple(tracks), markers=tuple(markers))


def test_guard_requires_passphrase_then_new_badge(cfg, clock):
    svc, runtime = _voice_auth_service(cfg, clock, require_both=True)
    badge_id = next(iter(cfg["auth"]["badge_marker_map"]))
    badge = Marker(marker_id=int(badge_id), center=(320.0, 300.0))

    runtime._auth_judge.judge(_frame(markers=(badge,)), clock.ms)
    assert runtime.behavior.state == "AUTH_WAIT", "암구호 전 사원증은 통과가 아니다"
    assert svc.auth("ok").accepted
    assert runtime.behavior.state == "AUTH_WAIT", "암구호만으로 출발하지 않는다"
    runtime._auth_judge.judge(_frame(markers=(badge,)), clock.ms)
    assert runtime.behavior.state == "AUTH_WAIT", "먼저 든 사원증을 재사용하지 않는다"
    runtime._auth_judge.judge(_frame(), clock.ms)
    runtime._auth_judge.judge(_frame(markers=(badge,)), clock.ms)
    assert runtime.behavior.state == "PATROL"


def test_voice_auth_holds_without_any_track(cfg, clock):
    """통과 뒤에는 **보이는 사람이 없어도** 인증 상태다 — 허가는 현장에 붙는다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    assert svc.auth("ok").accepted is True

    clock.advance(1_000)
    runtime._auth_judge.judge(_frame(), clock.ms)
    assert runtime.escalation.authenticated is True


def test_voice_auth_expires_after_session_valid_s(cfg, clock):
    """**만료되면 재인증을 요구한다** (FR-10.2.4). 유효 시간은 설정값이다."""
    valid_ms = int(cfg["auth"]["session_valid_s"]) * 1000
    svc, runtime = _voice_auth_service(cfg, clock)
    svc.auth("ok")

    clock.advance(valid_ms - 1)
    runtime._auth_judge.judge(_frame(), clock.ms)
    assert runtime.escalation.authenticated is True, "만료 직전은 아직 유효하다"

    clock.advance(2)
    runtime._auth_judge.judge(_frame(), clock.ms)
    assert runtime.escalation.authenticated is False, "만료 뒤에는 재인증을 요구한다"


def test_voice_auth_rejected_verdict_opens_no_window(cfg, clock):
    """**받아들여지지 않은 판정은 허가가 아니다** — `AUTH_WAIT` 밖의 `ok`."""
    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock)
    svc = CommandService(
        runtime.behavior,
        runtime.commander,
        lambda _line: None,
        apply_event=runtime.apply_external,
        note_voice_auth=runtime.note_voice_auth,
    )
    assert svc.auth("ok").accepted is False, "IDLE 에서는 인증 결과를 받지 않는다"

    runtime._auth_judge.judge(_frame(), clock.ms)
    assert runtime.escalation.authenticated is False


# ── 창이 열리기 전에 녹음된 발화 (2026-09-21 실기 · 빨간 눈의 원인) ──────
#
# 파이프라인은 `capture_pcm` 으로 최대 15초를 녹음하고 전사까지 마친 **뒤에**
# 상태를 묻는다. 그래서 «말한 시각» 과 «판정이 도착한 시각» 이 수 초 벌어지고,
# 그 사이에 `AUTH_WAIT` 가 열리면 **창 밖에서 한 말이 시도로 세어진다.** 실기에서
# 방문객이 말을 걸기도 전에 `max_attempts` 2회가 소진돼 눈이 빨개졌다.


def test_voice_auth_before_the_window_opened_is_not_counted(cfg, clock):
    """**창이 열리기 전에 녹음된 발화는 시도가 아니다.** 몇 번 와도 소진되지 않는다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    opened = runtime.voice_auth.opened_ms
    assert opened == clock.ms, "창이 열린 시각이 기록되어야 한다"

    result = svc.auth("fail", captured_at_ms=opened - 1)

    assert result.accepted is True, "제대로 받아 버린 것을 전달 실패로 말하면 안 된다"
    assert "다시 말해" in result.detail
    assert runtime.voice_auth.attempts == 0, "창 밖의 말이 시도로 세어지면 안 된다"

    svc.auth("fail", captured_at_ms=opened - 1)
    svc.auth("fail", captured_at_ms=opened - 1)
    assert runtime.behavior.state == "AUTH_WAIT", "창 밖의 말로는 소진되지 않는다"
    assert runtime.escalation.level.value != "L3"


def test_voice_auth_match_before_the_window_does_not_grant(cfg, clock):
    """**묻기 전의 대답은 허가가 아니다.** 우연히 맞는 말을 한 것은 인증이 아니다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    opened = runtime.voice_auth.opened_ms

    result = svc.auth("ok", captured_at_ms=opened - 500)

    assert result.accepted is True
    assert runtime.behavior.state == "AUTH_WAIT", "허가하지 않고 다시 묻는다"
    runtime._auth_judge.judge(_frame(), clock.ms)
    assert runtime.escalation.authenticated is False, "창이 열리지 않아야 한다"


def test_voice_auth_after_the_window_opened_is_counted(cfg, clock):
    """**창이 열린 뒤의 발화는 정상으로 센다** — 가드가 과하게 막으면 인증이 죽는다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    clock.advance(1200)

    result = svc.auth("fail", captured_at_ms=clock.ms)

    assert "1회 남았다" in result.detail
    assert runtime.voice_auth.attempts == 1


def test_voice_auth_without_capture_time_is_counted(cfg, clock):
    """발화 시각 없이 오는 호출(관제 화면의 수동 주입)은 **예전대로 센다.**"""
    svc, runtime = _voice_auth_service(cfg, clock)

    result = svc.auth("fail")

    assert "1회 남았다" in result.detail
    assert runtime.voice_auth.attempts == 1


def test_voice_auth_utterance_from_the_previous_window_is_stale(cfg, clock):
    """**앞 대기에서 한 말이 새 대기의 시도가 되면 안 된다.**

    시도 횟수는 대기마다 0 으로 되돌아가는데(`..._attempts_reset_on_each_auth_wait`),
    창이 열린 시각도 함께 갱신되어야 그 초기화가 의미를 갖는다.
    """
    svc, runtime = _voice_auth_service(cfg, clock)
    spoke_in_first_window = clock.ms + 10
    clock.advance(20)
    svc.auth("ok", captured_at_ms=spoke_in_first_window)
    assert runtime.behavior.state == "PATROL", "첫 대기에서는 창 안의 말이라 통과한다"

    clock.advance(1000)
    runtime.apply_external(Event.PERSON_FOUND)
    runtime.apply_external(Event.AUTH_REQUIRED)
    assert runtime.behavior.state == "AUTH_WAIT"

    result = svc.auth("fail", captured_at_ms=spoke_in_first_window)

    assert runtime.voice_auth.attempts == 0
    assert "다시 말해" in result.detail


def test_voice_auth_window_open_time_clears_on_leaving(cfg, clock):
    """`AUTH_WAIT` 를 떠나면 **열린 시각을 지운다** — 남겨 두면 다음 판정이 옛
    창을 기준으로 걸러진다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    assert runtime.voice_auth.opened_ms is not None

    svc.auth("ok", captured_at_ms=clock.ms)

    assert runtime.behavior.state == "PATROL"
    assert runtime.voice_auth.opened_ms is None


# ── 판정을 기다리는 동안의 유예 (ADR-37 · 2026-09-22 실기) ────────────
#
# ⑥ 으로 «창 밖의 말이 시도가 되는» 길은 막았지만, **창이 30초 만에 닫히는 것**은
# 그대로였다. 파이프라인은 녹음(최대 15초)·무음 1초·전사를 직렬로 하므로 방문객이
# 창 안에서 말해도 판정이 30초를 넘겨 도착할 수 있다 — 2026-09-22 실기에서 통과한
# 2건이 각각 24초·18초를 썼고 여유는 6초뿐이었다. **말하는 도중에 눈이 빨개진다.**
# 그래서 «말을 받았다» 를 먼저 보내 마감을 한 번 미룬다.


def _timeout_ms(cfg) -> int:
    return int(cfg["auth"]["timeout_s"]) * 1000


def _grace_ms(cfg) -> int:
    return int(cfg["auth"]["verdict_grace_s"]) * 1000


def test_voice_listening_holds_the_window_open(cfg, clock):
    """**말을 받았다고 알리면 창이 그만큼 더 열려 있다.**"""
    svc, runtime = _voice_auth_service(cfg, clock)

    result = svc.auth("pending", captured_at_ms=clock.ms)
    assert result.accepted is True
    assert "미뤘다" in result.detail

    runtime.behavior.tick(clock.advance(_timeout_ms(cfg)))
    assert runtime.behavior.state == "AUTH_WAIT", "원래 마감에는 아직 닫히지 않는다"
    runtime.behavior.tick(clock.advance(_grace_ms(cfg)))
    assert runtime.behavior.state == "ALERT", "유예가 끝나면 닫힌다"


def test_voice_listening_is_not_a_verdict(cfg, clock):
    """**유예는 인증이 아니다.** 시도를 세지도, 허가를 주지도 않는다 —
    그렇지 않으면 소리만 내서 통과하는 길이 생긴다."""
    svc, runtime = _voice_auth_service(cfg, clock)

    svc.auth("pending", captured_at_ms=clock.ms)

    assert runtime.behavior.state == "AUTH_WAIT"
    assert runtime.voice_auth.attempts == 0
    runtime._auth_judge.judge(_frame(), clock.ms)
    assert runtime.escalation.authenticated is False


def test_voice_listening_buys_grace_only_once_per_window(cfg, clock):
    """**창마다 1회다.** 계속 보내도 마감은 한 번만 밀린다 — 무한정 미룰 수
    있으면 소리만 내서 경보를 영영 막는다. 경보가 늦는 것보다 오지 않는 것이 나쁘다."""
    svc, runtime = _voice_auth_service(cfg, clock)

    assert "미뤘다" in svc.auth("pending", captured_at_ms=clock.ms).detail
    again = svc.auth("pending", captured_at_ms=clock.ms)
    assert again.accepted is True, "거절이 아니라 조용히 아무 일도 안 하는 것이다"
    assert "이미 한 번" in again.detail
    svc.auth("pending", captured_at_ms=clock.ms)

    assert runtime.behavior.timer_deferred_ms == _grace_ms(cfg)
    runtime.behavior.tick(clock.advance(_timeout_ms(cfg) + _grace_ms(cfg)))
    assert runtime.behavior.state == "ALERT", "몇 번을 보내도 상한에서 닫힌다"


def test_voice_listening_outside_auth_wait_is_refused(cfg, clock):
    """묻지도 않았는데 창을 늘릴 수는 없다 — `AUTH_WAIT` 에서만이다."""
    runtime = Runtime(cfg, device_id="mechdog-01", clock=clock)
    svc = CommandService(
        runtime.behavior,
        runtime.commander,
        lambda _line: None,
        apply_event=runtime.apply_external,
        note_voice_auth=runtime.note_voice_auth,
        note_voice_listening=runtime.note_voice_listening,
    )

    result = svc.auth("pending", captured_at_ms=clock.ms)

    assert result.accepted is False
    assert "AUTH_WAIT" in result.detail
    assert runtime.behavior.timer_deferred_ms == 0


def test_voice_listening_before_the_window_buys_nothing(cfg, clock):
    """**창이 열리기 전에 시작된 말은 창의 수명을 늘리지 못한다.**

    이것을 허용하면 ⑥ 에서 막은 길이 옆문으로 되살아난다 — 창 밖에서 떠들어
    두면 그 말이 창을 늘려 준다.
    """
    svc, runtime = _voice_auth_service(cfg, clock)
    opened = runtime.voice_auth.opened_ms

    result = svc.auth("pending", captured_at_ms=opened - 1)

    assert result.accepted is True
    assert "열리기 전" in result.detail
    assert runtime.behavior.timer_deferred_ms == 0
    runtime.behavior.tick(clock.advance(_timeout_ms(cfg)))
    assert runtime.behavior.state == "ALERT", "제 시각에 닫힌다"


def test_voice_listening_grace_returns_with_a_new_window(cfg, clock):
    """유예는 **대기마다** 새로 주어진다 — 앞 대기에서 썼다고 다음이 굶으면
    두 번째 방문객이 말하는 도중에 경보가 된다."""
    svc, runtime = _voice_auth_service(cfg, clock)
    svc.auth("pending", captured_at_ms=clock.ms)
    clock.advance(100)
    svc.auth("ok", captured_at_ms=clock.ms)
    assert runtime.behavior.state == "PATROL"

    clock.advance(1000)
    runtime.apply_external(Event.PERSON_FOUND)
    runtime.apply_external(Event.AUTH_REQUIRED)
    assert runtime.behavior.state == "AUTH_WAIT"

    result = svc.auth("pending", captured_at_ms=clock.ms)

    assert "미뤘다" in result.detail
    assert runtime.behavior.timer_deferred_ms == _grace_ms(cfg)


def test_voice_listening_without_capture_time_still_holds(cfg, clock):
    """시각 없이 오는 통지도 받는다 — 시각은 «오래됨» 을 가리기 위한 것일 뿐,
    없다고 해서 오래된 것은 아니다."""
    svc, runtime = _voice_auth_service(cfg, clock)

    assert "미뤘다" in svc.auth("pending").detail
    assert runtime.behavior.timer_deferred_ms == _grace_ms(cfg)
