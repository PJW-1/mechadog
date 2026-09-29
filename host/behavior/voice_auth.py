"""음성 암구호의 인증 창 (FR-10.2.4 · FR-10.3 · ADR-37).

`AUTH_WAIT` 한 번을 단위로 암구호 시도를 세고, 판정 대기 유예를 한 번 주고, 통과한
뒤 `session_valid_s` 동안 이 자리를 인증된 것으로 본다. 창을 열고 닫는 것은
`Runtime._apply` 가 전이를 본 자리에서 한다 — 사건이 지나는 유일한 지점이라서다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from host.behavior.fsm import Behavior, Event
from host.behavior.mission import Mission
from host.common.logging_setup import event_logger

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class VoiceAuthWindow:
    """음성 판정을 받아 시도를 세고 사건으로 옮긴다.

    `note_listening`·`note_verdict` 는 대시보드 스레드가 부르고(`dashboard_wiring`),
    `open`·`close`·`granted` 는 운용 루프가 부른다. 스레드 경계는 옮기기 전과 같다.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        behavior: Behavior,
        mission: Mission,
        apply: Callable[[Event, int], bool],
        clock: Callable[[], int],
        feed: Callable[..., None] | None = None,
    ) -> None:
        auth = config["auth"]
        self._behavior = behavior
        self._mission = mission
        self._apply = apply
        self._clock = clock
        self._feed = feed
        #: 암구호와 사원증을 둘 다 요구하나. 암구호가 먼저다.
        self.require_both = bool(auth.get("require_both", False))
        self._badge_wait_ms = int(auth["timeout_s"]) * 1000
        #: 암구호 통과 뒤 마커가 한 번 사라졌나 — 먼저 든 사원증을 재사용하지 않게 한다.
        self.badge_ready = False
        # 음성 암구호의 시도 횟수 (FR-10.3). **사원증과 같은 값을 쓰되 따로 센다** —
        # `Authenticator` 의 세션은 마커를 본 사람에게 붙는데, 암구호는 마커가 없어
        # 붙일 세션이 없다. 그래서 *지금 열려 있는 `AUTH_WAIT` 한 번*을 단위로 센다.
        self._max = int(auth["max_attempts"])
        self.attempts = 0
        # 음성 암구호의 유효 시간 (FR-10.2.4). **사원증과 같은 값을 쓰되 트랙이 아니라
        # 시각으로 센다** — `Runtime._judge_auth` 의 설명을 함께 읽을 것.
        self._valid_ms = int(auth["session_valid_s"]) * 1000
        self._until_ms = 0
        # 판정이 오는 중일 때 `auth.timeout_s` 를 미뤄 주는 상한 (ADR-37). 녹음·전사가
        # 직렬이라 창 안에서 한 말의 판정이 창 밖에 도착할 수 있다.
        self._grace_ms = int(auth["verdict_grace_s"]) * 1000
        # **이번 창에서 유예를 이미 썼는가.** 창마다 1회다 — `note_listening`.
        self._deferred = False
        #: `AUTH_WAIT` 가 열린 시각(epoch ms · 닫혀 있으면 `None`). 이보다 앞서 녹음된
        #: 발화는 시도로 세지 않는다 — 창이 언제 열렸는지 아는 쪽이 런타임뿐이라 여기 둔다.
        self.opened_ms: int | None = None

    def open(self, now_ms: int) -> None:
        """`AUTH_WAIT` 에 들어왔다 — 시도·유예를 창 단위로 되돌린다. `_apply` 만 부른다."""
        # 초기화하지 않으면 앞선 대기에서 쌓인 실패가 다음 사람에게 넘어간다.
        self.attempts = 0
        if self.require_both:
            self._until_ms = 0
            self.badge_ready = False
        self.opened_ms = now_ms
        # 유예도 창 단위다 — `Behavior` 쪽 누적은 상태가 바뀌며 이미 0 이 된다.
        self._deferred = False

    def close(self) -> None:
        """`AUTH_WAIT` 를 떠났다. `_apply` 만 부른다."""
        self.opened_ms = None
        self._deferred = False

    def granted(self, now_ms: int) -> bool:
        """암구호 허가가 아직 유효한가. 허가는 사람이 아니라 현장에 붙는다 (FR-10.2.4)."""
        return now_ms < self._until_ms

    def _is_stale(self, captured_at_ms: int | None) -> bool:
        """그 발화가 지금 열려 있는 창보다 앞선 것인가. `None`(수동 주입)은 오래된 것이 아니다."""
        return (
            captured_at_ms is not None
            and self.opened_ms is not None
            and captured_at_ms < self.opened_ms
        )

    def note_listening(self, captured_at_ms: int | None = None) -> tuple[bool, str]:
        """«발화를 받았고 판정이 오는 중» — 인증 창 마감을 창마다 한 번, 상한 안에서 미룬다.

        판정이 아니다 — 시도를 세지도 사건을 내지도 않는다. 창 밖 발화는 유예를 사지 못한다 (ADR-37).
        """
        if self._behavior.state != "AUTH_WAIT":
            return False, f"{self._behavior.state} 에서는 인증 대기가 없다 (AUTH_WAIT 만)"
        if self._is_stale(captured_at_ms):
            LOG.info(
                "voice_listening_stale",
                captured_at_ms=captured_at_ms,
                opened_ms=self.opened_ms,
            )
            return True, "인증 창이 열리기 전에 시작된 발화다 — 유예하지 않는다"
        if self._deferred:
            return True, "이 대기에서는 이미 한 번 미뤘다"
        granted = self._behavior.defer_timer(by_ms=self._grace_ms, cap_ms=self._grace_ms)
        self._deferred = True
        LOG.info("voice_auth_deferred", granted_ms=granted, cap_ms=self._grace_ms)
        return True, f"판정을 기다린다 — {granted // 1000}초 미뤘다"

    def note_verdict(self, ok: bool, captured_at_ms: int | None = None) -> tuple[bool, str]:
        """음성 암구호 판정을 받아 시도를 세고 사건으로 옮긴다 (FR-10.3).

        `captured_at_ms` 는 사람이 말한 시각이다 — 창보다 앞선 발화는 세지도 허가하지도
        않는다. 소진 전 실패도 `accepted=True` 다 — 파이프라인은 이것을 *전달됐나*로 읽는다.
        """
        now_ms = self._clock()
        # ⚠️ **세기 전에, 그리고 허가하기 전에 «언제 말했는가» 를 먼저 본다.**
        # 창이 열리기 전의 발화는 실패든 일치든 이 대기의 것이 아니다. 일치를
        # 허가하지 않는 이유는 **묻기 전에 한 대답**이기 때문이다 — 방문객이
        # 우연히 맞는 말을 한 것이 통행 허가가 되면 인증이 아니다.
        if self._is_stale(captured_at_ms):
            LOG.info(
                "voice_auth_stale",
                captured_at_ms=captured_at_ms,
                opened_ms=self.opened_ms,
                behind_ms=self.opened_ms - captured_at_ms,
                matched=ok,
            )
            # `True` 로 돌려준다 — 파이프라인은 `accepted` 를 *전달됐나* 로 읽어
            # 거짓이면 "전달하지 못했습니다" 라고 말한다. 제대로 받아 버린 것을
            # 전달 실패로 말하면 사람이 무엇을 해야 하는지 알 수 없다.
            return True, "인증 창이 열리기 전에 녹음된 발화다 — 다시 말해 주세요"
        if ok:
            if self.require_both:
                if self._behavior.state != "AUTH_WAIT" or not self._mission.enables("auth"):
                    return (
                        False,
                        f"{self._behavior.state} 에서는 인증 결과를 받지 않는다 (AUTH_WAIT 만)",
                    )
                if now_ms >= self._until_ms:
                    self._until_ms = now_ms + self._valid_ms
                    self.badge_ready = False
                    self._behavior.defer_timer(
                        by_ms=self._badge_wait_ms,
                        cap_ms=self._grace_ms + self._badge_wait_ms,
                    )
                    LOG.info("voice_auth_granted", valid_ms=self._valid_ms, next="badge")
                    if self._feed is not None:
                        self._feed("voice_auth_granted", now_ms, next="badge")
                return True, "암구호 확인 — 사원증을 제시해 주세요"
            accepted = self._apply(Event.AUTH_OK, now_ms)
            if accepted:
                # 여기서만 창을 연다 — 받아들여지지 않은 판정은 허가가 아니다.
                self._until_ms = now_ms + self._valid_ms
                LOG.info("voice_auth_granted", valid_ms=self._valid_ms)
            return accepted, (
                "인증 결과를 반영했다"
                if accepted
                else f"{self._behavior.state} 에서는 인증 결과를 받지 않는다 (AUTH_WAIT 만)"
            )
        # 실패다. **세기 전에 받을 수 있는 상태인지 먼저 묻는다** — 아니면 시도가
        # 엉뚱한 대기에 쌓인다. 모드 게이트도 `_apply` 와 같은 이유로 여기서 본다.
        if not self._mission.allows(Event.AUTH_FAILED.name) or not self._behavior.fsm.can(
            Event.AUTH_FAILED
        ):
            return False, f"{self._behavior.state} 에서는 인증 결과를 받지 않는다 (AUTH_WAIT 만)"
        self.attempts += 1
        remaining = self._max - self.attempts
        if remaining > 0:
            LOG.info(
                "voice_auth_rejected",
                attempts=self.attempts,
                max_attempts=self._max,
                exhausted=False,
            )
            return True, f"암구호가 일치하지 않는다 — {remaining}회 남았다"
        LOG.warning(
            "voice_auth_exhausted",
            attempts=self.attempts,
            max_attempts=self._max,
            exhausted=True,
        )
        return self._apply(Event.AUTH_FAILED, now_ms), "시도 횟수를 소진했다"
