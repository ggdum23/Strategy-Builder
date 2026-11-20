from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
_RADIUS = {16: 0.85, 20: 1.7, 24: 1.7, 32: 1.3, 40: 1.8, 48: 3.0, 64: 3.0, 128: 3.0, 256: 3.0}
_CRISP_AT = frozenset({16, 20, 24, 32})
_INK = ((15, 45, 98), (255, 107, 16), (0, 178, 160))
_PI = 3.141592653589793238462643383279502884


def _source_path() -> Path:
    return Path(__file__).resolve().parent / "assets" / "logo_source.png"


def _floor(value: float) -> int:
    whole = int(value)
    if whole > value:
        return whole - 1
    return whole


def _sin(value: float) -> float:
    two_pi = _PI * 2.0
    if value >= 0.0:
        cycles = int(value / two_pi + 0.5)
    else:
        cycles = int(value / two_pi - 0.5)
    reduced = value - cycles * two_pi
    sign = 1.0
    if reduced < 0.0:
        reduced = -reduced
        sign = -1.0
    if reduced > _PI * 0.5:
        reduced = _PI - reduced
    square = reduced * reduced
    term = reduced
    total = reduced
    factor = 1
    while factor <= 12:
        term = -term * square / ((2 * factor) * (2 * factor + 1))
        total += term
        factor += 1
    return sign * total


def _lanczos(value: float) -> float:
    distance = value if value >= 0.0 else -value
    if distance >= 3.0:
        return 0.0
    if distance < 1e-12:
        return 1.0
    scaled = _PI * value
    return (_sin(scaled) / scaled) * (_sin(scaled / 3.0) / (scaled / 3.0))


