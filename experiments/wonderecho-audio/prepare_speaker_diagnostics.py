"""Instrument the private speaker SDK without changing player timing or GPIOs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def prepare(root):
    sdk = root / 'offline-speaker-1.12.16'
    if not (sdk / 'speaker-manifest.json').exists():
        raise ValueError('Expected prepared speaker SDK')
    player = sdk / 'components/player/audio_play'
    patches = {
        'audio_play_process.c': [
            (b'    if(RETURN_OK != task_audio_play_init_step())', b'    we_play_trace = 100;\n    if(RETURN_OK != task_audio_play_init_step())'),
            (b'                audio_play_state = AUDIO_PLAY_STATE_START;', b'                we_play_trace = 110;\n                audio_play_state = AUDIO_PLAY_STATE_START;'),
            (b'                ret = audio_play_start();', b'                we_play_trace = 120;\n                ret = audio_play_start();\n                we_play_trace = 121;'),
            (b'                    cm_config_pcm_buffer(sg_play_device_index, CODEC_OUTPUT, &pcm_buffer_info);', b'                    we_play_trace = 210;\n                    cm_config_pcm_buffer(sg_play_device_index, CODEC_OUTPUT, &pcm_buffer_info);'),
            (b'                    audio_play_hw_start(DISABLE, &audio_format_info);', b'                    we_play_trace = 220;\n                    audio_play_hw_start(DISABLE, &audio_format_info);\n                    we_play_trace = 221;'),
        ],
        'get_play_data.c': [
            (b'void task_get_play_data(void *pvParameters)\n{', b'void task_get_play_data(void *pvParameters)\n{\n    we_read_trace = 300;'),
            (b'    if(-1 == check_ci_voice_head(', b'    we_read_trace = 400;\n    if(-1 == check_ci_voice_head('),
            (b'    prompt_decoder.config((void*)(&config));', b'    prompt_decoder.config((void*)(&config));\n    we_read_trace = 410 + config.mode;'),
        ],
    }
    hashes, pending = {}, []
    for name, replacements in patches.items():
        path = player / name
        backup = path.with_suffix('.c.before-speaker-diagnostics')
        data = (backup if backup.exists() else path).read_bytes().replace(b'\r\n', b'\n')
        for old, new in replacements:
            if data.count(old) < 1:
                raise ValueError(f'Unexpected source: {name}: {old!r}')
            data = data.replace(old, new)
        pending.append((path, backup, b'#include "voice_prompt.h"\n' + data))
    for path, backup, data in pending:
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_bytes(data)
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('voice_stream.c', 'voice_prompt.c', 'voice_prompt.h'):
        shutil.copy2(Path(__file__).parent / name, sdk / 'projects/offline_asr_sample/src' / name)
    (sdk / 'speaker-diagnostics-manifest.json').write_text(json.dumps(hashes, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    prepare(parser.parse_args().root)
