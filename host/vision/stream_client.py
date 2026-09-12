"""MJPEG 수신·복구·최신 프레임 큐 (WBS 4.3.3~4.3.5 · FR-5.3 · NFR-1.3).

XIAO 가 `multipart/x-mixed-replace` 로 보내는 바이트를 프레임으로 자른다.

**이 모듈은 소켓을 만지지 않는다.** 바이트를 먹여 주면 프레임을 돌려줄 뿐이고, 실제
연결과 재연결은 `4.3.4` 가 맡는다. 수신기(`4.3.6`)를 그렇게 만들어 둔 것과 같은
이유다 — 로봇도 카메라도 없이 시험된다.

예외는 `apply_profile()` 하나다. 제어 포트로 설정을 내려보내는 요청이며, 실제 운용에서는
`build_worker` 가 **연결할 때마다** 부른다 — 카메라가 재부팅하면 펌웨어 기본값으로 돌아간다.

⚠️ **경계 desync 를 무해하게 넘긴다.** MJPEG 은 프레임마다 경계 문자열이 오는 구조라
한 번 어긋나면 그 뒤가 전부 쓰레기가 된다. 그래서 ① JPEG 시작 표식(`FFD8`)이 없는
조각은 버리고 ② 버퍼가 상한을 넘으면 비우고 다시 경계를 찾는다. 둘 다 없으면
깨진 스트림 하나가 메모리를 끝없이 먹는다.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.vision")

#: 우리 펌웨어가 쓰는 경계. 실제로는 HTTP `Content-Type` 헤더에서 읽어 넘긴다.
DEFAULT_BOUNDARY = "mechdog-frame-boundary"

#: JPEG 시작·끝 표식. 경계가 어긋났는지 값싸게 판별하는 수단이다.
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

#: 프레임 하나가 이보다 크면 스트림이 깨진 것으로 본다. VGA JPEG 는 보통 수십 KB 다.
MAX_FRAME_BYTES = 512 * 1024

_LENGTH_RE = re.compile(rb"content-length:\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Frame:
    """완성된 프레임 하나. **디코드는 소비자가 한다.**

    바이트를 그대로 들고 다니는 이유 — 큐에 쌓일 수 있는데, 디코드해서 넣으면
    VGA 한 장이 수백 KB(RGB)로 부풀어 큐 깊이만큼 메모리를 먹는다. JPEG 로 두면
    수십 KB 이고, 어차피 버려질 프레임을 디코드하는 낭비도 없다 (`4.3.5`).
    """

    payload: bytes
    #: **도착 시각.** 신선도의 기준이며 추론 지연을 재는 출발점이다 (NFR-1.1).
    received_ms: int
    seq: int

    @property
    def size_bytes(self) -> int:
        return len(self.payload)

    @property
    def looks_like_jpeg(self) -> bool:
        return self.payload.startswith(JPEG_SOI) and self.payload.endswith(JPEG_EOI)


@dataclass(slots=True)
class ParserStats:
    """파서가 무엇을 버렸는지. **조용히 버리면 스트림 문제를 못 찾는다.**"""

    frames: int = 0
    discarded: int = 0
    resyncs: int = 0
    bytes_in: int = 0


class MjpegParser:
    """증분 파서. 조각이 어디서 끊겨 와도 상태를 이어서 맞춘다.

    TCP 는 우리가 보낸 경계에 맞춰 조각을 나눠 주지 않는다 — 한 프레임이 여러 조각에
    걸치거나 한 조각에 프레임 두 개가 들어온다. 그래서 상태를 들고 있어야 한다.
    """

    def __init__(
        self,
        boundary: str = DEFAULT_BOUNDARY,
        *,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        if not boundary:
            raise ValueError("boundary 가 비어 있음")
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes 는 1 이상이어야 함")
        self._marker = b"--" + boundary.encode("ascii")
        self._max = max_frame_bytes
        self._buffer = bytearray()
        self._seq = 0
        self.stats = ParserStats()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes, now_ms: int) -> list[Frame]:
        """바이트를 넣고 **완성된 프레임만** 돌려준다. 없으면 빈 목록."""
        self._buffer += chunk
        self.stats.bytes_in += len(chunk)
        frames: list[Frame] = []
        # ⚠️ **진행 여부와 프레임 획득을 구분한다.** 조각 하나를 폐기한 것도 진행이므로
        # 거기서 멈추면 **같은 조각에 함께 온 정상 프레임이 파싱되지 않는다** — 다음
        # 조각이 올 때까지 밀리고, 스트림이 끊기면 영원히 안 나온다. 시험이 잡았다.
        while True:
            progressed, frame = self._step(now_ms)
            if frame is not None:
                frames.append(frame)
            if not progressed:
                break
        self._guard_overflow()
        return frames

    def _step(self, now_ms: int) -> tuple[bool, Frame | None]:
        """버퍼를 한 단계 소비한다. `(진행했는가, 프레임)`."""
        start = self._buffer.find(self._marker)
        if start < 0:
            return False, None
        head = self._buffer.find(b"\r\n\r\n", start)
        if head < 0:
            return False, None  # 헤더가 아직 다 안 왔다
        header = bytes(self._buffer[start:head])
        body_at = head + 4

        match = _LENGTH_RE.search(header)
        if match is not None:
            length = int(match.group(1))
            if length > self._max:
                # 길이 자체가 말이 안 된다 — 경계가 어긋난 것으로 본다.
                self._resync(body_at)
                return True, None
            end = body_at + length
            if len(self._buffer) < end:
                return False, None  # 본문이 아직 다 안 왔다
        else:
            # ⚠️ `Content-Length` 가 없는 서버도 있다. 다음 경계까지를 본문으로 본다.
            nxt = self._buffer.find(self._marker, body_at)
            if nxt < 0:
                return False, None
            end = nxt

        payload = bytes(self._buffer[body_at:end])
        del self._buffer[:end]
        if not payload.startswith(JPEG_SOI):
            # 경계가 어긋났다. 버리고 다음 경계에서 다시 맞춘다.
            self.stats.discarded += 1
            self.stats.resyncs += 1
            LOG.warning("frame_discarded", reason="JPEG 표식 없음", size_bytes=len(payload))
            return True, None

        self._seq += 1
        self.stats.frames += 1
        return True, Frame(payload=payload, received_ms=now_ms, seq=self._seq)

    def _resync(self, from_index: int) -> None:
        """어긋난 지점부터 버리고 다음 경계를 찾는다."""
        self.stats.discarded += 1
        self.stats.resyncs += 1
        nxt = self._buffer.find(self._marker, from_index)
        del self._buffer[: nxt if nxt > 0 else len(self._buffer)]
        LOG.warning("stream_resync", reason="Content-Length 범위 이탈")

    def _guard_overflow(self) -> None:
        """경계를 못 찾은 채 버퍼가 커지면 비운다.

        **이게 없으면 깨진 스트림 하나가 메모리를 끝없이 먹는다.** 상한은 프레임
        하나의 최대치를 넉넉히 넘는 값이므로, 정상 스트림에서는 걸리지 않는다.
        """
        if len(self._buffer) <= self._max * 2:
            return
        self._buffer.clear()
        self.stats.discarded += 1
        self.stats.resyncs += 1
        LOG.warning("stream_resync", reason="버퍼 상한 초과", limit_bytes=self._max * 2)


@dataclass(slots=True)
class FrameQueue:
    """최신 프레임을 우선하는 유한 큐.

    ⚠️ **가득 차면 가장 오래된 것을 버린다.** 추론이 프레임 도착보다 느린 것이
    정상이므로(25fps 수신 · 10fps 추론), 큐가 밀리면
    **오래된 프레임을 처리하는 것이 아니라 버리는 것**이 맞다. 낡은 프레임으로
    판단하면 로봇이 과거를 보고 움직인다.

    수신과 추론은 서로 다른 스레드에서 이 객체를 함께 쓰므로 모든 복합 연산을 잠근다.
    1초마다 수신·처리·드롭·최대 깊이를 한 줄로 남겨 실제 부하에서 정책을 검증한다.
    """

    capacity: int = 2
    _items: deque[Frame] = field(default_factory=deque)
    received: int = 0
    processed: int = 0
    dropped: int = 0
    max_depth: int = 0
    _window_received: int = 0
    _window_processed: int = 0
    _window_dropped: int = 0
    _window_max_depth: int = 0
    _summary_due_ms: int | None = None
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity 는 1 이상이어야 함")

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._items)

    def put(self, frame: Frame) -> bool:
        """넣는다. **버린 것이 있으면 `True`.**"""
        with self._lock:
            self.received += 1
            self._window_received += 1
            dropped = False
            while len(self._items) >= self.capacity:
                self._items.popleft()
                self.dropped += 1
                self._window_dropped += 1
                dropped = True
            self._items.append(frame)
            depth = len(self._items)
            self.max_depth = max(self.max_depth, depth)
            self._window_max_depth = max(self._window_max_depth, depth)
            summary = self._summary_if_due(frame.received_ms)
        self._emit_summary(summary)
        return dropped

    def get(self) -> Frame | None:
        """가장 오래된 것부터 하나. 비었으면 `None`."""
        with self._lock:
            if not self._items:
                return None
            self.processed += 1
            self._window_processed += 1
            return self._items.popleft()

    def latest(self, *, now_ms: int | None = None) -> Frame | None:
        """**가장 새 것 하나만** 꺼내고 나머지를 버린다 — 추론이 쓰는 경로다."""
        with self._lock:
            while len(self._items) > 1:
                self._items.popleft()
                self.dropped += 1
                self._window_dropped += 1
            if not self._items:
                summary = self._summary_if_due(now_ms)
                frame = None
            else:
                frame = self._items.popleft()
                self.processed += 1
                self._window_processed += 1
                summary = self._summary_if_due(now_ms)
                if summary is not None and now_ms is not None:
                    summary["latest_age_ms"] = max(0, now_ms - frame.received_ms)
                    summary["latest_seq"] = frame.seq
        self._emit_summary(summary)
        return frame

    def _summary_if_due(self, now_ms: int | None) -> dict[str, int | float] | None:
        """1초에 한 번 큐 상태를 모은다. 호출할 때는 잠금을 잡고 있어야 한다."""
        if now_ms is None:
            return None
        if self._summary_due_ms is None:
            self._summary_due_ms = now_ms + 1000
            return None
        if now_ms < self._summary_due_ms:
            return None
        self._summary_due_ms += 1000
        if self._summary_due_ms <= now_ms:
            self._summary_due_ms = now_ms + 1000
        received = self._window_received
        summary: dict[str, int | float] = {
            "frames_received": received,
            "frames_processed": self._window_processed,
            "frames_dropped": self._window_dropped,
            "drop_rate": round(self._window_dropped / received, 3) if received else 0.0,
            "queue_depth": len(self._items),
            "queue_depth_max": self._window_max_depth,
        }
        self._window_received = 0
        self._window_processed = 0
        self._window_dropped = 0
        self._window_max_depth = len(self._items)
        return summary

    @staticmethod
    def _emit_summary(summary: dict[str, int | float] | None) -> None:
        if summary is not None:
            LOG.info("frame_queue_summary", **summary)


# ── 주소 조립 (WBS ③ · 하드코딩 IP 제거) ─────────────────────
@dataclass(frozen=True, slots=True)
class StreamEndpoints:
    control: str
    stream: str


def stream_endpoints(config: Mapping[str, Any]) -> StreamEndpoints:
    """개체 프로파일의 `xiao_ip` 에서 주소를 조립한다.

    ⚠️ **전역 설정에 고정 URL 을 두지 않는다.** 실제 공유기에서 주소가 달라지고,
    **틀린 주소가 적혀 있는 것이 비어 있는 것보다 나쁘다** — `mechdog_ip` 를
    `null` 로 둔 것과 같은 이유다 (`config/devices/*.yaml`).
    """
    network = config["network"]
    # ⚠️ 주소는 `network` 절 안에 있다. 최상위에서 찾으면 값이 있어도 못 읽는다 —
    # 실제로 그렇게 만들었고, 시험이 최상위에 값을 넣어 주고 있어서 통과했다.
    host = network.get("xiao_ip")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            "network.xiao_ip 가 비어 있음 — config/devices/<개체>.yaml 에 실측 주소를 넣는다"
        )
    return StreamEndpoints(
        control=f"http://{host}:{int(network['vision_control_port'])}",
        stream=f"http://{host}:{int(network['vision_stream_port'])}/stream",
    )


def apply_profile(
    config: Mapping[str, Any],
    *,
    endpoints: StreamEndpoints | None = None,
    timeout_s: float = 3.0,
    opener: Any = None,
) -> dict[str, Any]:
    """기동 시 **해상도와 프레임률 상한을 카메라에 내려보낸다.**

    설정을 정본으로 만드는 지점이다. 이 호출이 없으면 `config.yaml` 의
    `vision.resolution`·`vision.stream_fps_limit` 을 고쳐도 아무 일도 일어나지
    않는다 (NFR-3①). 펌웨어에도 기본값이 있지만 그것은 폴백이다.

    ⚠️ 타임아웃을 반드시 준다. 카메라가 안 켜져 있을 때 여기서 매달리면 호스트가
    기동하지 못한다.
    """
    target = endpoints if endpoints is not None else stream_endpoints(config)
    vision = config["vision"]
    url = (
        f"{target.control}/profile"
        f"?name={vision['resolution']}&fps={int(vision['stream_fps_limit'])}"
    )
    fetch = opener if opener is not None else urllib.request.urlopen
    try:
        with fetch(url, timeout=timeout_s) as response:  # noqa: S310 — 설정에서 온 http URL
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        LOG.warning("profile_apply_failed", url=url, error=str(exc))
        raise
    if not body.get("ok"):
        LOG.warning("profile_rejected", url=url, response=body)
        raise ValueError(f"카메라가 프로파일을 거부했다: {body}")
    LOG.info("profile_applied", profile=body.get("profile"), fps_limit=body.get("fps_limit"))
    return body


def decode_jpeg(payload: bytes) -> Any:
    """JPEG 를 배열로 푼다. **필요한 곳에서만 부른다.**

    `cv2` 를 모듈 최상단에서 가져오지 않는 이유 — 파서와 큐는 영상 라이브러리 없이
    시험돼야 하고, 큐에 쌓인 프레임 중 실제로 디코드되는 것은 일부다 (`4.3.5`).
    """
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("JPEG 디코드 실패 — 깨진 프레임")
    return image


# ══════════════════════════════════════════════════════════════
#  연결과 재연결 (WBS 4.3.4 · FR-5.3 · NFR-3②)
# ══════════════════════════════════════════════════════════════
def boundary_from(content_type: str) -> str:
    """`Content-Type` 헤더에서 경계를 뽑는다. 없으면 기본값.

    **응답이 알려주는 값을 쓴다.** 우리 펌웨어의 경계를 코드에 박아 두면 펌웨어가
    이름을 바꾸는 순간 조용히 프레임이 하나도 안 나온다.
    """
    for token in content_type.split(";"):
        key, _, value = token.partition("=")
        if key.strip().lower() == "boundary":
            return value.strip().strip('"') or DEFAULT_BOUNDARY
    return DEFAULT_BOUNDARY


@dataclass(slots=True)
class StreamStats:
    """연결이 몇 번 끊겼고 몇 번 되살아났는지."""

    connects: int = 0
    failures: int = 0
    frames: int = 0
    stalls: int = 0
    backoff_slept_s: float = 0.0


class StreamReader:
    """스트림에 붙어 프레임을 흘려주고, 끊기면 **지수 백오프로 되붙는다.**

    ⚠️ **백오프를 되돌리는 시점이 "연결 성공" 이 아니라 "프레임 수신" 이다.** 붙자마자
    끊기는 상태에서 연결만으로 초기화하면 **1초 간격으로 영원히 재시도**하게 되고,
    그것은 백오프를 두지 않은 것과 같다.

    ⚠️ **멈춘 스트림도 끊긴 것으로 본다.** TCP 는 상대가 조용해진 것과 살아 있는 것을
    구분해 주지 않으므로, 프레임이 `vision.stall_timeout_ms` 동안 없으면 끊고 다시
    붙는다 — 그것이 비전 단절의 정의다 (NFR-2.6).
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        endpoints: StreamEndpoints | None = None,
        opener: Any = None,
        clock: Any = None,
        sleeper: Any = None,
        before_connect: Any = None,
    ) -> None:
        from host.common.protocol import system_clock_ms

        vision = config["vision"]
        backoff = list(vision["reconnect_backoff_s"])
        if not backoff or any(float(step) <= 0 for step in backoff):
            raise ValueError("vision.reconnect_backoff_s 는 양수 목록이어야 함")
        self._endpoints = endpoints if endpoints is not None else stream_endpoints(config)
        self._backoff = [float(step) for step in backoff]
        self._stall_ms = int(vision["stall_timeout_ms"])
        self._read_bytes = 4096
        self._opener = opener if opener is not None else urllib.request.urlopen
        self._clock = clock if clock is not None else system_clock_ms
        self._stop = threading.Event()
        self._sleeper = sleeper if sleeper is not None else self._stop.wait
        #: 연결 시도마다 먼저 부른다 (`build_worker` 가 카메라 프로파일을 싣는다).
        self._before_connect = before_connect
        self.stats = StreamStats()

    @property
    def url(self) -> str:
        return self._endpoints.stream

    def stop(self) -> None:
        """재접속을 막고 백오프를 깨운다. 진행 중인 읽기는 기존 소켓 제한 후 닫힌다.

        HTTPResponse.close()를 여기서 부르면 다른 스레드의 read가 잡은 버퍼 잠금을
        기다릴 수 있다. 응답 정리는 읽기를 소유한 스레드가 with 블록에서 한다.
        """
        self._stop.set()

    def frames(self, *, max_frames: int | None = None, max_failures: int | None = None):
        """프레임을 흘려준다. **재연결은 안에서 알아서 한다.**

        생성기로 둔 이유 — 호출부(지연 측정·추론 워커)가 `for frame in ...` 만 쓰면
        되고 연결 상태를 신경 쓰지 않는다.
        """
        produced = 0
        attempt = 0
        while not self._stop.is_set() and (
            max_failures is None or self.stats.failures <= max_failures
        ):
            got_frame = False
            try:
                # 소비자가 중간에 닫아도 내부 generator의 HTTP 응답을 함께 정리한다.
                with contextlib.closing(self._read_once()) as connection:
                    for frame in connection:
                        if self._stop.is_set():
                            return
                        got_frame = True
                        attempt = 0  # 프레임을 받았을 때만 백오프를 되돌린다
                        produced += 1
                        yield frame
                        if max_frames is not None and produced >= max_frames:
                            return
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if not self._stop.is_set():
                    LOG.warning("stream_error", url=self.url, error=str(exc))
            if self._stop.is_set():
                return
            self.stats.failures += 1
            # 지금 시도 회차로 대기 시간을 정하고 **그 다음에** 회차를 올린다.
            # 먼저 올리면 첫 재시도가 `backoff[1]` 이 되어 1초 단계를 건너뛴다.
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            LOG.warning("stream_reconnect", after_s=delay, attempt=attempt + 1)
            self.stats.backoff_slept_s += delay
            self._sleeper(delay)
            if not got_frame:
                attempt += 1

    def _read_once(self):
        """한 번 붙어서 끊길 때까지 프레임을 낸다."""
        if self._stop.is_set():
            return
        if self._before_connect is not None:
            self._before_connect()
        if self._stop.is_set():
            return
        response = self._opener(self.url, timeout=self._stall_ms / 1000)  # noqa: S310
        self.stats.connects += 1
        with response:
            if self._stop.is_set():
                return
            content_type = ""
            headers = getattr(response, "headers", None)
            if headers is not None:
                content_type = headers.get("Content-Type", "") or ""
            parser = MjpegParser(boundary_from(content_type))
            LOG.info("stream_connected", url=self.url, boundary=boundary_from(content_type))
            last_frame_ms = self._clock()
            # HTTPResponse.read 는 요청 크기를 채우려고 다음 프레임까지 기다릴 수 있다.
            # read1 은 이미 도착한 조각부터 넘긴다. 기존 read 전용 응답도 지원한다.
            read_available = getattr(response, "read1", None)
            if not callable(read_available):
                read_available = response.read
            while not self._stop.is_set():
                chunk = read_available(self._read_bytes)
                if self._stop.is_set():
                    return
                now = self._clock()
                if not chunk:
                    LOG.warning("stream_closed", url=self.url)
                    return
                for frame in parser.feed(chunk, now):
                    self.stats.frames += 1
                    last_frame_ms = now
                    yield frame
                if now - last_frame_ms >= self._stall_ms:
                    # 바이트는 오는데 프레임이 안 만들어지는 경우도 여기서 걸린다.
                    self.stats.stalls += 1
                    LOG.warning("stream_stalled", silent_ms=now - last_frame_ms)
                    return
