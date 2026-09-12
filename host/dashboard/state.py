"""운용 스레드에서 관제 서버로 넘기는 최신 상태 한 건 (WBS 4.5.1)."""

import copy
import threading
import time
from collections.abc import Callable
from typing import Any


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
            "telemetry": None,
        }
        self._received_at: int | None = None
        self._updated_at: int | None = None

    def now(self) -> int:
        return self._clock()

    def publish(
        self,
        *,
        telemetry: dict[str, Any] | None,
        state: str,
        escalation: str,
        received_at: int | None,
    ) -> None:
        value = {
            "type": "telemetry",
            "device_id": self._value["device_id"],
            "state": state,
            "escalation": escalation,
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
