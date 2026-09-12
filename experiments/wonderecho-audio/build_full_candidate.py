"""Build a bench image with the official packer and unchanged factory resources.

No serial I/O. Does not establish runtime compatibility or flash readiness.
"""

import argparse
import hashlib
import json
import struct
import subprocess
from pathlib import Path

from inspect_factory import inspect

FACTORY_SHA = "c4328480d8f9cbe2e15dde41bb7d170d93c46c96c884742394322db87b5d2b2d"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build(
    root,
    output,
    code_package="offline-stream-package-startupfix",
    firmware_version="2.0.1",
    code_version=101,
):
    factory = root / "recovery/2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin"
    if Path(code_package).name != code_package:
        raise ValueError("Expected a package directory name")
    code = root / code_package / "user_code/user_code.bin"
    raw = factory.read_bytes()
    if sha(raw) != FACTORY_SHA:
        raise ValueError("Factory recovery hash mismatch")
    info = inspect(factory, code)
    if not info["fits_physical_code_space"]:
        raise ValueError("Candidate does not fit original code space")
    if "code2" in info["partitions"]:
        raise ValueError("Unexpected dual-code layout")
    table = raw[8192:8470]
    nv_offset, nv_size = struct.unpack_from("<II", table, 268)
    if nv_offset + nv_size != 2 * 1024 * 1024:
        raise ValueError("Unexpected factory NV layout")
    output.mkdir(parents=True, exist_ok=False)
    parts_dir = output / "factory-parts"
    parts_dir.mkdir()
    parts = info["partitions"]
    for name, part in parts.items():
        (parts_dir / (name + ".bin")).write_bytes(
            raw[part["offset"] : part["offset"] + part["size"]]
        )
    boot = parts_dir / "boot.bin"
    boot.write_bytes(raw[:8192])
    arguments = [
        str(root / "offline-stream-1.12.16/tools/ci-tool-kit.exe"),
        "mf",
        "-f",
        "v2",
        "--chip-name",
        "CI1302",
        "--rom-size",
        "2097152",
        "--nvdata-size",
        str(nv_size),
        "--factory-id",
        "100",
        "--brand-id",
        "100",
        "--board-name",
        "DEMO_Board",
        "--hardware-version",
        "2.0.0",
        "--firmware-name",
        "WonderEcho_Stream_Bench",
        "--firmware-version",
        firmware_version,
        "--boot-file",
        str(boot),
        "--user-code",
        str(code),
        "--user-code-version",
        str(code_version),
        "--user-code-size",
        str(info["code_space_before_next_partition"]),
    ]
    options = {
        "asr": ("asr-file", "asr-size", "asr-version"),
        "dnn": ("nn-file", "nn-size", "nn-version"),
        "voice": ("voice-file", "voice-size", "voice-version"),
        "user": ("user-file", "user-file-size", "user-file-version"),
    }
    for name, (file_opt, size_opt, version_opt) in options.items():
        part = parts[name]
        capacity = ((part["size"] + 4095) // 4096) * 4096
        arguments += [
            "--" + file_opt,
            str(parts_dir / (name + ".bin")),
            "--" + size_opt,
            str(capacity),
            "--" + version_opt,
            str(part["version"]),
        ]
    image_dir = output / "image"
    image_dir.mkdir()
    arguments += ["--output-path", str(image_dir)]
    run = subprocess.run(arguments, capture_output=True, timeout=60)
    (output / "packer.log").write_bytes(run.stdout + run.stderr)
    run.check_returncode()
    candidates = list(image_dir.glob("*.bin"))
    if len(candidates) != 1:
        raise ValueError("Expected one official packer image; inspect packer.log")
    result = inspect(candidates[0], code)
    packed = candidates[0].read_bytes()
    if packed[:8192] != raw[:8192]:
        raise ValueError("Packer changed boot bytes")
    for name in options:
        if result["partitions"][name] != parts[name]:
            raise ValueError(f"Factory resource content/layout changed: {name}")
    if result["partitions"]["code1"]["sha256"] != sha(code.read_bytes()):
        raise ValueError("Packed user code mismatch")
    if packed[8192 + 268 : 8192 + 276] != table[268:276]:
        raise ValueError("NV layout changed")
    result.update(
        {
            "original_factory_sha256": FACTORY_SHA,
            "boot_and_resource_bytes_verified": True,
            "runtime_verified": False,
            "flash_ready": False,
            "note": "Bench candidate only. Original resources retained; board/runtime not yet tested.",
        }
    )
    (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--code-package", default="offline-stream-package-startupfix")
    parser.add_argument("--firmware-version", default="2.0.1")
    parser.add_argument("--code-version", type=int, default=101)
    args = parser.parse_args()
    build(args.root, args.output, args.code_package, args.firmware_version, args.code_version)
