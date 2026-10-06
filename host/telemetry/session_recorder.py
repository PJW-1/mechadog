"""실기 세션 동시 기록 — 원본 SCAN·TELEMETRY·송신 명령·측위·영상 인식을 **한 시간축**에 남긴다.

    수신/송신 지점 ─▶ record(kind, ...) ─▶ 유한 큐 ─▶ 기록 스레드 ─▶ events.jsonl

⚠️ **기록이 제어를 막지 않는다.** `record` 는 큐에 넣기만 하고 바로 돌아온다. 큐가 차면
그 사건을 버리고 `dropped` 를 센다 — 오래된 사건을 대신 버리지 않는다(이미 쓴 순서를
흐트러뜨리지 않기 위해서다). 디스크 쓰기가 실패하면 `failed` 가 서고, 운용 쪽은 그것을
«기록 불가» 로 보고 출발을 거절할 수 있다(`healthy`).

⚠️ **시각은 Host 단조 시계(`system_clock_ms`) 하나로 찍는다.** 장치가 보낸 `ts`·`seq`·
`boot_id` 는 원본 전문 안에 그대로 남으므로 따로 빼지 않는다. 벽시계(UTC)와 단조 시계의
기준점은 `manifest.json` 에 한 번 남긴다.

이 기록은 **원본 보존용**이다 — 측위·지도 갱신은 이 파일을 읽지 않는다. 읽는 도구는 둘이다.
`tools/ops/session_summary.py` 는 흐름별 수·공백·명령 통계를 요약하고,
`tools/ops/replay_session.py` 는 입력 사건(`runtime_begin`·`telemetry`·`vision`·`operator`)을
`Runtime` 에 다시 넣어 같은 명령·FSM 전이·경보 단계(`escalation`)가 나오는지 맞춰 본다.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import queue
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from host.common.logging_setup import event_logger
from host.common.protocol import system_clock_ms

LOG = event_logger("mechadog.telemetry.session_recorder")

#: 큐 상한. LiDAR 원본이 초당 약 70건이라 10Hz 텔레메트리·명령을 더해도 수십 초분이다.
DEFAULT_QUEUE = 20000
EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
SUMMARY_FILE = "summary.json"
FRAMES_DIR = "frames"


class SessionRecorder:
    """사건을 JSONL 로 쓰는 기록기. 스레드 하나가 쓴다."""

    def __init__(
        self,
        directory: Path,
        *,
        manifest: Mapping[str, Any] | None = None,
        max_queue: int = DEFAULT_QUEUE,
        clock: Callable[[], int] = system_clock_ms,
    ) -> None:
        self.directory = directory
        self._clock = clock
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue)
        self._max_queue = max_queue
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.written = 0
        self.dropped = 0
        self.max_backlog = 0
        self.failed: str | None = None
        self.frames_saved = 0
        directory.mkdir(parents=True, exist_ok=True)
        (directory / FRAMES_DIR).mkdir(exist_ok=True)
        self._manifest = {
            "started_utc": dt.datetime.now(dt.UTC).isoformat(),
            "started_mono_ms": clock(),
            "clock_domain": "host system_clock_ms (monotonic)",
            "events_file": EVENTS_FILE,
            **(manifest or {}),
        }
        (directory / MANIFEST_FILE).write_text(
            json.dumps(self._manifest, indent=1, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )

    # ── 쓰기 쪽 ───────────────────────────────────────────────
    @property
    def healthy(self) -> bool:
        """디스크 쓰기가 살아 있고 버린 사건이 없나."""
        return self.failed is None and self.dropped == 0

    def record(self, kind: str, *, at_ms: int | None = None, **fields: Any) -> None:
        """사건 하나를 큐에 넣는다. 막히지 않는다."""
        event = {"t": self._clock() if at_ms is None else at_ms, "kind": kind, **fields}
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return
        with self._lock:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            backlog = self._queue.qsize()
            if backlog > self.max_backlog:
                self.max_backlog = backlog

    def record_raw(
        self, kind: str, raw: bytes | str, *, at_ms: int | None = None, **fields: Any
    ) -> None:
        """원본 전문을 그대로. UTF-8 이 아니면 base64 로 담는다(규약 밖 바이트도 버리지 않는다)."""
        if isinstance(raw, bytes):
            try:
                text: str | None = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is None:
                self.record(kind, at_ms=at_ms, raw_b64=base64.b64encode(raw).decode(), **fields)
                return
            raw = text
        self.record(kind, at_ms=at_ms, raw=raw, **fields)

    def save_frame(self, jpeg: bytes, *, frame_seq: int, received_ms: int, **fields: Any) -> str:
        """JPEG 한 장을 파일로 두고 해시와 함께 사건을 남긴다. 파일 이름을 돌려준다."""
        digest = hashlib.sha256(jpeg).hexdigest()
        name = f"{FRAMES_DIR}/{received_ms}_{frame_seq}.jpg"
        try:
            (self.directory / name).write_bytes(jpeg)
        except OSError as exc:
            self._fail(f"frame write: {exc}")
            return ""
        self.frames_saved += 1
        self.record(
            "camera_frame",
            frame_seq=frame_seq,
            received_ms=received_ms,
            file=name,
            sha256=digest,
            bytes=len(jpeg),
            **fields,
        )
        return name

    # ── 기록 스레드 ───────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="session-recorder", daemon=True)
        self._thread.start()
        LOG.info("session_recording", directory=str(self.directory))

    def close(self, timeout_s: float = 5.0) -> dict[str, Any]:
        """남은 사건을 쓰고 요약을 남긴다."""
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=timeout_s)
        summary = self.summary()
        try:
            (self.directory / SUMMARY_FILE).write_text(
                json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            self._fail(f"summary write: {exc}")
        LOG.info("session_recorded", **{k: v for k, v in summary.items() if k != "counts"})
        return summary

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stopped_utc": dt.datetime.now(dt.UTC).isoformat(),
                "stopped_mono_ms": self._clock(),
                "written": self.written,
                "dropped": self.dropped,
                "max_backlog": self.max_backlog,
                "queue_limit": self._max_queue,
                "frames_saved": self.frames_saved,
                "failed": self.failed,
                "counts": dict(self.counts),
            }

    def _fail(self, reason: str) -> None:
        if self.failed is None:
            self.failed = reason
            LOG.error("session_record_failed", reason=reason)

    def _run(self) -> None:
        path = self.directory / EVENTS_FILE
        try:
            handle = path.open("a", encoding="utf-8")
        except OSError as exc:
            self._fail(f"open: {exc}")
            return
        with handle:
            while True:
                event = self._queue.get()
                if event is None:
                    break
                try:
                    handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                except (OSError, ValueError) as exc:
                    self._fail(f"write: {exc}")
                    continue
                with self._lock:
                    self.written += 1
                if self._queue.empty():
                    handle.flush()
