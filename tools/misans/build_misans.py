#!/usr/bin/env python3
"""Locally rasterize the official MiSans archive for XTE-Link (no font installation).

Uses Pillow/FreeType for rasterization, fontTools only for reading cmap/metadata,
and the project's already inspected 48/12/13-byte EPDF layout. No outline edits.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path
import platform
import struct
import sys
import urllib.request
import zipfile

import fontTools
from fontTools.ttLib import TTFont
import PIL
from PIL import Image, ImageDraw, ImageFont, features

ROOT = Path(__file__).resolve().parents[2]
URL = 'https://hyperos.mi.com/font-download/MiSans.zip'
ZIP_SHA = 'b6aa1fc827035922612df8edf36e5609bca1c5441e25cd57572204569b7b81d9'
SOURCE_SHA = {
    'Medium': 'b03e98374e971594b0b7a9706d0704241f76e1b88556cdda79c5039ef8a638d1',
    'Regular': '9c120f0a849bc0aa5048daae2a3c0f6eecd828b5b33fce682a9622833f5feea6',
}
DIGITS = '0123456789 .%+-:/'
HEADER = struct.Struct('<12i')
INTERVAL = struct.Struct('<III')
GLYPH = struct.Struct('<BBBhhHI')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def intervals(cps):
    rows = []
    for index, cp in enumerate(cps):
        if rows and rows[-1][1] + 1 == cp:
            rows[-1][1] = cp
        else:
            rows.append([cp, cp, index])
    return rows


def raster(font, cp):
    mask, (left, y) = font.getmask2(chr(cp), mode='L', anchor='ls')
    width, height = mask.size
    advance = int(font.getlength(chr(cp)) + 0.5)
    coverage = bytes(mask)
    bitmap = bytearray((width * height + 7) // 8)
    for i, level in enumerate(coverage):
        if level >= 128:
            bitmap[i // 8] |= 0x80 >> (i % 8)
    return (width, height, advance, left, -y, len(bitmap)), bytes(bitmap)


def build(ttf, cps, size, output, progress=None):
    font = ImageFont.truetype(BytesIO(ttf), size, layout_engine=ImageFont.Layout.BASIC)
    ranges = intervals(cps)
    if len(ranges) > 256:
        raise ValueError('interval capacity exceeded')
    records, bitmap, metrics = bytearray(), bytearray(), []
    for index, cp in enumerate(cps):
        if progress and index % 256 == 0:
            progress(index, len(cps))
        info, bits = raster(font, cp)
        w, h, advance, left, top, length = info
        if w > 64 or h > 64 or advance > 64 or abs(left) > 64 or abs(top) > 64 or length > 512:
            raise ValueError(f'{size}px U+{cp:04X}: exceeds bounded firmware cache')
        records.extend(GLYPH.pack(*info, len(bitmap)))
        bitmap.extend(bits)
        metrics.append(info)
    ascender = max(g[4] for g in metrics)
    descent = max(g[1] - g[4] for g in metrics)
    line = ascender + descent
    glyph_offset = 48 + 12 * len(ranges)
    bitmap_offset = glyph_offset + 13 * len(cps)
    header = HEADER.pack(0x46445045, len(ranges), len(cps), line, 0, ascender, 0,
                         -descent, 0, 48, glyph_offset, bitmap_offset)
    data = header + b''.join(INTERVAL.pack(*r) for r in ranges) + records + bitmap
    name = f'MiSans-Medium-{size}.epdfont'
    (output / 'fonts' / name).write_bytes(data)
    return {'file': name, 'tf_path': '/fonts/' + name, 'pixel_size': size,
            'bytes': len(data), 'sha256': sha(data), 'glyphs': len(cps), 'intervals': len(ranges),
            'line_height': line, 'ascender': ascender, 'descender': -descent, 'bpp': 1,
            'max_bitmap_bytes': max(g[5] for g in metrics),
            'max_width': max(g[0] for g in metrics), 'max_height': max(g[1] for g in metrics),
            'max_advance': max(g[2] for g in metrics)}


def preview(ttfs, output):
    # Actual rasterized source, same text/size/threshold; no system font substitution.
    image = Image.new('L', (1200, 560), 255)
    draw = ImageDraw.Draw(image)
    draw.text((24, 16), 'MiSans 4.009 | pixels at 1:1 | threshold 128 | white background', fill=0)
    for col, weight in enumerate(('Regular', 'Medium')):
        x = 24 + col * 600
        draw.text((x, 52), weight, fill=0)
        y = 90
        for size in (24, 32, 40):
            f = ImageFont.truetype(BytesIO(ttfs[weight]), size, layout_engine=ImageFont.Layout.BASIC)
            draw.text((x, y), f'{size}px', fill=0)
            for text in ('电脑状态  系统音量  助手控制', '醒目清晰，等待确认。01234'):
                baseline = y + 52
                for ch in text:
                    info, bits = raster(f, ord(ch))
                    w, h, adv, left, top, _ = info
                    if w and h:
                        pixels = bytes(0 if bits[i // 8] & (0x80 >> (i % 8)) else 255 for i in range(w * h))
                        image.paste(Image.frombytes('L', (w, h), pixels), (x + left, baseline - top))
                    x += adv
                x = 24 + col * 600
                y += size + 8
            y += 34
        f = ImageFont.truetype(BytesIO(ttfs[weight]), 64, layout_engine=ImageFont.Layout.BASIC)
        draw.text((x, y), '64px numbers', fill=0)
        for ch in '65.0 % 12:34':
            info, bits = raster(f, ord(ch))
            w, h, adv, left, top, _ = info
            if w and h:
                pixels = bytes(0 if bits[i // 8] & (0x80 >> (i % 8)) else 255 for i in range(w * h))
                image.paste(Image.frombytes('L', (w, h), pixels), (x + left, y + 80 - top))
            x += adv
    image.save(output / 'MiSans-white-preview.png')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-zip', type=Path, default=ROOT / 'artifacts/misans-local/source/MiSans.zip')
    parser.add_argument('--download', action='store_true', help='fetch pinned official archive when absent (228 MB)')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/misans-local/misans-4.009-medium-v1')
    args = parser.parse_args()
    if not args.source_zip.exists() and args.download:
        args.source_zip.parent.mkdir(parents=True, exist_ok=True)
        part = args.source_zip.with_suffix('.zip.part')
        with urllib.request.urlopen(URL, timeout=60) as response, part.open('wb') as destination:
            while block := response.read(1024 * 1024):
                destination.write(block)
        part.replace(args.source_zip)
    if not args.source_zip.is_file():
        parser.error('provide the official MiSans.zip or use --download after reading the license')
    with args.source_zip.open('rb') as source:
        zip_sha = hashlib.file_digest(source, 'sha256').hexdigest()
    if zip_sha != ZIP_SHA:
        parser.error('official archive differs from the pinned SHA-256; review version/license before updating pins')
    ttfs = {}
    with zipfile.ZipFile(args.source_zip) as archive:
        for weight, expected in SOURCE_SHA.items():
            ttfs[weight] = archive.read(f'MiSans/ttf/MiSans-{weight}.ttf')
            if sha(ttfs[weight]) != expected:
                parser.error(f'{weight}: TTF digest mismatch')
    ttf = TTFont(BytesIO(ttfs['Medium']))
    cmap = ttf.getBestCmap()
    cps = sorted(cp for cp in cmap if 32 <= cp <= 0xffff and not 0xd800 <= cp <= 0xdfff)
    args.output.mkdir(parents=True, exist_ok=False)  # Preserve earlier local builds.
    (args.output / 'fonts').mkdir()
    files = [build(ttfs['Medium'], cps, size, args.output) for size in (24, 32, 40)]
    files.append(build(ttfs['Medium'], sorted(map(ord, DIGITS)), 64, args.output))
    preview(ttfs, args.output)
    manifest = {'schema': 1, 'source_url': URL, 'source_zip_bytes': args.source_zip.stat().st_size,
                'source_zip_sha256': zip_sha, 'source_ttf_sha256': SOURCE_SHA,
                'font_version': ttf['name'].getDebugName(5), 'weight': 'Medium',
                'notice': 'XTE-Link uses MiSans fonts. MiSans is provided by Xiaomi.',
                'license': 'https://hyperos.mi.com/font/zh/download/',
                'distribution': 'Local application assets; do not publish as a standalone font package.',
                'rasterization': 'Pillow FreeType BASIC, integer px/em, L coverage >=128, continuous MSB-first 1-bit',
                'environment': {'python': platform.python_version(), 'Pillow': PIL.__version__,
                                'fonttools': fontTools.__version__, 'FreeType': features.version('freetype2')},
                'tool_sha256': sha(Path(__file__).read_bytes()), 'files': files}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output / 'SHA256SUMS.txt').write_text(''.join(f'{f["sha256"]}  fonts/{f["file"]}\n' for f in files), encoding='ascii')
    for f in files:
        print(json.dumps(f, ensure_ascii=True))
    print('Local preview and TF assets:', args.output)


if __name__ == '__main__':
    main()
