"""Audio transport boundary (WBS 4.7.19 · 4.7.20).

The voice loop talks to the module through this interface only. Today the
only implementation is :class:`SerialTransport` — the temporary UART
validation link (--port, e.g. COM5). The robot 4-pin I2C relay was ruled
out on 2026-09-23 (WBS 4.7.9: 0x34 carries command ids only · ADR-38).
The target path splits listening and speaking

    listen: XIAO PDM mic -> :82/audio (PCM16LE 16 kHz) -> PC
    speak:  PC -> robot API -> MP3 module 0x7B (pre-recorded TF tracks)

and plugs in here as network transports **without touching anything above**:
capture/stream logic, knowledge retrieval, scenarios, and the --web API are
transport-agnostic by design.

One transport = one owner. The main loop owns the object and is the only
writer, so audio frames from different sources can never interleave — the
same rule the serial port enforces physically today and the Wi-Fi session
will enforce logically.
"""

from __future__ import annotations

from stream_client import open_port, probe_baud, send_command


class SerialTransport:
    """UART link to the voice module. **Temporary validation transport.**

    Wraps the verified stream_client serial path so the rest of the pipeline
    never sees `serial.Serial` directly. Swapping in a socket transport for
    the robot-mounted 4-pin path means implementing this same surface:

        write(bytes) -> int        paced PCM/command writes
        read(n) -> bytes           framed receive
        in_waiting -> int          pending RX byte count
        send_command(kind)         voice command packet (0x102/0x106/0x108)
        is_open / close()          lifecycle
    """

    kind = "serial"

    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self.dev = open_port(port, baud)

    def write(self, data: bytes) -> int:
        return self.dev.write(data)

    def read(self, n: int) -> bytes:
        return self.dev.read(n)

    @property
    def in_waiting(self) -> int:
        return self.dev.in_waiting

    @property
    def is_open(self) -> bool:
        return self.dev.is_open

    def send_command(self, kind: int) -> None:
        send_command(self.dev, kind)

    def close(self) -> None:
        if self.dev.is_open:
            self.dev.close()


def open_transport(args) -> SerialTransport:
    """Open the audio link. Serial today; the Wi-Fi relay registers here later.

    The factory is the single place that decides *how* the module is reached,
    so the rest of the pipeline never branches on transport type.
    """
    if not getattr(args, "port", None):
        raise ValueError(
            "no audio transport configured — pass --port for the temporary serial link"
        )
    baud = args.baud or probe_baud(args.port)
    return SerialTransport(args.port, baud)
