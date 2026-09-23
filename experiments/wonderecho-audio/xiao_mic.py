"""XIAO ESP32S3 Sense microphone over Wi-Fi (WBS 4.7.19 · ADR-38).

`firmware_xiao_vision` serves `GET :82/audio?gain=0..4` as an endless HTTP
chunked stream of `audio/L16;rate=16000;channels=1` (PCM16LE mono). This
module keeps one connection open on a background thread, buffers what
arrives and hands the voice loop fixed 20 ms frames — the same frame size the
WonderEcho path scores for VAD.

Two things differ from the WonderEcho link and shape the interface:

* **The mic never stops.** The module recorded only between 0x108 and 0x106.
  Here audio keeps arriving while the PC transcribes or speaks, so the caller
  `flush()`es at the start of each capture instead of reading stale speech.
* **The link is Wi-Fi.** A drop must not end the voice loop. The reader
  reconnects with backoff and counts drops (`stats()`). A stream that stops
  sending for `timeout_s` counts as dropped too: the firmware serves one
  client at a time and makes a second one *wait* (firmware README), so on the
  client side a dead stream and a busy board look the same — silence.

One XiaoMic per board for that same reason.
"""

from __future__ import annotations

import contextlib
import http.client
import socket
import threading

RATE = 16000
FRAME = 640  # 20 ms of PCM16 mono at 16 kHz


class XiaoMic:
    """Background reader for `http://<host>:<port>/audio?gain=<gain>`."""

    def __init__(
        self,
        host,
        port=82,
        gain=2,
        timeout_s=3.0,
        max_buffer_s=5.0,
        backoff_s=(0.5, 5.0),
    ):
        if not 0 <= gain <= 4:
            raise ValueError("gain must be 0..4 (firmware clamps the same range)")
        self.host, self.port, self.gain = host, port, gain
        self.timeout_s = timeout_s
        self.max_buffer = int(RATE * 2 * max_buffer_s)
        self.backoff_s = backoff_s
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._sock = None
        self._thread = None
        self.connected = False
        self.connects = 0  # streams that answered 200
        self.drops = 0  # connected streams that ended (EOF, error, stall)
        self.failures = 0  # attempts that never got a 200
        self.overflow_bytes = 0  # oldest audio discarded while nobody read
        self.last_error = ""

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/audio?gain={self.gain}"

    def start(self):
        self._thread = threading.Thread(target=self._run, name="xiao-mic", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            # 연결 객체를 닫지 않고 소켓만 내린다 — `conn.close()` 는 읽는 중인
            # 응답의 fp 를 None 으로 만들어 리더 스레드가 AttributeError 로 죽는다
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        if self._thread is not None:
            self._thread.join(timeout=self.timeout_s + 1.0)

    def flush(self):
        with self._cond:
            # 반쪽 샘플(홀수 꼬리)은 남긴다 — 버리면 이후 샘플 경계가 전부 어긋난다
            del self._buf[: len(self._buf) - len(self._buf) % 2]

    def read_frame(self, timeout):
        """Next 20 ms frame, or None if none arrived within `timeout` seconds."""
        with self._cond:
            if not self._cond.wait_for(lambda: len(self._buf) >= FRAME, timeout):
                return None
            frame = bytes(self._buf[:FRAME])
            del self._buf[:FRAME]
            return frame

    def stats(self):
        with self._cond:
            buffered = len(self._buf)
        return {
            "url": self.url,
            "connected": self.connected,
            "connects": self.connects,
            "drops": self.drops,
            "failures": self.failures,
            "overflow_bytes": self.overflow_bytes,
            "buffered_ms": buffered * 1000 // (RATE * 2),
            "last_error": self.last_error,
        }

    def _push(self, data):
        with self._cond:
            self._buf.extend(data)
            extra = len(self._buf) - self.max_buffer
            if extra > 0:
                # 프레임 경계를 지킨다 — 홀수 바이트를 버리면 이후 샘플이 전부 어긋난다
                extra += -extra % 2
                del self._buf[:extra]
                self.overflow_bytes += extra
            self._cond.notify_all()

    def _run(self):
        delay = self.backoff_s[0]
        while not self._stop.is_set():
            streamed = self._stream_once()
            if self._stop.is_set():
                break
            delay = self.backoff_s[0] if streamed else min(delay * 2, self.backoff_s[1])
            self._stop.wait(delay)

    def _stream_once(self):
        """One connection's lifetime. True if it delivered audio."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        got_audio = False
        try:
            conn.connect()
            self._sock = conn.sock
            conn.request("GET", f"/audio?gain={self.gain}")
            resp = conn.getresponse()
            if resp.status != 200:
                self.last_error = f"HTTP {resp.status} {resp.read(200)!r}"
                self.failures += 1  # 본문 읽기가 실패하면 except 에서 한 번만 센다
                print(f"[xiao] {self.url} → {self.last_error}")
                return False
            self.connects += 1
            self.connected = True
            print(f"[xiao] connected {self.url}")
            while not self._stop.is_set():
                data = resp.read1(4096)  # chunked framing is undone by http.client
                if not data:
                    raise ConnectionError("stream ended")
                got_audio = True
                self._push(data)
        except (OSError, http.client.HTTPException) as e:
            if self._stop.is_set():
                return got_audio
            self.last_error = f"{type(e).__name__}: {e}"
            if self.connected:
                self.drops += 1
                print(f"[xiao] dropped ({self.last_error}) — reconnecting")
            else:
                self.failures += 1
                print(f"[xiao] connect failed ({self.last_error})")
        finally:
            self.connected = False
            self._sock = None
            conn.close()
            with self._cond:
                # 끊긴 연결의 반쪽 샘플을 버린다 — 새 연결은 샘플 경계에서 시작한다
                del self._buf[len(self._buf) - len(self._buf) % 2 :]
        return got_audio
