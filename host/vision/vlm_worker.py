"""VLM 판독을 메인 루프 밖에서 돌린다 (WBS 4.8.0 · ADR-35).

`VlmReader.read()` 는 한 번에 **0.65초**가 걸린다(실측 0.22초 × 3질문 ·
`TEST_MECHDOG/results/20260920_4.8.0-vlm-compare/`). 메인 틱은 10Hz 이고 로봇은
`safety.cmd_timeout_ms`(300ms) 동안 명령을 못 받으면 스스로 선다 — **판독을 메인에서
부르면 로봇이 구역 앞에서 주저앉는다.**

그래서 `worker.py` 가 검출에 쓴 모양을 그대로 쓴다.

    ① submit()   요청을 스레드에 넘기고 **즉시 돌아온다**
    ② [스레드]   0.65초 동안 판독
    ③ take()     메인이 **막지 않고** 결과를 가져간다

⚠️ **일감은 한 번에 하나다.** 요청이 겹치면 뒤엣것을 **거절한다**(큐에 쌓지 않는다).
쌓으면 구역을 떠난 뒤에 그 구역의 판독이 도착하고, 그것은 *"다른 시점의 관찰이 이번
사이클을 채우는"* 것과 같은 고장이다(`ChangeConfirmer` 주석 참조).

⚠️ **결과는 한 번만 소비된다.** `take()` 가 슬롯을 비운다. 결과에는 어느 구역의
것인지가 없으므로, 건 구역은 호출부가 붙들어 둔다(`runtime._take_zone_reading`).

⚠️ **스레드가 죽어도 로봇은 걷는다.** 예외는 삼키지 않고 세며, 판독이 없으면 그냥
없는 채로 간다. 실패·타임아웃은 주행에 영향을 주지 않는다(ADR-35 결정 6). 구역에서
결과를 기다리는 것은 호출부의 일이며 상한이 있다(2026-09-24 개정).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any

from host.common.logging_setup import event_logger
from host.vision.vlm_reader import Reading, VlmReader

LOG = event_logger("mechadog.vision")

#: 스레드를 정리하며 기다리는 최대 시간. `worker.py` 와 같은 이유로 무한정 기다리지 않는다.
JOIN_TIMEOUT_S = 2.0


class VlmWorker:
    """판독 한 건을 스레드에서 돌리고 결과를 슬롯에 둔다.

    `reader` 를 주입받는 이유는 `VisionWorker` 가 `detector` 를 받는 것과 같다 —
    **모델도 GPU 도 없이 시험돼야 하고**, 특히 판독을 일부러 느리게 만들어 메인이
    밀리지 않는지 봐야 한다. 실제 0.65초로는 그것을 증명할 수 없다.
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
    def available(self) -> bool:
        """판독기를 쓸 수 있나. 가중치가 없으면 거짓이다."""
        return self._reader.loaded

    def start(self) -> None:
        """모델을 올리는 스레드를 띄우고 **즉시 돌아온다** (적재 14.5초).

        기동할 때 모드와 상관없이 **한 번만** 부른다 — ADR-35 결정 5 (2026-09-24 개정:
        상시 적재). `VisionWorker.start()` 처럼 기동 비용으로 내면 운용 루프가 14.5초
        늦게 서므로 메인 밖으로 뺀다. 올라오는 동안 `submit()` 은 거짓을 돌려준다.

        ⚠️ **두 번 부르지 마라.** `VlmReader.load()` 는 스레드 안전하지 않아 적재가
        겹치면 4.1GB 가 두 벌 올라간다.
        """
        self._loader = threading.Thread(target=self._reader.load, name="vlm-load", daemon=True)
        self._loader.start()

    def submit(self, image: Any, *, now_ms: int, keys: Sequence[str] | None = None) -> bool:
        """판독을 걸고 **즉시 돌아온다.** 받았으면 참, 거절했으면 거짓.

        ⚠️ **거절이 정상이다.** 이미 돌고 있거나 판독기가 없으면 거짓을 돌려주고,
        호출부는 그냥 지나간다 — 기다리면 그 자리가 메인 루프다.
        """
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
        """결과를 가져가고 **슬롯을 비운다.** 없으면 `None`. 막지 않는다."""
        with self._lock:
            reading, self._slot = self._slot, None
        return reading

    def peek(self) -> Reading | None:
        """비우지 않고 들여다본다. 대시보드처럼 소비하지 않는 쪽이 쓴다."""
        with self._lock:
            return self._slot

    def stop(self, timeout_s: float = JOIN_TIMEOUT_S) -> None:
        """돌고 있는 판독 하나를 기다린 뒤 모델을 내린다. **강제로 끊지 않는다.**

        네이티브 추론 중간에 끊을 방법이 없고, 종료 경로에는 `ESTOP` 송신이 있어
        오래 기다릴 수도 없다. `VisionWorker.stop()` 과 같은 타협이다 — 상한을 넘기면
        그래도 내린다.

        ⚠️ **적재 중이면 내리지 않고 나간다.** 적재(14.5초)를 끝까지 기다리면 종료가
        막히고, 기다리지 않고 내리면 적재가 끝난 뒤에 세션이 생겨 남는다. 스레드는
        데몬이라 프로세스가 끝나면 VRAM 도 풀린다.

        ⚠️ **`submit()` 과 같은 스레드(운용 루프)에서 부른다.** 락 없이 `_thread` 를
        읽으므로 다른 스레드에서 부르면 시작 전 스레드를 join 할 수 있다.
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
