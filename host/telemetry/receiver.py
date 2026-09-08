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

#: 온보드 상태에서 빠져나온 것을 알리는 사건. 로봇이 `AVOID` 를 벗어나
#: `PATROL` 을 보고하면 회피가 끝난 것이다 — 호스트는 그것으로만 알 수 있다.
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
            events=self._events_for(reading),
            reading=reading,
            warns=result.warns,
        )

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
