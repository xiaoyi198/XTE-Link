#pragma once

#include <SDCardManager.h>
#include <algorithm>
#include <mbedtls/sha256.h>
#include "protocol.h"

namespace xte {
// All calls run on the Arduino loop task, together with display SPI access.
class TfTransfer {
 public:
  static constexpr size_t CHUNK = 128;
  bool active() const { return mode != Mode::NONE; }
  void tick() {
    if (active() && millis() - touched >= 30000) abort();
  }
  void abort() {
    file.close();
    if (mode == Mode::WRITE) SdMan.remove(temporary);
    if (hashReady) {
      mbedtls_sha256_free(&hash);
      hashReady = false;
    }
    mode = Mode::NONE;
  }

  bool handle(char* line, size_t length) {
    if (!length || memchr(line, '\0', length)) return false;
    StaticJsonDocument<768> request;
    JsonReader reader{line, length};
    if (deserializeJson(request, reader, DeserializationOption::NestingLimit(2))) return false;
    if (request["cmd"] != "tf") return false;
    StaticJsonDocument<768> response;
    response["id"] = request["id"];
    response["ok"] = false;
    while (reader.position < length) {
      const char ch = line[reader.position++];
      if (ch != ' ' && ch != '\t' && ch != '\r') {
        response["error"] = "bad_request";
        reply(response);
        return true;
      }
    }
    const char* op = request["op"];
    if (request["v"] != 2 || !request["id"].is<uint32_t>() || !op) {
      response["error"] = "bad_request";
    } else {
      touched = millis();
      const char* error = execute(op, request, response);
      response["ok"] = error == nullptr;
      if (error) response["error"] = error;
    }
    reply(response);
    return true;
  }

 private:
  enum class Mode { NONE, LIST, READ, WRITE };
  Mode mode = Mode::NONE;
  FsFile file;
  uint32_t touched = 0, offset = 0, total = 0;
  char target[97] = {}, temporary[110] = {}, expected[65] = {};
  mbedtls_sha256_context hash = {};
  bool hashReady = false;

