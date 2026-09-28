"""운용 스레드에서 관제 서버로 넘기는 최신 상태와 사건 (WBS 4.5.1 · 4.4.3)."""

import copy
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

#: 브라우저가 늦게 붙거나 잠깐 끊겨도 최근 사건을 받을 수 있게 남겨 두는 개수.
#: ⚠️ **무한히 쌓지 않는다** — 운용 스레드가 쓰는 메모리이고, 원본은 블랙박스가
#: 디스크에 갖고 있다. 이 버퍼를 넘겨 못 받은 것은 `dropped` 로 알려 준다.
EVENT_BUFFER = 64


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class DashboardState:
    """네트워크 작업 없이 상태를 교환한다. 잠금 안에서는 참조만 교체한다."""

    def __init__(
        self,
        device_id: str,
        *,
        stale_after_ms: int,
        clock: Callable[[], int] = monotonic_ms,
    ) -> None:
        if stale_after_ms <= 0:
            raise ValueError("stale_after_ms must be positive")
        self._clock = clock
        self._stale_after_ms = stale_after_ms
        self._lock = threading.Lock()
        self._value: dict[str, Any] = {
            "type": "telemetry",
            "device_id": device_id,
            "state": None,
            "escalation": None,
            # 첫 `publish` 전에는 모른다 — `state`·`escalation` 과 같은 규칙이다.
            # 화면은 `null` 을 *"아직 수신 전"* 으로 그린다.
            "mode": None,
            "telemetry": None,
        }
        self._received_at: int | None = None
        self._updated_at: int | None = None
        # 사건 로그 (WBS 4.4.3). 텔레메트리와 달리 **합치지 않는다** — 최신만
        # 남기면 사람 감지 기록이 사라지고, 그것이 이 채널의 존재 이유다.
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER)
        self._event_seq = 0

    def now(self) -> int:
        return self._clock()

    def publish(
        self,
        *,
        telemetry: dict[str, Any] | None,
        state: str,
        escalation: str,
        mode: str,
        received_at: int | None,
    ) -> None:
        value = {
            "type": "telemetry",
            "device_id": self._value["device_id"],
            "state": state,
            "escalation": escalation,
            # ⚠️ **상시 실린다** (FR-4.7 · FR-11.5). 공장 모드인 줄 모르고 보면
            # *"사람이 지나갔는데 인증을 요구하지 않는다"* 가 고장으로 읽힌다.
            "mode": mode,
            "telemetry": copy.deepcopy(telemetry),
        }
        updated_at = self._clock()
        with self._lock:
            self._value = value
            self._received_at = received_at
            self._updated_at = updated_at

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value, received_at, updated_at = self._value, self._received_at, self._updated_at
        result = copy.deepcopy(value)
        now = self._clock()
        age = None if received_at is None else max(0, now - received_at)
        runtime_age = None if updated_at is None else max(0, now - updated_at)
        result.update(
            telemetry_age_ms=age,
            stale=age is None or age >= self._stale_after_ms,
            runtime_age_ms=runtime_age,
            runtime_stale=runtime_age is None or runtime_age >= self._stale_after_ms,
            # 로봇 uptime과 PC 시계는 동기화되지 않았다. age나 명령 age를 RTT로 쓰지 않는다.
            link_rtt_ms=None,
        )
        return result

    # ── 사건 (WBS 4.4.3 · FR-3.9) ────────────────────────────
    def record_event(self, payload: dict[str, Any]) -> int:
        """사건 하나를 버퍼에 넣고 부여한 순번을 돌려준다.

        ⚠️ **운용 스레드에서 불린다.** 네트워크도 디스크도 만지지 않는다 — 기록은
        블랙박스가 이미 했고 여기서는 브라우저에 넘길 사본만 쌓는다. 그래서
        실패할 여지가 없어야 한다.
        """
        with self._lock:
            self._event_seq += 1
            event = dict(copy.deepcopy(payload), type="event", seq=self._event_seq)
            self._events.append(event)
            return self._event_seq

    def events_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        """``cursor`` 뒤의 사건과 **버퍼에서 밀려 못 주는 개수**를 함께 돌려준다.

        ⚠️ **빠진 것을 조용히 넘기지 않는다.** 브라우저가 오래 끊겼으면 앞쪽이
        버퍼에서 밀려 나갔는데, 그것을 말하지 않으면 사람이 *"그 사이에 아무 일도
        없었다"* 고 읽는다. 사건 피드에서 그것은 거짓 안심이다.
        """
        with self._lock:
            events = [event for event in self._events if event["seq"] > cursor]
            oldest = self._events[0]["seq"] if self._events else self._event_seq + 1
            latest = self._event_seq
        dropped = max(0, min(oldest - 1, latest) - cursor) if cursor + 1 < oldest else 0
        return copy.deepcopy(events), dropped

    @property
    def event_seq(self) -> int:
        with self._lock:
            return self._event_seq
