"""Render the prompt with every Supertonic 3 preset voice, for side-by-side listening.

Supertonic ships fixed voices (M1..M5, F1..F5) and runs on ONNX Runtime, so this
needs no GPU and no reference recording. Output goes through build_prompt_audio
so each candidate is already in the module's 16 kHz mono 16-bit form and is
screened for the breath sounds that neural TTS reproduces from its training data.
"""
import argparse
import json
from pathlib import Path
import sys

DEFAULT_TEXT = '신원을 말씀해 주세요'


def run(repo, out_dir, text, lang, peak, speed, total_step):
    sys.path.insert(0, str(Path(__file__).parent))
    sys.path.insert(0, str(repo / 'py'))
    from build_prompt_audio import convert
    import soundfile as sf
    from helper import load_text_to_speech, load_voice_style

    assets = repo / 'assets'
    tts = load_text_to_speech(str(assets / 'onnx'))
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for style_path in sorted((assets / 'voice_styles').glob('*.json')):
        name = style_path.stem
        raw = out_dir / ('_raw_' + name + '.wav')
        final = out_dir / ('supertonic_' + name + '.wav')
        try:
            style = load_voice_style([str(style_path)], verbose=False)
            wav, duration = tts(text, lang, style, total_step, speed)
            # wav is [B, T] padded; duration gives the real length of each item.
            clip = wav[0, :int(tts.sample_rate * float(duration[0]))]
            sf.write(str(raw), clip, tts.sample_rate)
            info = convert(raw, final, peak)
            raw.unlink()
            results.append({'voice': name, 'seconds': info['seconds'],
                            'bytes': info['bytes'], 'clean': info['clean'],
                            'internal_gap_seconds': info['internal_gap_seconds'],
                            'edge_breath_blocks': info['edge_breath_blocks']})
        except Exception as exc:
            results.append({'voice': name, 'error': '%s: %s' % (type(exc).__name__, exc)})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    return results


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('out_dir', type=Path)
    p.add_argument('--repo', type=Path,
                   default=Path(r'C:\dev\mechadog-voice-20260913\supertonic-repo'))
    p.add_argument('--text', default=DEFAULT_TEXT)
    p.add_argument('--lang', default='ko')
    p.add_argument('--peak', type=int, default=29500)
    p.add_argument('--speed', type=float, default=1.0)
    p.add_argument('--total-step', type=int, default=8)
    a = p.parse_args()
    run(a.repo, a.out_dir, a.text, a.lang, a.peak, a.speed, a.total_step)