  static void reply(const JsonDocument& doc) {
    Serial.print("XTE: tf ");
    serializeJson(doc, Serial);
    Serial.println();
  }
  static bool pathValid(JsonVariantConst value) {
    const char* path = value.as<const char*>();
    if (!path || path[0] != '/' || strlen(path) > 96 ||
        value.as<JsonString>().size() != strlen(path)) return false;
    for (const char* p = path; *p; ++p) {
      if (static_cast<uint8_t>(*p) < 32 || *p == '\\' || *p == ':' || *p == '"' ||
          *p == '*' || *p == '?' || *p == '<' || *p == '>' || *p == '|') return false;
    }
    if (strstr(path, "//")) return false;
    for (const char* p = path + 1; *p;) {
      const char* end = strchr(p, '/');
      const size_t n = end ? static_cast<size_t>(end - p) : strlen(p);
      if ((n == 1 && p[0] == '.') || (n == 2 && p[0] == '.' && p[1] == '.')) return false;
      p += n;
      if (*p) ++p;
    }
    return strlen(path) == 1 || path[strlen(path) - 1] != '/';
  }
  static int nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
  }
  static void hex(const uint8_t* bytes, size_t count, char* out) {
    constexpr char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < count; ++i) {
      out[i * 2] = digits[bytes[i] >> 4];
      out[i * 2 + 1] = digits[bytes[i] & 15];
    }
    out[count * 2] = 0;
  }
  void startHash() {
    mbedtls_sha256_init(&hash);
    mbedtls_sha256_starts_ret(&hash, 0);
    hashReady = true;
    offset = 0;
  }
  void finishHash(char* out) {
    uint8_t digest[32];
    mbedtls_sha256_finish_ret(&hash, digest);
    mbedtls_sha256_free(&hash);
    hashReady = false;
    hex(digest, sizeof(digest), out);
  }

  const char* execute(const char* op, JsonDocument& req, JsonDocument& res) {
    if (!strcmp(op, "abort")) { abort(); return nullptr; }
    if (!strcmp(op, "mount")) {
      if (active()) return "busy";
      res["ready"] = SdMan.begin();
      res["chunk"] = CHUNK;
      return SdMan.ready() ? nullptr : "no_card";
    }
    if (!strcmp(op, "format")) {
      if (active()) return "busy";
      return SdMan.format() ? nullptr : "format_failed";
    }
    if (!SdMan.ready()) return "no_card";
    if (!strcmp(op, "write") || !strcmp(op, "read")) {
      const Mode required = !strcmp(op, "write") ? Mode::WRITE : Mode::READ;
      if (mode != required) return "no_session";
      if (!req["offset"].is<uint32_t>() || req["offset"].as<uint32_t>() != offset) return "bad_offset";
      uint8_t bytes[CHUNK];
      if (mode == Mode::WRITE) {
        const char* data = req["data"];
        if (!data || req["data"].as<JsonString>().size() != strlen(data)) return "bad_data";
        const size_t length = strlen(data);
        if (!length || length % 2 || length > CHUNK * 2 || length / 2 > total - offset) return "bad_data";
        for (size_t i = 0; i < length / 2; ++i) {
          const int a = nibble(data[i * 2]), b = nibble(data[i * 2 + 1]);
          if (a < 0 || b < 0) return "bad_data";
          bytes[i] = (a << 4) | b;
        }
        if (file.write(bytes, length / 2) != length / 2) { abort(); return "write_failed"; }
        mbedtls_sha256_update_ret(&hash, bytes, length / 2);
        offset += length / 2;
      } else {
        const size_t want = std::min(static_cast<uint32_t>(CHUNK), total - offset);
        const int count = file.read(bytes, want);
        if (count < 0 || static_cast<size_t>(count) != want) { abort(); return "read_failed"; }
        char data[CHUNK * 2 + 1];
        hex(bytes, want, data);
        res["data"] = data;
        mbedtls_sha256_update_ret(&hash, bytes, want);
        offset += want;
      }
      res["offset"] = offset;
      return nullptr;
    }
    if (!strcmp(op, "finish")) {
      if (mode != Mode::READ && mode != Mode::WRITE) return "no_session";
      if (offset != total) return "incomplete";
      char digest[65];
      finishHash(digest);
      res["sha256"] = digest;
      if (mode == Mode::WRITE) {
        if (strcmp(digest, expected)) { abort(); return "hash_mismatch"; }
        if (!file.sync()) { abort(); return "sync_failed"; }
        file.close();
        if (SdMan.exists(target) || !SdMan.rename(temporary, target)) { abort(); return "commit_failed"; }
      }
      file.close();
      mode = Mode::NONE;
      return nullptr;
    }
    if (!strcmp(op, "list_next")) {
      if (mode != Mode::LIST) return "no_session";
      FsFile entry = file.openNextFile();
      if (!entry) {
        const bool failed = file.getError();
        abort();
        res["eof"] = true;
        return failed ? "read_failed" : nullptr;
      }
      char name[256];
      if (!entry.getName(name, sizeof(name))) { entry.close(); return "name_failed"; }
      res["name"] = name;
      res["dir"] = entry.isDirectory();
      res["size"] = entry.fileSize();
      entry.close();
      return nullptr;
    }
    if (active()) return "busy";
    if (!pathValid(req["path"])) return "bad_path";
    const char* path = req["path"];
    if (!strcmp(op, "mkdir")) return SdMan.ensureDirectoryExists(path) ? nullptr : "mkdir_failed";
    if (!strcmp(op, "remove")) {
      FsFile entry = SdMan.open(path);
      if (!entry || entry.isDirectory()) { entry.close(); return "not_file"; }
      entry.close();
      return SdMan.remove(path) ? nullptr : "remove_failed";
    }
    if (!strcmp(op, "list") || !strcmp(op, "get")) {
      file = SdMan.open(path);
      if (!file) return "not_found";
      const bool listing = !strcmp(op, "list");
      if (file.isDirectory() != listing || (!listing && file.fileSize() > UINT32_MAX)) {
        file.close(); return "wrong_type_or_size";
      }
      mode = listing ? Mode::LIST : Mode::READ;
      if (!listing) {
        total = file.fileSize();
        startHash();
        res["size"] = total;
      }
      return nullptr;
    }
    if (!strcmp(op, "put")) {
      if (strstr(path, ".xte-part")) return "reserved_path";
      if (!req["size"].is<uint32_t>()) return "bad_size";
      const char* digest = req["sha256"];
      if (!digest || strlen(digest) != 64 || req["sha256"].as<JsonString>().size() != 64) return "bad_hash";
      for (size_t i = 0; i < 64; ++i) if (nibble(digest[i]) < 0) return "bad_hash";
      if (!strcmp(path, "/") || SdMan.exists(path)) return "exists";
      strcpy(target, path);
      snprintf(temporary, sizeof(temporary), "%s.xte-part", path);
      file = SdMan.open(temporary, O_WRONLY | O_CREAT | O_EXCL);
      if (!file) return "temporary_exists_or_open_failed";
      strcpy(expected, digest);
      total = req["size"];
      mode = Mode::WRITE;
      startHash();
      return nullptr;
    }
    return "unknown_op";
  }
};
}
