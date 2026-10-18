#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdexcept>
#include <vector>
#include <map>
#include <WString.h>

namespace nvs_host {
inline bool present = false, failOpen = false, failWrite = false;
inline uint32_t value = 0;
inline unsigned writes = 0;
inline int opens = 0;
inline std::vector<uint8_t> blob;
inline int lastApp = -1;
inline std::map<std::string, String> strings;
}
// Test-only single-key store, with open/write fault injection.
class Preferences {
 public:
  bool begin(const char* name, bool readOnly = false) {
    if (strcmp(name, "xte-link")) throw std::runtime_error("unexpected NVS namespace");
    if (nvs_host::failOpen || (readOnly && !nvs_host::present && nvs_host::blob.empty() &&
        nvs_host::lastApp < 0 && nvs_host::strings.empty())) return false;
    readOnly_ = readOnly;
    open_ = true;
    ++nvs_host::opens;
    return true;
  }
  void end() { if (open_) --nvs_host::opens; open_ = false; }
  uint32_t getUInt(const char* key, uint32_t fallback) {
    check(key);
    return nvs_host::present ? nvs_host::value : fallback;
  }
  size_t putUInt(const char* key, uint32_t value) {
    check(key);
    if (readOnly_ || nvs_host::failWrite) return 0;
    nvs_host::value = value;
    nvs_host::present = true;
    ++nvs_host::writes;
    return sizeof(uint32_t);
  }
  bool isKey(const char* key) { check(key); return !strcmp(key, "settings2") && !nvs_host::blob.empty(); }
  size_t getBytesLength(const char* key) { check(key); return nvs_host::blob.size(); }
  size_t getBytes(const char* key, void* out, size_t count) {
    check(key);
    if (count != nvs_host::blob.size()) return 0;
    memcpy(out, nvs_host::blob.data(), count); return count;
  }
  size_t putBytes(const char* key, const void* value, size_t count) {
    check(key);
    if (readOnly_ || nvs_host::failWrite) return 0;
    const auto* start = static_cast<const uint8_t*>(value);
    nvs_host::blob.assign(start, start + count); ++nvs_host::writes; return count;
  }
  uint8_t getUChar(const char* key, uint8_t fallback = 0) {
    check(key); return nvs_host::lastApp < 0 ? fallback : static_cast<uint8_t>(nvs_host::lastApp);
  }
  size_t putUChar(const char* key, uint8_t value) {
    check(key);
    if (readOnly_ || nvs_host::failWrite) return 0;
    nvs_host::lastApp = value; ++nvs_host::writes; return 1;
  }
  String getString(const char* key, String fallback = String()) {
    check(key); const auto found = nvs_host::strings.find(key);
    return found == nvs_host::strings.end() ? fallback : found->second;
  }
  size_t putString(const char* key, const String& value) {
    check(key);
    if (readOnly_ || nvs_host::failWrite) return 0;
    nvs_host::strings[key] = value; ++nvs_host::writes; return value.length();
  }
 private:
  bool open_ = false, readOnly_ = false;
  void check(const char* key) {
    if (!open_ || (strcmp(key, "settings") && strcmp(key, "settings2") && strcmp(key, "lastApp") &&
                  strcmp(key, "sleepLast"))) throw std::runtime_error("unexpected NVS key/access");
  }
};
