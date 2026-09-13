"""Render the access-control scenario prompts with the Korean Orpheus voice.

Four fixed phrases drive the flow: ask for the badge, report a failed image
check, fall back to spoken identity, then confirm. They are generated once here
and baked into the module image, so nothing runs on the robot.

The module's user area is small - run with --report first to see whether the
converted clips fit before building an image around them.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# (file id, label, text) - ids continue from the existing 65000 prompt.
PHRASES = [
    (65000, "badge", "사원증을 제시해 주세요"),
    (65001, "failed", "인증에 실패하였습니다"),
    (65002, "identity", "신원을 말씀해 주세요"),
    (65003, "confirm", "신원이 확인되었습니다"),
]

LEAD_MS = 250  # amplifier leaves shutdown before the first syllable


def run(out_dir, peak, device, temperature, seed, budget):
    import wave

    from build_prompt_audio import convert
    from synth_prompt_orpheus import synth_one

    out_dir.mkdir(parents=True, exist_ok=True)
    results, total = [], 0
    for file_id, label, text in PHRASES:
        raw = out_dir / ("_raw_" + label + ".wav")
        trimmed = out_dir / ("_trim_" + label + ".wav")
        final = out_dir / f"{file_id}_{label}.wav"
        synth_one(raw, text, device, temperature, seed)
        convert(raw, trimmed, peak)
        _prepend_silence(trimmed, final, LEAD_MS)
        raw.unlink()
        trimmed.unlink()
        size = final.stat().st_size
        total += size
        with wave.open(str(final), "rb") as w:
            seconds = w.getnframes() / w.getframerate()
        results.append(
            {
                "id": file_id,
                "label": label,
                "text": text,
                "seconds": round(seconds, 2),
                "bytes": size,
            }
        )
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)

    print(
        json.dumps(
            {
                "total_bytes": total,
                "budget": budget,
                "fits": total <= budget,
                "over_by": max(0, total - budget),
            },
            ensure_ascii=False,
        )
    )
    return results


def _prepend_silence(src, dst, lead_ms):
    import wave

    with wave.open(str(src), "rb") as w:
        rate, data = w.getframerate(), w.readframes(w.getnframes())
    with wave.open(str(dst), "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(rate)
        o.writeframes(b"\x00\x00" * int(rate * lead_ms / 1000) + data)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--peak", type=int, default=29500)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--temperature", type=float, default=0.4)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument(
        "--budget",
        type=int,
        default=101292,
        help="Bytes available for all prompts in the user area",
    )
    a = p.parse_args()
    run(a.out_dir, a.peak, a.device, a.temperature, a.seed, a.budget)
