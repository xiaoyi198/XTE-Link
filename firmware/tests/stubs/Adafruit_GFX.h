#pragma once
#include <stdint.h>
#ifdef HOST_UI
namespace host_rom {
#include "glcdfont.c"
}
#endif
// Host-only draw target; production still uses the pinned Adafruit GFX library.
class Adafruit_GFX {
 public:
  Adafruit_GFX(int16_t width, int16_t height) : w_(width), h_(height) {}
  virtual ~Adafruit_GFX() = default;
  virtual void drawPixel(int16_t x, int16_t y, uint16_t color) = 0;
  int16_t width() const { return w_; }
  int16_t height() const { return h_; }
#ifdef HOST_UI
  void setCursor(int16_t x, int16_t y) { x_ = x; y_ = y; }
  void setTextSize(uint8_t size) { size_ = size; }
  void setTextColor(uint16_t color) { color_ = color; }
  void setTextWrap(bool) {}
  void drawBitmap(int16_t x, int16_t y, const uint8_t* bitmap, int16_t w, int16_t h, uint16_t color) {
    const int stride = (w + 7) / 8;
    for (int row = 0; row < h; ++row) {
      for (int col = 0; col < w; ++col) {
        if (bitmap[row * stride + col / 8] & (0x80 >> (col % 8))) drawPixel(x + col, y + row, color);
      }
    }
  }
  void drawFastHLine(int16_t x, int16_t y, int16_t w, uint16_t color) {
    for (int col = 0; col < w; ++col) drawPixel(x + col, y, color);
  }
  void drawFastVLine(int16_t x, int16_t y, int16_t h, uint16_t color) {
    for (int row = 0; row < h; ++row) drawPixel(x, y + row, color);
  }
  void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color) {
    for (int row = 0; row < h; ++row) drawFastHLine(x, y + row, w, color);
  }
  void drawRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color) {
    drawFastHLine(x, y, w, color); drawFastHLine(x, y + h - 1, w, color);
    drawFastVLine(x, y, h, color); drawFastVLine(x + w - 1, y, h, color);
  }
  void print(const char* text) {
    while (*text) {
      const uint8_t ch = *text++;
      for (int col = 0; col < 5; ++col) {
        const uint8_t bits = host_rom::font[ch * 5 + col];
        for (int row = 0; row < 8; ++row) {
          if ((bits >> row) & 1) fillRect(x_ + col * size_, y_ + row * size_, size_, size_, color_);
        }
      }
      x_ += 6 * size_;
    }
  }
 private:
  int16_t x_ = 0, y_ = 0;
  uint8_t size_ = 1;
  uint16_t color_ = 0;
#endif
 private:
  int16_t w_, h_;
};
