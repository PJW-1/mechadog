"""대응 에스컬레이션 단계 L0~L3 · F (WBS 3.8.3 · 아키텍처 3.1).

⚠️ **FSM 상태와 직교하는 별도 축이다.** `PATROL`·`ALERT` 같은 상태가 *"무엇을
하는가"* 라면 이 단계는 *"얼마나 강하게 대응하는가"* 다. 같은 `ALERT` 에서도
단계가 L1 인지 L3 인지에 따라 눈 색깔과 음향이 다르다. 실제로 전이표에는
*"`ALERT` 에서의 PPE 위반은 상태가 그대로이므로 표에 없다 — 에스컬레이션 소관"*
이라고 적혀 있고, 이 파일이 그 소관이다.

**단계 표가 정본이다.** 아키텍처 3.1 의 표와 일치해야 하며 임계는 전부
`config.yaml` 에서 읽는다 — 코드에 숫자를 두지 않는다(`3.4.1` 전이표와 같은 방식).

    단계  진입                                해제
    L0   정상                                —
    L1   person 300ms 안에 3회 (FR-3.2)       미검출 5초 → L0
    L2   L1 이 10초 지속 & 미인증              인증 성공 → L0
                                             미검출 5초 · 30초 무응답 → **L3**
    L3   인증 실패 · PPE 위반 · 물체 변화       **관리자 확인만**
    F    링크두절 · 저전압 · 전도 · E-Stop     **로봇 래치 해제 확인만**

⚠️ **L3 와 F 는 자동으로 해제되지 않는다.** 사람이 확인해야 한다. 그래서 확인
경로를 만드는 것이 이 작업의 절반이다 — **해제 수단 없는 래치는 시연을 끝내
버린다.**

⚠️ **L3 해제는 진입 원인과 무관하다.** 원인별 조건을 만들면 안 되는 이유가 있다.
진입 원인 셋 중 *"인증 실패"* 만 인증으로 풀리고 **PPE 위반은 다시 인증할 대상이
아니며 물체 변화는 사람 자체가 없다.** 원인별로 갈면 해제 조건이 셋으로 나뉘고,
물건이 제자리로 돌아온 것이 정상인지는 로봇이 판단할 수 없다. 원인 해소 여부는
**표시하되 해제 조건으로 쓰지 않는다.**

⚠️ **F 해제와 L3 해제를 같은 사건으로 두지 않는다.** 확인해야 하는 것이 다르다 —
F 는 물리 상태(넘어졌나·배터리·링크), L3 는 상황 판단(침입자가 갔나·안전모·물건).
하나로 묶으면 **비상정지를 눌러 경보를 끄는 길**이 생긴다. 그래서 F 로 올라갈 때
경보를 기억해 두고, F 를 풀면 L3 로 되돌린다.

⚠️ **미검출 5초를 이 파일이 스스로 잰다.** FSM 의 `TARGET_LOST` 를 얻어 쓰지 않는
이유 — 그 사건은 `ALERT`·`TRACK` 에서만 전이를 만들므로 표에 걸러진다. 사람이
보이는 동안 조작자가 수동으로 전환하면 사건이 오지 않아 **L1 이 영구히 남는다.**
같은 설정 키(`fsm.target_lost_timeout_s`)를 읽으므로 숫자는 여전히 하나다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.escalation")


class Level(Enum):
    """대응 강도. **순서가 의미를 가진다** — 다중 대상에서 최댓값을 쓴다(FR-3.8.1)."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    F = "F"

    @property
    def rank(self) -> int:
        """비교용 순위. ⚠️ **F 가 가장 높다** — 안전이 대응보다 앞선다."""
        return _RANK[self]


_RANK: dict[Level, int] = {Level.L0: 0, Level.L1: 1, Level.L2: 2, Level.L3: 3, Level.F: 4}

#: 사람이 확인해야만 풀리는 단계. **자동 해제 금지** (아키텍처 3.1).
LATCHED: frozenset[Level] = frozenset({Level.L3, Level.F})

#: `config.yaml` 의 `escalation.led` 키. 단계와 설정을 잇는 유일한 지점이다.
LED_KEYS: dict[Level, str] = {
    Level.L0: "l0_patrol",
    Level.L1: "l1_observe",
    Level.L2: "l2_auth_request",
    Level.L3: "l3_alarm",
    Level.F: "failsafe",
}

