#include "tf_font.h"
#include <array>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>

using Face = xte::TfFont::Face;
void check(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
struct Canvas : Adafruit_GFX {
  Canvas() : Adafruit_GFX(80, 80) {}
  std::array<uint8_t, 800> bits{};
  void drawPixel(int16_t x, int16_t y, uint16_t color) override {
    check(x >= 0 && x < 80 && y >= 0 && y < 80, "screen bounds");
    const uint8_t mask = 0x80 >> (x & 7);
    if (color) bits[y * 10 + x / 8] &= ~mask;
    else bits[y * 10 + x / 8] |= mask;
  }
};
std::string utf8(uint32_t cp) {
  std::string out;
  if (cp < 128) out += char(cp);
  else if (cp < 0x800) { out += char(0xc0 | (cp >> 6)); out += char(0x80 | (cp & 63)); }
  else if (cp < 0x10000) {
    out += char(0xe0 | (cp >> 12)); out += char(0x80 | ((cp >> 6) & 63)); out += char(0x80 | (cp & 63));
  } else {
    out += char(0xf0 | (cp >> 18)); out += char(0x80 | ((cp >> 12) & 63));
    out += char(0x80 | ((cp >> 6) & 63)); out += char(0x80 | (cp & 63));
  }
  return out;
}
uint32_t u32(const std::vector<uint8_t>& b, size_t offset) {
  return b[offset] | (uint32_t(b[offset + 1]) << 8) | (uint32_t(b[offset + 2]) << 16) | (uint32_t(b[offset + 3]) << 24);
}
int main(int argc, char** argv) {
  try {
    check(argc == 2, "usage: misans-test fixture-directory");
    const std::string dir = argv[1];
    xte::TfFont font;
    const Face faces[] = {Face::Caption24, Face::Body32, Face::Title40, Face::Numbers64};
    const int sizes[] = {24, 32, 40, 64};
    const int lines[] = {35, 46, 57, 51}, ascenders[] = {27, 36, 45, 49};
    for (int i = 0; i < 4; ++i) {
      std::ifstream input(dir + "/MiSans-Medium-" + std::to_string(sizes[i]) + ".epdfont", std::ios::binary);
      check(bool(input), "open fixture");
      host::files[xte::TfFont::path(faces[i])].assign(std::istreambuf_iterator<char>(input), {});
    }
    for (int i = 0; i < 4; ++i) {
      check(font.beginRender(faces[i]) && host::opens == 1, "open one file only");
      check(font.lineHeight() == lines[i] && font.ascender() == ascenders[i], "profile metrics");
      const char* sample = i < 3 ? u8"中" : "0";
      host::reads = 0;
      const auto advance = font.measure(sample);
      check(font.measure(sample) == advance && host::reads == 2, "single glyph cache");
      Canvas clipped, normal, inverse;
      check(font.draw(clipped, 8, 8, sample, advance - 1) == 0, "whole glyph clipping");
      font.draw(normal, 8, 8, sample, 72);
      inverse.bits.fill(255); font.draw(inverse, 8, 8, sample, 72, 1);
      for (size_t j = 0; j < normal.bits.size(); ++j)
        check(uint8_t(~normal.bits[j]) == inverse.bits[j], "inverse pixels");
      font.draw(clipped, -60, -60, sample, 72);  // Clipping must never call GFX off-screen.
      std::ifstream cps(dir + "/cps-" + std::to_string(sizes[i]) + ".txt");
      std::ofstream output(dir + "/pixels-" + std::to_string(sizes[i]) + ".bin", std::ios::binary);
      uint32_t cp;
      while (cps >> std::hex >> cp) {
        const auto text = utf8(cp);
        const uint16_t width = font.measure(text.c_str());
        Canvas canvas; font.draw(canvas, 8, 8, text.c_str(), 72);
        check(font.healthy(), "all glyph records/bitmaps valid");
        output.write(reinterpret_cast<const char*>(&cp), 4);
        output.write(reinterpret_cast<const char*>(&width), 2);
        output.write(reinterpret_cast<const char*>(canvas.bits.data()), canvas.bits.size());
      }
      font.endRender(); check(host::opens == 0, "closed before refresh");
    }
    check(font.beginRender(Face::Caption24) && font.measure(u8"中") == 24, "24px");
    check(font.select(Face::Body32) && font.measure(u8"中") == 32 && host::opens == 1, "switch clears 24px cache");
    check(font.select(Face::Title40) && font.measure(u8"中") == 40 && host::opens == 1, "switch clears 32px cache");
    const auto path = xte::TfFont::path(Face::Body32);
    const auto original = host::files[path];
    host::files.erase(path);
    check(!font.select(Face::Body32) && host::opens == 0, "missing size fails closed");
    check(!font.select(Face::Caption24) && !font.healthy(), "error retained through remainder of frame");
    host::files[path] = original;
    check(font.beginRender(Face::Body32) && font.measure(u8"中") == 32, "recover next frame");
    host::mounted = false;
    check(!font.beginRender(Face::Body32), "no TF"); host::mounted = true;
    host::directory = true;
    check(!font.beginRender(Face::Body32) && host::opens == 0, "directory rejected"); host::directory = false;
    for (size_t offset : {size_t(4), size_t(12), size_t(20), size_t(28), size_t(32), size_t(40), size_t(56)}) {
      host::files[path] = original; host::files[path][offset] ^= 1;
      check(!font.beginRender(Face::Body32), "corrupt profile/header/interval rejected");
    }
    host::files[path] = original; host::files[path].resize(47);
    check(!font.beginRender(Face::Body32), "short header");
    size_t record = 0;
    for (uint32_t i = 0; i < u32(original, 4); ++i) {
      const size_t range = 48 + 12 * i;
      const auto first = u32(original, range), last = u32(original, range + 4);
      if (first <= 0x4e2d && last >= 0x4e2d)
        record = u32(original, 40) + 13 * (u32(original, range + 8) + 0x4e2d - first);
    }
    check(record != 0, "find glyph record");
    for (int kind = 0; kind < 3; ++kind) {
      host::files[path] = original;
      auto& bad = host::files[path];
      if (kind == 0) bad[record] = 255;
      if (kind == 1) bad[record + 7] ^= 1;
      if (kind == 2) for (size_t j = 9; j < 13; ++j) bad[record + j] = 255;
      check(font.beginRender(Face::Body32), "bad glyph checked lazily");
      font.measure(u8"中"); check(!font.healthy() && host::opens == 0, "glyph bounds/length/offset rejected");
    }
    host::files[path] = original; host::files[path].resize(u32(original, 44));
    check(font.beginRender(Face::Body32), "lazy bitmap validation");
    font.measure(u8"中"); check(!font.healthy() && host::opens == 0, "truncated bitmap");
    host::files[path] = original;
    check(font.beginRender(Face::Body32), "restart after truncated bitmap");
    host::failAfter = host::reads;
    font.measure(u8"中"); check(!font.healthy() && host::opens == 0, "read failure closes file");
    host::failAfter = -1;
    check(font.beginRender(Face::Body32) && font.measure(u8"中") == 32, "read recovery");
    font.endRender();
    std::cout << "PASS: all MiSans glyphs exported; size switches, cache, clipping, inverse, malformed metadata, missing-size and read recovery; host reader bytes=" << sizeof(font) << '\n';
    return 0;
  } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
