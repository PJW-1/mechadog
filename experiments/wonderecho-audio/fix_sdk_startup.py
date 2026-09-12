"""Apply narrowly scoped startup fixes to the private stream SDK, never vendor originals."""

import argparse
import hashlib
import json
from pathlib import Path


def fix_startup(sdk):
    sdk = Path(sdk)
    if sdk.name != "offline-stream-1.12.16" or not (sdk / "codec-manifest.json").is_file():
        raise ValueError("Expected the private offline-stream SDK candidate")
    edits = {
        "system/platform_config.h": [
            (
                b"float get_freq_factor();",
                b"float get_freq_factor();\nfloat load_freq_correct_factor(void);",
            )
        ],
        "startup/ci130x_init.c": [
            (b"#include <stdint.h>", b"#include <stdint.h>\n#include <string.h>"),
            (
                b"memset(&SHARE_SRAM_ADDR,0x00,&SHARE_SRAM_SIZE);",
                b"memset(&SHARE_SRAM_ADDR, 0, (size_t)(uintptr_t)&SHARE_SRAM_SIZE);",
            ),
        ],
        "system/platform_config.c": [
            (b'#include "platform_config.h"', b'#include "platform_config.h"\n#include <string.h>'),
            (b"float load_freq_correct_factor()", b"float load_freq_correct_factor(void)"),
            (
                b"float t = *(float*)&buffer[0];",
                b"float t;\n        memcpy(&t, buffer, sizeof(t));",
            ),
            (
                b"uint32_t check_value_i = ~*(uint32_t*)&buffer[4];",
                b"uint32_t check_value_i;\n        memcpy(&check_value_i, buffer + 4, sizeof(check_value_i));\n        check_value_i = ~check_value_i;",
            ),
            (
                b"float check_value_f = *(float*)&check_value_i;",
                b"float check_value_f;\n        memcpy(&check_value_f, &check_value_i, sizeof(check_value_f));",
            ),
        ],
    }
    prepared = []
    for name, replacements in edits.items():
        path = sdk / name
        original = path.read_bytes()
        updated = original
        for old, new in replacements:
            if new in updated:
                continue
            if updated.count(old) != 1:
                raise ValueError(f"Unexpected SDK contents: {name}: {old!r}")
            updated = updated.replace(old, new)
        prepared.append((path, original, updated))
    result = {}
    for path, original, updated in prepared:
        if original != updated:
            backup = path.with_name(path.name + ".before-startup-fix")
            if backup.exists():
                raise FileExistsError(backup)
            backup.write_bytes(original)
            path.write_bytes(updated)
        result[str(path.relative_to(sdk))] = hashlib.sha256(updated).hexdigest()
    (sdk / "startup-fix-manifest.json").write_text(
        json.dumps(
            {
                "files": result,
                "changes": [
                    "Correct float function prototype",
                    "Declare memset and convert linker absolute size explicitly",
                    "Copy flash calibration bits without aliasing/unaligned pointer casts",
                ],
                "unchanged": "Clock source, calibration validation limits and fallback factor; vendor source retained",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdk", type=Path)
    print(json.dumps(fix_startup(parser.parse_args().sdk), indent=2))
