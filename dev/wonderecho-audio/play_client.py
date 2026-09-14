"""Stream host-generated audio to the WonderEcho speaker over UART0.

Requires stream playback firmware (build tag 234 / image 2.1.23+): the module
accepts type-0x0110 PLAY_DATA packets carrying raw PCM16LE mono 16 kHz and a
type-0x0111 PLAY_END that flushes the partial block, drains the DMA ring, and
mutes the amp. The host pre-fills ~256 ms then paces at real-time; the device
holds 512 ms of DMA buffers and needs ~300 ms of amplifier warmup before the
first audible sample.

--record also starts a microphone capture (command 0x108) so the speaker
output is measurable in the returned voice.wav (loopback verification).
"""
import argparse
import json
import math
import struct
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from protocol import Decoder, HEADER
from stream_client import STATUS, open_port, probe_baud, send_command, save_wave

CHUNK = 2048        # PCM16 bytes per PLAY_DATA packet (32 ms; firmware max payload)
PREFILL = 4096      # bytes sent unpaced at stream start (~128 ms; pool fills during warmup anyway)


def play_packet(payload):
    return HEADER.pack(0x5A5AA5A5, sum(payload) & 0xFFFF, 0x110,
                       len(payload), 0x100, 0x12345678) + payload


def end_packet():
    return HEADER.pack(0x5A5AA5A5, 0, 0x111, 0, 0x100, 0x12345678)


def load_pcm(source, tone_seconds=0.0, pad_s=0.0):
    pad = b"\x00" * int(32000 * pad_s)
    if source:
        with wave.open(str(source), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                raise ValueError("Expected mono 16 kHz PCM16 WAV")
            return pad + wav.readframes(wav.getnframes())
    samples = int(16000 * tone_seconds)
    pcm = bytearray()
    for i in range(samples):
        envelope = min(1.0, i / 800, (samples - i) / 800)   # 50 ms edge ramps
        pcm += struct.pack("<h", int(12000 * envelope * math.sin(2 * math.pi * 440 * i / 16000)))
    return pad + bytes(pcm)


def play(port, pcm, folder, record=False, baud=0):
    import serial
    folder.mkdir(parents=True, exist_ok=False)
    baudrate = baud or probe_baud(port)
    device = open_port(port, baudrate)
    decoder = Decoder(max_payload=256)
    frames = []
    report = {"port": port, "baudrate": baudrate, "pcm_bytes": len(pcm),
              "record": record, "success": False, "source": "physical USB playback"}
    try:
        send_command(device, 0x102)
        ready = False
        deadline = time.monotonic() + 3
        while not ready and time.monotonic() < deadline:
            data = device.read(min(device.in_waiting, 4096) or 1)
            for packet in decoder.feed(data, now=time.monotonic()):
                if packet.message_type == 0x102 and len(packet.payload) == STATUS.size:
                    identity, phase, reason, sent, rate = STATUS.unpack(packet.payload)
                    if identity == b"WEC1" and phase == 1 and rate == 16000:
                        ready = True
        if not ready:
            raise TimeoutError("No WEC1 READY on the playback baud rate")
        if record:
            send_command(device, 0x108)
            started = False
            deadline = time.monotonic() + 3
            while not started and time.monotonic() < deadline:
                data = device.read(min(device.in_waiting, 4096) or 1)
                for packet in decoder.feed(data, now=time.monotonic()):
                    if packet.message_type == 0x102 and len(packet.payload) == STATUS.size:
                        if STATUS.unpack(packet.payload)[1] == 2:
                            started = True
            if not started:
                raise TimeoutError("Capture did not start")
        t0 = time.monotonic()
        sent_bytes = 0
        for offset in range(0, len(pcm), CHUNK):
            payload = pcm[offset:offset + CHUNK]
            if device.write(play_packet(payload)) != 16 + len(payload):
                raise TimeoutError("Incomplete PLAY_DATA write")
            sent_bytes += len(payload)
            target = t0 + max(0, sent_bytes - PREFILL) / 32000.0
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        if device.write(end_packet()) != 16:
            raise TimeoutError("Incomplete PLAY_END write")
        report["sent_at"] = t0
        # The device answers WE_FINISHED (phase 3, reason 0) with sent=bytes,
        # followed by a 0x0112 diagnostics packet — keep reading for both.
        play_done = False
        deadline = time.monotonic() + len(pcm) / 32000.0 + 4
        while time.monotonic() < deadline and not (play_done and "play_diagnostics" in report):
            data = device.read(min(device.in_waiting, 4096) or 1)
            for packet in decoder.feed(data, now=time.monotonic()):
                if packet.message_type == 0x102 and len(packet.payload) == STATUS.size:
                    _, phase, reason, sent, _ = STATUS.unpack(packet.payload)
                    if phase == 3 and reason == 0:
                        report["device_bytes"] = sent
                        report["finished_lag_ms"] = round((time.monotonic() - t0) * 1000
                                                          - len(pcm) / 32, 1)
                        play_done = sent == len(pcm)
                elif packet.message_type == 0x112 and len(packet.payload) in (32, 36, 40):
                    keys = ("bytes_in", "bufs_in", "underrun", "rx_dropped",
                            "output_irqs", "tx_peak", "cfg_rc", "start_rc",
                            "level_peak", "rx_bad")[:len(packet.payload) // 4]
                    report["play_diagnostics"] = dict(zip(
                        keys, struct.unpack(f"<{len(packet.payload)//4}I", packet.payload)))
                elif packet.message_type == 0x105 and record and len(frames) < 250:
                    if len(packet.payload) == 43 and packet.payload[0] == 42:
                        frames.append(packet.payload[1:])
        if not play_done:
            raise TimeoutError("No playback WE_FINISHED from the device")
        if record:
            send_command(device, 0x106)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                data = device.read(min(device.in_waiting, 4096) or 1)
                for packet in decoder.feed(data, now=time.monotonic()):
                    if (packet.message_type == 0x105 and len(frames) < 250
                            and len(packet.payload) == 43 and packet.payload[0] == 42):
                        frames.append(packet.payload[1:])
            if frames:
                save_wave(frames, folder / "voice.wav")
                report["frames"] = len(frames)
        report["success"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if device.is_open:
            try:
                send_command(device, 0x106)
            except (serial.SerialException, TimeoutError):
                pass
            device.close()
        (folder / "play.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Confirmed voice-module COM port; never the robot")
    parser.add_argument("--wav", type=Path, help="Mono 16 kHz PCM16 WAV to stream")
    parser.add_argument("--tone", type=float, default=0.0,
                        help="Seconds of 440 Hz test tone when --wav is absent")
    parser.add_argument("--pad", type=float, default=0.3,
                        help="Seconds of silence prepended to mask amp/DAC power-on pop")
    parser.add_argument("--record", action="store_true",
                        help="Capture the microphone during playback into voice.wav")
    parser.add_argument("--baud", type=int, default=0,
                        help="UART baud rate; 0 (default) probes 921600 then 115200")
    parser.add_argument("--out", type=Path, help="New measurement directory; existing directories are refused")
    args = parser.parse_args()
    if not args.wav and not args.tone:
        args.tone = 1.0
    now = datetime.now(timezone(timedelta(hours=9)))
    output = args.out or (Path(__file__).resolve().parents[2] / "로봇독 프로젝트" /
                          "05_실물_측정결과" / now.strftime("%Y-%m-%d") /
                          now.strftime("WonderEcho_play_%H%M%S"))
    pcm = load_pcm(args.wav, args.tone, args.pad)
    print(json.dumps(play(args.port, pcm, output, record=args.record, baud=args.baud), indent=2))
