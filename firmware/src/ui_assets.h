#pragma once

#include "sleep_image.h"

namespace xte {
// Bounded TF overlays on the existing SDK framebuffer. No image cache or
// additional full-screen buffer; all file handles close before display SPI.
class UiAssets {
 public:
  enum Icon : uint8_t {
    COMPUTER, VOLUME, ASSISTANT, SETTINGS, SHORTCUTS, CLASH, NOTE, ADD,
    DISPLAY_SETTINGS, GALLERY, STARTUP, CONNECTION, REMINDER, POWER,
    USB, WIFI, OFFLINE, TF_CARD, BATTERY, EXTERNAL_POWER,
    UP, DOWN, LEFT, RIGHT, CONFIRM, BACK, LOGO, COUNT
  };

  void begin(uint8_t* target) { frame = target; count = loadedCount = missingCount = 0; }
  bool hasGray() const { return count != 0; }
  uint8_t loaded() const { return loadedCount; }
  uint8_t missing() const { return missingCount; }

  bool draw(Icon icon, int16_t x, int16_t y, bool inverse = false) {
    if (icon >= COUNT || !frame) return false;
    const uint8_t size = iconSize(icon);
    if (x < 0 || y < 0 || x + size > 800 || y + size > 480) return false;
    SleepImage image;
    const bool opened = open(image, icon);
    const bool okay = opened && (!image.hasGray() || count < MAX_GRAY) && image.drawAt(frame, x, y, SleepImage::BASE, inverse);
    if (okay) {
      if (image.hasGray()) overlays[count++] = {icon, x, y, inverse};
      ++loadedCount;
    } else {
      // A partial SD read must not leave debris underneath the old icon.
      for (uint16_t row = y; row < y + size; ++row)
        for (uint16_t col = x; col < x + size; ++col) {
          uint8_t& pixel = frame[row * 100 + col / 8];
          const uint8_t bit = 0x80 >> (col & 7);
          if (inverse) pixel &= ~bit;
          else pixel |= bit;
        }
      ++missingCount;
    }
    return okay;
  }

  bool drawPlane(SleepImage::Plane plane) {
    if (!frame || (plane != SleepImage::LSB && plane != SleepImage::MSB)) return false;
    memset(frame, 0, 48000);  // Black/white UI pixels need neither gray plane.
    for (uint8_t i = 0; i < count; ++i) {
      const Overlay& item = overlays[i];
      SleepImage image;
      if (!open(image, item.icon) || !image.drawAt(frame, item.x, item.y, plane, item.inverse)) return false;
    }
    return true;
  }

 private:
  static constexpr uint8_t MAX_GRAY = 8;  // Logo + six settings categories = 7.
  struct Overlay { Icon icon; int16_t x, y; bool inverse; };
  Overlay overlays[MAX_GRAY];
  uint8_t* frame = nullptr;
  uint8_t count = 0, loadedCount = 0, missingCount = 0;

  static uint8_t iconSize(Icon icon) {
    return icon < DISPLAY_SETTINGS ? 64 : icon < USB ? 32 : icon < UP ? 24 : icon < LOGO ? 16 : 32;
  }
  static bool open(SleepImage& image, Icon icon) {
    static constexpr const char* names[] = {
      "computer", "volume", "assistant", "settings", "shortcuts", "clash", "note", "add",
      "display", "gallery", "startup", "connection", "reminder", "power",
      "usb", "wifi", "offline", "tf-card", "battery", "external-power",
      "up", "down", "left", "right", "confirm", "back", "logo"
    };
    char path[80];
    snprintf(path, sizeof(path), "/xte-link/ui/gray-flat-v1/%s-%u.bmp", names[icon], static_cast<unsigned>(iconSize(icon)));
    return image.openIcon(path, iconSize(icon));
  }
};
}  // namespace xte
