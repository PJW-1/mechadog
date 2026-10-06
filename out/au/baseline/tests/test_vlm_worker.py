"""VLM 워커 검증 (WBS 4.8.0 · ADR-35).

**판독을 일부러 느리게 만들어 메인이 밀리지 않는지 본다.** 실제 0.65초로는 증명이
안 된다 — 동기로 짜도 시험이 통과한다. `test_vision_worker` 가 같은 이유로 느린
검출기를 주입한다.

여기서 지키는 것은 넷이다.

    ① submit() 이 즉시 돌아온다 — 판독이 느려도
    ② 겹친 요청을 쌓지 않고 거절한다
    ③ 결과는 한 번만 소비된다
    ④ 스레드가 터져도 호출부로 새지 않는다
"""

from __future__ import annotations

import threading
import time

from host.vision.vlm_reader import QUESTIONS, VlmReader
from host.vision.vlm_worker import VlmWorker

#: 실제 판독(0.65초)보다 짧지만 메인 틱(100ms)보다는 확실히 긴 값.
SLOW_S = 0.25


class SlowSession:
    """질문 하나에 `delay` 를 쓰는 가짜 세션."""

    def __init__(self, delay: float = SLOW_S, *, boom: bool = False) -> None:
        self._delay = delay
        self._boom = boom
        self.asked = 0
        self.closed = 0

    def ask(self, _image: object, _prompt: str) -> str:
        if self._boom:
            raise RuntimeError("추론 폭발")
        time.sleep(self._delay)
        self.asked += 1
        return "yes"

    def close(self) -> None:
        self.closed += 1


def _loaded_worker(session: SlowSession) -> VlmWorker:
    reader = VlmReader(lambda: session, budget_ms=10_000)
    reader.load()
    return VlmWorker(reader)


def _wait_idle(worker: VlmWorker, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while worker.busy and time.monotonic() < deadline:
        time.sleep(0.005)


# ── ① 메인을 막지 않는다 ────────────────────────────────────


def test_submit_returns_immediately_even_when_reading_is_slow() -> None:
    """⚠️ 여기서 막히면 로봇이 구역 앞에서 주저앉는다 (cmd_timeout 300ms)."""
    session = SlowSession(SLOW_S)
    worker = _loaded_worker(session)

    started = time.monotonic()
    accepted = worker.submit(object(), now_ms=1)
    elapsed = time.monotonic() - started

    assert accepted is True
    # 판독 한 건이 SLOW_S × 질문수 인데 submit 은 그 100분의 1도 쓰지 않아야 한다.
    assert elapsed < SLOW_S / 2, f"submit 이 {elapsed:.3f}초 걸렸다 — 메인을 막고 있다"
    _wait_idle(worker)


def test_reading_lands_in_the_slot_after_the_thread_finishes() -> None:
    session = SlowSession(0.01)
    worker = _loaded_worker(session)
    worker.submit(object(), now_ms=7)
    _wait_idle(worker)

    reading = worker.take()
    assert reading is not None
    assert reading.taken_at_ms == 7
    assert len(reading.answers) == len(QUESTIONS)
    assert reading.get("person_down") is True


# ── ② 겹친 요청을 쌓지 않는다 ───────────────────────────────


def test_second_submit_while_busy_is_refused() -> None:
    """⚠️ 쌓으면 구역을 떠난 뒤에 그 구역의 판독이 도착한다."""
    session = SlowSession(SLOW_S)
    worker = _loaded_worker(session)

    assert worker.submit(object(), now_ms=1) is True
    assert worker.submit(object(), now_ms=2) is False
    assert worker.refused == 1
    assert worker.submitted == 1
    _wait_idle(worker)


def test_submit_works_again_once_idle() -> None:
    session = SlowSession(0.01)
    worker = _loaded_worker(session)
    assert worker.submit(object(), now_ms=1) is True
    _wait_idle(worker)
    assert worker.submit(object(), now_ms=2) is True
    _wait_idle(worker)
    assert worker.submitted == 2


def test_submit_is_refused_when_the_reader_is_not_loaded() -> None:
    """가중치가 없으면 조용히 거절한다 — 예외가 아니다."""
    worker = VlmWorker(VlmReader(None))
    assert worker.available is False
    assert worker.submit(object(), now_ms=1) is False
    assert worker.take() is None
    # 거절 횟수에 세지 않는다. 겹침이 아니라 «없음» 이다.
    assert worker.refused == 0


# ── ③ 결과는 한 번만 소비된다 ──────────────────────────────


def test_take_empties_the_slot() -> None:
    """⚠️ 남겨 두면 다음 구역에서 지난 구역의 판독을 자기 것으로 읽는다."""
    session = SlowSession(0.01)
    worker = _loaded_worker(session)
    worker.submit(object(), now_ms=1)
    _wait_idle(worker)

    assert worker.take() is not None
    assert worker.take() is None


def test_peek_does_not_empty_the_slot() -> None:
    session = SlowSession(0.01)
    worker = _loaded_worker(session)
    worker.submit(object(), now_ms=1)
    _wait_idle(worker)

    assert worker.peek() is not None
    assert worker.peek() is not None
    assert worker.take() is not None
    assert worker.peek() is None


# ── ④ 스레드가 터져도 로봇은 걷는다 ────────────────────────


def test_exploding_session_never_reaches_the_caller() -> None:
    """`VlmReader.read()` 가 이미 삼키지만, 워커도 한 겹 더 막는다."""
    session = SlowSession(0.0, boom=True)
    worker = _loaded_worker(session)
    assert worker.submit(object(), now_ms=1) is True
    _wait_idle(worker)
    # 판독기가 실패를 기능 저하로 접으므로 결과 자체는 온다.
    reading = worker.take()
    assert reading is not None
    assert reading.degraded is True


def test_reader_that_raises_outright_is_contained() -> None:
    """판독기가 예외를 올려도 워커 스레드에서 멈춘다."""

    class Exploding(VlmReader):
        def read(self, image: object, *, now_ms: int):  # type: ignore[override]  # noqa: ARG002
            raise RuntimeError("판독기 폭발")

    reader = Exploding(lambda: SlowSession(0.0))
    reader.load()
    worker = VlmWorker(reader)
    assert worker.submit(object(), now_ms=1) is True
    _wait_idle(worker)
    assert worker.errors == 1
    assert worker.take() is None


def test_stop_waits_for_the_running_read() -> None:
    session = SlowSession(0.05)
    worker = _loaded_worker(session)
    worker.submit(object(), now_ms=1)
    worker.stop(timeout_s=5.0)
    assert worker.busy is False


def test_worker_thread_is_a_daemon() -> None:
    """⚠️ 데몬이 아니면 메인이 끝나도 프로세스가 안 죽는다."""
    session = SlowSession(0.2)
    worker = _loaded_worker(session)
    worker.submit(object(), now_ms=1)
    alive = [t for t in threading.enumerate() if t.name == "vlm-read"]
    assert alive and all(t.daemon for t in alive)
    _wait_idle(worker)


def test_submit_passes_the_question_keys_to_the_reader() -> None:
    """물을 항목을 넘기면 그것만 묻는다 — 순찰 판독이 0.65초를 다 쓰지 않게 (S6)."""
    session = SlowSession(0.01)
    worker = _loaded_worker(session)
    worker.submit(object(), now_ms=1, keys=("person_down",))
    _wait_idle(worker)

    reading = worker.take()
    assert reading is not None
    assert [answer.key for answer in reading.answers] == ["person_down"]
    assert session.asked == 1
