"""Synthetic control/sequence fixtures; no microphone or serial interaction."""

import struct
import unittest

from protocol import Packet
from stream_client import STATUS, Capture, command, make_decoder, send_command


def status(phase, sent=0, reason=0, identity=b"WEC1"):
    return Packet(0x102, 0x100, 0x12345678, STATUS.pack(identity, phase, reason, sent, 16000))


def audio(sequence):
    return Packet(0x105, sequence, 0x12345678, bytes([42]) + bytes(42))


class StreamTests(unittest.TestCase):
    def test_prompt_command_matches_firmware_uart_frame(self):
        self.assertEqual(command(0x10B), bytes.fromhex("a5a55a5a00000b010000000178563412"))

    def test_prompt_diagnostics_before_completion(self):
        state = Capture(prompt=True)
        state.accept(status(1))
        state.accept(status(5))
        packet = Packet(
            0x10C, 0x100, 0x12345678, struct.pack("<8I", 120, 300, 1, 0, 10000, 9000, 211, 1000)
        )
        state.accept(packet)
        self.assertEqual(state.prompt_diagnostics[0]["build"], 211)
        self.assertFalse(state.started)
        with self.assertRaises(ValueError):
            state.accept(packet)

    def test_output_interrupt_diagnostics(self):
        state = Capture(prompt=True)
        state.accept(status(1))
        state.accept(status(5))
        state.accept(
            Packet(
                0x10C,
                0x100,
                0x12345678,
                struct.pack("<10I", 121, 412, 2, 0, 17000, 16000, 212, 500, 50, 24),
            )
        )
        self.assertEqual(state.prompt_diagnostics[0]["output_irqs"], 50)
        self.assertEqual(state.prompt_diagnostics[0]["buffer_events"], 24)

    def test_hardware_start_diagnostics(self):
        state = Capture(prompt=True)
        state.accept(status(1))
        state.accept(status(5))
        values = (121, 412, 2, 0, 17000, 16000, 213, 500, 0, 0, 1, 6, 76800, 0, 0, 0)
        state.accept(Packet(0x10C, 0x100, 0x12345678, struct.pack("<16I", *values)))
        self.assertEqual(state.prompt_diagnostics[0]["hw_starts"], 1)
        self.assertEqual(state.prompt_diagnostics[0]["wave_bytes"], 76800)

    def test_prompt_must_finish_before_capture(self):
        state = Capture(prompt=True)
        state.accept(status(1))
        state.accept(status(5))
        with self.assertRaisesRegex(ValueError, "before prompt"):
            state.accept(status(2))

    def test_prompt_then_capture_sequence(self):
        state = Capture(prompt=True)
        for phase in (1, 5, 6, 2):
            state.accept(status(phase))
        for i in range(250):
            state.accept(audio(i))
        state.accept(status(3, 250, 2))
        self.assertTrue(state.finished)

    def test_unsolicited_or_out_of_order_prompt_rejected(self):
        for expect, phase in ((False, 5), (True, 6)):
            state = Capture(prompt=expect)
            state.accept(status(1))
            with self.assertRaises(ValueError):
                state.accept(status(phase))

    def active(self):
        state = Capture()
        state.accept(status(1))
        state.accept(status(2))
        return state

    def test_five_second_control_sequence(self):
        state = self.active()
        for i in range(250):
            state.accept(audio(i))
        state.accept(status(3, 250, 2))
        self.assertTrue(state.finished)

    def test_wrong_identity_never_ready(self):
        state = Capture()
        with self.assertRaises(ValueError):
            state.accept(status(1, identity=b"BOOT"))
        self.assertFalse(state.ready)

    def test_audio_before_handshake_rejected(self):
        with self.assertRaises(ValueError):
            Capture().accept(audio(0))

    def test_loss_duplicate_and_restart_rejected(self):
        for packet in (audio(0), audio(2), status(1)):
            state = self.active()
            state.accept(audio(0))
            with self.assertRaises(ValueError):
                state.accept(packet)

    def test_incomplete_capture_not_success(self):
        state = self.active()
        state.accept(audio(0))
        with self.assertRaises(ValueError):
            state.accept(status(3, 1, 2))
        self.assertFalse(state.finished)

    def test_device_overflow_is_failure(self):
        with self.assertRaisesRegex(ValueError, "reason 3"):
            self.active().accept(status(4, reason=3))

    def test_diagnostics_do_not_turn_overflow_into_success(self):
        state = self.active()
        state.accept(audio(0))
        state.accept(
            Packet(0x109, 0x100, 0x12345678, struct.pack("<8I", 100, 20, 3, 8, 9, 1, 1000, 3))
        )
        self.assertEqual(state.diagnostics[0]["queue_peak"], 8)
        with self.assertRaisesRegex(ValueError, "reason 3"):
            state.accept(status(4, 1, 3))
        self.assertFalse(state.finished)

    def test_diagnostics_wrong_frame_count_rejected(self):
        with self.assertRaisesRegex(ValueError, "Inconsistent"):
            self.active().accept(
                Packet(0x109, 0x100, 0x12345678, struct.pack("<8I", 100, 20, 3, 8, 9, 2, 1000, 3))
            )

    def test_truncated_payload_rejected(self):
        with self.assertRaises(ValueError):
            self.active().accept(Packet(0x105, 0, 0x12345678, bytes([42])))

    def test_start_command_wire_layout(self):
        self.assertEqual(command(0x108).hex(), "a5a55a5a000008010000000178563412")

    def test_loopback_command_wire_layout(self):
        self.assertEqual(command(0x10E).hex(), "a5a55a5a00000e010000000178563412")

    def test_hardware_diagnostics_follow_prompt_diagnostics(self):
        state = Capture(prompt=True)
        state.accept(status(1))
        state.accept(status(5))
        state.accept(Packet(0x10C, 0x100, 0x12345678, struct.pack("<16I", *range(16))))
        with self.assertRaises(ValueError):
            state.accept(Packet(0x10D, 0x100, 0x12345678, struct.pack("<15I", *range(15))))
        state.accept(
            Packet(
                0x10D,
                0x100,
                0x12345678,
                struct.pack("<16I", 1, 16, 1, 0, 0, 0xAC, 0xB4, 0, 0, 0, 0, 0, 100, 0, 0, 1),
            )
        )
        state.accept(
            Packet(
                0x10D,
                0x100,
                0x12345678,
                struct.pack("<16I", 1, 0, 1, 0x10, 0, 0xF7, 0, 0x1F, 12, 0, 0, 0, 200, 0, 0, 2),
            )
        )
        # Words carry four packed 8-bit codec registers each: word0 = 1 gives
        # reg00=1 and reg01..reg03 = 0; word1 = 16 gives reg04 = 16.
        self.assertEqual(state.hw_diagnostics[0]["codec00"], 1)
        self.assertEqual(state.hw_diagnostics[0]["codec04"], 16)
        self.assertEqual(state.hw_diagnostics[0]["codec08"], 1)
        self.assertEqual(state.hw_diagnostics[0]["snap_tag"], 1)
        self.assertEqual(state.hw_diagnostics[1]["snap_tag"], 2)
        with self.assertRaises(ValueError):
            state.accept(Packet(0x10D, 0x100, 0x12345678, struct.pack("<16I", *range(16))))

    def test_hardware_diagnostics_without_prompt_rejected(self):
        state = self.active()
        with self.assertRaises(ValueError):
            state.accept(Packet(0x10D, 0x100, 0x12345678, struct.pack("<16I", *range(16))))

    def test_loopback_sequence(self):
        state = Capture(loopback=True)
        state.accept(status(1))
        state.accept(status(2))
        state.accept(audio(0))
        state.accept(Packet(0x10C, 0x100, 0x12345678, struct.pack("<16I", *range(16))))
        state.accept(Packet(0x10D, 0x100, 0x12345678, struct.pack("<16I", *range(16))))
        self.assertEqual(state.hw_diagnostics[0]["codec00"], 0)
        for i in range(1, 250):
            state.accept(audio(i))
        state.accept(status(3, 250, 2))
        self.assertTrue(state.finished)

    def test_pc_speex_decoder_initializes(self):
        self.assertEqual(make_decoder().codec.name, "speex")

    def test_partial_command_write_rejected(self):
        class ShortWrite:
            def write(self, data):
                return len(data) - 1

        with self.assertRaises(TimeoutError):
            send_command(ShortWrite(), 0x108)


if __name__ == "__main__":
    unittest.main()
