#!/usr/bin/env python3
"""Compare every production C++ glyph pixel with independent Pillow text drawing.
Also checks EPDF structure, pinned digests, source coverage and actual UI functions.
"""
from __future__ import annotations
import argparse
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zipfile

from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'firmware/tests'), str(ROOT / 'tools/font_preview')]
import verify_tf_font as host_test
from preview_epdfont import EpdFont, gb2312_codepoints
from build_misans import DIGITS, ZIP_SHA, SOURCE_SHA, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=ROOT / 'artifacts/misans-local/misans-4.009-medium-v1')
    parser.add_argument('--source-zip', type=Path, default=ROOT / 'artifacts/misans-local/source/MiSans.zip')
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    reference = json.loads((Path(__file__).parent / 'reference-manifest.json').read_text(encoding='utf-8'))
    assert manifest['files'] == reference['files'], 'generated files differ from pinned release'
    with zipfile.ZipFile(args.source_zip) as archive:
        ttf = archive.read('MiSans/ttf/MiSans-Medium.ttf')
    assert sha(ttf) == SOURCE_SHA['Medium']
    cmap = TTFont(BytesIO(ttf)).getBestCmap()
    source_cps = {cp for cp in cmap if 32 <= cp <= 0xffff and not 0xd800 <= cp <= 0xdfff}
    ui = (ROOT / 'firmware/src/main.cpp').read_text(encoding='utf-8')
    ui_cps = {ord(c) for s in re.findall(r'"((?:\\.|[^"\\])*)"', ui) for c in s if ord(c) > 127}
    assert ui_cps <= source_cps, f'UI characters missing: {ui_cps - source_cps}'
    out = ROOT / '.pio/misans-host-test'
    out.mkdir(parents=True, exist_ok=True)
    fonts, cases = {}, {}
    for item in manifest['files']:
        size = item['pixel_size']
        file = bundle / 'fonts' / item['file']
        assert sha(file.read_bytes()) == item['sha256']
        font = EpdFont(file)
        assert (font.advance_y, font.ascender, font.descender, font.bpp) == (
            item['line_height'], item['ascender'], item['descender'], 1)
        cps = sorted(source_cps if size != 64 else set(map(ord, DIGITS)))
        assert font.glyph_count == len(cps) and all(font.glyph(cp) for cp in cps)
        fonts[size] = font
        cases[size] = cps + [0x10ffff] + ([ord('中')] if size == 64 else [])
        shutil.copyfile(file, out / file.name)
        (out / f'cps-{size}.txt').write_text(''.join(f'{cp:x}\n' for cp in cases[size]), encoding='ascii')
    # Reuse the repository's MSVC/g++ harness, use an independent output folder.
    host_test.OUT = out
    exe = host_test.compile_host(ROOT / 'firmware/tests/misans_test.cpp', 'misans-test')
    subprocess.run([str(exe), out.relative_to(ROOT).as_posix()], cwd=ROOT, check=True)
    count = 0
    for item in manifest['files']:
        size = item['pixel_size']
        font = ImageFont.truetype(BytesIO(ttf), size, layout_engine=ImageFont.Layout.BASIC)
        actual = (out / f'pixels-{size}.bin').read_bytes()
        assert len(actual) == 806 * len(cases[size])
        for i, cp in enumerate(cases[size]):
            got_cp, advance = struct.unpack_from('<IH', actual, 806 * i)
            assert got_cp == cp
            image = Image.new('L', (80, 80), 0)
            draw = ImageDraw.Draw(image)
            if fonts[size].glyph(cp):
                # Deliberately use ImageDraw.text, not the converter or EPDF decoder.
                draw.text((8, 8 + item['ascender']), chr(cp), font=font, fill=255, anchor='ls')
                draw.rectangle((0, 0, 7, 79), fill=0)  # Production clips negative bearing at text origin.
                expected_advance = int(font.getlength(chr(cp)) + 0.5)
            else:
                side = size * 2 // 3
                x, y = 8 + (size - side) // 2, 8 + (item['line_height'] - side) // 2
                draw.rectangle((x, y, x + side - 1, y + side - 1), outline=255)
                expected_advance = size
            assert advance == expected_advance, (size, hex(cp), 'advance')
            expected = image.point(lambda v: 255 if v >= 128 else 0, mode='1').tobytes()
            assert actual[806 * i + 6:806 * (i + 1)] == expected, (size, hex(cp), 'pixels')
            count += 1
        print(f'PASS: {size}px all {len(cases[size])} glyph/missing cases match independent Pillow pixels')
    host_test.FONT = out / 'MiSans-Medium-32.epdfont'
    # Render the production UI with the generated MiSans assets.
    host_test.render_pages(out)
    report = {'pixel_cases': count, 'ui_chinese_characters': len(ui_cps),
              'gb2312_chinese_covered': sum(0x4e00 <= cp <= 0x9fff and cp in source_cps for cp in gb2312_codepoints()),
              'gb2312_missing': [f'U+{cp:04X}' for cp in sorted(gb2312_codepoints() - source_cps)],
              'scope': 'Host raster/reader/layout only; TF transfer and hardware not tested.'}
    (out / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
    print('Actual firmware pages:', out)


if __name__ == '__main__':
    main()
