"""PC decoder for CI130X LLM AIOT SDK 2.2.7 framed UART data.

Transport only: no serial access, recording, speech model, or authentication.
Payloads are opaque: PCM_MIDDLE may carry Speex, Opus, or PCM depending on
firmware configuration. This protocol is not WonderEcho's factory I2C protocol.
"""

import math
import struct
from dataclasses import dataclass

MAGIC = b"\xa5\xa5\x5a\x5a"
HEADER = struct.Struct("<IHHHHI")


@dataclass(frozen=True)
class Packet:
    message_type: int
    version: int
    fill_data: int
    payload: bytes


class Decoder:
    """Bounded incremental decoder; caller supplies monotonic seconds.

    feed(b'', now=...) also expires an incomplete packet while the link is idle.
    An expired/corrupt frame is counted, never converted into replacement audio.
    The SDK checksum covers payload only, so header corruption is not fully
    detectable. Serial frame counters require a separate utterance-aware layer.
    """

    def __init__(self, max_payload: int = 4096, idle_timeout: float = 1.0):
        if not 0 < max_payload <= 65535:
            raise ValueError("max_payload must be in 1..65535")
        if not math.isfinite(idle_timeout) or idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive and finite")
        self.max_payload = max_payload
        self.idle_timeout = idle_timeout
        self.buffer = bytearray()
        self.last_input = None
        self.last_now = None
        self.discarded_bytes = 0
        self.checksum_errors = 0
        self.length_errors = 0
        self.timeouts = 0

    def _discard(self, count: int):
        self.discarded_bytes += count
        del self.buffer[:count]

    def feed(self, data: bytes, *, now: float) -> list[Packet]:
        if not math.isfinite(now) or (self.last_now is not None and now < self.last_now):
            raise ValueError("now must be finite and monotonic")
        if len(data) > 65536:
            raise ValueError("feed at most 64 KiB at a time")
        self.last_now = now
        if self.buffer and now - self.last_input >= self.idle_timeout:
            self.timeouts += 1
            self._discard(len(self.buffer))
        if data:
            self.last_input = now
            self.buffer.extend(data)
        packets = []
        while self.buffer:
            offset = self.buffer.find(MAGIC)
            if offset < 0:
                keep = next((n for n in (3, 2, 1) if self.buffer.endswith(MAGIC[:n])), 0)
                self._discard(len(self.buffer) - keep)
                break
            if offset:
                self._discard(offset)
            if len(self.buffer) < HEADER.size:
                break
            _, checksum, kind, size, version, fill = HEADER.unpack_from(self.buffer)
            if size > self.max_payload:
                self.length_errors += 1
                self._discard(1)
                continue
            end = HEADER.size + size
            if len(self.buffer) < end:
                break
            payload = bytes(self.buffer[HEADER.size : end])
            if sum(payload) & 0xFFFF != checksum:
                self.checksum_errors += 1
                self._discard(1)
                continue
            packets.append(Packet(kind, version, fill, payload))
            del self.buffer[:end]
        return packets
