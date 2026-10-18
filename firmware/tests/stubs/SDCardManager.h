#pragma once
#include <stdint.h>
#include <string.h>
#include <vector>
#include <map>
#include <WString.h>

namespace host {
inline std::vector<uint8_t> bytes;
inline bool mounted = true, present = true, directory = false;
inline int reads = 0, opens = 0;
inline int failAfter = -1;
inline int fontOpens = 0;
inline std::map<std::string, std::vector<uint8_t>> files;
}
constexpr int O_RDONLY = 0;
class FsFile {
 public:
  FsFile() = default;
  explicit FsFile(bool valid) : valid_(valid) { if (valid) ++host::opens; }
  explicit FsFile(std::vector<uint8_t>* data, bool font = false) : valid_(data != nullptr), data_(data), font_(font) {
    if (valid_) { ++host::opens; if (font_) ++host::fontOpens; }
  }
  explicit operator bool() const { return valid_; }
  bool isDirectory() const { return host::directory; }
  uint64_t fileSize() const { return data_ ? data_->size() : host::bytes.size(); }
  void close() { if (valid_) { --host::opens; if (font_) --host::fontOpens; } valid_ = false; }
  bool seekSet(uint32_t offset) { position_ = offset; return valid_; }
  int read(void* target, size_t length) {
    const auto& bytes = data_ ? *data_ : host::bytes;
    if (!valid_ || position_ > bytes.size() || length > bytes.size() - position_ ||
        (host::failAfter >= 0 && host::reads >= host::failAfter)) return -1;
    ++host::reads;
    memcpy(target, bytes.data() + position_, length);
    position_ += length;
    return static_cast<int>(length);
  }
 private:
  bool valid_ = false;
  size_t position_ = 0;
  std::vector<uint8_t>* data_ = nullptr;
  bool font_ = false;
};
class SDCardManager {
 public:
  bool ready() const { return host::mounted; }
  FsFile open(const char* path, int = O_RDONLY) {
    if (!host::mounted) return {};
    if (host::present && !strcmp(path, "/fonts/Zheng20号.epdfont")) return FsFile(&host::bytes, true);
    const auto found = host::files.find(path);
    return found == host::files.end() ? FsFile{} : FsFile(&found->second, !strncmp(path, "/fonts/", 7));
  }
  bool exists(const char* path) { return host::mounted && host::files.count(path); }
  std::vector<String> listFiles(const char* folder, int limit = 200) {
    std::vector<String> names;
    if (!host::mounted) return names;
    const std::string prefix = std::string(folder) + "/";
    for (const auto& pair : host::files) {
      if (pair.first.find(prefix) == 0 && names.size() < static_cast<size_t>(limit))
        names.emplace_back(pair.first.substr(prefix.size()));
    }
    return names;
  }
  static SDCardManager& getInstance() { static SDCardManager manager; return manager; }
};
#define SdMan SDCardManager::getInstance()
