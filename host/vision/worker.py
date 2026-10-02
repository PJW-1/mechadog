"""추론 워커 스레드 (NFR-1.2 · NFR-3③).

    ① 수신 스레드   StreamReader.frames() → FrameQueue.put()
    ② 추론 스레드   FrameQueue.latest() → decode_jpeg → Detector.detect() → 게이트·추적·판정
                            ↓ 최신 결과 하나만 담는 슬롯 (`_slot_lock`)
    ③ 운용 루프     latest() 를 막지 않고 읽는다 (`runtime.py`)

⚠️ 운용 루프 스레드를 막지 않는 것이 이 모듈의 계약이다 — 로봇은 `safety.cmd_timeout_ms`
(600ms) 동안 명령을 못 받으면 스스로 정지한다. 추론은 대부분 GIL 을 놓는 C++ 안에서 돈다.

⚠️ 워커 스레드의 예외는 삼키지 않고 세며(`stats.errors`) 스레드는 계속 돈다. 워커가 조용히
멈춘 것은 운용 루프가 `stalled()`·`healthy()` 로 감시한다.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from host.common.logging_setup import event_logger
from host.common.protocol import system_clock_ms
from host.vision.badge import BadgeReader, Marker
from host.vision.detector import Detection, ModelMissingError
from host.vision.hazard_detector import HAZARD_CLASSES, HazardDetector, HazardVerdict
from host.vision.person import FallenGate, FallenVerdict, PersonGate, Sighting
from host.vision.ppe_detector import PPE_CLASSES, PpeDetector, PpeVerdict
from host.vision.stream_client import Frame, FrameQueue, decode_jpeg
from host.vision.tracker import PersonTracker, Track

LOG = event_logger("mechadog.vision")

#: 큐가 비었을 때 다시 볼 때까지의 간격 — 바쁜 대기로 GIL 을 잡지 않는다.
IDLE_WAIT_S = 0.005

#: 스레드 정리 대기 상한 — 종료 경로의 `ESTOP` 송신을 늦추지 않는다.
JOIN_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class VisionResult:
    """한 프레임의 검출·판정 결과. 결과의 나이를 알 수 있게 수신·완료 시각을 함께 싣는다."""

    detections: tuple[Detection, ...]
    #: 블랙박스에 보관할 수신 JPEG 원본(재인코딩하지 않는다 · FR-3.9).
    jpeg: bytes
    frame_seq: int
    #: 실제로 디코드한 프레임 크기 — 박스 좌표의 기준이다(설정 `vision.resolution` 이 아니다).
    frame_width: int
    frame_height: int
    frame_received_ms: int
    completed_ms: int
    inference_ms: float
    #: 사람 판정 (FR-3.2). 게이트·추적·쓰러짐은 운용 루프(10Hz)가 아니라 추론마다 관측한다.
    sighting: Sighting
    #: 지속 ID 가 붙은 사람들 (FR-3.6) — 이번 프레임에 보인 대상만이다. 게이트(«있는가»)와
    #: 달리 «누구인가» 다.
    tracks: tuple[Track, ...]
    #: 쓰러짐 규칙 판정 (FR-9 · ADR-35 대안 ⓓ · ADR-42 결정 2) — VLM 과 별개로 추론마다 돈다.
    fallen: FallenVerdict
    #: 이 프레임에서 읽은 사원증 마커 (FR-10.1). 추적 대상이 있을 때만 읽는다.
    markers: tuple[Marker, ...]
    ppe: PpeVerdict | None = None
    #: 화기 위험물 판정 — 위험구역 방문 중 켜졌을 때만 있다. `None` 이면 이 프레임은 보지 않았다.
    hazard: HazardVerdict | None = None


@dataclass
class WorkerStats:
    """워커가 실제로 한 일 — 실패와 유휴도 센다."""

    frames_in: int = 0
    inferences: int = 0
    idle: int = 0
    errors: int = 0
    last_error: str | None = None
    inference_ms_max: float = 0.0

    def note(self, ms: float) -> None:
        self.inferences += 1
        self.inference_ms_max = max(self.inference_ms_max, ms)


class VisionSource(Protocol):
    """운용 루프(`runtime.py`)가 비전에 기대는 표면. `VisionWorker` 와 시험 대역이 채운다.

    ⚠️ `set_ppe_enabled`·`set_hazard_enabled` 가 없는 대역도 있어 런타임은 켜기·끄기 전에
    `hasattr` 로 확인한다.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def latest(self) -> VisionResult | None: ...
    def age_ms(self, now_ms: int) -> int | None: ...
    def stalled(self, now_ms: int) -> bool: ...
    def healthy(self) -> bool: ...
    def set_ppe_enabled(self, enabled: bool) -> None: ...
    def set_hazard_enabled(self, enabled: bool) -> None: ...
    @property
    def hazard_available(self) -> bool: ...


