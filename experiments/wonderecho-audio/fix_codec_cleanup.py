"""Rebuild vendor Speex encoder sources with matched scratch-buffer ownership."""

import argparse
import hashlib
import json
from pathlib import Path


def fix(sdk):
    sdk = Path(sdk)
    if sdk.name != "offline-stream-1.12.16" or not (sdk / "stream-manifest.json").exists():
        raise ValueError("Expected the private stream candidate")
    directory = sdk / "components/cias_speex/libspeex"
    changes = {}
    for name in ("nb_celp.c", "sb_celp.c"):
        path = directory / name
        data = path.read_bytes()
        backup = path.with_name(name + ".before-cleanup-fix")
        if backup.exists():
            raise FileExistsError(backup)
        prefix = b"nb" if name.startswith("nb") else b"sb"
        start = data.index(b"void " + prefix + b"_encoder_destroy(void *state)")
        end = data.index(b"int " + prefix + b"_encoder_ctl", start)
        destructor = data[start:end]
        old = b"   speex_free_scratch(st->stack);"
        if destructor.count(old) != 1:
            raise ValueError("Unexpected encoder destructor")
        replacement = (
            b"   vPortFree(st->stack); /* Paired with pvPortMalloc in nb_encoder_init. */"
            if prefix == b"nb"
            else b"   /* Borrowed from the NB encoder; its destructor owns this buffer. */"
        )
        updated = data[:start] + destructor.replace(old, replacement) + data[end:]
        if prefix == b"nb":
            # Match the state layout observed in the shipped RISC-V library.
            marker = b"extern void *pvPortMalloc(size_t xWantedSize);"
            updated = updated.replace(
                marker,
                marker + b"\n#include <stddef.h>\n"
                b'_Static_assert(sizeof(EncState) == 1676, "Speex NB ABI size mismatch");\n'
                b'_Static_assert(offsetof(EncState, stack) == 48, "Speex NB ABI stack mismatch");',
            )
        backup.write_bytes(data)
        path.write_bytes(updated)
        changes[str(path.relative_to(sdk))] = hashlib.sha256(updated).hexdigest()
    project = sdk / "projects/offline_asr_sample/project_file/source_file.prj"
    data = project.read_bytes()
    for name in ("nb_celp.c", "sb_celp.c"):
        line = b"source-file: components/cias_speex/libspeex/" + name.encode()
        if line not in data:
            data += b"\n" + line + b"\n"
    project.write_bytes(data)
    changes[str(project.relative_to(sdk))] = hashlib.sha256(data).hexdigest()
    (sdk / "codec-cleanup-fix.json").write_text(
        json.dumps(
            {
                "files": changes,
                "reason": "NB scratch allocated with pvPortMalloc; SB borrows it. Free once using vPortFree.",
                "preserved": "Original libspeex.a retained; explicitly compiled encoder objects resolve symbols first.",
                "runtime_verified": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return changes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdk", type=Path)
    print(json.dumps(fix(parser.parse_args().sdk), indent=2))
