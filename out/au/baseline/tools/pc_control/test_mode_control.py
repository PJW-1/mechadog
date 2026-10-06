import hashlib
import unittest
from unittest.mock import MagicMock, patch

import mode_control as m

BOOT = (
    "MechDog command receiver booting\n"
    "Actuators: OFF, initial SAFE latch: ON\nSensor status: task_started=1"
)


class FakeHardware:
    def __init__(self, observations, rom=False):
        self.observations = iter(observations)
        self.rom_results = iter(rom if isinstance(rom, list) else [rom])
        self.resets = []
        self.flash_validated = False

    def observe(self, _duration):
        return next(self.observations)

    def probe_rom(self):
        return next(self.rom_results)

    def validate_flash(self, _data):
        self.flash_validated = True

    def normal_reset(self):
        self.resets.append("normal")

    def download_reset(self):
        self.resets.append("download")


class ModeTests(unittest.TestCase):
    def run_action(self, action, hw):
        return m.operate(action, hw, guard=lambda: b"verified")

    def test_read_only_application(self):
        hw = FakeHardware(["Sensor status: ok"])
        self.assertEqual(self.run_action("check", hw)["state"], "APPLICATION_RUNNING")
        self.assertEqual(hw.resets, [])

    def test_read_only_rom(self):
        hw = FakeHardware([""], True)
        self.assertEqual(self.run_action("check", hw)["state"], "DOWNLOAD_READY")
        self.assertEqual(hw.resets, [])

    def test_already_download_no_reset_or_guard(self):
        hw = FakeHardware([""], True)
        result = m.operate("download", hw, guard=lambda: self.fail("unneeded guard"))
        self.assertEqual(result["state"], "DOWNLOAD_READY")
        self.assertEqual(hw.resets, [])

    def test_download_verified(self):
        hw = FakeHardware(["Sensor status: ok"], True)
        self.assertEqual(self.run_action("download", hw)["state"], "DOWNLOAD_READY")
        self.assertEqual(hw.resets, ["download"])

    def test_download_failure_never_success(self):
        hw = FakeHardware(["Sensor status: ok", BOOT], False)
        self.assertEqual(self.run_action("download", hw)["state"], "AUTO_DOWNLOAD_FAILED")
        self.assertEqual(hw.resets, ["download"])

    def test_download_panic_stays_error(self):
        hw = FakeHardware(["Sensor status: ok", "Guru Meditation"], False)
        self.assertEqual(self.run_action("download", hw)["state"], "BOOT_ERROR")

    def test_normal_from_rom_checks_flash(self):
        hw = FakeHardware(["", BOOT], True)
        self.assertEqual(self.run_action("normal", hw)["state"], "NORMAL_BOOT_CONFIRMED")
        self.assertTrue(hw.flash_validated)

    def test_normal_requires_fresh_boot(self):
        hw = FakeHardware(["Sensor status: ok", "Sensor status: ok"])
        self.assertEqual(self.run_action("normal", hw)["state"], "MODE_UNKNOWN")

    def test_unknown_never_reset(self):
        hw = FakeHardware(["garbage"], False)
        self.assertEqual(self.run_action("download", hw)["state"], "MODE_UNKNOWN")
        self.assertEqual(hw.resets, [])

    def test_failed_guard_prevents_reset(self):
        hw = FakeHardware(["Sensor status: ok"])

        def bad_guard():
            raise ValueError("unverified app")

        with self.assertRaises(ValueError):
            m.operate("normal", hw, bad_guard)
        self.assertEqual(hw.resets, [])

    def test_panic_and_duplicate_boot(self):
        for log in [BOOT + "\nTask watchdog got triggered", BOOT * 2]:
            self.assertEqual(m.classify_uart(log, fresh=True), "BOOT_ERROR")

    def test_actuator_on_blocks(self):
        hw = FakeHardware(["Actuators: ON"])
        self.assertEqual(self.run_action("normal", hw)["state"], "ACTUATORS_ON")
        self.assertEqual(hw.resets, [])

    def test_installation_guard(self):
        data = b"verified image"
        digest = hashlib.sha256(data).hexdigest()
        good = {
            "app_and_protected_verified": True,
            "actuators_enabled": False,
            "actuator_off_elf_reviewed": True,
            "application_sha256": digest,
        }
        with patch.object(m, "APP_SHA", digest):
            self.assertTrue(m.reset_allowed(good, data))
            self.assertFalse(m.reset_allowed(good, data + b"changed"))
            for key in good:
                bad = dict(good)
                del bad[key]
                self.assertFalse(m.reset_allowed(bad, data))

    def test_failed_sync_reopens_port_without_reset(self):
        from esptool.util import FatalError

        hw = m.Hardware()
        hw.port = MagicMock()
        hw.port.is_open = False
        with patch("esptool.targets.esp32.ESP32ROM") as rom:
            rom.return_value.connect.side_effect = FatalError("not in ROM")
            self.assertFalse(hw.probe_rom())
        hw.port.open.assert_called_once()
        self.assertFalse(hw.port.dtr)
        self.assertFalse(hw.port.rts)

    def test_wrong_device_rejected(self):
        hw = m.Hardware()
        hw.port = MagicMock()
        with patch("esptool.targets.esp32.ESP32ROM") as rom:
            rom.return_value.read_mac.return_value = bytes(6)
            with self.assertRaises(ValueError):
                hw.probe_rom()

    def test_rom_flash_mismatch_rejected(self):
        hw = m.Hardware()
        hw.rom = MagicMock()
        hw.rom.flash_md5sum.return_value = "wrong"
        with self.assertRaises(ValueError):
            hw.validate_flash(b"approved")


if __name__ == "__main__":
    unittest.main()
