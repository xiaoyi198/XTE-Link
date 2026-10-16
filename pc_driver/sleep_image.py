"""Convert pictures to the device sleep format: 800x480, 4-gray, 4-bit BMP."""

import hashlib
from pathlib import Path
import re
import struct

from PIL import Image, ImageOps

WIDTH, HEIGHT = 800, 480
FOLDER = "/.sleep"
MAX_ZOOM = 5.0
# Palette index = firmware level: black, dark gray, light gray, white.
GRAYS = (0, 85, 170, 255)


def load(path):
    with Image.open(path) as source:
        return ImageOps.exif_transpose(source).convert("RGB")


def crop_box(size, zoom=1.0, center=(0.5, 0.5)):
    """Largest 5:3 source window, divided by zoom and clamped around center."""
    width, height = size
    box_width = min(width, height * WIDTH / HEIGHT) / min(max(zoom, 1.0), MAX_ZOOM)
    box_height = box_width * HEIGHT / WIDTH
    left = min(max(center[0] * width - box_width / 2, 0), width - box_width)
    top = min(max(center[1] * height - box_height / 2, 0), height - box_height)
    return left, top, left + box_width, top + box_height


def render(image, zoom=1.0, center=(0.5, 0.5)):
    """Return the exact 4-gray palette image the device will show."""
    framed = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS, box=crop_box(image.size, zoom, center))
    palette = Image.new("P", (1, 1))
    palette.putpalette([value for gray in GRAYS for value in (gray, gray, gray)] + [0] * 3 * 252)
    return framed.convert("L").convert("RGB").quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)


def encode(indexed):
    pixels = indexed.tobytes()
    rows = []
    for y in range(HEIGHT - 1, -1, -1):  # Bottom-up, as most BMP readers expect.
        line = pixels[y * WIDTH:(y + 1) * WIDTH]
        rows.append(bytes(line[x] << 4 | line[x + 1] for x in range(0, WIDTH, 2)))
    data = b"".join(rows)
    colors = b"".join(bytes((gray, gray, gray, 0)) for gray in GRAYS)
    offset = 14 + 40 + len(colors)
    header = b"BM" + struct.pack("<IHHI", offset + len(data), 0, 0, offset)
    info = struct.pack("<IiiHHIIiiII", 40, WIDTH, HEIGHT, 1, 4, 0, len(data), 2835, 2835, len(GRAYS), len(GRAYS))
    return header + info + colors + data


def convert(path, zoom=1.0, center=(0.5, 0.5)):
    return encode(render(load(path), zoom, center))


def remote_name(path, data):
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(path).stem).strip("-")[:24] or "image"
    return f"{FOLDER}/{stem}-{hashlib.sha256(data).hexdigest()[:8]}.bmp"
