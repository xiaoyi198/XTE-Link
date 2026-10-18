#pragma once

#include <Preferences.h>
#include <SDCardManager.h>
#include <esp_random.h>
#include <algorithm>
#include "settings.h"

namespace xte {
// Streams an 800x480 uncompressed 1/4/8-bit palette BMP from TF into the SDK
// framebuffer. Gray planes follow the CrossPoint/SDK X4 convention: the base
// pass draws all non-white pixels black, then LSB marks dark gray and MSB marks
// both grays for the SDK grayscale LUT.
class SleepImage {
 public:
  ~SleepImage() { close(); }
  enum Plane : uint8_t { BASE, LSB, MSB };
  static constexpr const char* SINGLE = "/sleep.bmp";
  static constexpr const char* FOLDER = "/.sleep";

  // Use the SDK directory helper; cap the catalog at 200 paths, no image cache.
  static std::vector<String> catalog() {
    std::vector<String> names;
    if (!SdMan.ready()) return names;
    if (SdMan.exists(SINGLE)) names.push_back(String(SINGLE));
    for (const String& name : SdMan.listFiles(FOLDER, 200)) {
      const String path = String(FOLDER) + "/" + name;
      if (names.size() < 200 && path.length() < 80 && Settings::imagePathValid(path.c_str()))
        names.push_back(path);
    }
    std::sort(names.begin(), names.end(), [](const String& a, const String& b) { return a.compareTo(b) < 0; });
    return names;
  }

  // Preview deliberately avoids the sleep history key and all preference writes.
  bool openPath(const char* path) {
    return Settings::imagePathValid(path) && SdMan.ready() && load(path);
  }

  bool open(const Settings& settings) {
    if (settings.imageMode == 1) return openPath(settings.sleepFile);
    const auto names = catalog();
    if (names.empty()) return false;
    Preferences prefs;
    String last;
    if (prefs.begin("xte-link", true)) {
      last = prefs.getString("sleepLast", "");
      prefs.end();
    }
    size_t previous = names.size();
    for (size_t i = 0; i < names.size(); ++i)
      if (names[i] == last || names[i] == String(FOLDER) + "/" + last) previous = i;
    size_t index = 0;
    if (settings.imageMode == 2) {
      if (previous < names.size()) index = (previous + 1) % names.size();
    } else if (names.size() > 1 && previous < names.size()) {
      // Uniform choice from all images except the previous one.
      index = esp_random() % (names.size() - 1);
      if (index >= previous) ++index;
    } else index = esp_random() % names.size();
    if (!openPath(names[index].c_str())) return false;
    if (names[index] != last && prefs.begin("xte-link", false)) {
      prefs.putString("sleepLast", names[index]); prefs.end();
    }
    return true;
  }

  bool hasGray() const { return gray; }

  // UI assets share this palette BMP reader; the sleep path stays 800x480.
  bool openIcon(const char* path, uint16_t size) {
    return size && size <= 128 && SdMan.ready() && load(path, size, size);
  }

  bool drawAt(uint8_t* frame, int16_t left, int16_t top, Plane plane = BASE, bool inverse = false) {
    if (!file || left < 0 || top < 0 || left + imageWidth > WIDTH || top + imageHeight > HEIGHT ||
        !file.seekSet(dataOffset)) return false;
    uint8_t row[128];  // Largest UI asset is 128x128, at most 8 bpp.
    if (imageWidth > 128 || stride > sizeof(row)) return false;
    for (uint16_t i = 0; i < imageHeight; ++i) {
      if (file.read(row, stride) != static_cast<int>(stride)) return false;
      const uint16_t y = top + (bottomUp ? imageHeight - 1 - i : i);
      for (uint16_t x = 0; x < imageWidth; ++x) {
        const uint8_t level = inverse ? 3 - levels[index(row, x)] : levels[index(row, x)];
        const bool set = plane == BASE ? level == 3 : plane == LSB ? level == 1 : level == 1 || level == 2;
        const uint16_t dx = left + x;
        uint8_t& out = frame[y * (WIDTH / 8) + dx / 8];
        const uint8_t bit = 0x80 >> (dx & 7);
        if (set) out |= bit;
        else out &= ~bit;
      }
    }
    return true;
  }

  bool draw(uint8_t* frame, Plane plane) {
    if (!file || imageWidth != WIDTH || imageHeight != HEIGHT || !file.seekSet(dataOffset)) return false;
    static uint8_t row[WIDTH];  // Largest stride: 8 bpp.
    for (uint16_t i = 0; i < HEIGHT; ++i) {
      if (file.read(row, stride) != static_cast<int>(stride)) return false;
      uint8_t* out = frame + (bottomUp ? HEIGHT - 1 - i : i) * (WIDTH / 8);
      for (uint16_t x = 0; x < WIDTH; x += 8) {
        uint8_t bits = 0;
        for (uint8_t b = 0; b < 8; ++b) {
          const uint8_t level = levels[index(row, x + b)];
          const bool set = plane == BASE ? level == 3 : plane == LSB ? level == 1 : level == 1 || level == 2;
          bits = static_cast<uint8_t>((bits << 1) | static_cast<uint8_t>(set));
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
  uint16_t imageWidth = WIDTH, imageHeight = HEIGHT;
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

  bool load(const char* path, uint16_t expectedWidth = WIDTH, uint16_t expectedHeight = HEIGHT) {
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
    if (dibSize < 40 || dibSize > 124 || width != expectedWidth ||
        (height != expectedHeight && height != -int32_t(expectedHeight)) || u16(header + 26) != 1 ||
        (bpp != 1 && bpp != 4 && bpp != 8) || u32(header + 30) != 0) return fail();
    if (!colors) colors = 1u << bpp;
    if (colors > (1u << bpp)) return fail();
    imageWidth = expectedWidth; imageHeight = expectedHeight;
    bottomUp = height > 0;
    stride = (imageWidth * bpp + 31) / 32 * 4;
    if (dataOffset < 14 + dibSize + colors * 4 || uint64_t(dataOffset) + stride * imageHeight > file.fileSize() ||
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
