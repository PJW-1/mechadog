"""Local MechDog USB mode control. No flash writes or motion commands."""

import argparse
import hashlib
import json
import struct
import sys
import time
import zlib
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from settings import SETTINGS, require_settings

ROOT = SETTINGS.data_dir
MAC = SETTINGS.mac
PORT = SETTINGS.port
APP_SHA = SETTINGS.values.get("legacy_application_sha256", "")


def ota_client_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ota_update

    return ota_update


def selected_ota_slot(entries):
    candidates = []
    for entry in entries:
        seq, _, state, crc = struct.unpack("<I20sII", entry)
        valid_crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF
        # Pending verify becomes ABORTED on reset. Only NEW/VALID can boot.
        if 0 < seq < 0xFFFFFFFE and state in (0, 2) and crc == valid_crc:
            candidates.append(seq)
    if not candidates or max(candidates) - min(candidates) > 0x7FFFFFFF:
        raise ValueError("OTA boot selection is not verified")
    return (max(candidates) - 1) % 2


def classify_uart(text, fresh=False):
    if any(
        x in text
        for x in (
            "Task watchdog got triggered",
            "Guru Meditation",
            "abort() was called",
        )
    ):
        return "BOOT_ERROR"
    if text.count("MechDog command receiver booting") > 1:
        return "BOOT_ERROR"
    if "Actuators: ON" in text:
        return "ACTUATORS_ON"
    if fresh:
        if (
            "MechDog command receiver booting" in text
            and "Actuators: OFF, initial SAFE latch: ON" in text
            and "Sensor status:" in text
        ):
            return "NORMAL_BOOT_CONFIRMED"
    elif "Sensor status:" in text or "MechDog command receiver booting" in text:
        return "APPLICATION_RUNNING"
    return "MODE_UNKNOWN"


def reset_allowed(report, data):
    return (
        report.get("app_and_protected_verified") is True
        and report.get("actuators_enabled") is False
        and report.get("actuator_off_elf_reviewed") is True
        and report.get("application_sha256") == APP_SHA
        and hashlib.sha256(data).hexdigest() == APP_SHA
    )


def verified_app():
    require_settings()
    packages = [ota_client_module().load_package(path) for path in SETTINGS.packages]
    if not packages or any(p.get("mac") != MAC for p, _ in packages):
        raise ValueError("Reviewed packages for the configured robot are required")
    partition = SETTINGS.path("partition_table").read_bytes()
    entries = []
    for offset in range(0, len(partition), 32):
        if partition[offset : offset + 2] != b"\xaa\x50":
            break
        _, type_, subtype, address, size, _, flags = struct.unpack(
            "<HBBII16sI", partition[offset : offset + 32]
        )
        entries.append((type_, subtype, address, size, flags))
    expected = [
        (1, 2, 0x9000, 0x6000, 0),
        (1, 1, 0xF000, 0x1000, 0),
        (0, 16, 0x10000, 0xF0000, 0),
        (0, 17, 0x100000, 0xF0000, 0),
        (1, 0, 0x1F0000, 0x2000, 0),
        (1, 129, 0x200000, 0x200000, 0),
    ]
    if entries != expected:
        raise ValueError("Only the documented preserved-data OTA layout is supported")
    return {"packages": packages, "partition": partition}


@contextmanager
def exclusive_operation():
    import msvcrt

    require_settings()
    SETTINGS.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS.lock_file.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


