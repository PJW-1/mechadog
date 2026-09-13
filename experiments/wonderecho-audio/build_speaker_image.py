"""Official CI1302 packaging: keep factory resources, add one PCM prompt, preserve NV."""

import argparse
import json
import struct
import subprocess
import wave
from pathlib import Path

from build_full_candidate import FACTORY_SHA, sha
from inspect_factory import inspect


def entries(data):
    count = struct.unpack_from("<H", data)[0]
    if count > 1000 or 2 + count * 10 > len(data):
        raise ValueError("Invalid file container")
    output = {}
    for i in range(count):
        key, offset, size = struct.unpack_from("<HII", data, 2 + 10 * i)
        if key in output or offset < 2 + count * 10 or offset + size > len(data):
            raise ValueError("Invalid resource entry")
        output[key] = data[offset : offset + size]
    return output


def pcm_prompt(source):
    with wave.open(str(source), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError("Expected mono 16 kHz PCM16")
        pcm = wav.readframes(wav.getnframes())
    if not 16000 <= len(pcm) <= 128000:
        raise ValueError("Prompt must be between 0.5 and 4 seconds")
    # SDK check_ci_voice_head expects a 20-byte fmt chunk for uncompressed PCM.
    fmt = struct.pack("<HHIIHHHH", 1, 1, 16000, 32000, 2, 16, 2, 0)
    return (
        b"RIFF"
        + struct.pack("<I", len(pcm) + 40)
        + b"WAVEfmt "
        + struct.pack("<I", 20)
        + fmt
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def build(root, output, package, source):
    factory = root / "recovery/2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin"
    raw = factory.read_bytes()
    if sha(raw) != FACTORY_SHA or Path(package).name != package:
        raise ValueError("Unexpected factory or package")
    code = root / package / "user_code/user_code.bin"
    original = inspect(factory, code)["partitions"]
    nv = raw[8192 + 268 : 8192 + 276]
    nv_offset, nv_size = struct.unpack("<II", nv)
    output.mkdir(parents=True, exist_ok=False)
    parts = output / "parts"
    parts.mkdir()
    (parts / "boot.bin").write_bytes(raw[:8192])
    for name, part in original.items():
        (parts / (name + ".bin")).write_bytes(raw[part["offset"] : part["offset"] + part["size"]])
    old_entries = entries((parts / "user.bin").read_bytes())
    if 65000 in old_entries:
        raise ValueError("Prompt resource ID is already in use")
    prompt = pcm_prompt(source)
    user_parts = output / "user-parts"
    user_parts.mkdir()
    for key, data in old_entries.items():
        (user_parts / f"[{key}]original.bin").write_bytes(data)
    (user_parts / "[65000]identity.wav").write_bytes(prompt)
    new_user = user_parts / "user-parts.bin"
    tool = root / "offline-speaker-1.12.16/tools/ci-tool-kit.exe"
    subprocess.run([str(tool), "merge", "user-file", "-i", str(user_parts)], check=True, timeout=60)
    expected_entries = dict(old_entries)
    expected_entries[65000] = prompt
    if entries(new_user.read_bytes()) != expected_entries:
        raise ValueError("Official merge did not preserve resource bytes")
    files = {name: parts / (name + ".bin") for name in ("asr", "dnn", "voice")}
    files.update(user=new_user, code1=code)
    capacities = {key: (path.stat().st_size + 4095) // 4096 * 4096 for key, path in files.items()}
    if 16384 + sum(capacities.values()) > nv_offset:
        raise ValueError("Image would overlap preserved NV storage")
    images = output / "image"
    images.mkdir()
    args = [
        str(tool),
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
        "WonderEcho_Speaker_Bench",
        "--firmware-version",
        "2.1.0",
        "--boot-file",
        str(parts / "boot.bin"),
        "--output-path",
        str(images),
    ]
    for key, option in (
        ("code1", "user-code"),
        ("asr", "asr"),
        ("dnn", "nn"),
        ("voice", "voice"),
        ("user", "user-file"),
    ):
        file_opt = option if key in ("code1", "user") else option + "-file"
        size_opt = option + "-size"
        version = 110 if key in ("code1", "user") else original[key]["version"]
        args += [
            "--" + file_opt,
            str(files[key]),
            "--" + size_opt,
            str(capacities[key]),
            "--" + option + "-version",
            str(version),
        ]
    run = subprocess.run(args, capture_output=True, timeout=60)
    (output / "packer.log").write_bytes(run.stdout + run.stderr)
    run.check_returncode()
    bins = list(images.glob("*.bin"))
    if len(bins) != 1:
        raise ValueError("Expected one image")
    packed = bins[0].read_bytes()
    actual = inspect(bins[0], code)
    if packed[:8192] != raw[:8192] or packed[8460:8468] != nv:
        raise ValueError("Boot bytes or NV layout changed")
    for key in ("asr", "dnn", "voice"):
        if actual["partitions"][key]["sha256"] != original[key]["sha256"]:
            raise ValueError("Factory resources changed")
    for key in ("code1", "user"):
        if actual["partitions"][key]["sha256"] != sha(files[key].read_bytes()):
            raise ValueError("Packaged content mismatch")
    actual.update(
        original_factory_sha256=FACTORY_SHA,
        boot_and_factory_resource_bytes_preserved=True,
        nv_layout_preserved=True,
        user_entries_preserved=True,
        prompt_id=65000,
        prompt_seconds=(len(prompt) - 48) / 32000,
        prompt_sha256=sha(prompt),
        layout_relocated=True,
        runtime_verified=False,
    )
    (output / "verification.json").write_text(json.dumps(actual, indent=2) + "\n")
    print(json.dumps(actual, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--code-package", required=True)
    parser.add_argument("--prompt", required=True, type=Path)
    args = parser.parse_args()
    build(args.root, args.output, args.code_package, args.prompt)
