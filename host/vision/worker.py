"""추론 워커 스레드 (WBS 3.3.2 · NFR-1.2 · NFR-3③).

**비전 경로를 처음으로 실제로 연결하는 곳이다.** 지금까지 조각은 다 있었지만
(`StreamReader` · `FrameQueue` · `Detector`) 아무것도 이어져 있지 않았다.

    ① 수신 스레드   StreamReader.frames() → FrameQueue.put()
    ② 추론 스레드   FrameQueue.latest() → decode_jpeg → Detector.detect()
                            ↓ 최신 결과 하나만 담는 슬롯
    ③ 메인 루프     latest() 를 **무블로킹으로** 읽는다 (여기 없음 — `runtime.py`)

⚠️ **메인 루프를 막으면 로봇이 멈춘다.** 로봇은 `safety.cmd_timeout_ms`(300ms) 동안
명령을 못 받으면 스스로 정지한다. 우리가 100ms 마다 보내므로 여유는 3배뿐이고,
추론이 메인 스레드에서 돌면 그 여유가 사라진다. **그것이 이 모듈의 존재 이유다.**

⚠️ **파이썬 스레드로 충분한 이유** — 추론 시간의 거의 전부가 C++ 안이다(onnxruntime ·
OpenCV · numpy). 그 구간에서는 GIL 을 놓으므로 메인 스레드가 자유롭다. 반대로 순수
파이썬 계산을 스레드에 넣으면 GIL 때문에 **메인을 실제로 밀어낸다.**

⚠️ **워커가 죽어도 로봇은 계속 걷는다 — 그것이 가장 위험하다.** 스레드에서 예외가
나면 스레드만 사라지고 메인은 아무것도 모른다. 그래서 ① 예외를 삼키지 않고 세며
② 스레드를 죽이지 않고 살려 두고 ③ **마지막 결과의 나이를 메인이 감시한다.**
로봇이 우리 명령을 폐기하는 조용한 고장을 `last_cmd_age_ms` 로 잡은 것과 같은 형태다.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from host.common.logging_setup import event_logger
from host.common.protocol import system_clock_ms
from host.vision.badge import BadgeReader, Marker
from host.vision.detector import Detection
from host.vision.person import PersonGate, Sighting
from host.vision.stream_client import Frame, FrameQueue, decode_jpeg
from host.vision.tracker import PersonTracker, Track

LOG = event_logger("mechadog.vision")

#: 큐가 비었을 때 다시 볼 때까지의 간격. **바쁜 대기를 하지 않는다** — 그러면 GIL 을
#: 계속 잡아 메인 루프를 밀어낸다. 정지 신호로 즉시 깨어날 수 있게 `Event.wait` 를 쓴다.
IDLE_WAIT_S = 0.005

#: 스레드를 정리하며 기다리는 최대 시간. **무한히 기다리지 않는다** — 종료 경로에는
#: `ESTOP` 송신이 있고, 그것이 워커를 기다리다 늦어지면 안 된다.
JOIN_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class VisionResult:
    """한 프레임의 검출 결과. **프레임의 도착 시각을 함께 들고 다닌다.**

    ⚠️ 결과만 넘기면 **그것이 언제 찍힌 것인지 알 수 없다.** 추론이 밀리면 결과가
    낡는데, 나이를 모르면 낡은 판단을 최신처럼 쓴다.
    """

    detections: tuple[Detection, ...]
    #: 블랙박스에 보관할 **수신 JPEG 원본**. 다시 인코딩하면 시간과 화질이 달라져
    #: 사건 당시 실제 입력을 보존했다는 의미가 사라진다 (FR-3.9).
    jpeg: bytes
    frame_seq: int
    frame_received_ms: int
    completed_ms: int
    inference_ms: float
    #: 사람 판정 (FR-3.2). ⚠️ **게이트는 추론마다 관측해야 한다** — 메인 루프(10Hz)에서
    #: 부르면 25fps 결과 중 10개만 보게 되고, 그러면 추론률을 올린 이유가 사라진다.
    sighting: Sighting
    #: 지속 ID 가 붙은 사람들 (FR-3.6 · `3.3.4`). **이번 프레임에 보인 대상만**이며
    #: 소실 버퍼에 있는 대상은 들어 있지 않다.
    #:
    #: ⚠️ **게이트와 목적이 다르다.** 게이트는 *"사람이 있는가"*(로봇 단위)이고 이쪽은
    #: *"누구인가"*(개인별)다. 인증이 ID 에 귀속되므로 둘을 합칠 수 없다 (FR-3.6.2).
    #: 추적도 게이트와 같은 이유로 **추론마다** 돌려야 한다 — 10Hz 로 관측하면
    #: 프레임 간 겹침이 그만큼 줄어 ID 가 끊긴다.
    tracks: tuple[Track, ...]
    #: 이 프레임에서 읽은 사원증 마커 (FR-10.1 · `3.8.1`).
    #:
    #: ⚠️ **추적 대상이 있을 때만 읽는다** — FR-3.1.1 의 PPE 게이팅과 같은 원칙이고,
    #: 애초에 귀속시킬 사람이 없으면 인증이 성립하지 않는다. 마커 없는 VGA 프레임에
    #: 0.75ms 가 들므로 빈 순찰 구간에서 그만큼을 아낀다.
    markers: tuple[Marker, ...]


@dataclass
class WorkerStats:
    """워커가 실제로 무엇을 했는지. **성공만 세지 않는다.**"""

    frames_in: int = 0
    inferences: int = 0
    idle: int = 0
    errors: int = 0
    last_error: str | None = None
    inference_ms_max: float = 0.0

    def note(self, ms: float) -> None:
        self.inferences += 1
        self.inference_ms_max = max(self.inference_ms_max, ms)


class VisionWorker:
    """수신·추론을 두 스레드로 돌리고 **최신 결과 하나**를 내놓는다.

    `reader`·`detector` 를 주입받는 이유 — 카메라도 GPU 도 없이 시험돼야 한다.
    특히 **추론을 일부러 느리게 만들어** 메인이 밀리지 않는지 봐야 하는데, 실제
    8ms 추론으로는 그것을 증명할 수 없다(동기로 짜도 통과한다).
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        detector: Any,
        reader: Any,
        queue: FrameQueue | None = None,
        clock: Any = None,
    ) -> None:
        vision = config["vision"]
        self._detector = detector
        self._reader = reader
        self._gate = PersonGate(config)
        self._tracker = PersonTracker(config)
        self._badges = BadgeReader(config)
        self._queue = queue if queue is not None else FrameQueue()
        self._clock = clock if clock is not None else system_clock_ms
        self._stall_ms = int(vision["stall_timeout_ms"])
        # ⚠️ **추론률 상한을 지킨다.** 큐에는 25fps 로 들어오지만 추론은 10fps 다
        # (ADR-23). 상한 없이 돌리면 GPU 가 허용하는 만큼 돌아 전력과 GIL 을 낭비한다.
        self._period_ms = max(1, round(1000 / float(vision["inference_fps"])))
        self._next_due_ms: int | None = None
        self._started_ms: int | None = None

        self._stop = threading.Event()
        self._slot_lock = threading.Lock()
        self._slot: VisionResult | None = None
        self._threads: list[threading.Thread] = []
        self.stats = WorkerStats()

    # ── 수명 ────────────────────────────────────────────────
    def start(self) -> None:
        """세션을 **부르는 스레드에서 먼저 연 뒤** 두 스레드를 띄운다.

        ⚠️ **세션 생성을 워커 스레드에 두면 메인 루프가 막힌다.** 실제로 그렇게 만들어
        재 봤더니 기동 +684ms 지점에서 **틱 간격이 582ms** 로 벌어졌다 — `cmd_timeout_ms`
        (300ms)를 넘겨 **로봇이 멈추는 값**이다. 세션 생성 632ms + 워밍업이 C++ 안이지만
        GIL 을 고르게 놓지 않는다(DirectML 장치 초기화 포함).

        그래서 비용을 **운용 루프가 시작되기 전**에 낸다. 첫 프레임 워밍업을 기동으로
        옮긴 것(3.3.1)과 같은 판단이고, 이번에는 그 대상이 세션 자체다.

        **데몬으로 둔다** — 메인이 끝나면 프로세스가 죽어야 한다.
        """
        if self._threads:
            return
        opened = self._clock()
        self._detector.open()
        LOG.info("vision_detector_opened", ms=self._clock() - opened)
        # 첫 프레임 전에도 단절 시간을 잴 기준이 필요하다. 세션 준비 시간은 기동 비용이고,
        # 실제 카메라 대기는 수신 스레드가 뜬 뒤부터이므로 여기서 시계를 시작한다.
        self._started_ms = self._clock()
        for name, target in (("vision-recv", self._recv_loop), ("vision-infer", self._infer_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        LOG.info("vision_worker_started", period_ms=self._period_ms)

    def stop(self, timeout_s: float = JOIN_TIMEOUT_S) -> None:
        """정지 신호를 주고 **제한 시간만** 기다린다."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout_s)
        alive = [t.name for t in self._threads if t.is_alive()]
        self._threads.clear()
        LOG.info(
            "vision_worker_stopped",
            inferences=self.stats.inferences,
            errors=self.stats.errors,
            still_alive=alive,
        )

    def __enter__(self) -> VisionWorker:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ── 메인 루프가 쓰는 표면 ───────────────────────────────
    def latest(self) -> VisionResult | None:
        """**막지 않는다.** 아직 결과가 없으면 `None`."""
        with self._slot_lock:
            return self._slot

    def age_ms(self, now_ms: int) -> int | None:
        """마지막 결과가 얼마나 낡았나. `None` 이면 아직 하나도 없다."""
        result = self.latest()
        return None if result is None else max(0, now_ms - result.completed_ms)

    def stalled(self, now_ms: int) -> bool:
        """비전 단절 판정 (NFR-2.6).

        ⚠️ **기동 직후 유예와 영구 미연결을 구분한다.** 첫 결과 전에는 워커 기동
        시각을 기준으로 재고, 한 번 결과가 나온 뒤에는 마지막 완료 시각을 기준으로 잰다.
        그러지 않으면 카메라가 처음부터 꺼져 있을 때 영원히 정상으로 남는다.
        """
        age = self.age_ms(now_ms)
        if age is not None:
            return age > self._stall_ms
        return self._started_ms is not None and now_ms - self._started_ms > self._stall_ms

    def healthy(self) -> bool:
        """두 스레드가 아직 살아 있나. **죽은 워커는 조용하다.**"""
        return bool(self._threads) and all(t.is_alive() for t in self._threads)

    @property
    def queue(self) -> FrameQueue:
        return self._queue

    @property
    def gate(self) -> PersonGate:
        """사람 판정 게이트. 스트림이 끊기면 호출부가 `reset()` 한다."""
        return self._gate

    # ── ① 수신 스레드 ───────────────────────────────────────
    def _recv_loop(self) -> None:
        try:
            for frame in self._frames():
                if self._stop.is_set():
                    return
                self._queue.put(frame)
                self.stats.frames_in += 1
        except Exception as exc:  # noqa: BLE001 — 스레드에서 새면 조용히 사라진다
            self._note_error("vision_recv_failed", exc)

    def _frames(self) -> Iterable[Frame]:
        return self._reader.frames()

    # ── ② 추론 스레드 ───────────────────────────────────────
    def _infer_loop(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            if self._next_due_ms is None:
                self._next_due_ms = now
            if now < self._next_due_ms:
                # 정지 신호로 즉시 깨어난다 — `sleep` 은 그러지 못한다.
                self._stop.wait((self._next_due_ms - now) / 1000)
                continue

            frame = self._queue.latest(now_ms=now)
            if frame is None:
                self.stats.idle += 1
                self._stop.wait(IDLE_WAIT_S)
                continue

            # ⚠️ **절대 마감으로 누적한다.** `now + period` 로 잡으면 추론이 느릴 때마다
            # 주기가 뒤로 밀려 실제 추론률이 설정값보다 낮아진다.
            self._next_due_ms += self._period_ms
            if self._next_due_ms < now:  # 크게 밀렸으면 따라잡기를 포기하고 재동기
                self._next_due_ms = now + self._period_ms
            self._run_one(frame, now)

    def _run_one(self, frame: Frame, started_ms: int) -> None:
        """한 프레임을 검출한다. **예외가 스레드를 죽이지 못하게 한다.**"""
        try:
            image = decode_jpeg(frame.payload)
            detections = self._detector.detect(image)
        except Exception as exc:  # noqa: BLE001
            self._note_error("vision_inference_failed", exc, seq=frame.seq)
            return
        completed = self._clock()
        elapsed = float(completed - started_ms)
        self.stats.note(elapsed)
        sighting = self._gate.observe(completed, detections)
        tracks = self._tracker.update(detections, completed)
        # 사람이 없으면 사원증도 읽지 않는다 (위 `markers` 주석).
        markers = self._badges.read(image) if tracks else ()
        result = VisionResult(
            detections=tuple(detections),
            jpeg=frame.payload,
            frame_seq=frame.seq,
            frame_received_ms=frame.received_ms,
            completed_ms=completed,
            inference_ms=elapsed,
            sighting=sighting,
            tracks=tracks,
            markers=markers,
        )
        with self._slot_lock:
            # ⚠️ **덮어쓴다. 쌓지 않는다.** 낡은 검출로 판단하면 로봇이 과거를 보고
            # 움직인다 — 프레임 큐에 적용한 것과 같은 논리다 (ADR/결정 24번).
            self._slot = result

    def _note_error(self, event: str, exc: BaseException, **detail: Any) -> None:
        self.stats.errors += 1
        self.stats.last_error = f"{type(exc).__name__}: {exc}"
        LOG.error(event, error=self.stats.last_error, count=self.stats.errors, **detail)


def build_worker(
    config: Mapping[str, Any],
    *,
    labels: Sequence[str] | None = None,
    section: str = "coco",
) -> VisionWorker:
    """설정만으로 실제 워커를 만든다. **여기서만 카메라와 GPU 를 만진다.**"""
    from host.vision.coco_labels import COCO_CLASSES
    from host.vision.detector import Detector
    from host.vision.stream_client import StreamReader, apply_profile

    def push_profile() -> None:
        # ⚠️ **연결할 때마다 내려보낸다.** 이 호출이 어디에도 없어서 `vision.resolution`·
        # `stream_fps_limit` 을 고쳐도 카메라가 그대로였다. 실패해도 스트림은 카메라
        # 기본값으로 받는다 — 사유는 `apply_profile` 이 이미 남긴다.
        with contextlib.suppress(OSError, ValueError):
            apply_profile(config)

    detector = Detector(config, section=section, labels=labels or COCO_CLASSES)
    reader = StreamReader(config, before_connect=push_profile)
    return VisionWorker(config, detector=detector, reader=reader)


@dataclass
class TickIntervals:
    """틱 간격 기록. **개수만 세면 최악을 놓친다.**

    ⚠️ 601회/60초는 "평균 10Hz" 만 증명한다. 중간에 500ms 벌어져도 뒤에서 몰아 치면
    개수는 같다 — 그 사이 로봇은 `cmd_timeout_ms` 를 넘겨 멈췄을 것이다. **그래서
    분포를 본다.**
    """

    limit_ms: int
    window: int = 2048
    samples: list[int] = field(default_factory=list)
    max_ms: int = 0
    late: int = 0
    _last_ms: int | None = None

    def note(self, now_ms: int) -> None:
        if self._last_ms is not None:
            gap = now_ms - self._last_ms
            self.max_ms = max(self.max_ms, gap)
            if gap > self.limit_ms:
                self.late += 1
            self.samples.append(gap)
            if len(self.samples) > self.window:
                # 무한히 쌓지 않는다. 최악값은 위에서 따로 보존한다.
                del self.samples[: len(self.samples) - self.window]
        self._last_ms = now_ms

    def percentile(self, fraction: float) -> int | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        return ordered[max(0, round(fraction * (len(ordered) - 1)))]

    def digest(self) -> dict[str, Any]:
        return {
            "n": len(self.samples),
            "p95_ms": self.percentile(0.95),
            "max_ms": self.max_ms,
            "late": self.late,
            "limit_ms": self.limit_ms,
        }
