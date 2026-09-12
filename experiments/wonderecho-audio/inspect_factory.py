"""Read the SDK v2 partition table; never patch a firmware image."""

import argparse
import hashlib
import json
import struct
from pathlib import Path


def inspect(factory, candidate):
    image = factory.read_bytes()
    table = bytearray(image[0x2000 : 0x2000 + 278])
    if len(table) != 278 or table[152:161] != b"CI1302\0\0\0" or table[161] != 2:
        raise ValueError("Not the expected CI1302 v2 factory partition table")
    # SDK get_partition_list_checksum normalizes mutable status bytes.
    for i, value in enumerate((0xF0, 0xFF, 0, 0, 0, 0)):
        table[166 + 17 * i + 16] = value
    checksum = sum(table[:276]) & 0xFFFF
    if checksum != struct.unpack_from("<H", table, 276)[0]:
        raise ValueError("Factory partition table checksum mismatch")
    partitions = {}
    for i, name in enumerate(("code1", "code2", "asr", "dnn", "voice", "user")):
        version, offset, size, crc, status = struct.unpack_from("<IIIIB", table, 166 + i * 17)
        if offset == 0xFFFFFFFF:
            continue
        if offset < 0x4000 or size == 0 or offset + size > len(image):
            raise ValueError(f"Invalid partition bounds: {name}")
        partitions[name] = {
            "offset": offset,
            "size": size,
            "version": version,
            "sha256": hashlib.sha256(image[offset : offset + size]).hexdigest(),
        }
    ordered = sorted(partitions.values(), key=lambda p: p["offset"])
    if any(
        a["offset"] + a["size"] > b["offset"] for a, b in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("Overlapping factory partitions")
    code_start = partitions["code1"]["offset"]
    next_start = min(p["offset"] for p in ordered if p["offset"] > code_start)
    size = candidate.stat().st_size
    return {
        "factory": str(factory),
        "factory_sha256": hashlib.sha256(image).hexdigest(),
        "partition_checksum_verified": checksum,
        "partitions": partitions,
        "candidate": str(candidate),
        "candidate_size": size,
        "code_space_before_next_partition": next_start - code_start,
        "fits_physical_code_space": size <= next_start - code_start,
        "runtime_model_abi_verified": False,
        "flash_ready": False,
        "note": "Size fit alone does not validate resource ABI, board routing, or the updater.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("factory", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.factory, args.candidate), indent=2))
