"""Five-second WonderEcho candidate capture. Never flashes; no speech model.

Requires the WEC1 identity handshake before START. A factory module does not
implement this protocol. Do not run on the robot's serial port.
"""

import argparse
import contextlib
import json
import struct
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from protocol import HEADER, Decoder

STATUS = struct.Struct("<4sHHII")
MAX_FRAMES = 250


def command(kind):
    if kind not in (0x102, 0x108, 0x106):
        raise ValueError("Unsupported voice command")
    return HEADER.pack(0x5A5AA5A5, 0, kind, 0, 0x100, 0x12345678)


def send_command(device, kind):
    packet = command(kind)
    if device.write(packet) != len(packet):
        raise TimeoutError("Incomplete voice command write")


class Capture:
    def __init__(self, prompt=False):
        self.expect_prompt = prompt
        self.prompt_phase = 0
        self.ready = False
        self.started = False
        self.finished = False
        self.frames = []
        self.events = []
        self.diagnostics = []

    def accept(self, packet):
        if packet.fill_data != 0x12345678:
            raise ValueError("Wrong stream marker")
        if packet.message_type == 0x102:
            if len(packet.payload) != STATUS.size or packet.version != 0x100:
                raise ValueError("Invalid status packet")
            identity, phase, reason, sent, rate = STATUS.unpack(packet.payload)
            if identity != b"WEC1" or rate != 16000:
                raise ValueError("Not the WEC1 voice candidate")
            self.events.append({"phase": phase, "reason": reason, "sent": sent})
            if phase == 1:
                if self.started or sent or reason:
                    raise ValueError("Unexpected READY; possible device reboot")
                self.ready = True
            elif phase == 2:
                if self.expect_prompt and self.prompt_phase != 6:
                    raise ValueError("Recording began before prompt completion")
                if not self.ready or self.started or sent or reason:
                    raise ValueError("Unexpected STARTED")
                self.started = True
            elif phase == 3:
                if not self.started or self.finished or sent != len(self.frames) or not sent:
                    raise ValueError("Incomplete capture or mismatched final count")
                if reason != 2 or sent != MAX_FRAMES:
                    raise ValueError("Five-second capture did not finish normally")
                self.finished = True
            elif phase == 4:
                raise ValueError(f"Device stopped with reason {reason}, sent {sent}")
            elif phase in (5, 6):
                if (
                    not self.expect_prompt
                    or not self.ready
                    or self.started
                    or sent
                    or reason
                    or self.prompt_phase != (0 if phase == 5 else 5)
                ):
                    raise ValueError("Unexpected prompt phase")
                self.prompt_phase = phase
            else:
                raise ValueError("Unknown device state")
        elif packet.message_type == 0x109:
            if (
                not self.started
                or self.finished
                or self.diagnostics
                or packet.version != 0x100
                or len(packet.payload) != 32
            ):
                raise ValueError("Invalid capture diagnostics")
            values = struct.unpack("<8I", packet.payload)
            keys = (
                "elapsed_ticks",
                "encode_max_ticks",
                "send_max_ticks",
                "queue_peak",
                "produced",
                "sent",
                "tick_hz",
                "reason",
            )
            metrics = dict(zip(keys, values, strict=True))
            if (
                not metrics["tick_hz"]
                or metrics["sent"] != len(self.frames)
                or not metrics["sent"] <= metrics["produced"] <= MAX_FRAMES
            ):
                raise ValueError("Inconsistent capture diagnostics")
            self.diagnostics.append(metrics)
        elif packet.message_type == 0x105:
            if not self.started or self.finished:
                raise ValueError("Audio outside an active capture")
            if packet.version != len(self.frames) or len(self.frames) >= MAX_FRAMES:
                raise ValueError("Missing, duplicated, or excess audio frame")
            if len(packet.payload) != 43 or packet.payload[0] != 42:
                raise ValueError("Expected Speex WB quality-5, one 42-byte frame")
            self.frames.append(packet.payload[1:])
        else:
            raise ValueError("Unexpected voice packet type")


