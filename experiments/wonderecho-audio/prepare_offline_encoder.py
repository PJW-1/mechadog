"""Link the vendor Speex codec into a separate, inactive compile candidate."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    root = parser.parse_args().root
    source = root / "offline-ci1302-1.12.16"
    target = root / "offline-codec-1.12.16"
    vendor = root / "vendor/CI130X_SDK_LLM_AIOT_2.2.7"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite {target}")
    if not (source / "candidate-manifest.json").exists():
        raise SystemExit("Prepare and build the internal-RC candidate first")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("build"))
    shutil.copytree(vendor / "components/cias_speex", target / "components/cias_speex")
    shutil.copy2(vendor / "libs/libspeex.a", target / "libs/libspeex.a")
    for name in ("voice_encoder.c", "voice_encoder.h"):
        shutil.copy2(
            Path(__file__).parent / name, target / "projects/offline_asr_sample/src" / name
        )
    project = target / "projects/offline_asr_sample/project_file"
    with (project / "source_file.prj").open("ab") as f:
        f.write(
            b"\nsource-file: projects/offline_asr_sample/src/voice_encoder.c\n"
            b"include-path: components/cias_speex/include\n"
            b"include-path: components/cias_speex/include/speex\n"
            b"include-path: components/cias_speex/port\n"
            b"include-path: components/cias_speex/libspeex\n"
        )
    makefile = project / "makefile"
    content = makefile.read_bytes()
    marker = b"include $(ROOT_DIR)/utils/common_tail.mk"
    if content.count(marker) != 1:
        raise SystemExit("Unexpected makefile; do not build candidate")
    content = content.replace(
        marker,
        (
            b"# Retain inactive adapter to verify real codec linkage; no startup call.\n"
            b"LD_FLAGS += -Wl,-u,we_encoder_init,-u,we_encoder_close,-u,we_encode_frame\n"
            b"LIBS += -lspeex\n"
            b"LIB_FILES += $(ROOT_DIR)/libs/libspeex.a\n" + marker
        ),
    )
    makefile.write_bytes(content)
    manifest = {
        "source": str(source),
        "codec_source": str(vendor),
        "status": "compile-only; codec not started; no microphone or UART hook installed",
        "vendor_library_sha256": hashlib.sha256(
            (target / "libs/libspeex.a").read_bytes()
        ).hexdigest(),
        "adapter_sha256": hashlib.sha256(
            (Path(__file__).parent / "voice_encoder.c").read_bytes()
        ).hexdigest(),
        "remaining": "runtime memory, CPU, sample clock, board routing, capture and transmit integration",
    }
    (target / "codec-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
