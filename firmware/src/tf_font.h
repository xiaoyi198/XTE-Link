#pragma once

#include <Adafruit_GFX.h>
#include <SDCardManager.h>
#include <stdint.h>
#include <string.h>

namespace xte {
// Bounded, streamed reader for the inspected EPDF layout (not .cpfont).
// All SD access uses the SDK and finishes before sending the display buffer.
class TfFont {
 public:
  static constexpr const char* PATH = "/fonts/Zheng20号.epdfont";
  static constexpr uint8_t LINE_HEIGHT = 42;
  static constexpr uint8_t ASCENDER = 35;
  enum class Face : uint8_t { Legacy20, Caption24, Body32, Title40, Numbers64 };

  // Legacy default keeps the old font and its verification/rollback path intact.
  bool beginRender(Face face = Face::Legacy20) {
    endRender();
    error_ = nullptr;
    rendering_ = true;
    return loadFace(face);
  }

  bool select(Face face) {
    if (face == face_) return rendering_ && healthy();
    profile_ = &profile(face);
    if (!rendering_ || !healthy()) { face_ = face; return false; }
    return loadFace(face);
  }

  uint8_t lineHeight() const { return profile_->line; }
  uint8_t ascender() const { return profile_->ascender; }
  static const char* path(Face face) { return profile(face).path; }
  void endRender() { file_.close(); rendering_ = false; }
  bool healthy() const { return error_ == nullptr; }
  const char* error() const { return error_; }

 private:
  struct Profile {
    const char* path;
    uint8_t pixels, line, ascender;
    int8_t descender;
    uint8_t bpp, maxWidth, maxHeight, maxAdvance;
  };
  static const Profile& profile(Face face) {
    // Generated/verified metrics: tools/misans/reference-manifest.json.
    static constexpr Profile profiles[] = {
      {PATH, 42, 42, 35, -9, 2, 48, 44, 42},
      {"/fonts/MiSans-Medium-24.epdfont", 24, 35, 27, -8, 1, 38, 28, 38},
      {"/fonts/MiSans-Medium-32.epdfont", 32, 46, 36, -10, 1, 51, 38, 51},
      {"/fonts/MiSans-Medium-40.epdfont", 40, 57, 45, -12, 1, 63, 47, 63},
      {"/fonts/MiSans-Medium-64.epdfont", 64, 51, 49, -2, 1, 53, 51, 53},
    };
    return profiles[static_cast<uint8_t>(face)];
  }

  bool loadFace(Face face) {
    file_.close();
    face_ = face;
    profile_ = &profile(face);
    for (auto& entry : cache_) entry.valid = false;
    nextSlot_ = 0;
    if (!SdMan.ready()) return fail("TF not mounted");
    file_ = SdMan.open(profile_->path, O_RDONLY);
    if (!file_ || file_.isDirectory()) return fail("font missing");
    const uint64_t size = file_.fileSize();
    if (size > UINT32_MAX || size < 48) return fail("font size");
    fileSize_ = static_cast<uint32_t>(size);
    uint8_t header[48];
    if (!readAt(0, header, sizeof(header))) return false;
    intervalCount_ = u32(header + 4);
    glyphCount_ = u32(header + 8);
    glyphsOffset_ = u32(header + 40);
    bitmapOffset_ = u32(header + 44);
    if (u32(header) != 0x46445045 || u32(header + 12) != profile_->line ||
        u32(header + 16) != 0 || u32(header + 20) != profile_->ascender ||
        u32(header + 24) != 0 || static_cast<int32_t>(u32(header + 28)) != profile_->descender ||
        u32(header + 32) != (profile_->bpp == 2 ? 1u : 0u) || u32(header + 36) != 48 ||
        intervalCount_ == 0 || intervalCount_ > MAX_INTERVALS ||
        glyphCount_ == 0 || glyphCount_ > 65536 ||
        glyphsOffset_ != 48 + intervalCount_ * 12 ||
        bitmapOffset_ != glyphsOffset_ + glyphCount_ * 13 || bitmapOffset_ > fileSize_) {
      return fail("font header");
    }
    if (!readAt(48, intervals_, intervalCount_ * 12)) return false;
    uint32_t index = 0, previous = 0;
    for (uint32_t i = 0; i < intervalCount_; ++i) {
      auto& r = intervals_[i];
      const auto* bytes = reinterpret_cast<const uint8_t*>(&r);
      const uint32_t first = u32(bytes), last = u32(bytes + 4), offset = u32(bytes + 8);
      r = {first, last, offset};
      if (first > last || last > 0x10ffff || (i && first <= previous) ||
          offset != index || last - first + 1 > glyphCount_ - index) {
        return fail("font intervals");
      }
      index += last - first + 1;
      previous = last;
    }
    if (index != glyphCount_) return fail("font glyph count");
    return true;
  }