def make_decoder():
    import av

    decoder = av.CodecContext.create("speex", "r")
    decoder.extradata = struct.pack(
        "<8s20s13i",
        b"Speex   ",
        b"1.2.1".ljust(20, b"\0"),
        1,
        80,
        16000,
        1,
        4,
        1,
        16800,
        320,
        0,
        1,
        0,
        0,
        0,
    )
    decoder.open()
    return decoder


def save_wave(frames, destination):
    """Decode only a fully validated capture; publish WAV after all checks pass."""
    import av

    decoder = make_decoder()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    output = bytearray()
    for encoded in frames:
        decoded = decoder.decode(av.Packet(encoded))
        if sum(frame.samples for frame in decoded) != 320:
            raise ValueError("Speex output is not one 20 ms frame")
        for frame in decoded:
            for pcm in resampler.resample(frame):
                output.extend(bytes(pcm.planes[0])[: pcm.samples * 2])
    for pcm in resampler.resample(None):
        output.extend(bytes(pcm.planes[0])[: pcm.samples * 2])
    if len(output) != len(frames) * 640:
        raise ValueError("Decoded sample count mismatch")
    # Exclusive open: no prior measurement can be overwritten.
    with destination.open("xb") as file, wave.open(file, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(output)


def capture(port, folder, prompt=False):
    import serial

    folder.mkdir(parents=True, exist_ok=False)
    decoder, result = Decoder(max_payload=128), Capture(prompt=prompt)
    device = serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=0.5)
    device.dtr = False
    device.rts = False
    device.port = port
    started_command = False
    capture_timed = False
    report = {
        "port": port,
        "baudrate": 115200,
        "success": False,
        "prompt_requested": prompt,
        "source": "physical USB capture",
    }
    try:
        device.open()
        with (folder / "uart.bin").open("xb") as raw:
            send_command(device, 0x102)
            deadline = time.monotonic() + 3
            while not result.finished:
                if time.monotonic() >= deadline:
                    raise TimeoutError("WEC1 handshake/capture deadline exceeded")
                # Do not wait for a 4096-byte batch (~1.4 s of this stream).
                # Drain currently available bytes; when idle, wait for one byte
                # using the serial timeout so the parser sees real idle gaps.
                data = device.read(min(device.in_waiting, 4096) or 1)
                raw.write(data)
                errors = decoder.checksum_errors + decoder.length_errors + decoder.timeouts
                discarded = decoder.discarded_bytes
                packets = decoder.feed(data, now=time.monotonic())
                if decoder.checksum_errors + decoder.length_errors + decoder.timeouts != errors:
                    raise ValueError("Corrupted or incomplete UART packet")
                if result.started and decoder.discarded_bytes != discarded:
                    raise ValueError("Unexpected bytes during audio capture")
                for packet in packets:
                    result.accept(packet)
                if result.ready and not started_command:
                    send_command(device, 0x10B if prompt else 0x108)
                    started_command = True
                    deadline = time.monotonic() + 8
                if result.started and not capture_timed:
                    capture_timed = True
                    deadline = time.monotonic() + 8
        save_wave(result.frames, folder / "voice.wav")
        report["success"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if device.is_open:
            if started_command and not result.finished:
                with contextlib.suppress(serial.SerialException, TimeoutError):
                    send_command(device, 0x106)
            device.close()
        report.update(
            frames=len(result.frames),
            events=result.events,
            diagnostics=result.diagnostics,
            checksum_errors=decoder.checksum_errors,
            length_errors=decoder.length_errors,
            timeouts=decoder.timeouts,
        )
        (folder / "capture.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", required=True, help="Confirmed voice-module COM port; never the robot"
    )
    parser.add_argument(
        "--out", type=Path, help="New measurement directory; existing directories are refused"
    )
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="Play the module prompt before recording; requires speaker firmware",
    )
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=9)))
    output = args.out or (
        Path(__file__).resolve().parents[2]
        / "로봇독 프로젝트"
        / "05_실물_측정결과"
        / now.strftime("%Y-%m-%d")
        / now.strftime("WonderEcho_audio_%H%M%S")
    )
    print(json.dumps(capture(args.port, output, prompt=args.prompt), indent=2))
