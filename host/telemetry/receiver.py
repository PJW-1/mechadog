"""텔레메트리 수신 및 사건 변환 (WBS 4.3.6 · FR-1.4).

로봇이 10Hz 로 보내는 레코드를 받아 **FSM 사건으로 바꾼다.** 이것이 없으면
전이표의 안전 전이(`ONBOARD_FAILSAFE`·`ONBOARD_AVOID`)를 아무도 발생시킬 수
없다 — FSM 이 눈을 감고 있는 상태가 된다.

**이 모듈은 소켓을 만지지 않는다.** 바이트를 받아 사건 목록을 돌려줄 뿐이고
실제 수신은 호출자가 한다 (ENGINEERING_GUIDE 2.1). 그래서 로봇 없이 골든
픽스처와 목업으로 전부 검증된다.

⚠️ **호스트가 안전을 판정하지 않는다.** 전압이 낮거나 기울기가 크다는 것을 보고
호스트가 `ONBOARD_FAILSAFE` 를 만들지 않는다 — **로봇이 그렇게 보고했을 때만**
옮긴다 (아키텍처 1.2 불변 규칙). 이유가 둘이다.

1. **Tier 1 을 호스트로 옮기는 것이 된다.** 그러면 호스트가 꺼졌을 때 판정이
   사라지고, 그것이 이 프로젝트가 처음부터 피한 구조다.
2. **호스트와 로봇의 상태가 어긋난다.** 호스트만 `FAILSAFE` 인데 로봇은 걷고
   있으면 로그를 봐도 원인을 찾을 수 없다.

전압·기울기는 `Reading` 으로 그대로 올려보내며, 그것을 경고로 표시하는 것은
대시보드(`4.6.2`)의 일이다. **보여주는 것과 판정하는 것은 다르다.**
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from host.behavior.fsm import Event
from host.common.protocol import ONBOARD_STATES, DecodeResult, TelemetryDecoder


@dataclass(frozen=True, slots=True)
class Reading:
    """받아들인 레코드 하나. **판정 결과가 아니라 관측값이다.**"""

    device_id: str
    boot_id: str
    seq: int
    state: str
    batt_v: float | None
    dist_cm: float | None
    tipped: bool
    lowbatt: bool
    link_ok: bool
    #: 로봇의 **자체 안전 래치**. `state` 와 달리 호스트가 내려보낸 값의 반향이
    #: 아니다 — `FAILSAFE` 는 `STATE` 로도 내려가므로 되돌아온 것과 구분할 수 없다.
    #: 그래서 규약이 *"송신측은 `safety_latched=false` 를 확인해야 한다"* 고 못박았다
    #: (PROTOCOL 2절 안전 정지와 해제). 구형 펌웨어는 이 필드가 없어 `None` 이다.
    safety_latched: bool | None = None
    #: 로봇이 **마지막으로 받아들인** 명령의 경과 시각. 우리가 10Hz 로 보내는데도
    #: 이 값이 계속 커지면 **로봇이 우리 명령을 폐기하고 있다** — 호스트 재시작으로
    #: seq 가 되돌아간 경우가 대표적이다 (PROTOCOL 4절 세션 개시).
    last_cmd_age_ms: int | None = None
    #: **온보드 근거리 반사 정지가 지금 걸려 있는가.** 없으면 `None` (구형 펌웨어).
    #:
    #: ⚠️ `state == "AVOID"` 로는 해제를 알 수 없다 — 그 값은 호스트가 `STATE` 로
    #: 내려보낸 것이 되돌아온 것일 수도 있다 (ADR-22). 이 플래그는 로봇의 센서
    #: 판정이며 반향되지 않는다.
    obstacle: bool | None = None

    @classmethod
    def of(cls, msg: Mapping[str, Any]) -> Reading:
        flags = msg["flags"]
        return cls(
            device_id=msg["device_id"],
            boot_id=msg["boot_id"],
            seq=msg["seq"],
            state=msg["state"],
            batt_v=msg.get("batt_v"),
            dist_cm=msg.get("dist_cm"),
            tipped=flags["tipped"],
            lowbatt=flags["lowbatt"],
            link_ok=flags["link_ok"],
            safety_latched=msg.get("safety_latched"),
            last_cmd_age_ms=msg.get("last_cmd_age_ms"),
            obstacle=flags.get("obstacle"),
        )


@dataclass(frozen=True, slots=True)
class Ingested:
    """수신 한 건의 결과. 사건과 관측값과 폐기 사유를 함께 돌려준다."""

    events: tuple[Event, ...] = ()
    reading: Reading | None = None
    discarded: str = ""
    warns: bool = False

    @property
    def accepted(self) -> bool:
        return self.reading is not None


#: 로봇이 보고한 온보드 상태를 그대로 옮기는 사건. **표이며 분기문이 아니다.**
#: 온보드가 스스로 아는 것은 셋뿐이고(`ONBOARD_STATES`) 나머지는 호스트가
#: `STATE` 로 내려보낸 값이 되돌아온 것이라 새 정보가 없다.
ONBOARD_EVENTS: dict[str, Event] = {
    "FAILSAFE": Event.ONBOARD_FAILSAFE,
    "AVOID": Event.ONBOARD_AVOID,
}

#: 온보드 상태에서 빠져나온 것을 알리는 사건.
#:
#: ⚠️ **이것만으로는 부족하다.** 호스트가 `AVOID` 를 `STATE` 로 알려주면 로봇이 그
#: 값을 되돌려주므로, 장애물이 사라져도 `state` 는 계속 `AVOID` 로 온다. 그래서
#: 해제의 정본은 `flags.obstacle` 의 참→거짓 변화이며(ADR-22) 이 표는 그 플래그를
#: 보내지 않는 펌웨어를 위한 폴백이다.
RECOVERY_EVENTS: dict[tuple[str, str], Event] = {
    ("AVOID", "PATROL"): Event.AVOID_CLEARED,
}


class TelemetryReceiver:
    """텔레메트리를 사건으로 바꾼다. 개체별로 마지막 상태를 기억한다.

    `device_id` 별로 나누는 이유 — 3대를 동시에 돌릴 때 IP 로 구분하면 안 되고
    (DR-17), 한 개체의 `AVOID` 회복을 다른 개체의 것으로 오인하면 안 된다.
    """

    def __init__(self, decoder: TelemetryDecoder | None = None) -> None:
        self._decoder = decoder if decoder is not None else TelemetryDecoder()
        self._last_onboard: dict[tuple[str, str], str] = {}
        self._current_boot: dict[str, str] = {}
        self._last_obstacle: dict[str, bool] = {}

    @property
    def decoder(self) -> TelemetryDecoder:
        return self._decoder

    def last_onboard_state(self, device_id: str, boot_id: str | None = None) -> str | None:
        """그 개체가 마지막으로 보고한 **온보드 상태**. 호스트 전용 상태는 담지 않는다."""
        selected_boot = boot_id if boot_id is not None else self._current_boot.get(device_id)
        if selected_boot is None:
            return None
        return self._last_onboard.get((device_id, selected_boot))

    def ingest(self, raw: str | bytes) -> Ingested:
        """레코드 하나를 받아 사건으로 바꾼다.

        **폐기된 레코드는 아무 사건도 내지 않는다.** 깨진 데이터를 근거로 상태를
        옮기면 규약의 검증 규칙이 무의미해진다 (PROTOCOL.md 5절).
        """
        result: DecodeResult = self._decoder.decode(raw)
        if not result.accepted or result.message is None:
            return Ingested(discarded=result.reason, warns=result.warns)

        reading = Reading.of(result.message)
        return Ingested(
            events=self._obstacle_events(reading) + self._events_for(reading),
            reading=reading,
            warns=result.warns,
        )

    def _obstacle_events(self, reading: Reading) -> tuple[Event, ...]:
        """근거리 반사 정지 플래그의 **변화**를 사건으로 바꾼다 (ADR-22).

        참→거짓이 곧 *"전방이 비었다"* 는 로봇의 보고다. 거짓→참은 `state` 의
        `AVOID` 가 이미 같은 것을 말하므로 여기서 따로 내지 않는다.
        """
        if reading.obstacle is None:
            return ()
        previous = self._last_obstacle.get(reading.device_id)
        self._last_obstacle[reading.device_id] = reading.obstacle
        if previous is True and reading.obstacle is False:
            return (Event.AVOID_CLEARED,)
        return ()

    def _events_for(self, reading: Reading) -> tuple[Event, ...]:
        """상태 보고를 사건으로 바꾼다. **표를 보고 옮기기만 한다.**"""
        # 호스트 전용 상태(`ALERT`·`TRACK` 등)는 우리가 `STATE` 로 내려보낸 값이
        # 되돌아온 것이므로 새 정보가 없다. 기억도 하지 않는다 — 기억하면
        # `AVOID → ALERT → PATROL` 같은 경로에서 회복을 놓친다.
        if reading.state not in ONBOARD_STATES:
            return ()

        session = (reading.device_id, reading.boot_id)
        previous = self._last_onboard.get(session)
        self._last_onboard[session] = reading.state
        self._current_boot[reading.device_id] = reading.boot_id

        # 10Hz 주기 보고는 상태 변화가 아니다. 동일 사건을 반복 발행하면 로그와
        # WebSocket 피드가 초당 10건씩 쌓이므로 엣지에서만 사건을 만든다.
        if previous == reading.state:
            return ()

        events: list[Event] = []
        if recovery := RECOVERY_EVENTS.get((previous or "", reading.state)):
            events.append(recovery)
        if onboard := ONBOARD_EVENTS.get(reading.state):
            events.append(onboard)
        return tuple(events)
