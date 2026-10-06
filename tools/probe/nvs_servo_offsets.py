"""MechDog 서보 편차를 NVS 이미지에서 읽고, **편차 항목만** 바꾼 이미지를 만든다 (기기 접속 없음).

제조사 PC 프로그램은 순정 펌웨어와만 통신해서 우리 펌웨어에서는 편차를 못 읽고 못 쓴다. 벤더 라이브러리는
편차를 NVS 이름공간 `mechdog` 의 `offset_s1..10`(int32)에 두고 부팅 때 `pwm_servo_init` 이 읽어 적용한다.
그래서 부트 모드에서 NVS 영역을 읽어(esptool read_flash) 이 도구로 해당 항목의 값과 항목 CRC 만 고친 뒤
같은 영역에 다시 쓰면 된다. Wi-Fi 등 다른 항목은 바이트 그대로다.

    python tools/probe/nvs_servo_offsets.py show nvs.bin
    python tools/probe/nvs_servo_offsets.py set nvs.bin out.bin 4=60 5=70

항목 CRC 는 `crc32(항목[0:4] + 항목[8:32], 초기값 0xFFFFFFFF)` — 실제 기기 이미지의 51개 항목으로 확인.
편차 범위는 벤더 검사와 같은 −125..125. 새 키를 만들지는 않는다(이미 있는 키만 고친다).
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

PAGE = 4096
ENTRIES = 126
NAMESPACE = "mechdog"
TYPE_I32 = 0x14
#: 순정 펌웨어(제조사)가 남긴 형식. 우리 펌웨어의 `Preferences.getInt` 는 `nvs_get_i32` 라 이 형식을
#: 형식 불일치로 못 읽고 기본값 0 을 쓴다 — 편차가 통째로 빠진다 (2026-10-04 mechdog-02 실측).
TYPE_I8 = 0x11
OFFSET_TYPES = (TYPE_I8, TYPE_I32)
WRITTEN = 2


def _entries(image: bytes):
    """(오프셋, 항목 바이트) — 쓰인(written) 항목만, 여러 칸짜리는 첫 칸만."""
    for page_at in range(0, len(image), PAGE):
        page = image[page_at : page_at + PAGE]
        if len(page) < PAGE or struct.unpack("<I", page[:4])[0] == 0xFFFFFFFF:
            continue
        bitmap = page[32:64]
        index = 0
        while index < ENTRIES:
            state = (bitmap[index // 4] >> ((index % 4) * 2)) & 3
            at = page_at + 64 + index * 32
            entry = image[at : at + 32]
            if state != WRITTEN:
                index += 1
                continue
            yield at, entry
            index += max(1, entry[2])


def entry_crc(entry: bytes) -> int:
    return zlib.crc32(entry[0:4] + entry[8:32], 0xFFFFFFFF) & 0xFFFFFFFF


def _key(entry: bytes) -> str:
    return entry[8:24].split(b"\0")[0].decode("latin1")


def _namespace_index(image: bytes, name: str) -> int | None:
    for _, entry in _entries(image):
        if entry[0] == 0 and _key(entry) == name:
            return entry[24]
    return None


def _value(entry: bytes) -> int:
    if entry[1] == TYPE_I8:
        return struct.unpack("<b", entry[24:25])[0]
    return struct.unpack("<i", entry[24:28])[0]


def read_offsets(image: bytes, *, with_type: bool = False) -> dict[int, Any]:
    """서보 번호(1..10) → 편차 (`with_type` 이면 (편차, «i8»|«i32»)). 이름공간이 없으면 빈 사전."""
    ns = _namespace_index(image, NAMESPACE)
    out: dict[int, Any] = {}
    if ns is None:
        return out
    for _, entry in _entries(image):
        key = _key(entry)
        if entry[0] == ns and entry[1] in OFFSET_TYPES and key.startswith("offset_s"):
            servo = int(key[len("offset_s") :])
            value = _value(entry)
            out[servo] = (value, "i8" if entry[1] == TYPE_I8 else "i32") if with_type else value
    return out


def verify(image: bytes) -> int:
    """CRC 가 맞지 않는 쓰인 항목 수 (0 이어야 한다)."""
    return sum(1 for _, e in _entries(image) if struct.unpack("<I", e[4:8])[0] != entry_crc(e))


def patch_offsets(image: bytes, changes: dict[int, int]) -> bytes:
    """`changes` 의 서보 편차만 바꾼 새 이미지. 없는 키·범위 밖이면 ValueError.

    고친 항목은 **i32 형식으로 쓴다** — 우리 펌웨어가 읽는 형식이다(i8 로 남아 있으면 0 이 적용된다).
    """
    for servo, value in changes.items():
        if not 1 <= servo <= 10:
            raise ValueError(f"서보 번호는 1..10: {servo}")
        if not -125 <= value <= 125:
            raise ValueError(f"편차는 −125..125: 서보 {servo} = {value}")
    ns = _namespace_index(image, NAMESPACE)
    if ns is None:
        raise ValueError("NVS 에 mechdog 이름공간이 없다")
    out = bytearray(image)
    done = set()
    for at, entry in _entries(image):
        key = _key(entry)
        if entry[0] != ns or entry[1] not in OFFSET_TYPES or not key.startswith("offset_s"):
            continue
        servo = int(key[len("offset_s") :])
        if servo not in changes:
            continue
        new = bytearray(entry)
        new[1] = TYPE_I32
        new[24:32] = struct.pack("<i", changes[servo]) + bytes([0xFF] * 4)
        new[4:8] = struct.pack("<I", entry_crc(bytes(new)))
        out[at : at + 32] = new
        done.add(servo)
    missing = set(changes) - done
    if missing:
        raise ValueError(f"이미지에 없는 편차 키: {sorted(missing)} — 새 키는 만들지 않는다")
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show")
    show.add_argument("nvs", type=Path)
    setp = sub.add_parser("set")
    setp.add_argument("nvs", type=Path)
    setp.add_argument("out", type=Path)
    setp.add_argument("changes", nargs="+", help="서보=값, 예 4=60")
    fix = sub.add_parser("fix-type", help="값은 그대로, 편차 항목을 펌웨어가 읽는 i32 형식으로")
    fix.add_argument("nvs", type=Path)
    fix.add_argument("out", type=Path)
    a = ap.parse_args()
    image = a.nvs.read_bytes()
    if a.cmd == "show":
        print(read_offsets(image, with_type=True), "crc_bad", verify(image))
        return 0
    if a.cmd == "fix-type":
        changes = read_offsets(image)
    else:
        changes = {int(k): int(v) for k, v in (c.split("=", 1) for c in a.changes)}
    new = patch_offsets(image, changes)
    if verify(new) != 0:
        print("CRC 검증 실패 — 쓰지 않는다", file=sys.stderr)
        return 2
    changed = sum(1 for x, y in zip(image, new, strict=True) if x != y)
    a.out.write_bytes(new)
    print("전", read_offsets(image, with_type=True))
    print("후", read_offsets(new, with_type=True), f"바뀐 바이트 {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
