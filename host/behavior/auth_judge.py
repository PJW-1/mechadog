"""경비 모드의 사원증·암구호 인증 판정 — 사건으로 옮기기와 인증 요구 (FR-10 · ADR-28 · ADR-37).

마커 읽기는 비전 워커가, 누구의 사원증인지는 `Authenticator` 가 정한다. 여기서는 그
결과와 음성 인증 창(`VoiceAuthWindow`)을 합쳐 `AUTH_OK`·`AUTH_FAILED` 사건과 단계 축의
인증 표시로 옮기고, L2 에서 `AUTH_REQUIRED` 를 낸다. 창을 열고 닫는 것은 여전히
`Runtime._apply` 다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from host.behavior.auth import Authenticator, Outcome
from host.behavior.escalation import Escalation, Level
from host.behavior.fsm import Behavior, Event
from host.behavior.mission import Mission
from host.behavior.voice_auth import VoiceAuthWindow
from host.common.logging_setup import event_logger

if TYPE_CHECKING:
    from host.vision.worker import VisionResult

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechdog.runtime")


class AuthJudge:
    """사원증 세션(`Authenticator`)을 쥐고 인증 판정·요구·순찰 재개 대기를 맡는다.

    모든 메서드는 운용 루프 스레드에서만 부른다(`Runtime.tick`·`_apply`·시퀀스).
    대시보드는 `authenticator` 를 읽기만 한다 — 스레드 경계는 옮기기 전과 같다.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        behavior: Behavior,
        mission: Mission,
        escalation: Escalation,
        voice: VoiceAuthWindow,
        apply: Callable[[Event, int], bool],
    ) -> None:
        self._behavior = behavior
        self._mission = mission
        self._escalation = escalation
        self._voice = voice
        self._apply = apply
        self._auth = Authenticator(config)
        self._resume_delay_ms = int(config["auth"].get("resume_delay_ms", 3500))
        self._resume_after: int | None = None

    @property
    def authenticator(self) -> Authenticator:
        """사원증 세션. 대시보드가 *"누가 인증됐나"* 를 보이는 데 쓴다 (FR-3.6.2)."""
        return self._auth

    def holds_patrol(self, now_ms: int) -> bool:
        """인증 통과 뒤 순찰 재개를 `auth.resume_delay_ms` 동안 미루는 중인가."""
        return self._resume_after is not None and now_ms < self._resume_after

    def judge(self, result: VisionResult, now_ms: int) -> None:
        """사원증·암구호를 판정해 사건과 단계 축의 인증 표시로 옮긴다 (FR-10.1 · ADR-37).

        인증 표시는 사건이 아니라 매번 *"보이는 전원이 인증됐나"* 로 정한다 — 유효 시간
        만료(FR-10.2.4)와 미인증자 합류(FR-3.8.1)는 사건 없이 일어난다. 경비 모드에서만
        돈다(FR-11.1) — 단계 축 호출은 `_apply` 의 모드 게이트를 지나지 않는다.
        """
        if not self._mission.enables("auth"):
            return
        voice = self._voice
        voice_granted = voice.granted(now_ms)
        self._auth.note_tracks(result.tracks)
        if voice.require_both and voice_granted and not result.markers:
            voice.badge_ready = True
        outcome = (
            self._auth.observe(result.markers, result.tracks, now_ms)
            if not voice.require_both or (voice_granted and voice.badge_ready)
            else Outcome.NOTHING
        )
        if outcome in (Outcome.GRANTED, Outcome.BADGE_SEEN):
            if self._apply(Event.AUTH_OK, now_ms) and self._behavior.state == "PATROL":
                self._resume_after = now_ms + self._resume_delay_ms
                LOG.info("auth_resume_wait", delay_ms=self._resume_delay_ms)
        elif outcome is Outcome.EXHAUSTED:
            # 2회 실패 — 30초 무응답과 같은 결론이다 (FR-10.3).
            self._apply(Event.AUTH_FAILED, now_ms)
        # ⚠️ **음성 허가는 사람이 아니라 현장에 붙는다** (FR-10.2.4).
        # 사원증은 화면 안 좌표에 보이니 그 좌표의 트랙에 붙일 근거가 있지만, 마이크는
        # 로봇 몸통에 하나뿐이라 **그 소리가 누구 목소리인지 모른다**. 그러니 "제일
        # 가까운 트랙에 붙인다"는 건 없는 근거를 지어내는 것이고, 틀리면 엉뚱한 사람이
        # 허가를 받는다 — 안 붙이는 것보다 나쁘다. 그래서 암구호가 맞으면
        # `session_valid_s` 동안 **이 자리**를 인증된 것으로 본다.
        #
        # ⚠️ **창이 열린 동안 트랙이 죽어도 허가는 유지된다.** 트랙에 기대면 검출이
        # 잠깐 끊기는 것만으로 허가가 날아가 10초마다 재인증을 요구한다.
        #
        # ⚠️ **대신 그 60초 동안 새로 들어온 사람도 함께 허가된다.** 암구호는 원래
        # *아는 사람은 통과*라 결론은 같지만, 사원증과 다른 성질이니 알고 쓸 것.
        badge_granted = self._auth.all_authenticated(result.tracks, now_ms)
        authenticated = (
            voice_granted and badge_granted
            if voice.require_both
            else voice_granted or badge_granted
        )
        if authenticated:
            self._escalation.note_authenticated(now_ms)
        else:
            self._escalation.note_authentication_lost()

    def request(self, now_ms: int) -> None:
        """L2 에 올랐으면 `AUTH_REQUIRED` 를 낸다 (FR-10 · 아키텍처 3.1).

        이것이 `AUTH_WAIT` 의 유일한 입구라 30초 타이머와 `AUTH_FAILED` 경로가 여기 걸린다.
        전이 가능 여부는 표에 물으므로 이미 `AUTH_WAIT` 면 다시 내지 않는다.
        """
        if self._escalation.level is not Level.L2:
            return
        if not self._behavior.fsm.can(Event.AUTH_REQUIRED):
            return
        self._apply(Event.AUTH_REQUIRED, now_ms)

    def reset(self) -> None:
        """사원증 세션을 지운다 — `require_both` 에서 `AUTH_WAIT` 에 들 때 `_apply` 가 부른다."""
        self._auth.reset()
