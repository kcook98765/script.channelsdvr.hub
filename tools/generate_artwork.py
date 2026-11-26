"""Original Hub alpha artwork from solid colours and rectangles; no image inputs.

SPDX-License-Identifier: MIT
Run with --check to compare, or --write to update this checkout's five PNGs.
This tool never reads historical packages or downloads third-party assets.
"""
import argparse
from pathlib import Path
import struct
import zlib


ROOT = Path(__file__).resolve().parents[1] / 'script.channelsdvr.hub'
BACKGROUND = (15, 24, 36)
CELL = (40, 64, 88)
GAP = (29, 40, 52)
SELECTED = (20, 117, 171)


def chunk(kind, data):
    return (struct.pack('>I', len(data)) + kind + data
            + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))


def png(width, height, pixels):
    if len(pixels) != width * height * 3:
        raise ValueError('Expected RGB pixels')
    rows = b''.join(b'\0' + pixels[y * width * 3:(y + 1) * width * 3]
                    for y in range(height))
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def artwork():
    pixels = bytearray(bytes(BACKGROUND) * 512 * 512)

    def rectangle(left, top, right, bottom, colour):
        row = bytes(colour) * (right - left)
        for y in range(top, bottom):
            start = (y * 512 + left) * 3
            pixels[start:start + len(row)] = row

    # Independently specified geometry: a header and nine separate tiles.
    rectangle(80, 88, 432, 104, SELECTED)
    for row, top in enumerate((144, 240, 336)):
        for col, left in enumerate((80, 208, 336)):
            colour = (32, 151, 190) if (row, col) == (0, 0) else SELECTED
            rectangle(left, top, left + 96, top + 64, colour)
    output = {'resources/icon.png': png(512, 512, pixels)}
    for name, colour in [('background', BACKGROUND), ('cell', CELL),
                         ('gap', GAP), ('selected', SELECTED)]:
        output[f'resources/skins/Default/media/{name}.png'] = png(
            8, 8, bytes(colour) * 64)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--write', action='store_true')
    args = parser.parse_args()
    for name, data in artwork().items():
        path = ROOT / name
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Linked artwork path forbidden: ' + name)
        if args.write:
            path.write_bytes(data)
        elif path.read_bytes() != data:
            raise ValueError('Generated artwork differs: ' + name)
    print('Five original generated artwork files ' + ('written' if args.write else 'verified'))


if __name__ == '__main__':
    main()
