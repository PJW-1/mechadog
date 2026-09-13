"""Synthesize the identity prompt with Qwen3-TTS (Apache 2.0) on the host PC.

The prompt is generated once here and baked into the module image as PCM, so
nothing in this file runs on the robot. Output is written raw; run
build_prompt_audio.py afterwards to reach the 16 kHz mono 16-bit form the
module accepts.
"""
import argparse
import json
from pathlib import Path

DEFAULT_TEXT = '신원을 말씀해 주세요'
DEFAULT_MODEL = 'Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice'
DEFAULT_SPEAKER = 'Sohee'


def synth(model_id, text, speaker, language, target, cache_dir, device):
    # The speech tokenizer is loaded by a nested from_pretrained that never sees a
    # cache_dir kwarg, so passing one splits the download across two cache roots.
    # HF_HUB_CACHE points every loader at the same place.
    if cache_dir:
        import os
        os.environ['HF_HUB_CACHE'] = str(cache_dir)
        os.environ.pop('HF_HOME', None)

    import torch
    import soundfile as sf
    from qwen_tts import Qwen3TTSModel

    model = Qwen3TTSModel.from_pretrained(
        model_id,
        device_map=device,
        dtype=torch.bfloat16 if device.startswith('cuda') else torch.float32,
    )
    wavs, sr = model.generate_custom_voice(text=text, language=language, speaker=speaker)
    sf.write(str(target), wavs[0], sr)
    return {
        'model': model_id,
        'speaker': speaker,
        'language': language,
        'text': text,
        'sample_rate': sr,
        'seconds': round(len(wavs[0]) / sr, 3),
        'target': str(target),
        'device': device,
    }


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('target', type=Path)
    p.add_argument('--text', default=DEFAULT_TEXT)
    p.add_argument('--model', default=DEFAULT_MODEL)
    p.add_argument('--speaker', default=DEFAULT_SPEAKER)
    p.add_argument('--language', default='Korean')
    p.add_argument('--cache-dir', type=Path)
    p.add_argument('--device', default='cuda:0')
    args = p.parse_args()
    print(json.dumps(synth(args.model, args.text, args.speaker, args.language,
                           args.target, args.cache_dir, args.device),
                     ensure_ascii=False, indent=2))
