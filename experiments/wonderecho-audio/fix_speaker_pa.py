"""Restore the vendor power-amplifier init the capture-only bench removed.

The CI-D02GS01J board enables its amplifier exactly once, from board init:
pad_config_for_power_amplifier() muxes PC4 to GPIO output and, because
PLAYER_CONTROL_PA is 0, ends by calling power_amplifier_on(). The player never
touches the PA in that configuration (audio_play_device.c guards both calls with
is_control_pa). The capture-only bench replaced that call with a comment, so PC4
was never configured nor driven and playback stayed silent while the DAC, DMA,
IRQs, decoder and completion callback all reported success.

This restores the vendor line verbatim; it introduces no new GPIO handling.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

BOARD = "driver/boards/CI-D02GS01J.c"
REMOVED = b"    /* Capture-only bench: do not drive an unverified amplifier GPIO. */\r"
VENDOR = b"    pad_config_for_power_amplifier();\r"


def prepare(root):
    sdk = root / "offline-speaker-1.12.16"
    if not (sdk / "speaker-pcm-buffer-fix.json").exists():
        raise ValueError("Expected PCM-buffer-fixed speaker SDK")
    board = sdk / BOARD
    data = board.read_bytes()
    if data.count(VENDOR):
        raise ValueError("PA init already restored")
    if data.count(REMOVED) != 1:
        raise ValueError("Unexpected SDK source: " + BOARD)

    reference = root / "vendor/offline-1.12.16-archive/extracted" / "CI130X_SDK_V1.12.16" / BOARD
    expected = reference.read_bytes()
    patched = data.replace(REMOVED, VENDOR)
    if patched != expected:
        raise ValueError("Patched board would still differ from the vendor original")

    backup = board.with_suffix(board.suffix + ".before-pa")
    if backup.exists():
        raise ValueError("Backup exists: " + str(backup))
    shutil.copy2(board, backup)
    board.write_bytes(patched)

    (sdk / "speaker-pa-fix.json").write_text(
        json.dumps(
            {
                "change": "Restored pad_config_for_power_amplifier() in board init; "
                "PC4 amplifier enable now matches the vendor original.",
                "evidence": {
                    "player_controls_pa": False,
                    "player_control_pa_default": 0,
                    "only_enable_path": "pad_config_for_power_amplifier() -> power_amplifier_on()",
                    "v10_symptom": "hw_starts=1, output_irqs=131, decoded_frames=67, "
                    "callback_result=1, user heard nothing",
                },
                "files": {BOARD: hashlib.sha256(patched).hexdigest()},
                "matches_vendor_original": True,
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
