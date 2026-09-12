"""Create a separate speaker SDK; never overwrite the validated USB capture SDK."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def prepare(root):
    source = root / "offline-stream-1.12.16"
    target = root / "offline-speaker-1.12.16"
    if not (source / "codec-cleanup-fix.json").exists():
        raise ValueError("Expected the verified cleanup-fixed capture SDK")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("build", "__pycache__"))
    config = target / "projects/offline_asr_sample/src/user_config.h"
    data = config.read_bytes()
    # Fixed PCM prompt: no additional compressed audio decoder or recognition model.
    for name, value in (
        ("AUDIO_PLAYER_ENABLE", 1),
        ("USE_MP3_DECODER", 0),
        ("AUDIO_PLAY_SUPPT_MP3_PROMPT", 0),
    ):
        data, count = re.subn(
            rb"(?m)^(#define\s+" + name.encode() + rb"\s+)\d+",
            lambda m, value=value: m[1] + str(value).encode(),
            data,
        )
        if count != 1:
            raise ValueError(name)
    data += b"\n#define WE_HOST_PROMPT_ONLY 1\n"
    config.write_bytes(data)
    prompts = list(target.rglob("prompt_player.c"))
    if len(prompts) != 1:
        raise ValueError("Expected one SDK prompt wrapper")
    path = prompts[0]
    data = path.read_bytes()
    if b"#if !AUDIO_PLAYER_ENABLE" not in data:
        raise ValueError("Unexpected SDK prompt wrappers")
    data = data.replace(
        b"#if !AUDIO_PLAYER_ENABLE", b"#if !AUDIO_PLAYER_ENABLE || WE_HOST_PROMPT_ONLY"
    )
    multi = b"uint32_t prompt_play_by_multi_cmd_id(prompt_play_info_t *p_play_info, int number, play_done_callback_t play_done_callback)"
    start = data.index(b"{", data.index(multi)) + 1
    data = (
        data[:start]
        + b"\n#if WE_HOST_PROMPT_ONLY\n    if (play_done_callback) play_done_callback(NULL);\n    return 0;\n#endif\n"
        + data[start:]
    )
    path.write_bytes(data)
    for name in ("voice_stream.c", "voice_prompt.c", "voice_prompt.h"):
        shutil.copy2(
            Path(__file__).parent / name, target / "projects/offline_asr_sample/src" / name
        )
    project = target / "projects/offline_asr_sample/project_file/source_file.prj"
    with project.open("ab") as file:
        file.write(b"\nsource-file: projects/offline_asr_sample/src/voice_prompt.c\n")
    (target / "speaker-manifest.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "status": "unflashed playback candidate",
                "policy": "Host-triggered prompt only; no factory boot/wake/ASR prompts",
                "amplifier": "PC4 remains untouched pending board evidence",
                "files": {
                    str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (config, path)
                },
            },
            indent=2,
        )
        + "\n"
    )
    print(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    prepare(parser.parse_args().root)
