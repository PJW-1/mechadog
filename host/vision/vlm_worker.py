"""VLM 판독을 운용 루프 밖의 스레드에서 돌린다 (ADR-35).

판독 한 번은 수백 ms 가 걸려 운용 루프(10Hz)에서 부르면 `safety.cmd_timeout_ms` 를 넘긴다.

    ① submit()   요청을 스레드에 넘기고 즉시 돌아온다
    ② [스레드]   판독
    ③ take()     운용 루프가 막지 않고 결과를 가져간다 (슬롯을 비운다)

- 일감은 한 번에 하나다 — 겹치는 요청은 거절한다(큐에 쌓지 않는다). 결과에 구역 이름이
  없으므로 건 구역은 호출부가 붙들어 둔다(`ZoneInspector._take_reading`).
- 스레드 예외는 세고 삼키지 않으며, 판독 실패는 주행에 영향을 주지 않는다 (ADR-35 결정 6).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any

from host.common.logging_setup import event_logger
from host.vision.vlm_reader import Reading, VlmReader

LOG = event_logger("mechadog.vision")

#: 스레드 정리 대기 상한 — 종료 경로를 막지 않는다.
JOIN_TIMEOUT_S = 2.0


class VlmWorker:
    """판독 한 건을 스레드에서 돌리고 결과를 슬롯에 둔다. `reader` 는 시험을 위해 주입받는다.

    `start`·`submit`·`take`·`stop` 은 운용 루프 스레드에서 부른다.
    """

    def __init__(self, reader: VlmReader) -> None:
        self._reader = reader
        self._lock = threading.Lock()
        self._slot: Reading | None = None
        self._thread: threading.Thread | None = None
        #: 적재 스레드 (`start`). 종료가 적재 도중인지 여기서 본다.
        self._loader: threading.Thread | None = None
        self.submitted = 0
        self.refused = 0
        self.errors = 0

    @property
    def busy(self) -> bool:
        """판독이 돌고 있나."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def loading(self) -> bool:
        """모델 적재 스레드가 아직 살아 있나.

        적재는 수초~수십 초 동안 CPU·GIL 을 잡아먹는다 — 이 동안 보행을 시작하면
        명령 공백이 `safety.cmd_timeout_ms` 를 넘길 수 있다 (2026-10-02 실기:
        `cmd_gap 585ms` → `ONBOARD_FAILSAFE`).
        """
        loader = self._loader
        return loader is not None and loader.is_alive()

    @property
    def available(self) -> bool:
        """판독기를 쓸 수 있나. 가중치가 없으면 거짓이다."""
        return self._reader.loaded

    def start(self) -> None:
        """모델을 올리는 스레드를 띄우고 즉시 돌아온다. 올라오는 동안 `submit()` 은 거짓이다.

        ⚠️ 기동 때 모드와 상관없이 한 번만 부른다 (ADR-35 결정 5) — 적재가 겹치면 VRAM 에
        두 벌이 올라간다.
        """
        self._loader = threading.Thread(target=self._reader.load, name="vlm-load", daemon=True)
        self._loader.start()

    def submit(self, image: Any, *, now_ms: int, keys: Sequence[str] | None = None) -> bool:
        """판독을 걸고 즉시 돌아온다. 받았으면 참, 이미 돌고 있거나 판독기가 없으면 거짓."""
        if not self._reader.loaded:
            return False
        if self.busy:
            self.refused += 1
            return False
        thread = threading.Thread(
            target=self._run, args=(image, now_ms, keys), name="vlm-read", daemon=True
        )
        self._thread = thread
        self.submitted += 1
        thread.start()
        return True

    def take(self) -> Reading | None:
        """결과를 가져가고 슬롯을 비운다. 없으면 `None`. 막지 않는다."""
        with self._lock:
            reading, self._slot = self._slot, None
        return reading

    def peek(self) -> Reading | None:
        """비우지 않고 들여다본다. 대시보드처럼 소비하지 않는 쪽이 쓴다."""
        with self._lock:
            return self._slot

    def stop(self, timeout_s: float = JOIN_TIMEOUT_S) -> None:
        """돌고 있는 판독을 상한까지 기다린 뒤 모델을 내린다(강제로 끊지 않는다).

        적재 중이면 내리지 않고 나간다 — 데몬 스레드라 프로세스가 끝나면 VRAM 도 풀린다.
        ⚠️ `submit()` 과 같은 스레드(운용 루프)에서 부른다 — `_thread` 를 락 없이 읽는다.
        """
        loader = self._loader
        if loader is not None:
            loader.join(timeout=max(0.0, timeout_s))
            if loader.is_alive():
                LOG.warning("vlm_unload_skipped", reason="loading")
                return
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout_s))
        self._thread = None
        self._reader.unload()

    def _run(self, image: Any, now_ms: int, keys: Sequence[str] | None) -> None:
        started = time.monotonic()
        try:
            reading = self._reader.read(image, now_ms=now_ms, keys=keys)
        except Exception as exc:  # noqa: BLE001 — 스레드에서 새면 조용히 사라진다
            self.errors += 1
            LOG.warning("vlm_worker_failed", error=type(exc).__name__, detail=str(exc))
            return
        with self._lock:
            self._slot = reading
        LOG.info(
            "vlm_reading",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            degraded=reading.degraded,
            reason=reading.reason,
            **{answer.key: answer.value for answer in reading.answers},
        )


__all__ = ["VlmWorker"]
