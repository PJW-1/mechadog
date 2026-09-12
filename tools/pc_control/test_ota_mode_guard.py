import hashlib
import struct
import unittest
import zlib
from unittest.mock import MagicMock

import mode_control as m


def entry(seq, state):
    crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF
    return struct.pack("<I20sII", seq, bytes(20), state, crc)


class OtaGuardTests(unittest.TestCase):
    def test_valid_selection_and_pending_rollback(self):
        self.assertEqual(m.selected_ota_slot([entry(4, 2), entry(5, 0)]), 0)
        self.assertEqual(m.selected_ota_slot([entry(4, 2), entry(5, 1)]), 1)
        self.assertEqual(m.selected_ota_slot([entry(4, 2), entry(5, 4)]), 1)

    def test_corrupt_or_ambiguous_selection_refused(self):
        with self.assertRaises(ValueError):
            m.selected_ota_slot([bytes(32), bytes(32)])
        with self.assertRaises(ValueError):
            m.selected_ota_slot([entry(1, 2), entry(0xFFFFFFFD, 2)])

    def test_selected_app_and_partition_verified(self):
        hw = m.Hardware()
        hw.rom = MagicMock()
        partition, image = b"partition", b"reviewed actuator OFF"
        hw.rom.flash_md5sum.side_effect = [
            hashlib.md5(partition).hexdigest(),
            hashlib.md5(image).hexdigest(),
        ]
        hw.rom.read_flash.side_effect = [entry(4, 2), entry(5, 1)]
        hw.validate_flash({"partition": partition, "packages": [({}, image)]})
        hw.rom.flash_md5sum.assert_called_with(0x100000, len(image))

    def test_wrong_partition_refused(self):
        hw = m.Hardware()
        hw.rom = MagicMock()
        hw.rom.flash_md5sum.return_value = "wrong"
        with self.assertRaises(ValueError):
            hw.validate_flash({"partition": b"table", "packages": []})
        hw.rom.read_flash.assert_not_called()


if __name__ == "__main__":
    unittest.main()
