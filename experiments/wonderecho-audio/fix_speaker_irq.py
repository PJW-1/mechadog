"""Fix SDK ISR wake-pointer forwarding, with output interrupt counters."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def prepare(root):
    sdk = root / "offline-speaker-1.12.16"
    if not (sdk / "speaker-diagnostics-manifest.json").exists():
        raise ValueError("Prepare speaker diagnostics first")
    path = sdk / "components/codec_manager/codec_manager.c"
    data = path.read_bytes()
    old = b"send_one_buf_done_event(&xHigherPriorityTaskWoken);"
    signature = b"void cm_output_interrupt_handler(IISDMAChax dma_channel, BaseType_t * xHigherPriorityTaskWoken)"
    if data.count(old) != 1 or data.count(signature) != 1:
        raise ValueError("Unexpected SDK ISR source")
    shutil.copy2(path, path.with_suffix(".c.before-speaker-irq"))
    data = data.replace(
        old, b"++we_buffer_events;\n        send_one_buf_done_event(xHigherPriorityTaskWoken);"
    )
    start = data.index(b"{", data.index(signature)) + 1
    data = data[:start] + b"\n    ++we_output_irqs;\n" + data[start:]
    path.write_bytes(b'#include "voice_prompt.h"\n' + data)
    for name in ("voice_stream.c", "voice_prompt.c", "voice_prompt.h"):
        shutil.copy2(Path(__file__).parent / name, sdk / "projects/offline_asr_sample/src" / name)
    (sdk / "speaker-irq-fix.json").write_text(
        json.dumps(
            {
                "file": str(path.relative_to(sdk)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "change": "Forward BaseType_t* itself, not its address (BaseType_t**), to FreeRTOS ISR event wake flag.",
                "runtime_verified": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    prepare(parser.parse_args().root)
