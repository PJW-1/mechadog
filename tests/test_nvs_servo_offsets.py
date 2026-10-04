"""NVS 서보 편차 도구 — 합성 이미지로 읽기·바꾸기·CRC·다른 항목 보존."""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "nvs_servo_offsets",
    Path(__file__).resolve().parents[1] / "tools" / "probe" / "nvs_servo_offsets.py",
)
nvs = importlib.util.module_from_spec(spec)
sys.modules["nvs_servo_offsets"] = nvs
spec.loader.exec_module(nvs)


def _entry(ns: int, typ: int, key: str, data: bytes) -> bytes:
    body = (
        bytes([ns, typ, 1, 0xFF])
        + b"\0\0\0\0"
        + key.encode().ljust(16, b"\0")
        + data.ljust(8, b"\xff")
    )
    return body[:4] + struct.pack("<I", nvs.entry_crc(body)) + body[8:]


def _image(offsets: dict[int, int], extra: list[bytes]) -> bytes:
    entries = [_entry(0, 0x01, "mechdog", bytes([3])), _entry(0, 0x01, "nvs.net80211", bytes([4]))]
    entries += [
        _entry(3, nvs.TYPE_I32, f"offset_s{k}", struct.pack("<i", v)) for k, v in offsets.items()
    ]
    entries += extra
    page = bytearray(b"\xff" * nvs.PAGE)
    page[0:4] = struct.pack("<I", 0xFFFFFFFE)  # active
    bitmap = bytearray(b"\xff" * 32)
    for i in range(len(entries)):
        bitmap[i // 4] &= ~(1 << ((i % 4) * 2)) & 0xFF  # 11 → 10 (written)
        page[64 + i * 32 : 64 + (i + 1) * 32] = entries[i]
    page[32:64] = bitmap
    return bytes(page) + b"\xff" * nvs.PAGE


SECRET = _entry(4, 0x21, "sta.pswd", b"\x01\x02\x03\x04\x05\x06\x07\x08")


def test_reads_and_patches_only_offsets():
    image = _image({1: 10, 4: 67, 5: 75}, [SECRET])
    assert nvs.read_offsets(image) == {1: 10, 4: 67, 5: 75}
    assert nvs.verify(image) == 0
    new = nvs.patch_offsets(image, {4: 60})
    assert nvs.read_offsets(new) == {1: 10, 4: 60, 5: 75}
    assert nvs.verify(new) == 0, "고친 항목의 CRC 도 다시 계산"
    assert SECRET in new, "다른 이름공간(Wi-Fi) 항목은 바이트 그대로"
    changed = [i for i, (a, b) in enumerate(zip(image, new, strict=True)) if a != b]
    assert len(changed) <= 8 and all(64 <= i < 64 + 32 * 10 for i in changed)


def test_rejects_out_of_range_and_missing_keys():
    image = _image({1: 10}, [])
    with pytest.raises(ValueError):
        nvs.patch_offsets(image, {1: 200})
    with pytest.raises(ValueError):
        nvs.patch_offsets(image, {7: 5})
    with pytest.raises(ValueError):
        nvs.patch_offsets(image, {11: 0})


def test_vendor_i8_offsets_are_read_and_rewritten_as_i32():
    """순정 펌웨어가 남긴 i8 항목 — 우리 펌웨어(getInt=i32)는 못 읽는다. 같은 값으로 형식만 바꾼다."""
    i8 = [
        _entry(3, nvs.TYPE_I8, f"offset_s{k}", struct.pack("<b", v))
        for k, v in ((4, 67), (5, 75), (2, -8))
    ]
    image = _image({}, [*i8, SECRET])
    assert nvs.read_offsets(image, with_type=True) == {4: (67, "i8"), 5: (75, "i8"), 2: (-8, "i8")}
    fixed = nvs.patch_offsets(image, nvs.read_offsets(image))
    assert nvs.read_offsets(fixed, with_type=True) == {
        4: (67, "i32"),
        5: (75, "i32"),
        2: (-8, "i32"),
    }
    assert nvs.verify(fixed) == 0
    assert SECRET in fixed
