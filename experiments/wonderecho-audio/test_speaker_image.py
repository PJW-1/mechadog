import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from build_speaker_image import entries, pcm_prompt


class SpeakerImageTests(unittest.TestCase):
    def test_pcm_sdk_header_and_samples(self):
        pcm = b"\x12\x34" * 16000
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(pcm)
            output = pcm_prompt(source)
        self.assertEqual(struct.unpack_from("<I", output, 16)[0], 20)
        self.assertEqual(output[48:], pcm)
        with wave.open(io.BytesIO(output), "rb") as wav:
            self.assertEqual(wav.getnframes(), 16000)
            self.assertEqual(wav.readframes(16000), pcm)

    def test_container_preserves_distinct_resources(self):
        raw = struct.pack("<H", 2) + struct.pack("<HII", 60000, 22, 3)
        raw += struct.pack("<HII", 65000, 25, 2) + b"abcde"
        self.assertEqual(entries(raw), {60000: b"abc", 65000: b"de"})
        with self.assertRaises(ValueError):
            entries(raw[:-1])

    def test_container_rejects_duplicate_id(self):
        raw = struct.pack("<H", 2) + struct.pack("<HII", 65000, 22, 3)
        raw += struct.pack("<HII", 65000, 25, 2) + b"abcde"
        with self.assertRaises(ValueError):
            entries(raw)
