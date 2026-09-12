"""Synthetic control/sequence fixtures; no microphone or serial interaction."""

import unittest

from protocol import Packet
from stream_client import STATUS, Capture, command, make_decoder, send_command


def status(phase, sent=0, reason=0, identity=b"WEC1"):
    return Packet(0x102, 0x100, 0x12345678, STATUS.pack(identity, phase, reason, sent, 16000))


def audio(sequence):
    return Packet(0x105, sequence, 0x12345678, bytes([42]) + bytes(42))


class StreamTests(unittest.TestCase):
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

    def test_truncated_payload_rejected(self):
        with self.assertRaises(ValueError):
            self.active().accept(Packet(0x105, 0, 0x12345678, bytes([42])))

    def test_start_command_wire_layout(self):
        self.assertEqual(command(0x108).hex(), "a5a55a5a000008010000000178563412")

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