class VisionWorker:
    """수신·추론을 두 스레드로 돌리고 최신 결과 하나를 내놓는다.

    `latest`·`age_ms`·`stalled`·`healthy` 는 어느 스레드에서 불러도 막지 않는다.
    `reader`·`detector` 는 시험을 위해 주입받는다.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        detector: Any,
        reader: Any,
        queue: FrameQueue | None = None,
        clock: Any = None,
        ppe: PpeDetector | None = None,
        hazard: HazardDetector | None = None,
    ) -> None:
        vision = config["vision"]
        self._detector = detector
        self._reader = reader
        self._gate = PersonGate(config)
        self._fallen = FallenGate(config)
        self._tracker = PersonTracker(config)
        self._badges = BadgeReader(config)
        self._ppe = ppe
        self._ppe_enabled = False
        self._ppe_requirements: tuple[str, ...] = ("helmet", "vest")
        self._ppe_opened = False
        self._hazard = hazard
        self._hazard_enabled = False
        self._queue = queue if queue is not None else FrameQueue()
        self._clock = clock if clock is not None else system_clock_ms
        self._stall_ms = int(vision["stall_timeout_ms"])
        # 추론률 상한 `vision.inference_fps` 를 지킨다 (ADR-23).
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
        """추론 세션을 부르는 스레드에서 먼저 연 뒤 두 데몬 스레드를 띄운다.

        ⚠️ 운용 루프가 돌기 전에 부른다 — 세션 생성·워밍업은 GIL 을 고르게 놓지 않아 워커
        스레드에서 열면 운용 루프의 틱이 `cmd_timeout_ms` 가까이 벌어진다.
        """
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if self._threads or self._stop.is_set():
            return
        opened = self._clock()
        self._detector.open()
        if self._ppe_enabled and self._ppe is not None:
            self._ppe.open()
            self._ppe_opened = True
        self._open_hazard()
        LOG.info("vision_detector_opened", ms=self._clock() - opened)
        # 첫 결과 전 단절 판정의 기준 시각 — 세션 준비 뒤부터 잰다.
        self._started_ms = self._clock()
        for name, target in (("vision-recv", self._recv_loop), ("vision-infer", self._infer_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        LOG.info("vision_worker_started", period_ms=self._period_ms)

    def stop(self, timeout_s: float = JOIN_TIMEOUT_S) -> None:
        """정지 신호를 주고 두 스레드의 join에 하나의 대기 예산을 쓴다.

        실행 중인 네이티브 추론을 강제 중단하지 않는다. 주입된 reader.stop()은
        비블로킹이어야 하며, 아직 살아 있는 스레드는 다음 stop에서도 추적한다.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        self._stop.set()
        stop_reader = getattr(self._reader, "stop", None)
        if callable(stop_reader):
            try:
                stop_reader()
            except Exception as exc:  # noqa: BLE001 — 읽기 정리 실패가 join을 건너뛰면 안 된다
                self._note_error("vision_reader_stop_failed", exc)
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # 아직 끝나지 않은 읽기를 잊지 않는다. 다음 stop에서도 회수를 기다릴 수 있다.
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        alive = [thread.name for thread in self._threads]
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
        """최신 결과. 막지 않는다. 아직 없으면 `None`."""
        with self._slot_lock:
            return self._slot

    def age_ms(self, now_ms: int) -> int | None:
        """마지막 결과가 얼마나 낡았나. `None` 이면 아직 하나도 없다."""
        result = self.latest()
        return None if result is None else max(0, now_ms - result.completed_ms)

    def stalled(self, now_ms: int) -> bool:
        """비전 단절 판정 (NFR-2.6). 첫 결과 전에는 워커 기동 시각, 뒤에는 마지막 완료
        시각을 기준으로 잰다 — 처음부터 꺼진 카메라도 단절로 잡힌다."""
        age = self.age_ms(now_ms)
        if age is not None:
            return age > self._stall_ms
        return self._started_ms is not None and now_ms - self._started_ms > self._stall_ms

    def healthy(self) -> bool:
        """두 스레드가 아직 살아 있나."""
        return (
            not self._stop.is_set()
            and bool(self._threads)
            and all(t.is_alive() for t in self._threads)
        )

    @property
    def queue(self) -> FrameQueue:
        return self._queue

    @property
    def gate(self) -> PersonGate:
        """사람 판정 게이트. 스트림이 끊기면 호출부가 `reset()` 한다."""
        return self._gate

    def set_ppe_requirements(self, required: tuple[str, ...]) -> None:
        # 불변 튜플을 전달하고 검출기의 상태 변경은 아래 워커 스레드에서 수행한다.
        self._ppe_requirements = required

    def set_ppe_enabled(self, enabled: bool) -> None:
        """Only factory mode pays for PPE inference; the worker owns its state."""
        if enabled and self._started_ms is not None and not self._ppe_opened:
            if self._ppe is None:
                raise RuntimeError("PPE 판정기가 없다")
            self._ppe.open()
            self._ppe_opened = True
        self._ppe_enabled = enabled

    def _open_hazard(self) -> None:
        """위험물 세션을 기동 때 한 번 연다 — 방문마다 켜고 끄므로 그때 열면 틱이 막힌다.

        ⚠️ **없으면 멈추지 않는다.** 위험물 검출은 가벼운 경고라 모델이 없어도 순찰·사람 인지는
        그대로 돌아야 한다. 기록만 남기고 꺼 둔다 — VLM `hazard_item` 판독은 따로 돈다.
        """
        if self._hazard is None:
            return
        try:
            self._hazard.open()
        except (ModelMissingError, OSError, RuntimeError) as exc:
            LOG.warning("hazard_detector_unavailable", error=f"{type(exc).__name__}: {exc}")
            self._hazard = None

    @property
    def hazard_available(self) -> bool:
        """위험물 검출기가 있나 (모델이 없어 끈 경우 거짓)."""
        return self._hazard is not None

    def set_hazard_enabled(self, enabled: bool) -> None:
        """위험물 추론을 켜고 끈다 — 위험구역 방문 중에만 켠다. 세션은 열지 않는다."""
        self._hazard_enabled = enabled

    # ── ① 수신 스레드 ───────────────────────────────────────
    def _recv_loop(self) -> None:
        frames = None
        try:
            frames = iter(self._frames())
            for frame in frames:
                if self._stop.is_set():
                    return
                self._queue.put(frame)
                self.stats.frames_in += 1
        except Exception as exc:  # noqa: BLE001 — 스레드에서 새면 조용히 사라진다
            self._note_error("vision_recv_failed", exc)
        finally:
            close_frames = getattr(frames, "close", None)
            if callable(close_frames):
                try:
                    close_frames()
                except Exception as exc:  # noqa: BLE001
                    self._note_error("vision_recv_close_failed", exc)

    def _frames(self) -> Iterable[Frame]:
        return cast("Iterable[Frame]", self._reader.frames())

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

            # 절대 마감으로 누적한다 — 느린 추론이 주기를 뒤로 밀지 않게.
            self._next_due_ms += self._period_ms
            if self._next_due_ms < now:  # 크게 밀렸으면 따라잡기를 포기하고 재동기
                self._next_due_ms = now + self._period_ms
            self._run_one(frame, now)

    def _run_one(self, frame: Frame, started_ms: int) -> None:
        """한 프레임을 검출·판정해 슬롯에 넣는다. 예외는 세고 스레드는 살린다."""
        try:
            image = decode_jpeg(frame.payload)
            height, width = int(image.shape[0]), int(image.shape[1])
            detections = self._detector.detect(image)
            observed = self._clock()
            sighting = self._gate.observe(observed, detections)
            tracks = self._tracker.update(detections, observed)
            # 게이트의 대표 박스를 추적 ID 와 함께 본다 — 다른 사람의 정지가 섞이지 않게.
            fallen = self._fallen.observe(
                observed, sighting.box, track_id=tracks[0].track_id if tracks else None
            )
            # 후처리도 워커의 일부다. 오류를 세고 다음 프레임에서 다시 시도한다.
            markers = self._badges.read(image) if tracks else ()
            if self._ppe is not None and hasattr(self._ppe, "set_requirements"):
                self._ppe.set_requirements(self._ppe_requirements)
            ppe = (
                self._ppe.observe(image, tracks, observed)
                if self._ppe_enabled and self._ppe
                else None
            )
            if not self._ppe_enabled and self._ppe is not None:
                self._ppe.reset()
            hazard_on = self._hazard_enabled and self._hazard is not None
            hazard = self._hazard.observe(image, observed) if hazard_on and self._hazard else None
            if not hazard_on and self._hazard is not None:
                self._hazard.reset()
        except Exception as exc:  # noqa: BLE001
            self._note_error("vision_inference_failed", exc, seq=frame.seq)
            return
        completed = self._clock()
        elapsed = float(completed - started_ms)
        self.stats.note(elapsed)
        result = VisionResult(
            detections=tuple(detections),
            jpeg=frame.payload,
            frame_seq=frame.seq,
            frame_width=width,
            frame_height=height,
            frame_received_ms=frame.received_ms,
            completed_ms=completed,
            inference_ms=elapsed,
            sighting=sighting,
            fallen=fallen,
            tracks=tracks,
            markers=markers,
            ppe=ppe,
            hazard=hazard,
        )
        with self._slot_lock:
            # 덮어쓴다 — 낡은 결과를 쌓지 않는다 (ADR-23 최신 프레임 우선).
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
    """설정만으로 실제 워커를 만든다. 여기서만 카메라와 GPU 를 만진다."""
    from host.vision.coco_labels import COCO_CLASSES
    from host.vision.detector import Detector
    from host.vision.stream_client import StreamReader, apply_profile

    def push_profile() -> None:
        # 연결할 때마다 카메라 프로파일을 내려보낸다. 실패해도 카메라 기본값으로 받는다
        # (사유는 `apply_profile` 이 남긴다).
        with contextlib.suppress(OSError, ValueError):
            apply_profile(config)

    detector = Detector(config, section=section, labels=labels or COCO_CLASSES)
    reader = StreamReader(config, before_connect=push_profile)
    ppe = PpeDetector(
        config,
        Detector(config, section="ppe", labels=PPE_CLASSES),
    )
    hazard_spec = config["vision"].get("hazard") or {}
    hazard = (
        HazardDetector(config, Detector(config, section="hazard", labels=HAZARD_CLASSES))
        if hazard_spec.get("enabled")
        else None
    )
    return VisionWorker(config, detector=detector, reader=reader, ppe=ppe, hazard=hazard)


@dataclass
class TickIntervals:
    """틱 간격의 분포(p95·최대·`limit_ms` 초과 수)를 기록한다 — 평균은 한 번의 긴 공백을 숨긴다."""

    limit_ms: int
    window: int = 2048
    samples: list[int] = field(default_factory=list)
    max_ms: int = 0
    late: int = 0
    _last_ms: int | None = None

    def note(self, now_ms: int) -> int | None:
        """간격을 기록하고 그 간격을 돌려준다(첫 호출은 `None`) — 호출자의 구간 집계용."""
        gap: int | None = None
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
        return gap

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
