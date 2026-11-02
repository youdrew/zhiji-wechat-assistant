"""Read PE import names without loading or executing a native dependency."""
from __future__ import annotations

from pathlib import Path
import re
import struct


def imported_dlls(path: Path) -> set[str]:
    data = Path(path).read_bytes()
    def number(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            raise ValueError("truncated PE metadata")
        return struct.unpack_from(fmt, data, offset)[0]
    if data[:2] != b"MZ":
        raise ValueError("native dependency is not a PE image")
    pe = number("<I", 0x3c)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("invalid PE signature")
    sections = number("<H", pe + 6)
    optional_size = number("<H", pe + 20)
    optional = pe + 24
    magic = number("<H", optional)
    if magic not in (0x10b, 0x20b) or not 1 <= sections <= 96:
        raise ValueError("unsupported PE layout")
    directory = optional + (112 if magic == 0x20b else 96)
    image_base = number("<Q" if magic == 0x20b else "<I", optional + (24 if magic == 0x20b else 28))
    header_size = number("<I", optional + 60)
    table = optional + optional_size
    mapping = []
    for index in range(sections):
        row = table + index * 40
        mapping.append((number("<I", row + 12), max(number("<I", row + 8), number("<I", row + 16)),
                        number("<I", row + 20)))
    def offset(rva):
        if 0 <= rva < header_size and rva < len(data):
            return rva
        for address, size, raw in mapping:
            if address <= rva < address + size and raw + rva - address < len(data):
                return raw + rva - address
        raise ValueError("PE import RVA is outside the image")
    def name(rva):
        start = offset(rva)
        end = data.find(b"\0", start, min(start + 512, len(data)))
        if end < 0:
            raise ValueError("unterminated PE import name")
        value = data[start:end].decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*\.(?:dll|exe|pyd|drv)", value):
            raise ValueError("unsafe PE import name")
        return value
    result = set()
    directory_count = number("<I", directory - 4)
    for index, stride in ((1, 20), (13, 32)):
        if directory_count <= index:
            continue
        rva, size = struct.unpack_from("<II", data, directory + index * 8)
        if not rva:
            continue
        start = offset(rva)
        for entry in range(min(4096, max(1, size // stride))):
            row = start + entry * stride
            if row + stride > len(data):
                raise ValueError("truncated PE import table")
            if not any(data[row:row + stride]):
                break
            target = number("<I", row + (12 if index == 1 else 4))
            if index == 13 and not number("<I", row) & 1:
                target -= image_base
            result.add(name(target))
        else:
            raise ValueError("unterminated PE import table")
    return result
