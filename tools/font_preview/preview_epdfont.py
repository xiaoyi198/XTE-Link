#!/usr/bin/env python3
"""Validate supplied Zheng EPDF fonts and build a self-contained offline HTML preview.
This is an inspection tool for these files, not a general EPDF loader or firmware.
Run: .venv/Scripts/python.exe tools/font_preview/preview_epdfont.py --font-dir PATH
"""
from __future__ import annotations
import argparse
import base64
import bisect
import hashlib
import json
import struct
from pathlib import Path

TOOL = Path(__file__).resolve().parent
ROOT = TOOL.parents[1]
HEADER = struct.Struct('<12i')
INTERVAL = struct.Struct('<III')
GLYPH = struct.Struct('<BBBhhHI')

class EpdFont:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        if len(self.data) < HEADER.size:
            raise ValueError(f'{path.name}: truncated header')
        (magic, self.interval_count, self.glyph_count, self.advance_y,
         self.reserved_16, self.ascender, self.reserved_24, self.descender,
         self.is_2bit, self.intervals_offset, self.glyphs_offset,
         self.bitmap_offset) = HEADER.unpack_from(self.data)
        if magic != 0x46445045 or self.is_2bit not in (0, 1):
            raise ValueError(f'{path.name}: unsupported header')
        if not (0 < self.interval_count < 10000 and 0 < self.glyph_count <= 65536):
            raise ValueError(f'{path.name}: unreasonable counts')
        if (self.intervals_offset != HEADER.size or
            self.glyphs_offset != self.intervals_offset + self.interval_count * INTERVAL.size or
            self.bitmap_offset != self.glyphs_offset + self.glyph_count * GLYPH.size or
            self.bitmap_offset > len(self.data)):
            raise ValueError(f'{path.name}: inconsistent section offsets')
        self.intervals = [INTERVAL.unpack_from(self.data, self.intervals_offset + i * INTERVAL.size)
                          for i in range(self.interval_count)]
        expected_index, previous = 0, -1
        for first, last, index in self.intervals:
            if first <= previous or first > last or index != expected_index:
                raise ValueError(f'{path.name}: invalid interval table')
            expected_index += last - first + 1
            previous = last
        if expected_index != self.glyph_count:
            raise ValueError(f'{path.name}: interval/glyph count mismatch')
        self.starts = [r[0] for r in self.intervals]
        end = 0
        for index in range(self.glyph_count):
            width, height, _, _, _, length, offset = self.record(index)
            if length != (width * height * self.bpp + 7) // 8 or offset != end:
                raise ValueError(f'{path.name}: invalid glyph {index}')
            end = offset + length
        if self.bitmap_offset + end != len(self.data):
            raise ValueError(f'{path.name}: bitmap boundary mismatch')

    @property
    def bpp(self):
        return 2 if self.is_2bit else 1

    def record(self, index):
        w, h, adv, left, top, length, offset = GLYPH.unpack_from(
            self.data, self.glyphs_offset + index * GLYPH.size)
        # Supplied legacy files zero-extend signed 8-bit bearings into two
        # bytes: e.g. the left bearing of '秒' is FF 00 (-1), not +255 px.
        # Scope this normalization to the inspected Zheng format only.
        left = left - 256 if 128 <= left <= 255 else left
        top = top - 256 if 128 <= top <= 255 else top
        return w, h, adv, left, top, length, offset

    def glyph(self, cp):
        i = bisect.bisect_right(self.starts, cp) - 1
        if i < 0 or cp > self.intervals[i][1]:
            return None
        first, _, offset = self.intervals[i]
        return self.record(offset + cp - first)

    def export(self, codepoints):
        bitmap, records, missing = bytearray(), [], []
        for cp in sorted(codepoints):
            record = self.glyph(cp)
            if record is None:
                missing.append(cp)
                continue
            w, h, adv, left, top, length, pos = record
            records.append([cp, w, h, adv, left, top, length, len(bitmap)])
            bitmap.extend(self.data[self.bitmap_offset + pos:self.bitmap_offset + pos + length])
        return {'name': self.path.stem, 'lineHeight': self.advance_y,
                'ascender': self.ascender, 'descender': self.descender, 'bpp': self.bpp,
                'glyphs': records, 'bitmap': base64.b64encode(bitmap).decode('ascii'),
                'gbMissing': [f'U+{cp:04X}' for cp in missing if cp > 127]}

def gb2312_codepoints():
    result = set()
    for first in range(0xA1, 0xF8):
        for second in range(0xA1, 0xFF):
            try:
                result.add(ord(bytes((first, second)).decode('gb2312')))
            except UnicodeDecodeError:
                pass
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font-dir', type=Path, default=ROOT / 'backups/fonts')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/font-preview/preview_epdfont.html')
    args = parser.parse_args()
    gb = gb2312_codepoints()
    fonts, report = [], []
    for size in (17, 20, 25):
        font = EpdFont(args.font_dir / f'Zheng{size}号.epdfont')
        fonts.append(font.export(gb | set(range(32, 127))))
        missing = [cp for cp in gb if font.glyph(cp) is None]
        chinese = {cp for cp in gb if 0x4E00 <= cp <= 0x9FFF}
        cjk = [font.glyph(cp) for cp in gb if 0x4E00 <= cp <= 0x9FFF and font.glyph(cp)]
        report.append({'file': font.path.name, 'bytes': len(font.data),
                       'sha256': hashlib.sha256(font.data).hexdigest(),
                       'headerBytes': HEADER.size, 'glyphRecordBytes': GLYPH.size,
                       'sourceGlyphs': font.glyph_count, 'intervals': font.interval_count,
                       'bpp': font.bpp, 'lineHeight': font.advance_y,
                       'ascender': font.ascender, 'descender': font.descender,
                       'chineseGlyphHeightRange': [min(g[1] for g in cjk), max(g[1] for g in cjk)],
                       'chineseAdvanceRange': [min(g[2] for g in cjk), max(g[2] for g in cjk)],
                       'gb2312Covered': len(gb) - len(missing), 'gb2312Total': len(gb),
                       'gb2312ChineseCovered': sum(font.glyph(cp) is not None for cp in chinese),
                       'gb2312ChineseTotal': len(chinese),
                       'gb2312Missing': [f'U+{cp:04X}' for cp in sorted(missing)],
                       'sampleChineseGlyph': list(font.glyph(ord('中')))})
    template = (TOOL / 'preview_epdfont.template.html').read_text(encoding='utf-8')
    if template.count('/* FONT_DATA */') != 1:
        raise ValueError('invalid template placeholder')
    html = template.replace('/* FONT_DATA */', json.dumps(fonts, ensure_ascii=True, separators=(',', ':')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding='utf-8')
    (args.output.parent / 'preview_epdfont_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for item in report:
        print(json.dumps({k: v for k, v in item.items() if k != 'gb2312Missing'}, ensure_ascii=True))
    print(f'Preview generated: {args.output} ({len(html):,} characters)')

if __name__ == '__main__':
    main()
