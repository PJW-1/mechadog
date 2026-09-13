"""Separate decoder progress from hardware startup in the private speaker SDK."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def prepare(root):
    sdk = root / "offline-speaker-1.12.16"
    if not (sdk / "speaker-irq-fix.json").exists():
        raise ValueError("Expected IRQ-fixed speaker SDK")
    player = sdk / "components/player/audio_play"
    specs = {
        "audio_play_process.c": [
            (
                b"            decode_frame_count++;",
                b"            decode_frame_count++;\n            ++we_decoded_frames;",
            )
        ],
        "get_play_data.c": [
            (
                b"we_read_trace = 410 + config.mode;",
                b"we_read_trace = 410 + config.mode;\n    we_wave_bytes = wave_total_lens;",
            )
        ],
        "audio_play_device.c": [
            (
                b"        cm_config_codec(sg_play_device_index, CODEC_OUTPUT, &sound_info);",
                b"        we_config_result = cm_config_codec(sg_play_device_index, CODEC_OUTPUT, &sound_info);",
            ),
            (
                b"    cm_start_codec(sg_play_device_index, CODEC_OUTPUT);",
                b"    ++we_hw_starts;\n    we_start_result = cm_start_codec(sg_play_device_index, CODEC_OUTPUT);",
            ),
        ],
    }
    pending = []
    for name, replacements in specs.items():
        path = player / name
        data = path.read_bytes()
        for old, new in replacements:
            if old not in data or new in data:
                raise ValueError("Unexpected or already instrumented source: " + name)
            data = data.replace(old, new)
        if b'#include "voice_prompt.h"' not in data:
            data = b'#include "voice_prompt.h"\n' + data
        pending.append((path, data))
    for path, data in pending:
        shutil.copy2(path, path.with_suffix(".c.before-output-trace"))
        path.write_bytes(data)
    for name in ("voice_stream.c", "voice_prompt.c", "voice_prompt.h"):
        shutil.copy2(Path(__file__).parent / name, sdk / "projects/offline_asr_sample/src" / name)
    (sdk / "speaker-output-trace.json").write_text(
        json.dumps(
            {
                str(p.relative_to(sdk)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p, _ in pending
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    prepare(p.parse_args().root)
