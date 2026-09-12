"""Create an unflashed UART0 stream candidate from the compiled codec SDK."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

from fix_sdk_startup import fix_startup


def replace_once(path, old, new):
    data = path.read_bytes()
    if data.count(old) != 1:
        raise RuntimeError(f"Unexpected SDK source: {path}: {old!r}")
    path.write_bytes(data.replace(old, new))


def capture_only_board(target):
    target = Path(target)
    if target.name != "offline-stream-1.12.16" or not (target / "codec-manifest.json").exists():
        raise ValueError("Expected private stream candidate")
    board = target / "driver/boards/CI-D02GS01J.c"
    old = b"    pad_config_for_power_amplifier();"
    new = b"    /* Capture-only bench: do not drive an unverified amplifier GPIO. */"
    if new not in board.read_bytes():
        replace_once(board, old, new)
    (target / "capture-only-board.json").write_text(
        json.dumps(
            {
                "file": str(board.relative_to(target)),
                "sha256": hashlib.sha256(board.read_bytes()).hexdigest(),
                "change": "Microphone registration leaves amplifier PC4 pin untouched",
                "runtime_verified": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return board


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    root = parser.parse_args().root
    source = root / "offline-codec-1.12.16"
    target = root / "offline-stream-1.12.16"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite {target}")
    if not (source / "codec-manifest.json").exists():
        raise SystemExit("Prepare the codec SDK first")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("build"))
    src = target / "projects/offline_asr_sample/src"
    changes = []
    for name in ("voice_encoder.c", "voice_encoder.h", "voice_stream.c", "voice_stream.h"):
        shutil.copy2(Path(__file__).parent / name, src / name)
        changes.append(src / name)
    cfg = src / "user_config.h"
    data = cfg.read_bytes()
    for name in (b"CONFIG_CI_LOG_UART", b"MSG_COM_USE_UART_EN", b"AUDIO_PLAYER_ENABLE"):
        data, n = re.subn(rb"(?m)^(#define\s+" + name + rb"\s+)[^\r\n]+", rb"\g<1>0", data)
        if n != 1:
            raise RuntimeError(f"Unexpected config: {name!r}")
    data = data.replace(
        b"#endif /* _USER_CONFIG_H_ */",
        b"#define COMMAND_LINE_CONSOLE_EN 0\n#endif /* _USER_CONFIG_H_ */",
    )
    cfg.write_bytes(data)
    changes.append(cfg)
    main_c = src / "main.c"
    replace_once(
        main_c,
        b'#include "sdk_default_config.h"',
        b'#include "sdk_default_config.h"\n#include "voice_stream.h"',
    )
    replace_once(
        main_c,
        b"    xTaskCreate(audio_in_manage_inner_task,",
        b"    we_stream_init(); /* Failure leaves capture streaming disabled. */\n    xTaskCreate(audio_in_manage_inner_task,",
    )
    changes.append(main_c)
    ssp = src / "ci_ssp_config.c"
    replace_once(ssp, b".iis_out_enable = USE_IIS1_OUT_PRE_RSLT_AUDIO,", b".iis_out_enable = true,")
    replace_once(ssp, b".vad_mark_enable = true,", b".vad_mark_enable = false,")
    # Keep USE_IIS1_OUT_PRE_RSLT_AUDIO=0: enable only DSP callback, not external IIS pins.
    changes.append(ssp)
    callback = target / "components/audio_pre_rslt_iis_out/ci130x_audio_pre_rslt_out.c"
    replace_once(
        callback,
        b'#include "codec_manager.h"',
        b'#include "codec_manager.h"\n#include "voice_stream.h"',
    )
    replace_once(
        callback,
        b"void audio_pre_rslt_write_data(int16_t* left,int16_t* right)\r\n{",
        b"void audio_pre_rslt_write_data(int16_t* left,int16_t* right)\r\n{\n"
        b"    we_stream_submit(right, AUDIO_CAP_POINT_NUM_PER_FRM); /* DST1, 16 kHz */",
    )
    changes.append(callback)
    board = capture_only_board(target)
    changes.append(board)
    project = target / "projects/offline_asr_sample/project_file/source_file.prj"
    with project.open("ab") as f:
        f.write(b"\nsource-file: projects/offline_asr_sample/src/voice_stream.c\n")
    changes.append(project)
    startup_files = fix_startup(target)
    changes.extend(target / name for name in startup_files)
    manifest = {
        "source": str(source),
        "status": "UNFLASHED; board USB/UART0 route unverified",
        "capture": "SDK DST1 after existing 32-to-16 kHz processing; no custom decimation",
        "start": "Host framed command only; maximum 250 frames / five seconds",
        "transport": "UART0 115200, interrupt-driven TX with 40 ms deadline; 8 PCM frames",
        "remaining": [
            "board mapping",
            "recovery binary",
            "packaging",
            "real audio/CPU/memory test",
        ],
        "files": {
            str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest() for p in changes
        },
    }
    (target / "stream-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
