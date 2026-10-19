"""Checks that converted sleep pictures match the firmware BMP subset."""

from pathlib import Path
import struct
import tempfile
import unittest

from PIL import Image

from pc_driver import sleep_image


class SleepImageTests(unittest.TestCase):
    def convert(self, image):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "测试 photo.png"
            image.save(path)
            return sleep_image.convert(path), path

    def test_header_matches_firmware_subset(self):
        data, path = self.convert(Image.new("RGB", (1200, 1600), "white"))
        self.assertEqual(data[:2], b"BM")
        offset, = struct.unpack_from("<I", data, 10)
        dib, width, height, planes, bpp, compression = struct.unpack_from("<IiiHHI", data, 14)
        colors, = struct.unpack_from("<I", data, 46)
        self.assertEqual((dib, width, height, planes, bpp, compression, colors), (40, 800, 480, 1, 4, 0, 4))
        self.assertEqual(offset, 14 + 40 + 16)
        self.assertEqual(len(data), offset + 400 * 480)
        self.assertEqual(struct.unpack_from("<I", data, 2)[0], len(data))
        self.assertTrue(set(data[offset:]) <= {0x33})  # White is palette index 3.
        name = sleep_image.remote_name(path, data)
        self.assertRegex(name, r"^/\.sleep/photo-[0-9a-f]{8}\.bmp$")

    def test_gradient_uses_all_four_levels_bottom_up(self):
        image = Image.new("L", (800, 480))
        image.putdata([x * 255 // 799 for _ in range(480) for x in range(800)])
        image.paste(0, (0, 0, 800, 10))  # Black top band must be stored last.
        data, _ = self.convert(image)
        pixels = data[70:]
        levels = {byte >> 4 for byte in pixels} | {byte & 15 for byte in pixels}
        self.assertEqual(levels, {0, 1, 2, 3})
        self.assertTrue(set(pixels[-400:]) == {0})


    def test_crop_box_zooms_and_clamps_inside_source(self):
        self.assertEqual(sleep_image.crop_box((1000, 1000)), (0, 200, 1000, 800))
        self.assertEqual(sleep_image.crop_box((1000, 1000), zoom=2), (250, 350, 750, 650))
        self.assertEqual(sleep_image.crop_box((1000, 1000), zoom=2, center=(0, 1)), (0, 700, 500, 1000))
        left, top, right, bottom = sleep_image.crop_box((1000, 1000), zoom=99)
        self.assertAlmostEqual(right - left, 1000 / sleep_image.MAX_ZOOM)

    def test_render_follows_crop_center(self):
        image = Image.new("RGB", (1600, 480), "white")
        image.paste((0, 0, 0), (0, 0, 800, 480))  # Left half black.
        left = sleep_image.render(image, center=(0, 0.5))
        right = sleep_image.render(image, center=(1, 0.5))
        self.assertEqual(set(left.tobytes()), {0})
        self.assertEqual(set(right.tobytes()), {3})


if __name__ == "__main__":
    unittest.main()
