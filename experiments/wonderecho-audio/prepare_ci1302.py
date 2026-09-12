"""Prepare a compile-only CI1302 candidate; never accesses hardware.

Generic CI-D02GS01J pin mapping still needs WonderEcho schematic verification.
Only official documented chip/clock/UART settings are adapted; codec/model stay
at SDK defaults. Destination must not exist to preserve previous build evidence.
"""

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


def prepare(base: Path):
    source = base / "vendor/CI130X_SDK_LLM_AIOT_2.2.7"
    target = base / "candidate-sdk-2.2.7"
    relative = Path("projects/offline_asr_llm_aiot_uart_sample/app/app_main/user_config.h")
    content = (source / relative).read_bytes()
    original_hash = hashlib.sha256(content).hexdigest()
    changes = {
        "USE_CI_D02GS01J_BOARD": ("0", "1"),
        "USE_CI_D06GT01D_BOARD": ("1", "0"),
        "UART0_PAD_OPENDRAIN_MODE_EN": ("0", "1"),
        "UART1_PAD_OPENDRAIN_MODE_EN": ("0", "1"),
        "USE_EXTERNAL_CRYSTAL_OSC": ("1", "0"),
        "UART_BAUDRATE_CALIBRATE": ("1", "0"),
        "UART_NUM_SEND_PLAY_AUDIO_BAUDRATE": ("UART_BaudRate921600", "UART_BaudRate115200"),
    }
    for name, (old, new) in changes.items():
        pattern = (
            rb"(?m)^(#define[ \t]+" + name.encode() + rb"[ \t]+)" + old.encode() + rb"(?=[ \t\r\n])"
        )
        content, count = re.subn(pattern, lambda match, new=new: match[1] + new.encode(), content)
        if count != 1:
            raise ValueError(f"Unexpected SDK definition for {name}: {count} matches")
    shutil.copytree(source, target)  # fails if destination exists; no overwrites
    (target / relative).write_bytes(content)
    manifest = {
        "status": "compile_only_not_flash_ready",
        "source": str(source),
        "target": str(target),
        "changed_file": relative.as_posix(),
        "source_sha256": original_hash,
        "candidate_sha256": hashlib.sha256(content).hexdigest(),
        "changes": changes,
        "unverified": [
            "WonderEcho GPIO/mic/PA mapping",
            "USB UART routing",
            "factory recovery image",
            "runtime audio and RAM use",
        ],
        "reference": "https://docs.hiwonder.com/projects/WonderEcho/en/latest/docs/5_Firmware_Development.html",
    }
    (target / "candidate-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(target)


if __name__ == "__main__":
    prepare(Path(sys.argv[1]).resolve())