class Hardware:
    def open(self):
        import serial
        from serial.tools import list_ports

        matches = [p for p in list_ports.comports() if p.device == PORT]
        if not matches:
            raise FileNotFoundError(f"{PORT} is not connected")
        if (matches[0].vid, matches[0].pid) != (0x1A86, 0x7523):
            raise ValueError(f"{PORT} is not the expected CH340 adapter")
        self.port = serial.Serial(port=None, baudrate=115200, timeout=0.15, write_timeout=1)
        self.port.dtr = False
        self.port.rts = False
        self.port.port = PORT
        self.port.open()
        self.rom = None
        self.reset_requested = False

    def close(self):
        if getattr(self, "port", None):
            self.port.close()

    def observe(self, duration):
        raw = bytearray()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            raw.extend(self.port.read(min(4096, max(1, self.port.in_waiting))))
            if len(raw) > 131072:
                del raw[:-131072]
        text = raw.decode("utf-8", errors="replace")
        print(text)
        return text

    def probe_rom(self):
        from esptool.targets.esp32 import ESP32ROM
        from esptool.util import FatalError

        rom = ESP32ROM(self.port, 115200)
        try:
            rom.connect(mode="no-reset", attempts=1)
        except FatalError:
            # esptool closes even a caller-owned port after failed sync.
            # Reopen without asserting either reset line before UART reading.
            if not self.port.is_open:
                self.port.dtr = False
                self.port.rts = False
                self.port.open()
            return False
        if bytes(rom.read_mac()).hex() != MAC:
            raise ValueError("ESP32 MAC does not match the MechDog")
        self.rom = rom
        return True

    def validate_flash(self, data):
        if isinstance(data, dict):
            partition = data["partition"]
            if (
                self.rom.flash_md5sum(0x8000, len(partition)).lower()
                != hashlib.md5(partition).hexdigest()
            ):
                raise ValueError("OTA partition table mismatch")
            slot = selected_ota_slot(
                [self.rom.read_flash(offset, 32) for offset in (0x1F0000, 0x1F1000)]
            )
            address = (0x10000, 0x100000)[slot]
            for _, image in data["packages"]:
                if (
                    self.rom.flash_md5sum(address, len(image)).lower()
                    == hashlib.md5(image).hexdigest()
                ):
                    return
            raise ValueError("Selected OTA slot is not a reviewed actuator-OFF app")
        observed = self.rom.flash_md5sum(0x10000, len(data))
        if observed.lower() != hashlib.md5(data).hexdigest():
            raise ValueError("Installed app differs from the approved actuator-OFF app")

    def validate_application(self, data):
        if isinstance(data, dict):
            module = ota_client_module()
            client = module.RobotOta(SETTINGS.client())
            status = client.status()
            known = {package["image_sha256"] for package, _ in data["packages"]}
            if status["image_sha256"] not in known or not status["confirmed"] or status["updating"]:
                raise ValueError("OTA app is unverified or maintenance is active")

    def download_reset(self):
        from esptool.reset import ClassicReset

        self.port.reset_input_buffer()
        self.reset_requested = True
        ClassicReset(self.port, reset_delay=0.5).reset()

    def normal_reset(self):
        from esptool.reset import HardReset

        self.port.reset_input_buffer()
        self.port.dtr = False
        self.reset_requested = True
        HardReset(self.port).reset()


def operate(action, hw, guard=verified_app):
    result = {
        "action": action,
        "reset_requested": False,
        "flash_written": False,
        "motion_command_sent": False,
        "port": PORT,
    }
    # Listen before sending ROM sync bytes to an already-running app.
    before = hw.observe(1.5)
    state = classify_uart(before)
    if state in ("BOOT_ERROR", "ACTUATORS_ON"):
        result["state"] = state
        return result
    in_rom = False if state == "APPLICATION_RUNNING" else hw.probe_rom()
    if action == "check":
        result["state"] = "DOWNLOAD_READY" if in_rom else state
        return result
    if action == "download" and in_rom:
        result["state"] = "DOWNLOAD_READY"
        return result
    app = guard()
    if isinstance(app, dict) and not in_rom:
        hw.validate_application(app)
    if action == "normal":
        if in_rom:
            hw.validate_flash(app)
        elif state != "APPLICATION_RUNNING":
            result["state"] = "MODE_UNKNOWN"
            return result
        result["reset_requested"] = True
        hw.normal_reset()
        result["state"] = classify_uart(hw.observe(12), fresh=True)
        return result
    if state != "APPLICATION_RUNNING":
        result["state"] = "MODE_UNKNOWN"
        return result
    result["reset_requested"] = True
    hw.download_reset()
    if hw.probe_rom():
        result["state"] = "DOWNLOAD_READY"
        result["mac"] = MAC
    else:
        result["observed_after"] = classify_uart(hw.observe(5))
        result["state"] = (
            result["observed_after"]
            if result["observed_after"] in ("BOOT_ERROR", "ACTUATORS_ON")
            else "AUTO_DOWNLOAD_FAILED"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "download", "normal"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    require_settings()
    # Reserve outputs before acquiring hardware; never overwrite evidence.
    args.report.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).isoformat()
    with (
        args.report.open("x", encoding="utf-8") as report,
        args.report.with_suffix(".log").open("x", encoding="utf-8") as trace,
        redirect_stdout(trace),
        redirect_stderr(trace),
    ):
        hw = Hardware()
        try:
            with exclusive_operation():
                try:
                    hw.open()
                    result = operate(args.action, hw)
                finally:
                    hw.close()
        except FileNotFoundError as exc:
            result = {"state": "PORT_NOT_FOUND", "detail": str(exc)}
        except (ValueError, KeyError) as exc:
            result = {"state": "SAFETY_REFUSED", "detail": str(exc)}
        except Exception as exc:
            result = {"state": "CONNECTION_ERROR", "detail": f"{type(exc).__name__}: {exc}"}
        result.update(
            action=args.action,
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
            reset_requested=getattr(hw, "reset_requested", False),
            flash_written=False,
            motion_command_sent=False,
        )
        json.dump(result, report, ensure_ascii=False, indent=2)
    return (
        0
        if result["state"] in ("DOWNLOAD_READY", "APPLICATION_RUNNING", "NORMAL_BOOT_CONFIRMED")
        else 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
