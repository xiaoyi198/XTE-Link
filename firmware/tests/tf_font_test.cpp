#include "tf_font.h"
#include <array>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>

void check(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
struct Canvas : Adafruit_GFX {
  Canvas() : Adafruit_GFX(64, 64) {}
  std::array<uint8_t, 512> bits{};
  void drawPixel(int16_t x, int16_t y, uint16_t color) override {
    check(x >= 0 && x < 64 && y >= 0 && y < 64, "out-of-screen draw");
    uint8_t& byte = bits[y * 8 + x / 8];
    const uint8_t mask = 0x80 >> (x & 7);
    if (color) byte &= ~mask; else byte |= mask;
  }
};
std::string utf8(uint32_t cp) {
  std::string value;
  if (cp < 0x80) value += char(cp);
  else if (cp < 0x800) { value += char(0xc0 | (cp >> 6)); value += char(0x80 | (cp & 63)); }
  else if (cp < 0x10000) {
    value += char(0xe0 | (cp >> 12)); value += char(0x80 | ((cp >> 6) & 63)); value += char(0x80 | (cp & 63));
  } else {
    value += char(0xf0 | (cp >> 18)); value += char(0x80 | ((cp >> 12) & 63));
    value += char(0x80 | ((cp >> 6) & 63)); value += char(0x80 | (cp & 63));
  }
  return value;
}
void testUtf8(const std::string& value, const std::initializer_list<uint32_t>& expected) {
  const char* cursor = value.data();
  const char* end = cursor + value.size();
  for (uint32_t cp : expected) check(xte::TfFont::nextCodepoint(cursor, end) == cp, "UTF-8 decode");
  check(cursor == end, "UTF-8 cursor boundary");
  check(xte::TfFont::nextCodepoint(cursor, end) == 0, "UTF-8 empty");
}
int main(int argc, char** argv) {
  try {
    check(argc == 4, "usage: font-test font.bin codepoints.txt pixels.bin");
    std::ifstream source(argv[1], std::ios::binary);
    check(bool(source), "open font");
    host::bytes.assign(std::istreambuf_iterator<char>(source), {});
    const auto original = host::bytes;
    xte::TfFont font;
    check(font.beginRender(), "valid font header");
    host::reads = 0;
    check(font.measure(u8"中中") == 84 && host::reads == 2, "cache hit and Chinese advance");
    Canvas clipped;
    check(font.draw(clipped, 4, 8, u8"中文", 41) == 0, "whole-character clipping");
    check(font.draw(clipped, 4, 8, u8"中文", 42) == 42, "one character clipping");
    Canvas normal, inverted;
    font.draw(normal, 4, 8, u8"秒", 56);
    inverted.bits.fill(0xff);
    font.draw(inverted, 4, 8, u8"秒", 56, 1);
    for (size_t i = 0; i < normal.bits.size(); ++i) {
      check(uint8_t(~normal.bits[i]) == inverted.bits[i], "inverse rendering");
    }
    Canvas edge;
    font.draw(edge, -20, -20, u8"秒中文", 500);
    font.endRender();
    check(host::opens == 0, "close before display / transfer");

    // Export every GB2312 symbol plus printable ASCII using the production reader.
    check(font.beginRender(), "reopen font");
    std::ifstream cps(argv[2]);
    std::ofstream output(argv[3], std::ios::binary);
    uint32_t cp;
    int count = 0;
    while (cps >> std::hex >> cp) {
      Canvas canvas;
      const std::string value = utf8(cp);
      const uint16_t width = font.measure(value.c_str());
      font.draw(canvas, 4, 8, value.c_str(), 56);
      check(font.healthy(), "all glyph records and bitmap bounds");
      output.write(reinterpret_cast<const char*>(&cp), 4);
      output.write(reinterpret_cast<const char*>(&width), 2);
      output.write(reinterpret_cast<const char*>(canvas.bits.data()), canvas.bits.size());
      ++count;
    }
    font.endRender();

    testUtf8(u8"A中😀", {65, 0x4e2d, 0x1f600});
    testUtf8("\xe4\xb8", {0xfffd, 0xfffd});
    testUtf8("\xc0\xaf", {0xfffd, 0xfffd});
    testUtf8("\xed\xa0\x80", {0xfffd, 0xfffd, 0xfffd});
    testUtf8("\xf4\x90\x80\x80", {0xfffd, 0xfffd, 0xfffd, 0xfffd});
    testUtf8("\xe4" "A", {0xfffd, 65});
    testUtf8("", {});
    Canvas absent;
    host::mounted = false;
    check(!font.beginRender() && font.measure(u8"中文") == 84, "no TF");
    check(font.draw(absent, 4, 8, u8"中", 56) == 42, "no TF draws box");
    host::mounted = true; host::present = false;
    check(!font.beginRender(), "missing font");
    Canvas missing;
    font.draw(missing, 4, 8, u8"中", 56);
    check(absent.bits == missing.bits, "consistent error box");
    host::present = true; host::directory = true;
    check(!font.beginRender() && host::opens == 0, "directory rejected and closed");
    host::directory = false;
    host::bytes.resize(47);
    check(!font.beginRender(), "truncated header");
    host::bytes = original; host::bytes[12] = 35;
    check(!font.beginRender(), "wrong font size rejected");
    host::bytes = original; host::bytes[48 + 8] = 1;
    check(!font.beginRender(), "bad interval offset");
    host::bytes = original; host::bytes.resize(279686);
    check(font.beginRender(), "metadata present");
    font.draw(missing, 4, 8, u8"中", 56);
    check(!font.healthy() && host::opens == 0, "truncated bitmap rejected");
    host::bytes = original;
    check(font.beginRender(), "recovery next frame");
    host::failAfter = host::reads;
    Canvas readError;
    font.draw(readError, 4, 8, u8"中", 56);
    check(!font.healthy() && host::opens == 0 && readError.bits == absent.bits, "read failure box");
    host::failAfter = -1;
    check(font.beginRender() && font.measure(u8"中") == 42, "recovery and cache reset");
    // A new frame must not retain stale glyphs after a transfer replaces a file.
    font.endRender();
    host::bytes = original;
    for (size_t i = 48; i < 1356; i += 12) {
      uint32_t first, last, base;
      memcpy(&first, &host::bytes[i], 4); memcpy(&last, &host::bytes[i + 4], 4);
      memcpy(&base, &host::bytes[i + 8], 4);
      if (first <= 0x4e2d && last >= 0x4e2d) {
        host::bytes[1356 + (base + 0x4e2d - first) * 13] = 255;
        break;
      }
    }
    check(font.beginRender(), "corrupt glyph lazy validation");
    font.measure(u8"中");
    check(!font.healthy() && host::opens == 0, "glyph width cap after cache reset");
    std::cout << "PASS: " << count << " glyphs; UTF-8, clipping, cache, failures; reader bytes="
              << sizeof(font) << '\n';
    return 0;
  } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
