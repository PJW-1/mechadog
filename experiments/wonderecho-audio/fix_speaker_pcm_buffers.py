"""Match PCM input/read capacities; retain every sample and reject oversized blocks."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def prepare(root):
    sdk = root / "offline-speaker-1.12.16"
    if not (sdk / "speaker-output-trace.json").exists():
        raise ValueError("Expected output-traced speaker SDK")
    player = sdk / "components/player/audio_play"
    config = sdk / "projects/offline_asr_sample/src/user_config.h"
    if b"WE_PCM_PROMPT_INPUT_BYTES" in config.read_bytes():
        raise ValueError("PCM buffer fix already applied")
    pending = [(config, config.read_bytes() + b"\n#define WE_PCM_PROMPT_INPUT_BYTES 1152U\n")]
    replacements = {
        "get_play_data.h": [
            (
                b"#define GET_PLAY_DATA_BUFF_SIZE                    (576U)",
                b"#define GET_PLAY_DATA_BUFF_SIZE                    WE_PCM_PROMPT_INPUT_BYTES",
            )
        ],
        "audio_play_decoder.c": [
            (b"#define MS_WAV_IN_SIZE (1152)", b"#define MS_WAV_IN_SIZE WE_PCM_PROMPT_INPUT_BYTES")
        ],
        "audio_play_process.c": [
            (
                b"#define READBUF_SIZE            (576U)",
                b'#define READBUF_SIZE            WE_PCM_PROMPT_INPUT_BYTES\n_Static_assert(READBUF_SIZE >= WE_PCM_PROMPT_INPUT_BYTES, "PCM input buffer too small");\n_Static_assert(GET_PLAY_DATA_BUFF_SIZE >= WE_PCM_PROMPT_INPUT_BYTES, "PCM read block too small");',
            ),
            (
                b"        data_in_size = curr_decoder_ops->data_in_size;",
                b"        data_in_size = curr_decoder_ops->data_in_size;\n        if (data_in_size == 0 || data_in_size > READBUF_SIZE || data_in_size > GET_PLAY_DATA_BUFF_SIZE) {\n            play_end_cb_flag = AUDIO_PLAY_CB_STATE_INTERNAL_ERR;\n            return RETURN_ERR;\n        }",
            ),
        ],
    }
    for name, changes in replacements.items():
        path = player / name
        data = path.read_bytes()
        for old, new in changes:
            if data.count(old) != 1:
                raise ValueError("Unexpected SDK source: " + name)
            data = data.replace(old, new)
        pending.append((path, data))
    for path, _ in pending:
        backup = path.with_suffix(path.suffix + ".before-pcm-buffers")
        if backup.exists():
            raise ValueError("Backup exists: " + str(backup))
    for path, data in pending:
        shutil.copy2(path, path.with_suffix(path.suffix + ".before-pcm-buffers"))
        path.write_bytes(data)
    prompt = Path(__file__).parent / "voice_prompt.c"
    shutil.copy2(prompt, sdk / "projects/offline_asr_sample/src/voice_prompt.c")
    (sdk / "speaker-pcm-buffer-fix.json").write_text(
        json.dumps(
            {
                "change": "1152-byte PCM blocks now fit input buffer and each flash read block; no frame skipping.",
                "additional_buffer_bytes": 1728,
                "files": {
                    str(p.relative_to(sdk)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p, _ in pending
                },
                "runtime_verified": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    prepare(p.parse_args().root)