 public:
  // Strict UTF-8: invalid input consumes one byte, without reading past end.
  static uint32_t nextCodepoint(const char*& cursor, const char* end) {
    if (cursor == end) return 0;
    const auto* p = reinterpret_cast<const uint8_t*>(cursor);
    const uint8_t first = p[0];
    if (first < 0x80) { ++cursor; return first; }
    const int count = first >= 0xc2 && first <= 0xdf ? 2 :
                      first >= 0xe0 && first <= 0xef ? 3 :
                      first >= 0xf0 && first <= 0xf4 ? 4 : 0;
    if (!count || end - cursor < count) { ++cursor; return 0xfffd; }
    uint32_t cp = first & (count == 2 ? 0x1f : count == 3 ? 0x0f : 0x07);
    for (int i = 1; i < count; ++i) {
      if ((p[i] & 0xc0) != 0x80) { ++cursor; return 0xfffd; }
      cp = (cp << 6) | (p[i] & 0x3f);
    }
    if ((count == 2 && cp < 0x80) || (count == 3 && cp < 0x800) ||
        (count == 4 && cp < 0x10000) || cp > 0x10ffff ||
        (cp >= 0xd800 && cp <= 0xdfff)) { ++cursor; return 0xfffd; }
    cursor += count;
    return cp;
  }

  uint16_t measure(const char* text) {
    const char* end = text + strlen(text);
    uint32_t width = 0;
    while (text < end) {
      const Entry* entry = glyph(nextCodepoint(text, end));
      width += entry ? entry->advance : profile_->pixels;
      if (width > UINT16_MAX) return UINT16_MAX;
    }
    return static_cast<uint16_t>(width);
  }

  uint16_t draw(Adafruit_GFX& target, int16_t x, int16_t top, const char* text,
                uint16_t maxWidth, uint16_t color = 0) {
    const char* end = text + strlen(text);
    uint32_t used = 0;
    while (text < end) {
      const Entry* entry = glyph(nextCodepoint(text, end));
      const uint8_t advance = entry ? entry->advance : profile_->pixels;
      if (used + advance > maxWidth) break;  // Never split a UTF-8 character.
      if (!entry) {
        // Missing symbols are visible; do not silently replace them with a font.
        const int side = face_ == Face::Legacy20 ? 26 : profile_->pixels * 2 / 3;
        const int dx = face_ == Face::Legacy20 ? 8 : (advance - side) / 2;
        const int dy = face_ == Face::Legacy20 ? 7 : (profile_->line - side) / 2;
        for (int row = 0; row < side; ++row) {
          for (int col = 0; col < side; ++col) {
            if (row && row != side - 1 && col && col != side - 1) continue;
            pixel(target, x, top + dy + row, used + dx + col, maxWidth, color);
          }
        }
      } else {
        for (int row = 0; row < entry->height; ++row) {
          for (int col = 0; col < entry->width; ++col) {
            const unsigned n = row * entry->width + col;
            const bool black = profile_->bpp == 1 ? (entry->bitmap[n / 8] & (0x80 >> (n % 8))) != 0 :
                ((entry->bitmap[n / 4] >> (6 - 2 * (n % 4))) & 3) >= 2;
            if (black) {
              pixel(target, x, top + profile_->ascender - entry->top + row,
                    static_cast<int32_t>(used) + entry->left + col, maxWidth, color);
            }
          }
        }
      }
      used += advance;
    }
    return static_cast<uint16_t>(used);
  }

