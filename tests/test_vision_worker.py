"""추론 워커 검증 (WBS 3.3.2 · NFR-3③).

**실제 8ms 추론으로 시험하면 아무것도 증명되지 않는다.** 8ms 는 100ms 주기 안에
여유롭게 들어가므로 **동기로 짜도 통과한다.** 그래서 여기서는 추론을 **일부러 느리게**
만들고, 그래도 메인 루프가 자기 주기를 지키는지 본다.

⚠️ 워커가 죽어도 로봇은 계속 걷는다 — 그래서 예외를 주입해서 **스레드가 살아남는지**와
**메인이 그것을 알아채는지**를 함께 검사한다.
"""

from __future__ import annotations

import threading
import time

import pytest

from host.common.config import load_config
from host.vision.detector import Detection
from host.vision.stream_client import Frame, FrameQueue
from host.vision.worker import TickIntervals, VisionResult, VisionWorker


@pytest.fixture
def cfg() -> dict:
    return load_config("mechdog-01")


# ── 가짜 조각 ───────────────────────────────────────────────
def _frame(seq: int) -> Frame:
    # 실제 JPEG 가 아니어도 된다 — 디코드도 주입으로 대체한다.
    # `size_bytes` 는 필드가 아니라 프로퍼티다 — 넘기면 `TypeError` 가 난다.
    return Frame(payload=b"\xff\xd8fake\xff\xd9", received_ms=1000 + seq, seq=seq)


class _FakeReader:
    """프레임을 흘려준다. **기본은 끝나지 않는다** — 실제 스트림이 그렇다.

    ⚠️ 처음에 유한한 가짜로 썼더니 수신 스레드가 조용히 끝나 `healthy()` 가 False 가
    되고 큐가 말라서 시험 셋이 깨졌다. 실제 `StreamReader.frames()` 는 재연결을 안에서
    하며 **돌아오지 않는다.** 가짜가 실물과 다르면 시험 결과도 실물과 무관해진다.

    ⚠️ 간격을 두는 이유 — 한 번에 다 쏟으면 큐가 최신 하나만 남기므로 추론이 한 번밖에
    돌지 않는다. 실제로는 25fps 로 들어온다.
    """

    def __init__(self, count: int | None = None, *, gap_s: float = 0.002) -> None:
        self._count = count
        self._gap = gap_s

    def frames(self, **_kw):
        seq = 0
        while self._count is None or seq < self._count:
            seq += 1
            if self._gap:
                time.sleep(self._gap)
            yield _frame(seq)


class _FakeDetector:
    """호출 횟수를 세고, 원하면 **일부러 느리게** 돈다.

    ⚠️ `open()` 을 반드시 갖춘다. 처음에 빼먹었더니 시험 10건이 `AttributeError` 로
    깨졌다 — **가짜가 실물의 표면을 안 갖추면 시험이 실물과 무관해진다.**
    """

    def __init__(self, *, delay_s: float = 0.0, raises: bool = False) -> None:
        self.delay_s = delay_s
        self.raises = raises
        self.calls = 0
        self.opened = 0
        self.opened_on: str | None = None

    def open(self) -> None:
        self.opened += 1
        self.opened_on = threading.current_thread().name

    def detect(self, _image):
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.raises:
            raise RuntimeError("추론 실패 흉내")
        return [Detection(label="person", score=0.9, box=(1.0, 2.0, 3.0, 4.0))]


def _worker(cfg: dict, reader, detector, monkeypatch, **kw) -> VisionWorker:
    # JPEG 디코드는 이 시험의 대상이 아니다 — 가짜 바이트가 통과하게 둔다.
    monkeypatch.setattr("host.vision.worker.decode_jpeg", lambda payload: payload)
    return VisionWorker(cfg, reader=reader, detector=detector, **kw)


