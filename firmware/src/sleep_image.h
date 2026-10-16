#pragma once

#include <Preferences.h>
#include <SDCardManager.h>
#include <esp_random.h>

namespace xte {
// Streams an 800x480 uncompressed 1/4/8-bit palette BMP from TF into the SDK
// framebuffer. Gray planes follow the CrossPoint/SDK X4 convention: the base
// pass draws all non-white pixels black, then LSB marks dark gray and MSB marks
// both grays for the SDK grayscale LUT.
class SleepImage {
 public:
  enum Plane : uint8_t { BASE, LSB, MSB };
  static constexpr const char* SINGLE = "/sleep.bmp";
  static constexpr const char* FOLDER = "/.sleep";

  // /sleep.bmp wins; otherwise pick a random folder image, avoiding the last one.
  bool open() {
    if (!SdMan.ready()) return false;
    if (SdMan.exists(SINGLE)) return load(SINGLE);
    std::vector<String> names;
    for (const String& name : SdMan.listFiles(FOLDER, 200)) {
      if (!name.startsWith(".") && name.length() < 64 && (name.endsWith(".bmp") || name.endsWith(".BMP")))
        names.push_back(name);
    }
    if (names.empty()) return false;
    Preferences prefs;
    String last;
    if (prefs.begin("xte-link", true)) {
      last = prefs.getString("sleepLast", "");
      prefs.end();
    }
    size_t index = esp_random() % names.size();
    if (names.size() > 1 && names[index] == last) index = (index + 1) % names.size();
    if (prefs.begin("xte-link", false)) {
      prefs.putString("sleepLast", names[index]);
      prefs.end();
    }
    return load((String(FOLDER) + "/" + names[index]).c_str());
  }

  bool hasGray() const { return gray; }

  bool draw(uint8_t* frame, Plane plane) {
    if (!file || !file.seekSet(dataOffset)) return false;
    static uint8_t row[WIDTH];  // Largest stride: 8 bpp.
    for (uint16_t i = 0; i < HEIGHT; ++i) {
      if (file.read(row, stride) != static_cast<int>(stride)) return false;
      uint8_t* out = frame + (bottomUp ? HEIGHT - 1 - i : i) * (WIDTH / 8);
      for (uint16_t x = 0; x < WIDTH; x += 8) {
        uint8_t bits = 0;
        for (uint8_t b = 0; b < 8; ++b) {
          const uint8_t level = levels[index(row, x + b)];
          const bool set = plane == BASE ? level == 3 : plane == LSB ? level == 1 : level == 1 || level == 2;
          bits = bits << 1 | set;
        }
        out[x / 8] = bits;
      }
    }
    return true;
  }

  void close() { file.close(); }

 private:
  static constexpr uint16_t WIDTH = 800, HEIGHT = 480;
  FsFile file;
  uint32_t dataOffset = 0, stride = 0;
  uint16_t bpp = 0;
  bool bottomUp = true, gray = false;
  uint8_t levels[256] = {};  // 0 black, 1 dark gray, 2 light gray, 3 white

  static uint32_t u32(const uint8_t* b) {
    return uint32_t(b[0]) | uint32_t(b[1]) << 8 | uint32_t(b[2]) << 16 | uint32_t(b[3]) << 24;
  }
  static uint16_t u16(const uint8_t* b) { return uint16_t(b[0]) | uint16_t(b[1]) << 8; }

  uint8_t index(const uint8_t* row, uint16_t x) const {
    if (bpp == 8) return row[x];
    if (bpp == 4) return x & 1 ? row[x / 2] & 15 : row[x / 2] >> 4;
    return row[x / 8] >> (7 - (x & 7)) & 1;
  }

  bool load(const char* path) {
    file = SdMan.open(path);
    if (!file || file.isDirectory()) return fail();
    uint8_t header[54];
    if (file.read(header, sizeof(header)) != sizeof(header) || header[0] != 'B' || header[1] != 'M') return fail();
    dataOffset = u32(header + 10);
    const uint32_t dibSize = u32(header + 14);
    const int32_t width = static_cast<int32_t>(u32(header + 18));
    const int32_t height = static_cast<int32_t>(u32(header + 22));
    bpp = u16(header + 28);
    uint32_t colors = u32(header + 46);
    if (dibSize < 40 || width != WIDTH || (height != HEIGHT && height != -HEIGHT) || u16(header + 26) != 1 ||
        (bpp != 1 && bpp != 4 && bpp != 8) || u32(header + 30) != 0) return fail();
    if (!colors) colors = 1u << bpp;
    if (colors > (1u << bpp)) return fail();
    bottomUp = height > 0;
    stride = (WIDTH * bpp + 31) / 32 * 4;
    if (dataOffset < 14 + dibSize + colors * 4 || file.fileSize() < dataOffset + stride * HEIGHT ||
        !file.seekSet(14 + dibSize)) return fail();
    gray = false;
    memset(levels, 3, sizeof(levels));
    for (uint32_t i = 0; i < colors; ++i) {
      uint8_t bgra[4];
      if (file.read(bgra, 4) != 4) return fail();
      const uint32_t luma = (bgra[2] * 299u + bgra[1] * 587u + bgra[0] * 114u) / 1000u;
      levels[i] = (luma + 42) / 85;
      gray |= levels[i] == 1 || levels[i] == 2;
    }
    return true;
  }

  bool fail() {
    file.close();
    return false;
  }
};
}  // namespace xte