#: 사건 → 올릴 단계. **표가 정본이다** (아키텍처 3.1 진입 조건).
#:
#: 사건을 `Event` 가 아니라 이름 문자열로 두는 이유 — `fsm` 을 import 하면 순환
#: 참조가 되고, 그러면 이 축을 FSM 없이 단독으로 시험할 수 없다.
RAISED_BY: dict[str, Level] = {
    # ── L3 경보 ──
    "AUTH_FAILED": Level.L3,  # 2회 실패 또는 30초 무응답 (FR-10.3)
    "PPE_VIOLATION": Level.L3,  # 보호구 미착용 확정 (FR-9.3)
    "ZONE_CHANGED": Level.L3,  # 물체 변화 확정 (FR-8.4)
    # ── F 페일세이프. ⚠️ 어느 단계에서든 즉시 들어간다 ──
    "ONBOARD_FAILSAFE": Level.F,  # 링크두절·저전압을 로봇이 보고
    "LINK_LOST": Level.F,
    "ESTOP": Level.F,
}

#: 인증 성공으로 보는 사건. **L2 만** L0 으로 내린다 (FR-10) — `note_authenticated` 참고.
AUTH_CLEARS: frozenset[str] = frozenset({"AUTH_OK"})

#: F 해제로 보는 사건. **`RESET_CONFIRMED` 는 로봇이 래치를 풀었다는 확인이다**
#: (PROTOCOL 2절) — 사람이 원인을 확인하고 로봇이 실제로 풀렸을 때만 온다.
FAILSAFE_CLEARS: frozenset[str] = frozenset({"RESET_CONFIRMED"})


@dataclass(frozen=True, slots=True)
class Presentation:
    """그 단계에서 로봇이 내보내는 표현.

    **눈 LED 는 장식이 아니라 관측 가능한 상태 출력이다** — 로봇이 지금 무엇을
    판단하고 있는지 관객과 작업자가 즉시 읽어야 하고, 디버깅 수단도 된다(FR-10.4).
    """

    level: Level
    led: str
    #: 점멸 주기. **L3 에만 붙는다** — 켜져 있는 것과 경보를 구분하기 위해서다.
    blink_hz: float | None
    #: WonderEcho 사전 등록 문구 ID. 플래싱 전이라 아직 `None` 이다 (OI-10/11).
    sound_id: str | None


