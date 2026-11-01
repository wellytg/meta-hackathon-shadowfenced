"""Generate Shadowfenced extension icons (stdlib only, no external tools).

Reads nothing; draws the same square pixel-style artwork as
``src/icons/icon.svg`` (paper field, ink 2px-style outline, steel-blue
title bar, safety-orange fence emblem) at 16/32/48/128 px and writes
plain RGBA PNGs via ``zlib`` + ``struct``.

Runnable as:  python extension/make_icons.py   (from the repo root)
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent  # extension/
OUT = HERE / "src" / "icons"

INK = (26, 26, 26, 255)
PAPER = (244, 241, 232, 255)
BLUE = (47, 111, 159, 255)      # steel blue: title bar / allowed
ORANGE = (232, 93, 31, 255)     # safety orange: emblem / blocked
WHITE = (255, 255, 255, 255)

SIZES = (16, 32, 48, 128)


def _png_rgba(size: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        out += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return out

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        row = y * size
        for x in range(size):
            raw.extend(pixels[row + x])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def draw(size: int) -> list[tuple[int, int, int, int]]:
    """Pixel-art icon: ink border, blue title bar, orange fence emblem."""
    px: list[tuple[int, int, int, int]] = [PAPER] * (size * size)

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int, int]) -> None:
        for y in range(max(0, y0), min(size, y1)):
            for x in range(max(0, x0), min(size, x1)):
                px[y * size + x] = color

    border = max(2, size // 16)
    # Ink frame.
    rect(0, 0, size, border, INK)
    rect(0, size - border, size, size, INK)
    rect(0, 0, border, size, INK)
    rect(size - border, 0, size, size, INK)
    # Steel-blue title bar.
    bar_h = max(3, size // 4)
    rect(border, border, size - border, border + bar_h, BLUE)
    # Window button: one white square on the right of the bar.
    btn = max(1, bar_h // 3)
    rect(size - border - btn - max(1, border // 2), border + (bar_h - btn) // 2,
         size - border - max(1, border // 2), border + (bar_h + btn) // 2 + (btn % 2 == 0 and 0 or 1), WHITE)
    # Orange fence emblem: ink outline, orange field, paper pickets.
    top = border + bar_h
    avail = size - top - border
    side = max(4, (avail * 3) // 4)
    ex0 = (size - side) // 2
    ey0 = top + (avail - side) // 2
    frame = max(1, size // 32)
    rect(ex0, ey0, ex0 + side, ey0 + side, INK)
    rect(ex0 + frame, ey0 + frame, ex0 + side - frame, ey0 + side - frame, ORANGE)
    inner0x, inner0y = ex0 + frame, ey0 + frame
    inner1x, inner1y = ex0 + side - frame, ey0 + side - frame
    inner_w = inner1x - inner0x
    # Three vertical paper pickets (the "fence").
    picket = max(1, inner_w // 7)
    gap = max(1, (inner_w - 3 * picket) // 4)
    for i in range(3):
        px0 = inner0x + gap + i * (picket + gap)
        rect(px0, inner0y + max(1, frame), px0 + picket, inner1y - max(1, frame), PAPER)
    return px


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT / f"icon{size}.png"
        path.write_bytes(_png_rgba(size, draw(size)))
        print(f"wrote {path} ({size}x{size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