def _png(path: Path) -> tuple[int, int, list[bytes]]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a png: {path}")
    offset = 8
    width = 0
    height = 0
    compressed = bytearray()
    saw_header = False
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        start = offset + 8
        chunk = data[start : start + length]
        crc = struct.unpack(">I", data[start + length : start + length + 4])[0]
        if crc != (zlib.crc32(kind + chunk) & 0xFFFFFFFF):
            raise ValueError(f"png crc mismatch: {path}")
        if kind == b"IHDR":
            width, height, depth, color, compression, filtered, interlace = struct.unpack(">IIBBBBB", chunk)
            if (depth, color, compression, filtered, interlace) != (8, 6, 0, 0, 0):
                raise ValueError(f"png must be 8-bit rgba: {path}")
            saw_header = True
        elif kind == b"IDAT":
            compressed.extend(chunk)
        elif kind == b"IEND":
            break
        offset = start + length + 4
    if not saw_header or width < 1 or height < 1:
        raise ValueError(f"png header missing: {path}")
    raw = zlib.decompress(bytes(compressed))
    stride = width * 4
    if len(raw) != height * (stride + 1):
        raise ValueError(f"png size mismatch: {path}")
    rows: list[bytes] = []
    cursor = 0
    previous = bytearray(stride)
    for _line in range(height):
        kind = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        if kind == 1:
            for index in range(stride):
                left = row[index - 4] if index >= 4 else 0
                row[index] = (row[index] + left) & 255
        elif kind == 2:
            for index in range(stride):
                row[index] = (row[index] + previous[index]) & 255
        elif kind == 3:
            for index in range(stride):
                left = row[index - 4] if index >= 4 else 0
                row[index] = (row[index] + ((left + previous[index]) // 2)) & 255
        elif kind == 4:
            for index in range(stride):
                left = row[index - 4] if index >= 4 else 0
                up = previous[index]
                up_left = previous[index - 4] if index >= 4 else 0
                estimate = left + up - up_left
                left_distance = estimate - left
                if left_distance < 0:
                    left_distance = -left_distance
                up_distance = estimate - up
                if up_distance < 0:
                    up_distance = -up_distance
                diagonal = estimate - up_left
                if diagonal < 0:
                    diagonal = -diagonal
                if left_distance <= up_distance and left_distance <= diagonal:
                    predictor = left
                elif up_distance <= diagonal:
                    predictor = up
                else:
                    predictor = up_left
                row[index] = (row[index] + predictor) & 255
        elif kind != 0:
            raise ValueError(f"png filter {kind} is unsupported")
        previous = row
        rows.append(bytes(row))
    return width, height, rows


def _square(width: int, height: int, rows: list[bytes]) -> list[bytes]:
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    for y, row in enumerate(rows):
        for x in range(width):
            if row[x * 4 + 3] == 0:
                continue
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
    if max_x < min_x or max_y < min_y:
        raise ValueError("logo has no visible pixels")
    cropped_width = max_x - min_x + 1
    cropped_height = max_y - min_y + 1
    side = cropped_width if cropped_width > cropped_height else cropped_height
    origin_x = (side - cropped_width) // 2
    origin_y = (side - cropped_height) // 2
    canvas = [bytearray(side * 4) for _index in range(side)]
    for y in range(cropped_height):
        source = rows[min_y + y]
        canvas[origin_y + y][origin_x * 4 : (origin_x + cropped_width) * 4] = source[min_x * 4 : (max_x + 1) * 4]
    return [bytes(row) for row in canvas]


def _weights(src_size: int, dst_size: int, radius: float) -> list[tuple[int, list[float]]]:
    scale = dst_size / src_size
    support = radius / scale
    tables: list[tuple[int, list[float]]] = []
    for index in range(dst_size):
        center = (index + 0.5) / scale - 0.5
        left = _floor(center - support)
        right = _floor(center + support)
        if left < 0:
            left = 0
        if right >= src_size:
            right = src_size - 1
        weights: list[float] = []
        total = 0.0
        for src in range(left, right + 1):
            weight = _lanczos((src - center) / support * 3.0)
            weights.append(weight)
            total += weight
        if total == 0.0:
            tables.append((min(max(int(center), 0), src_size - 1), [1.0]))
            continue
        inverse = 1.0 / total
        tables.append((left, [weight * inverse for weight in weights]))
    return tables


def _channel(value: float, alpha: float) -> int:
    if alpha <= 0.0:
        return 0
    color = value / alpha
    if color <= 0.0:
        return 0
    if color >= 255.0:
        return 255
    return int(color + 0.5)


def _resize(rows: list[bytes], size: int, radius: float) -> list[bytes]:
    src_size = len(rows)
    horizontal = _weights(src_size, size, radius)
    vertical = _weights(src_size, size, radius)
    mid: list[list[float]] = []
    span = size * 4
    for row in rows:
        acc = [0.0] * span
        for out_x, (left, weights) in enumerate(horizontal):
            red = 0.0
            green = 0.0
            blue = 0.0
            alpha = 0.0
            base = left * 4
            for weight in weights:
                sample_red = row[base]
                sample_green = row[base + 1]
                sample_blue = row[base + 2]
                sample_alpha = row[base + 3]
                red += sample_red * sample_alpha * weight
                green += sample_green * sample_alpha * weight
                blue += sample_blue * sample_alpha * weight
                alpha += sample_alpha * weight
                base += 4
            slot = out_x * 4
            acc[slot] = red
            acc[slot + 1] = green
            acc[slot + 2] = blue
            acc[slot + 3] = alpha
        mid.append(acc)
    output: list[bytes] = []
    for top, weights in vertical:
        acc = [0.0] * span
        for offset, weight in enumerate(weights):
            source = mid[top + offset]
            for index in range(span):
                acc[index] += source[index] * weight
        row = bytearray(span)
        for x in range(size):
            slot = x * 4
            alpha = acc[slot + 3]
            if alpha <= 0.5:
                continue
            stored = int(alpha + 0.5)
            if stored > 255:
                stored = 255
            if stored < 0:
                stored = 0
            row[slot] = _channel(acc[slot], alpha)
            row[slot + 1] = _channel(acc[slot + 1], alpha)
            row[slot + 2] = _channel(acc[slot + 2], alpha)
            row[slot + 3] = stored
        output.append(bytes(row))
    return output


def _crisp(rows: list[bytes]) -> list[bytes]:
    output: list[bytes] = []
    width = len(rows[0]) // 4
    for row in rows:
        dest = bytearray(len(row))
        for x in range(width):
            slot = x * 4
            red = row[slot]
            green = row[slot + 1]
            blue = row[slot + 2]
            alpha = row[slot + 3]
            if alpha < 28:
                continue
            best = 0
            score = 1 << 30
            for index, ink in enumerate(_INK):
                distance = (red - ink[0]) * (red - ink[0])
                distance += (green - ink[1]) * (green - ink[1])
                distance += (blue - ink[2]) * (blue - ink[2])
                if distance < score:
                    score = distance
                    best = index
            lifted = int((alpha / 255.0) ** 0.55 * 255.0 + 0.5)
            if lifted < alpha:
                lifted = alpha
            if lifted < 200:
                bumped = 140 + alpha
                if bumped > 255:
                    bumped = 255
                if bumped > lifted:
                    lifted = bumped
            if lifted > 255:
                lifted = 255
            ink = _INK[best]
            dest[slot] = ink[0]
            dest[slot + 1] = ink[1]
            dest[slot + 2] = ink[2]
            dest[slot + 3] = lifted
        output.append(bytes(dest))
    return output


def _mask(rows: list[bytes]) -> bytes:
    size = len(rows)
    stride = ((size + 31) // 32) * 4
    mask = bytearray(stride * size)
    for y in range(size):
        row = rows[size - 1 - y]
        base = y * stride
        for x in range(size):
            if row[x * 4 + 3] == 0:
                mask[base + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(mask)


def _dib(rows: list[bytes]) -> bytes:
    size = len(rows)
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
    pixels = bytearray()
    for row in reversed(rows):
        for x in range(size):
            slot = x * 4
            pixels.extend((row[slot + 2], row[slot + 1], row[slot], row[slot + 3]))
    return header + bytes(pixels) + _mask(rows)


def _pack(frames: list[tuple[int, bytes]]) -> bytes:
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    blob = bytearray()
    offset = 6 + 16 * count
    for size, image in frames:
        stored = 0 if size == 256 else size
        entries += struct.pack("<BBBBHHII", stored, stored, 0, 0, 1, 32, len(image), offset)
        blob += image
        offset += len(image)
    return bytes(header + entries + blob)


def build_ico(sizes: tuple[int, ...] = SIZES) -> bytes:
    width, height, rows = _png(_source_path())
    square = _square(width, height, rows)
    master = _resize(square, 256, _RADIUS[256])
    frames: list[tuple[int, bytes]] = []
    for size in sizes:
        if size == 256:
            frame = master
        else:
            frame = _resize(master, size, _RADIUS.get(size, 3.0))
            if size in _CRISP_AT:
                frame = _crisp(frame)
        if len(frame) != size or len(frame[0]) != size * 4:
            raise ValueError(f"resized frame {size} has the wrong shape")
        frames.append((size, _dib(frame)))
    return _pack(frames)


def write_icon(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_ico())


def main() -> None:
    destination = Path(__file__).resolve().parent / "assets" / "strategy_builder.ico"
    write_icon(destination)


if __name__ == "__main__":
    main()