class Escalation:
    """단계를 올리고 내린다. **시계를 만들지 않는다** — `now_ms` 를 받는다.

    올리는 것은 사건이 하고 내리는 것은 조건이 한다. 그 둘이 섞이지 않도록
    `raise_to()` 와 확인 계열(`confirm_*`)을 나눠 두었다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        esc = config["escalation"]
        self._hold_ms = int(esc["l1_to_l2_hold_s"]) * 1000
        # ⚠️ FSM 과 **같은 키**를 읽는다. 따로 두면 두 개의 5초가 생긴다.
        self._lost_ms = int(config["fsm"]["target_lost_timeout_s"]) * 1000
        self._led = dict(esc["led"])
        self._sound = dict(esc.get("sound") or {})
        self._level = Level.L0
        #: L1 진입 시각. L2 승격 타이머의 기준이다.
        self._l1_since_ms: int | None = None
        #: 마지막으로 사람을 **실제 검출한** 시각. 게이트 확정(300ms)이 아니다.
        self._last_seen_ms: int | None = None
        self._authenticated = False
        #: F 로 올라갈 때 경보가 걸려 있었나. **비상정지로 경보를 끄지 못하게 한다.**
        self._alarm_pending = False

    # ── 상태 ────────────────────────────────────────────────
    @property
    def level(self) -> Level:
        return self._level

    @property
    def latched(self) -> bool:
        """사람 확인 없이는 내려갈 수 없는 단계인가."""
        return self._level in LATCHED

    @property
    def authenticated(self) -> bool:
        """지금 대상이 인증을 통과했나. **표시용이며 L3 해제 조건이 아니다.**"""
        return self._authenticated

    @property
    def alarm_pending(self) -> bool:
        """F 아래에 경보가 깔려 있나. 대시보드가 *"풀면 빨강으로 돌아간다"* 를 알린다."""
        return self._alarm_pending

    def presentation(self) -> Presentation:
        return Presentation(
            level=self._level,
            led=str(self._led[LED_KEYS[self._level]]),
            blink_hz=float(self._led["l3_blink_hz"]) if self._level is Level.L3 else None,
            sound_id=self._sound_for(self._level),
        )

    def _sound_for(self, level: Level) -> str | None:
        value = None
        if level is Level.L2:
            value = self._sound.get("l2_beep")
        elif level is Level.L3:
            value = self._sound.get("l3_warning")
        return None if value is None else str(value)

    # ── 올리기 ──────────────────────────────────────────────
    def raise_to(self, level: Level, *, reason: str, now_ms: int) -> bool:
        """단계를 올린다. **내리지는 않는다** — 그래서 사건 순서가 뒤바뀌어도 안전하다."""
        if level.rank <= self._level.rank:
            return False
        return self._enter(level, reason=reason, now_ms=now_ms)

    def note_event(self, event: str, now_ms: int, *, accepted: bool = True) -> None:
        """FSM 사건 하나를 넣는다. **표에 있는 것만 반응한다.**

        `accepted` 는 FSM 이 그 사건을 받아들였는지다. **올리는 것과 내리는 것에
        다르게 적용한다.**

        - **올릴 때는 전이 여부를 보지 않는다.** `ALERT` 에서의 PPE 위반처럼 상태는
          그대로인데 대응은 올라가야 하는 경우가 있고, 전이표에 그 줄이 없는 것이
          바로 *"에스컬레이션 소관"* 이라는 뜻이다.
        - ⚠️ **내릴 때는 받아들여졌을 때만이다.** `RESET_CONFIRMED` 는 로봇의 안전
          래치가 걸려 있으면 FSM 이 거부한다(`LATCH_GUARDED`). 그때 F 를 풀면
          **로봇은 잠긴 채 호스트만 풀려** 관제 화면이 거짓을 말한다.
        """
        level = RAISED_BY.get(event)
        if level is not None:
            self.raise_to(level, reason=event, now_ms=now_ms)
        if not accepted:
            return
        if event in AUTH_CLEARS:
            self.note_authenticated(now_ms)
        if event in FAILSAFE_CLEARS:
            self.confirm_failsafe(now_ms)

    # ── 관측 ────────────────────────────────────────────────
    def note_person(self, *, present: bool, last_seen_ms: int | None, now_ms: int) -> None:
        """사람 게이트 결과를 넣는다 (FR-3.2 · `3.3.3`).

        `present` 는 시간 창 확정이고 `last_seen_ms` 는 **실제 검출** 시각이다. 둘을
        나눠 받는 이유 — 확정 해제(300ms)와 대상 상실(5초)은 다른 시간이며, 하나로
        합치면 창이 빈 순간마다 단계가 내려간다.

        ⚠️ **래치된 단계에서는 올리지도 내리지도 않는다.** L3 에서 사람이 사라졌다고
        경보가 풀리면 *"관리자 확인"* 이라는 규칙이 무의미해진다.
        """
        if last_seen_ms is not None:
            self._last_seen_ms = last_seen_ms
        if self.latched:
            return
        if present:
            self.raise_to(Level.L1, reason="person_present", now_ms=now_ms)

    def note_authenticated(self, now_ms: int) -> None:
        """인증 성공 (FR-10). **L2 를 L0 으로 내리고 승격을 막는다.**

        ⚠️ **L3 는 내리지 않는다.** 진입 원인이 인증 실패였더라도 원인별 해제를
        만들지 않기로 했다(모듈 주석). 통과 사실은 `authenticated` 로 **표시만**
        하고 해제는 관리자 확인이 한다.

        ⚠️ **L1 도 내리지 않는다.** 단계 표에 `L1` 의 해제는 *미검출 5초* 하나뿐이며
        *인증 성공*은 `L2` 줄에만 있다. 처음에는 둘을 같이 내렸는데, 그러면 인증된
        사람이 앞에 서 있는 동안 **10Hz 로 `L0↔L1` 이 진동한다** — 관측이 L1 로
        올리고 인증이 곧바로 L0 으로 내리기 때문이다. 실기에서 초당 20줄씩 로그가
        쏟아지고 눈 LED 가 파랑/노랑으로 깜빡이는 것으로 드러났다. **시험 786건이
        놓쳤다** — 두 호출을 번갈아 반복하는 시험이 없었기 때문이다.

        인증된 사람 앞에서 L1(관찰)에 머무는 것은 옳다. 로봇은 그 사람을 실제로
        보고 있고, 승격만 하지 않는다.
        """
        self._authenticated = True
        if self.latched:
            return
        if self._level is Level.L2:
            self._enter(Level.L0, reason="authenticated", now_ms=now_ms)

    def stand_down(self, now_ms: int) -> None:
        """임무 밖(대기·수동)이다 — **래치되지 않은 단계(L1·L2)를 내린다** (잠정).

        L3·F 는 그대로다. 사람이 확인해야 풀린다는 규칙은 상태와 무관하다.
        무엇이 임무 밖인지는 `fsm.STANDBY` 가 정한다.
        """
        if self._level in (Level.L1, Level.L2):
            self._release("standby", now_ms)

    def note_authentication_lost(self) -> None:
        """인증이 더 이상 유효하지 않다 — 유효 시간 만료(FR-10.2.4)나 미인증자 합류.

        ⚠️ **표시만 지운다. 단계는 올리지 않는다.** 만료된 순간 경보로 뛰면 60초마다
        한 번씩 경보가 울린다. 승격은 평소처럼 `tick()` 의 시간 조건이 정한다 — 즉
        L1 에 머문 시간이 이미 임계를 넘었다면 다음 틱에 L2 로 올라가고, 그것이
        *"만료 후 재인증을 요구한다"* (FR-10.2.4) 의 뜻이다.
        """
        self._authenticated = False

    def tick(self, now_ms: int) -> None:
        """시간으로 정해지는 것들을 처리한다 — L1 해제 · **L2 승격** · L2 승급.

        ⚠️ **L1 과 L2 에서 대상 상실의 뜻이 다르다.** L1 은 그냥 지나간 사람이므로
        L0 으로 돌아가지만, L2 는 *"인증하라고 요구했는데 응답 없이 사라졌다"* 이므로
        경보로 올라간다 — 미인증 통과는 경비 대응 대상이다(FR-10.3 과 같은 결론).
        L2 를 L0 으로 내리면 **인증을 무시하고 지나가는 것이 가장 이득인** 정책이
        된다.
        """
        if self._lost(now_ms):
            if self._level is Level.L1:
                self._release("target_lost", now_ms)
                return
            if self._level is Level.L2:
                self.raise_to(Level.L3, reason="unauthenticated_left", now_ms=now_ms)
                return
        if self._level is Level.L1 and not self._authenticated:
            since = self._l1_since_ms
            if since is not None and now_ms - since >= self._hold_ms:
                self.raise_to(Level.L2, reason="unauthenticated_hold", now_ms=now_ms)

    def _lost(self, now_ms: int) -> bool:
        """마지막 검출 이후 임계가 지났나 (FR-3.7).

        한 번도 못 봤으면 상실이 아니다 — 기동 직후를 상실로 보면 아무 일도 없이
        단계가 내려갔다는 로그가 남는다.
        """
        return self._last_seen_ms is not None and now_ms - self._last_seen_ms >= self._lost_ms

    # ── 내리기 (사람 확인) ──────────────────────────────────
    def confirm_alarm(self, now_ms: int) -> bool:
        """관리자가 **경보(L3)** 를 확인했다 — 유일한 L3 해제 경로다.

        ⚠️ **여기서 `F` 를 풀지 않는다.** 확인한 것은 상황 판단이며 물리 상태가
        아니다. 하나로 묶으면 경보를 끄려는 조작이 페일세이프까지 해제해 로봇이
        넘어진 채로 다시 움직인다.
        """
        if self._level is not Level.L3:
            return False
        self._alarm_pending = False
        self._release("alarm_confirmed", now_ms)
        return True

    def confirm_failsafe(self, now_ms: int) -> bool:
        """로봇이 안전 래치를 풀었다 — F 해제 (`RESET_CONFIRMED`).

        ⚠️ **경보가 깔려 있었으면 L0 이 아니라 L3 로 돌아간다.** 그렇지 않으면
        비상정지를 눌렀다 풀는 것으로 경보를 지울 수 있다.
        """
        if self._level is not Level.F:
            return False
        if self._alarm_pending:
            return self._enter(Level.L3, reason="failsafe_confirmed_alarm_kept", now_ms=now_ms)
        self._release("failsafe_confirmed", now_ms)
        return True

    # ── 내부 ────────────────────────────────────────────────
    def _release(self, reason: str, now_ms: int) -> None:
        """정상으로 되돌린다. **인증 사실도 함께 잊는다** — 다음 사람은 다시 인증한다."""
        self._authenticated = False
        self._enter(Level.L0, reason=reason, now_ms=now_ms)

    def _enter(self, level: Level, *, reason: str, now_ms: int) -> bool:
        previous, self._level = self._level, level
        self._l1_since_ms = now_ms if level is Level.L1 else None
        if level is Level.L3:
            self._alarm_pending = True
        elif level is Level.L0:
            self._alarm_pending = False
        # ⚠️ `level=` 을 쓰면 안 된다 — 로거의 `_emit(level, event, ...)` 인자와
        # 부딪혀 `TypeError` 가 난다. 전이 로그와 같은 `from`/`to` 표기로 맞춘다.
        LOG.info(
            "escalation",
            **{
                "from": previous.value,
                "to": level.value,
                "reason": reason,
                "led": self.presentation().led,
            },
        )
        return True
