"""Render several prompt candidates from the already-downloaded Qwen3-TTS model.

Loads the model once and writes one converted 16 kHz mono 16-bit WAV per
candidate, so a speaker and delivery can be chosen by listening rather than by
reading voice descriptions. No network access and no additional model download.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DEFAULT_TEXT = "신원을 말씀해 주세요"

# Only sohee: the sole native Korean speaker in this model. The delivery is
# steered with instruct because the default reading is too affected for an
# access-control prompt.
# (label, speaker, instruct)
CANDIDATES = [
    (
        "11_뉴스앵커",
        "sohee",
        "뉴스 앵커처럼 중립적이고 또렷하게, 꾸밈이나 애교 없이 평이하게 말한다.",
    ),
    (
        "12_공항안내방송",
        "sohee",
        "공항 안내 방송처럼 일정한 속도로 침착하게, 감정 기복 없이 말한다.",
    ),
    ("13_중저음_느리게", "sohee", "낮은 중저음으로 천천히, 끝을 올리지 않고 평평하게 말한다."),
    ("14_기계적", "sohee", "기계가 읽는 것처럼 억양을 최소화하고 일정한 높이로 말한다."),
    ("15_공손한직원", "sohee", "전문적이고 공손하게, 과장된 친절함 없이 간결하게 말한다."),
    ("16_짧고단호", "sohee", "짧고 단호하게, 군더더기 없이 끊어서 말한다."),
]


def run(model_id, text, out_dir, cache_dir, device, peak):
    if cache_dir:
        import os

        os.environ["HF_HUB_CACHE"] = str(cache_dir)
        os.environ.pop("HF_HOME", None)

    import soundfile as sf
    import torch
    from build_prompt_audio import convert
    from qwen_tts import Qwen3TTSModel

    out_dir.mkdir(parents=True, exist_ok=True)
    model = Qwen3TTSModel.from_pretrained(
        model_id,
        device_map=device,
        dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
    )

    results = []
    for label, speaker, instruct in CANDIDATES:
        raw = out_dir / ("_raw_" + label + ".wav")
        final = out_dir / (label + ".wav")
        try:
            kwargs = {"text": text, "language": "Korean", "speaker": speaker}
            if instruct:
                kwargs["instruct"] = instruct
            wavs, sr = model.generate_custom_voice(**kwargs)
            sf.write(str(raw), wavs[0], sr)
            info = convert(raw, final, peak)
            raw.unlink()
            results.append(
                {
                    "label": label,
                    "speaker": speaker,
                    "instruct": instruct,
                    "seconds": info["seconds"],
                    "bytes": info["bytes"],
                    "fits_module": info["bytes"] <= info["limit_bytes"],
                }
            )
        except Exception as exc:
            results.append(
                {"label": label, "speaker": speaker, "error": f"{type(exc).__name__}: {exc}"}
            )
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--text", default=DEFAULT_TEXT)
    p.add_argument("--model", default="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
    p.add_argument("--cache-dir", type=Path)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--peak", type=int, default=29500)
    a = p.parse_args()
    run(a.model, a.text, a.out_dir, a.cache_dir, a.device, a.peak)
