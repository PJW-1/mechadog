"""Pre-render the phrase library to WAV cache on the PC.

The runtime loop still synthesizes on the fly; the cache is for cases where
latency matters (e.g. the future 4-pin path where a pre-rendered clip can be
pushed as fixed data — WBS 4.7.9 대역폭 예외 정책) or for offline review of
what the robot will say.

    python gen_voice_cache.py [--piper-model PATH] [--out DIR] [--speed 1.2]

Output: voice_cache/<category>/<sha1-8>.wav + index.json mapping text->file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import wave
from pathlib import Path

from phrases import all_lines

RATE = 22050  # piper kss-medium sample rate; cache stores native rate


def key(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:8]


def synth_wav_bytes(voice, text: str) -> bytes:
    """piper synth -> raw PCM16 mono bytes at the model's native rate."""
    pcm = bytearray()
    for chunk in voice.synthesize(text):
        pcm.extend(chunk.audio_int16_bytes)
    return bytes(pcm)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--piper-model",
        type=Path,
        default=Path(r"C:\dev\voice\piper\ko_KR-kss-medium.onnx"),
    )
    ap.add_argument("--out", type=Path, default=Path(__file__).with_name("voice_cache"))
    ap.add_argument("--speed", type=float, default=1.2, help="piper length_scale")
    args = ap.parse_args()

    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(str(args.piper_model))
    syn = SynthesisConfig(length_scale=args.speed) if args.speed != 1.0 else None
    args.out.mkdir(parents=True, exist_ok=True)

    index = {}
    count = 0
    for category, text in all_lines():
        cat_dir = args.out / category
        cat_dir.mkdir(exist_ok=True)
        path = cat_dir / f"{key(text)}.wav"
        if not path.exists():
            pcm = bytearray()
            for chunk in voice.synthesize(text, syn_config=syn):
                pcm.extend(chunk.audio_int16_bytes)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(voice.config.sample_rate)
                w.writeframes(bytes(pcm))
        rel = re.sub(r"[/\\]", "/", path.relative_to(args.out).as_posix())
        index[text] = {"category": category, "file": rel}
        count += 1

    (args.out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[cache] {count} phrases -> {args.out} ({len(index)} in index)")


if __name__ == "__main__":
    main()
