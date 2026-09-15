"""Prepare an isolated CI1302/internal-RC compile candidate; never flash."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    source = args.root / "vendor/offline-1.12.16-archive/extracted/CI130X_SDK_V1.12.16"
    target = args.root / "offline-ci1302-1.12.16"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite {target}")
    config = Path("projects/offline_asr_sample/src/user_config.h")
    original = (source / config).read_bytes()
    changed = original
    settings = {
        b"CI_CHIP_TYPE": b"1302",
        b"USE_EXTERNAL_CRYSTAL_OSC": b"0",
        b"UART_PROTOCOL_NUMBER": b"(HAL_UART1_BASE)",
        b"UART_PROTOCOL_BAUDRATE": b"(UART_BaudRate115200)",
    }
    for name, value in settings.items():
        pattern = rb"(?m)^(#define\s+" + name + rb"\s+)[^\r\n]+"
        changed, count = re.subn(pattern, lambda m, value=value: m[1] + value, changed)
        if count != 1:
            raise SystemExit(f"Expected exactly one {name!r}; got {count}")
    # Reference board selection is for compile verification, not a verified PCB map.
    additions = (
        b'\n#define BOARD_PORT_FILE "CI-D02GS01J.c"\n'
        b"#define UART0_PAD_OPENDRAIN_MODE_EN 1\n"
        b"#define UART1_PAD_OPENDRAIN_MODE_EN 1\n"
    )
    marker = b"#endif /* _USER_CONFIG_H_ */"
    if changed.count(marker) != 1:
        raise SystemExit("Missing config closing guard")
    changed = changed.replace(marker, additions + marker)
    shutil.copytree(source, target)
    (target / config).write_bytes(changed)
    manifest = {
        "source": str(source),
        "target": str(target),
        "config": str(config),
        "before_sha256": hashlib.sha256(original).hexdigest(),
        "after_sha256": hashlib.sha256(changed).hexdigest(),
        "status": "compile-only; not flash-ready; board routing and model packaging unverified",
        "settings": {k.decode(): v.decode() for k, v in settings.items()},
        "notes": "Internal RC calibration remains OFF. No model files or clock guards changed.",
    }
    (target / "candidate-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