 private:
  static constexpr uint32_t MAX_INTERVALS = 256;
  static constexpr uint16_t BITMAP_BYTES = 512;  // Legacy <=492; MiSans <=325.
  struct Interval { uint32_t first, last, offset; };
  struct Entry {
    uint32_t cp = 0;
    uint8_t width = 0, height = 0, advance = 0;
    int16_t left = 0, top = 0;
    bool valid = false;
    uint8_t bitmap[BITMAP_BYTES];
  };
  FsFile file_;
  Interval intervals_[MAX_INTERVALS];
  Entry cache_[8];
  uint8_t nextSlot_ = 0;
  uint32_t intervalCount_ = 0, glyphCount_ = 0, glyphsOffset_ = 0;
  uint32_t bitmapOffset_ = 0, fileSize_ = 0;
  const char* error_ = "font not opened";
  const Profile* profile_ = &profile(Face::Legacy20);
  Face face_ = Face::Legacy20;
  bool rendering_ = false;

  static uint32_t u32(const uint8_t* b) {
    return uint32_t(b[0]) | (uint32_t(b[1]) << 8) | (uint32_t(b[2]) << 16) | (uint32_t(b[3]) << 24);
  }
  static uint16_t u16(const uint8_t* b) { return uint16_t(b[0]) | (uint16_t(b[1]) << 8); }
  static int16_t bearing(const uint8_t* b) {
    const int16_t value = static_cast<int16_t>(u16(b));
    // This legacy file zero-extends negative int8 bearings (FF 00 means -1).
    return value >= 128 && value <= 255 ? value - 256 : value;
  }
  static void pixel(Adafruit_GFX& target, int16_t x, int32_t y, int32_t dx,
                    uint16_t maxWidth, uint16_t color) {
    const int32_t px = int32_t(x) + dx;
    if (dx >= 0 && dx < maxWidth && px >= 0 && px < target.width() &&
        y >= 0 && y < target.height()) target.drawPixel(static_cast<int16_t>(px), static_cast<int16_t>(y), color);
  }
  bool fail(const char* reason) {
    error_ = reason;
    endRender();
    return false;
  }
  bool readAt(uint32_t offset, void* output, size_t length) {
    if (!file_ || offset > fileSize_ || length > fileSize_ - offset ||
        !file_.seekSet(offset) || file_.read(output, length) != static_cast<int>(length)) {
      return fail("font read");
    }
    return true;
  }
  const Entry* glyph(uint32_t cp) {
    if (!healthy()) return nullptr;
    if (!file_) { fail("font closed"); return nullptr; }
    for (const auto& entry : cache_) if (entry.valid && entry.cp == cp) return &entry;
    uint32_t lo = 0, hi = intervalCount_;
    while (lo < hi) {
      const uint32_t mid = (lo + hi) / 2;
      if (intervals_[mid].last < cp) lo = mid + 1;
      else hi = mid;
    }
    if (lo == intervalCount_ || cp < intervals_[lo].first) return nullptr;
    const uint32_t index = intervals_[lo].offset + cp - intervals_[lo].first;
    uint8_t record[13];
    if (!readAt(glyphsOffset_ + index * 13, record, sizeof(record))) return nullptr;
    Entry& entry = cache_[nextSlot_];
    entry.valid = false;
    entry.cp = cp;
    entry.width = record[0]; entry.height = record[1]; entry.advance = record[2];
    entry.left = face_ == Face::Legacy20 ? bearing(record + 3) : static_cast<int16_t>(u16(record + 3));
    entry.top = face_ == Face::Legacy20 ? bearing(record + 5) : static_cast<int16_t>(u16(record + 5));
    const uint16_t length = u16(record + 7);
    const uint32_t offset = u32(record + 9);
    const int leftLimit = face_ == Face::Legacy20 ? 48 : 64;
    const int minTop = face_ == Face::Legacy20 ? -44 : -64;
    if (entry.width > profile_->maxWidth || entry.height > profile_->maxHeight || entry.advance > profile_->maxAdvance ||
        entry.left < -leftLimit || entry.left > leftLimit || entry.top < minTop || entry.top > profile_->ascender ||
        length != (entry.width * entry.height * profile_->bpp + 7) / 8 || length > BITMAP_BYTES ||
        offset > fileSize_ - bitmapOffset_ || length > fileSize_ - bitmapOffset_ - offset) {
      fail("font glyph");
      return nullptr;
    }
    if (length && !readAt(bitmapOffset_ + offset, entry.bitmap, length)) return nullptr;
    entry.valid = true;
    nextSlot_ = (nextSlot_ + 1) % 8;
    return &entry;
  }
};
static_assert(sizeof(TfFont) <= 8192, "TF font must fit the 8 KiB extra-memory budget");
}  // namespace xte
