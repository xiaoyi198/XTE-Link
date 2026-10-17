#pragma once

#include <Preferences.h>
#include <stdint.h>
#include <string.h>

namespace xte {
enum class Page : uint8_t { STATUS, VOLUME, AGENTS, SETTINGS, CLASH };

// One byte-only, versioned blob commits preferences and the fixed image together.
// The old "settings" word is read for migration and never overwritten: old firmware
// can still load its preferences after a rollback.
struct Settings {
  uint8_t version = 2;
  uint8_t volumeIndex = 1;
  uint8_t intervalIndex = 0;
  uint8_t startupPage = 0;
  uint8_t startupMode = 0;  // Menu / selected app / last used app.
  uint8_t ghostIndex = 0;
  uint8_t imageMode = 0;    // Random / fixed / sequential.
  uint8_t reminderMode = 1; // Off / badge / switch to assistant.
  uint8_t sleepTimeoutIndex = 0;
  char sleepFile[80] = {};

  static bool imagePathValid(const char* path) {
    if (strnlen(path, 80) == 80) return false;
    if (!strcmp(path, "/sleep.bmp")) return true;
    if (strncmp(path, "/.sleep/", 8)) return false;
    const char* name = path + 8;
    const size_t n = strlen(name);
    if (n < 5 || n > 63 || name[0] == '.' ||
        (strcmp(name + n - 4, ".bmp") && strcmp(name + n - 4, ".BMP"))) return false;
    for (const char* p = name; *p; ++p)
      if (static_cast<uint8_t>(*p) < 32 || *p == '/' || *p == '\\') return false;
    return true;
  }
  bool valid() const {
    return version == 2 && volumeIndex < 3 && intervalIndex < 3 && startupPage < 5 &&
           startupMode < 3 && ghostIndex < 4 && imageMode < 3 && reminderMode < 3 &&
           sleepTimeoutIndex < 4 && (imageMode != 1 || sleepFile[0]) &&
           (!sleepFile[0] || imagePathValid(sleepFile));
  }
  bool sameAs(const Settings& other) const { return !memcmp(this, &other, sizeof(Settings)); }
  int volumeStep() const {
    constexpr int values[] = {1, 5, 10};
    return values[volumeIndex];
  }
  uint32_t updateMs() const {
    constexpr uint32_t values[] = {5000, 10000, 30000};
    return values[intervalIndex];
  }
  uint16_t ghostEvery() const {
    constexpr uint16_t values[] = {0, 20, 50, 100};
    return values[ghostIndex];
  }
  uint32_t sleepMs() const {
    constexpr uint32_t values[] = {0, 5 * 60000, 15 * 60000, 30 * 60000};
    return values[sleepTimeoutIndex];
  }
  bool decodeLegacy(uint32_t value) {
    Settings next;
    next.volumeIndex = value & 0xff;
    next.intervalIndex = (value >> 8) & 0xff;
    next.startupPage = (value >> 16) & 0xff;
    const bool ok = (value >> 24) == 1 && next.startupPage < 3 && next.valid();
    *this = ok ? next : Settings{};
    return ok;
  }
  bool load(bool* legacy = nullptr) {
    if (legacy) *legacy = false;
    *this = Settings{};
    Preferences prefs;
    if (!prefs.begin("xte-link", true)) return false;
    Settings next;
    bool ok;
    if (prefs.isKey("settings2")) {
      ok = prefs.getBytesLength("settings2") == sizeof(next) &&
           prefs.getBytes("settings2", &next, sizeof(next)) == sizeof(next) && next.valid();
      *this = ok ? next : Settings{};
    } else {
      if (legacy) *legacy = true;
      ok = decodeLegacy(prefs.getUInt("settings", 0x01000001u));
    }
    prefs.end();
    return ok;
  }
  bool save() const {
    if (!valid()) return false;
    Preferences prefs;
    if (!prefs.begin("xte-link", false)) return false;
    const bool ok = prefs.putBytes("settings2", this, sizeof(*this)) == sizeof(*this);
    prefs.end();
    return ok;
  }
};
static_assert(sizeof(Settings) == 89, "NVS settings layout must not contain padding");
}  // namespace xte
