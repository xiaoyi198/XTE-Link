#pragma once

#include <Preferences.h>
#include <stdint.h>

namespace xte {
enum class Page : uint8_t { STATUS, VOLUME, AGENTS, SETTINGS };

// Only the three editable preferences are persisted, as one versioned NVS value.
struct Settings {
  uint8_t volumeIndex = 1;
  uint8_t intervalIndex = 0;
  uint8_t startupPage = 0;

  bool valid() const { return volumeIndex < 3 && intervalIndex < 3 && startupPage < 3; }
  int volumeStep() const {
    constexpr int values[] = {1, 5, 10};
    return values[volumeIndex];
  }
  uint32_t updateMs() const {
    constexpr uint32_t values[] = {5000, 10000, 30000};
    return values[intervalIndex];
  }
  uint32_t encode() const {
    return 0x01000000u | (uint32_t(startupPage) << 16) |
           (uint32_t(intervalIndex) << 8) | volumeIndex;
  }
  bool decode(uint32_t value) {
    Settings next;
    next.volumeIndex = value & 0xff;
    next.intervalIndex = (value >> 8) & 0xff;
    next.startupPage = (value >> 16) & 0xff;
    const bool ok = (value >> 24) == 1 && next.valid();
    *this = ok ? next : Settings{};
    return ok;
  }
  bool load() {
    *this = Settings{};
    Preferences prefs;
    if (!prefs.begin("xte-link", true)) return false;
    const uint32_t value = prefs.getUInt("settings", encode());
    prefs.end();
    return decode(value);
  }
  bool save() const {
    if (!valid()) return false;
    Preferences prefs;
    if (!prefs.begin("xte-link", false)) return false;
    const bool ok = prefs.putUInt("settings", encode()) == sizeof(uint32_t);
    prefs.end();
    return ok;
  }
};
}  // namespace xte
