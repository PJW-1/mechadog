"""Convert a synthesized prompt to the exact PCM the module accepts.

voice_prompt.c refuses a user file outside 48..128000 bytes, and the player is
configured for 16 kHz mono 16-bit, so the packed WAV must stay inside those
limits. Peak normalisation is applied because the module multiplies the sample
by its own 40/100 volume gain; the first prompt measured only 17% of full scale.

Nothing here resamples in place: the source file is left untouched.
"""
import argparse
import audioop
import hashlib
import json
from pathlib import Path
import wave

RATE = 16000            # module default; halve it when four clips must share the user area
WIDTH = 2
CHANNELS = 1
MAX_BYTES = 128000
MIN_BYTES = 48


def convert(source, target, peak, rate=None):
    with wave.open(str(source), 'rb') as src:
        params = {
            'channels': src.getnchannels(),
            'rate': src.getframerate(),
            'width': src.getsampwidth(),
            'frames': src.getnframes(),
        }
        data = src.readframes(src.getnframes())

    if params['width'] != WIDTH:
        data = audioop.lin2lin(data, params['width'], WIDTH)
    if params['channels'] == 2:
        data = audioop.tomono(data, WIDTH, 0.5, 0.5)
    elif params['channels'] != CHANNELS:
        raise ValueError('Unsupported channel count: %d' % params['channels'])
    out_rate = rate or RATE
    if params['rate'] != out_rate:
        data, _ = audioop.ratecv(data, WIDTH, CHANNELS, params['rate'], out_rate, None)

    data = _trim_silence(data)

    source_peak = audioop.max(data, WIDTH)
    if source_peak == 0:
        raise ValueError('Converted audio is silent')
    data = audioop.mul(data, WIDTH, peak / source_peak)

    total = len(data) + 44
    if total > MAX_BYTES or total < MIN_BYTES:
        raise ValueError('Packed WAV would be %d bytes; module accepts %d..%d '
                         '(%.2f s at %d Hz)'
                         % (total, MIN_BYTES, MAX_BYTES, len(data) / (out_rate * WIDTH), out_rate))

    with wave.open(str(target), 'wb') as dst:
        dst.setnchannels(CHANNELS)
        dst.setsampwidth(WIDTH)
        dst.setframerate(out_rate)
        dst.writeframes(data)

    written = target.read_bytes()
    result = {
        'source': str(source),
        'source_format': params,
        'target': str(target),
        'rate': out_rate,
        'channels': CHANNELS,
        'width': WIDTH,
        'seconds': round(len(data) / (out_rate * WIDTH), 3),
        'bytes': len(written),
        'limit_bytes': MAX_BYTES,
        'peak': audioop.max(data, WIDTH),
        'rms': audioop.rms(data, WIDTH),
        'sha256': hashlib.sha256(written).hexdigest(),
    }
    result.update(_profile(data))
    return result


def _trim_silence(data, ratio=0.03):
    """Drop leading and trailing non-speech.

    Neural TTS reproduces the breath intakes present in its training recordings.
    Those sit roughly 30-45 dB under the speech, so an absolute floor leaves them
    in; the gate is taken relative to the clip's own peak instead (0.03 is about
    -30 dB). Two blocks of padding keep the first consonant and the final decay.
    """
    step = RATE // 100 * WIDTH
    blocks = [data[i:i + step] for i in range(0, len(data), step)]
    threshold = audioop.max(data, WIDTH) * ratio
    loud = [i for i, b in enumerate(blocks) if audioop.max(b, WIDTH) > threshold]
    if not loud:
        return data
    start = max(loud[0] - 2, 0)
    stop = min(loud[-1] + 3, len(blocks))
    return b''.join(blocks[start:stop])


def _profile(data, ratio_breath=0.03, ratio_speech=0.10):
    """Report non-speech stretches that survive trimming, so breathy takes can be rejected."""
    step = int(RATE * 0.05) * WIDTH
    peak = audioop.max(data, WIDTH)
    blocks = [audioop.max(data[i:i + step], WIDTH) for i in range(0, len(data), step)]
    speech = [i for i, p in enumerate(blocks) if p > peak * ratio_speech]
    if not speech:
        return {'internal_gap_seconds': 0.0, 'breath_blocks': len(blocks), 'clean': False}
    inner = blocks[speech[0]:speech[-1] + 1]
    gap = sum(1 for p in inner if p <= peak * ratio_breath)
    edge = sum(1 for p in (blocks[:speech[0]] + blocks[speech[-1] + 1:])
               if p > peak * ratio_breath)
    return {
        'internal_gap_seconds': round(gap * 0.05, 2),
        'edge_breath_blocks': edge,
        'clean': gap == 0 and edge == 0,
    }


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path)
    p.add_argument('target', type=Path)
    p.add_argument('--peak', type=int, default=29500,
                   help='Peak sample after normalisation (default 29500 of 32767)')
    args = p.parse_args()
    print(json.dumps(convert(args.source, args.target, args.peak), indent=2))