def _wait_until(predicate, timeout_s: float = 3.0) -> bool:
    """조건이 참이 될 때까지 기다린다. **고정 `sleep` 을 쓰지 않는다** — 느린 CI 에서
    깨지거나, 빠른 기계에서 불필요하게 느려진다."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ── 기본 동작 ───────────────────────────────────────────────
def test_result_appears_and_is_not_blocking(cfg: dict, monkeypatch) -> None:
    detector = _FakeDetector()
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    assert worker.latest() is None, "시작 전에는 결과가 없다"
    with worker:
        assert _wait_until(lambda: worker.latest() is not None)
        result = worker.latest()
    assert isinstance(result, VisionResult)
    assert result.detections[0].label == "person"
    assert result.frame_received_ms > 0, "프레임 도착 시각을 함께 들고 다녀야 한다"


def test_latest_keeps_only_the_newest(cfg: dict, monkeypatch) -> None:
    """⚠️ **결과를 쌓지 않는다.** 낡은 검출로 판단하면 로봇이 과거를 보고 움직인다."""
    detector = _FakeDetector()
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    with worker:
        assert _wait_until(lambda: detector.calls >= 3)
        first = worker.latest()
        assert first is not None
        assert _wait_until(lambda: (worker.latest() or first).frame_seq > first.frame_seq)
    assert worker.latest().frame_seq > first.frame_seq


def test_inference_rate_is_capped(cfg: dict, monkeypatch) -> None:
    """⚠️ **상한 없이 돌리면 GPU 가 허용하는 만큼 돈다.** 25fps 입력·10fps 추론이다."""
    detector = _FakeDetector()
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    with worker:
        time.sleep(0.35)
        calls = detector.calls
    # 10fps 면 350ms 에 3~4회. 상한이 없으면 수백 회가 된다.
    assert calls <= 10, f"추론률 상한이 지켜지지 않았다: {calls}회"


def test_session_opens_on_the_caller_thread_before_workers_run(cfg: dict, monkeypatch) -> None:
    """⚠️ **세션 생성을 워커 스레드에 두면 메인 루프가 막힌다.**

    실제로 그렇게 만들어 재 봤더니 기동 **+684ms 지점에서 틱 간격이 582ms** 로
    벌어졌다 — `cmd_timeout_ms`(300ms)를 넘겨 **로봇이 멈추는 값**이다. 세션 생성
    632ms 가 C++ 안이지만 GIL 을 고르게 놓지 않는다(DirectML 장치 초기화 포함).

    ⚠️ **느린 추론 주입 시험은 이것을 잡지 못했다.** 주입한 지연은 `time.sleep` 이라
    GIL 을 완벽히 놓기 때문이다. **가짜가 실물보다 친절했다** — 그래서 실물 프로파일이
    필요했고, 여기서는 "어느 스레드에서 열렸나" 를 직접 본다.
    """
    detector = _FakeDetector()
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    main_thread = threading.current_thread().name
    worker.start()
    try:
        assert detector.opened == 1, "start() 가 세션을 열어야 한다"
        assert detector.opened_on == main_thread, "부르는 스레드에서 열어야 한다"
    finally:
        worker.stop()


# ── ⚠️ 핵심: 느린 추론이 메인을 막지 않는다 ────────────────
def test_slow_inference_does_not_block_the_caller(cfg: dict, monkeypatch) -> None:
    """**추론이 도는 중에도 `latest()` 는 즉시 돌아온다.**

    이것이 3.3.2 의 존재 이유다. 동기로 짰다면 `latest()` 를 부르는 쪽이 추론을
    기다리게 되고, 그 순간 로봇은 `cmd_timeout_ms`(300ms)를 넘겨 멈춘다.

    ⚠️ **지연을 실제(8ms)로 두면 이 시험은 아무것도 구분하지 못한다** — 동기
    구현도 통과한다. 다만 필요한 것은 "조회하는 동안 추론이 진행 중" 이므로
    100ms 면 충분하다. 시험 시간을 낭비하지 않는다.
    """
    detector = _FakeDetector(delay_s=0.1)
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    with worker:
        assert _wait_until(lambda: detector.calls >= 1)
        # 추론이 돌고 있는 중에 100번 물어본다.
        started = time.perf_counter()
        for _ in range(100):
            worker.latest()
            worker.age_ms(1_000_000)
        elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 50, f"조회가 추론에 붙잡혔다: {elapsed_ms:.1f}ms"


def test_main_loop_keeps_its_period_under_slow_inference(cfg: dict, monkeypatch) -> None:
    """**추론이 `cmd_timeout_ms` 보다 길어도** 메인 루프가 자기 주기를 지킨다.

    ⚠️ **350ms 를 고른 이유가 있다.** 이 값이 300ms(`cmd_timeout_ms`)를 넘으므로,
    동기 구현이라면 **로봇이 확실히 멈추는** 조건이다. 그보다 짧으면 통과해도
    "여유가 있었다" 일 뿐 구조를 증명하지 못한다.
    """
    detector = _FakeDetector(delay_s=0.35)
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    intervals = TickIntervals(limit_ms=300)
    with worker:
        for _ in range(12):
            worker.latest()  # 메인 루프가 매 틱에 하는 일
            intervals.note(int(time.perf_counter() * 1000))
            time.sleep(0.02)
    assert intervals.late == 0, f"틱이 상한을 넘었다: {intervals.digest()}"
    assert intervals.max_ms < 150, f"간격이 튀었다: {intervals.digest()}"


# ── ⚠️ 조용한 고장 ─────────────────────────────────────────
def test_inference_exception_does_not_kill_the_thread(cfg: dict, monkeypatch) -> None:
    """⚠️ **예외로 스레드가 사라지면 검출이 멈춘 것을 아무도 모른다.**

    로봇은 계속 순찰한다. 그래서 세고, 남기고, **스레드는 살려 둔다.**
    """
    detector = _FakeDetector(raises=True)
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    with worker:
        assert _wait_until(lambda: worker.stats.errors >= 2)
        assert worker.healthy(), "예외가 스레드를 죽여서는 안 된다"
        assert worker.latest() is None, "실패한 추론이 결과로 남아서는 안 된다"
    assert worker.stats.last_error is not None
    assert "RuntimeError" in worker.stats.last_error


def test_stalled_distinguishes_startup_from_disconnect(cfg: dict, monkeypatch) -> None:
    """⚠️ **결과가 아직 없는 것과 끊긴 것은 다르다.**

    기동 직후를 단절로 보면 매 기동마다 거짓 경보가 난다.
    """
    worker = _worker(cfg, _FakeReader(count=0), _FakeDetector(), monkeypatch)
    assert worker.stalled(now_ms=10_000_000) is False, "결과가 없으면 단절이 아니다"
    assert worker.age_ms(now_ms=10_000_000) is None


def test_stalled_after_timeout(cfg: dict, monkeypatch) -> None:
    detector = _FakeDetector()
    worker = _worker(cfg, _FakeReader(), detector, monkeypatch)
    with worker:
        assert _wait_until(lambda: worker.latest() is not None)
        result = worker.latest()
    stall_ms = int(cfg["vision"]["stall_timeout_ms"])
    assert worker.stalled(result.completed_ms + stall_ms + 1) is True
    assert worker.stalled(result.completed_ms + 1) is False


def test_stop_is_idempotent_and_bounded(cfg: dict, monkeypatch) -> None:
    """종료가 무한히 기다리면 `ESTOP` 송신이 그 뒤로 밀린다."""
    worker = _worker(cfg, _FakeReader(), _FakeDetector(), monkeypatch)
    worker.start()
    worker.start()  # 두 번 불러도 스레드가 늘어나지 않는다
    started = time.perf_counter()
    worker.stop(timeout_s=0.5)
    worker.stop(timeout_s=0.5)
    assert (time.perf_counter() - started) < 2.0


def test_queue_is_shared_and_drops_old_frames(cfg: dict, monkeypatch) -> None:
    """수신이 추론보다 빠른 것이 정상이다 — 큐가 드롭을 센다 (4.3.5)."""
    queue = FrameQueue()
    detector = _FakeDetector(delay_s=0.05)
    worker = _worker(cfg, _FakeReader(gap_s=0.0), detector, monkeypatch, queue=queue)
    with worker:
        assert _wait_until(lambda: queue.dropped > 0, timeout_s=2.0)
    assert queue.received > queue.dropped >= 1


# ── 틱 간격 기록 ────────────────────────────────────────────
def test_intervals_catch_a_gap_that_counts_would_hide() -> None:
    """⚠️ **개수는 평균만 말한다.**

    601회/60초는 중간에 500ms 벌어진 것을 숨긴다. 그 사이 로봇은 멈췄을 것이다.
    """
    smooth = TickIntervals(limit_ms=300)
    spiky = TickIntervals(limit_ms=300)
    for i in range(7):
        smooth.note(i * 100)
    for gap in (0, 100, 200, 700, 720, 740, 840):  # 같은 7회, 중간에 500ms 구멍
        spiky.note(gap)

    assert len(smooth.samples) == len(spiky.samples), "개수로는 구분되지 않는다"
    assert smooth.late == 0 and smooth.max_ms == 100
    assert spiky.late == 1 and spiky.max_ms == 500


def test_intervals_window_is_bounded() -> None:
    """장시간 운용에서 무한히 쌓이지 않는다. **최악값은 따로 보존한다.**"""
    intervals = TickIntervals(limit_ms=300, window=10)
    intervals.note(0)
    intervals.note(5000)  # 큰 값을 먼저 넣고
    for i in range(50):
        intervals.note(5000 + (i + 1) * 100)
    assert len(intervals.samples) == 10
    assert intervals.max_ms == 5000, "창에서 밀려나도 최악값은 남아야 한다"


def test_intervals_first_tick_has_no_gap() -> None:
    intervals = TickIntervals(limit_ms=300)
    intervals.note(12345)
    assert intervals.samples == [] and intervals.max_ms == 0
    assert intervals.digest()["p95_ms"] is None


# ── 런타임 연결 ─────────────────────────────────────────────
def test_runtime_runs_without_vision(cfg: dict) -> None:
    """⚠️ **카메라가 없어도 순찰·회피는 돌아야 한다** (NFR-2.6)."""
    from host.runtime import Runtime

    runtime = Runtime(cfg, device_id="mechdog-01")
    assert runtime.vision is None
    assert runtime.tick(1000) or True  # 죽지 않는다
    assert runtime.intervals.digest()["limit_ms"] == cfg["safety"]["cmd_timeout_ms"]


def test_runtime_polls_vision_without_waiting(cfg: dict) -> None:
    """런타임은 결과를 **집어 올 뿐** 기다리지 않는다."""
    from host.runtime import Runtime

    class _Slot:
        def __init__(self) -> None:
            self.polls = 0

        def latest(self):
            self.polls += 1
            return

        def healthy(self) -> bool:
            return True

        def stalled(self, _now_ms: int) -> bool:
            return False

        def age_ms(self, _now_ms: int):
            return None

        def stop(self) -> None:
            pass

    slot = _Slot()
    runtime = Runtime(cfg, device_id="mechdog-01", vision=slot)
    runtime.tick(1000)
    runtime.tick(1100)
    assert slot.polls == 2, "틱마다 한 번 본다"


def test_runtime_records_tick_intervals(cfg: dict) -> None:
    from host.runtime import Runtime

    runtime = Runtime(cfg, device_id="mechdog-01")
    for now in (0, 100, 200, 900):  # 마지막이 700ms 벌어짐
        runtime.tick(now)
    digest = runtime.intervals.digest()
    assert digest["max_ms"] == 700
    assert digest["late"] == 1, "cmd_timeout_ms 초과를 세야 한다"


def test_worker_threads_are_daemons(cfg: dict, monkeypatch) -> None:
    """⚠️ 데몬이 아니면 **프로세스가 종료되지 않는다.**"""
    worker = _worker(cfg, _FakeReader(), _FakeDetector(), monkeypatch)
    with worker:
        names = {t.name for t in threading.enumerate() if t.name.startswith("vision-")}
        assert names == {"vision-recv", "vision-infer"}
        assert all(t.daemon for t in threading.enumerate() if t.name.startswith("vision-"))
