"""Write the Strategy Builder icon.

The bytes are deterministic. The installer build regenerates the file so a
checkout and a CI build use the same image.
"""

from __future__ import annotations

import struct
from pathlib import Path


def _bgra(red: int, green: int, blue: int, alpha: int = 255) -> bytes:
    return bytes((blue, green, red, alpha))


def _pixel(x: int, y: int, size: int) -> bytes:
    """A dark field with three gold bars. Coordinates are top-left."""
    margin = max(1, size // 16)
    if x < margin or y < margin or x >= size - margin or y >= size - margin:
        return _bgra(0x0B, 0x1C, 0x28)
    inner_x = x - margin
    inner_y = y - margin
    inner = size - 2 * margin
    gap = max(1, inner // 12)
    bar_w = (inner - 4 * gap) // 3
    heights = (0.42, 0.68, 0.90)
    baseline = inner - gap
    for index, height_frac in enumerate(heights):
        left = gap + index * (bar_w + gap)
        right = left + bar_w
        top = baseline - int(inner * height_frac)
        if left <= inner_x < right and top <= inner_y < baseline:
            shade = (0xD4, 0xA0, 0x3C) if index < 2 else (0xF0, 0xC4, 0x5C)
            return _bgra(*shade)
    return _bgra(0x14, 0x3A, 0x4E)


def _dib(size: int) -> bytes:
    """BMP-in-ICO image: XOR mask (bottom-up BGRA) plus a zero AND mask."""
    header = struct.pack(
        "<IIIHHIIIIII",
        40,
        size,
        size * 2,
        1,
        32,
        0,
        size * size * 4,
        0,
        0,
        0,
        0,
    )
    rows = []
    for y in range(size - 1, -1, -1):
        rows.append(b"".join(_pixel(x, y, size) for x in range(size)))
    xor_mask = b"".join(rows)
    row_bytes = (size + 31) // 32 * 4
    and_mask = b"\x00" * (row_bytes * size)
    return header + xor_mask + and_mask


def build_ico(sizes: tuple[int, ...] = (16, 32, 48)) -> bytes:
    images = [_dib(size) for size in sizes]
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    offset = 6 + 16 * count
    blob = bytearray()
    for size, image in zip(sizes, images):
        width = 0 if size == 256 else size
        height = 0 if size == 256 else size
        entries += struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(image), offset)
        blob += image
        offset += len(image)
    return bytes(header + entries + blob)


def write_icon(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_ico())


def main() -> None:
    destination = Path(__file__).resolve().parent / "assets" / "strategy_builder.ico"
    write_icon(destination)


if __name__ == "__main__":
    main()
