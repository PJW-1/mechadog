"""Synthetic protocol tests; these are not microphone/hardware measurements."""

import struct
import unittest

from protocol import MAGIC, Decoder, Packet

# Independently spelled wire fixture: magic/checksum/type/length/version/fill/payload.
FRAME = bytes.fromhex("a5 a5 5a 5a 06 00 05 01 03 00 07 00 78 56 34 12 01 02 03")
EXPECTED = Packet(0x105, 7, 0x12345678, b"\x01\x02\x03")


class DecoderTests(unittest.TestCase):
    def test_every_split(self):
        for split in range(len(FRAME) + 1):
            decoder = Decoder()
            result = decoder.feed(FRAME[:split], now=0)
            result += decoder.feed(FRAME[split:], now=0.01)
            self.assertEqual(result, [EXPECTED])

    def test_noise_and_partial_magic(self):
        decoder = Decoder()
        self.assertEqual(decoder.feed(b"boot log" + FRAME[:3], now=0), [])
        self.assertEqual(decoder.feed(FRAME[3:] + FRAME, now=0.1), [EXPECTED, EXPECTED])
        self.assertEqual(decoder.discarded_bytes, 8)

    def test_checksum_corruption_then_recovery(self):
        decoder = Decoder()
        bad = FRAME[:-1] + b"\xff"
        self.assertEqual(decoder.feed(bad + FRAME, now=0), [EXPECTED])
        self.assertEqual(decoder.checksum_errors, 1)

    def test_length_limit_then_recovery(self):
        decoder = Decoder(max_payload=128)
        bad = FRAME[:8] + b"\xff\xff" + FRAME[10:16]
        self.assertEqual(decoder.feed(bad + FRAME, now=0), [EXPECTED])
        self.assertEqual(decoder.length_errors, 1)

    def test_incomplete_timeout_then_recovery(self):
        decoder = Decoder()
        decoder.feed(FRAME[:17], now=0)
        self.assertEqual(decoder.feed(b"", now=1), [])
        self.assertEqual(decoder.timeouts, 1)
        self.assertEqual(decoder.feed(FRAME, now=1.1), [EXPECTED])

    def test_magic_inside_payload_is_data(self):
        wire = struct.pack("<IHHHHI", 0x5A5AA5A5, sum(MAGIC), 0x105, 4, 0, 0) + MAGIC
        self.assertEqual(Decoder().feed(wire, now=0)[0].payload, MAGIC)

    def test_payload_checksum_wrap_and_empty_control(self):
        payload = b"\xff" * 512
        wire = (
            struct.pack("<IHHHHI", 0x5A5AA5A5, sum(payload) & 65535, 0x105, 512, 65535, 0) + payload
        )
        control = bytes.fromhex("a5 a5 5a 5a 00 00 06 01 00 00 00 01 00 00 00 00")
        result = Decoder().feed(wire + control, now=0)
        self.assertEqual(result[0].payload, payload)
        self.assertEqual(result[1], Packet(0x106, 256, 0, b""))

    def test_bounds_and_invalid_clock(self):
        decoder = Decoder()
        decoder.feed(b"x" * 65536, now=10)
        self.assertEqual(len(decoder.buffer), 0)
        for now in (9, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                decoder.feed(b"", now=now)
        with self.assertRaises(ValueError):
            decoder.feed(b"x" * 65537, now=11)


if __name__ == "__main__":
    unittest.main()
