"""Read-only PE inspection of the vendor CLI; does not execute it or open ports."""

import argparse
from pathlib import Path

import capstone
import pefile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("exe", type=Path)
args = parser.parse_args()
pe = pefile.PE(str(args.exe))
engine = capstone.Cs(
    capstone.CS_ARCH_X86,
    capstone.CS_MODE_32 if pe.FILE_HEADER.Machine == 0x14C else capstone.CS_MODE_64,
)
engine.detail = True
raw = args.exe.read_bytes()
labels = {}
for needle in (
    b"Support Device",
    b"Use config serial port",
    b"Use default serial port",
    b"Wrong file path",
    b"File path does not exist",
):
    pos = raw.find(needle)
    if pos < 0:
        continue
    start = raw.rfind(b"\0", 0, pos) + 1
    address = pe.OPTIONAL_HEADER.ImageBase + pe.get_rva_from_offset(start)
    labels[address] = raw[start : raw.index(b"\0", pos)].decode("ascii")
print(labels)
for section in pe.sections:
    if not section.Characteristics & 0x20000000:
        continue
    instructions = list(
        engine.disasm(section.get_data(), pe.OPTIONAL_HEADER.ImageBase + section.VirtualAddress)
    )
    shown = set()
    for n, inst in enumerate(instructions):
        if not any(op.type == capstone.x86.X86_OP_IMM and op.imm in labels for op in inst.operands):
            continue
        for j in range(max(0, n - 30), min(len(instructions), n + 35)):
            if j not in shown:
                item = instructions[j]
                print(f"{item.address:08x} {item.mnemonic:8s} {item.op_str}")
                shown.add(j)
