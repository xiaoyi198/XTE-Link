#pragma once
#include "protocol.h"
#include "tf_font.h"

namespace xte {
struct ClashPanel {
  static constexpr uint8_t ROWS = 5;
  struct Row { char name[97] = {}; bool current = false; bool enabled = false; };
  Row rows[ROWS], staging[ROWS];
  char view[12] = "home", nextView[12] = {}, revision[17] = {}, nextRevision[17] = {};
  char message[97] = {};
  uint32_t sequence = 0, requestedAt = 0;
  uint16_t page = 0, pages = 1, nextPages = 1, nextPage = 0;
  uint8_t count = 0, selected = 0, received = 0, nextCount = 0;
  bool pending = false, complete = false, confirming = false;
  bool writeRequest = false, unseenResult = false;

  static bool copyText(JsonVariantConst value, char* out, size_t capacity) {
    if (!value.is<const char*>()) return false;
    const char* raw = value.as<const char*>();
    const size_t length = value.as<JsonString>().size();
    if (length >= capacity || strlen(raw) != length) return false;
    const char* cursor = raw;
    const char* end = raw + length;
    while (cursor != end) {
      const uint32_t cp = TfFont::nextCodepoint(cursor, end);
      if (cp < 32 || cp == 127 || cp == 0xfffd) return false;
    }
    memcpy(out, raw, length + 1);
    return true;
  }

  bool selectionWrites() const {
    return !strcmp(view, "nodes") || !strcmp(view, "profiles") ||
           (!strcmp(view, "home") && (selected == 2 || selected == 3));
  }

  bool begin(uint32_t now, bool writes = false) {
    if (pending) return false;
    if (++sequence == 0) ++sequence;
    requestedAt = now; pending = true; complete = false; confirming = false;
    writeRequest = writes; unseenResult = false;
    received = 0; nextCount = 0;
    nextRevision[0] = 0;
    strcpy(message, writes ? "正在执行，请等待结果" : "正在读取");
    return true;
  }

  bool expire(uint32_t now) {
    if (!pending || now - requestedAt < (writeRequest ? 70000u : 10000u)) return false;
    pending = false; complete = false;
    strcpy(message, writeRequest ? "结果待确认，请刷新" : "驱动未响应，请更新电脑驱动");
    return true;
  }

  bool accept(JsonDocument& doc) {
    if (!pending || !doc["id"].is<uint32_t>() || doc["id"].as<uint32_t>() != sequence) return false;
    const char* phase = doc["phase"] | "";
    if (!strcmp(phase, "error")) {
      char next[sizeof(message)];
      if (!copyText(doc["message"], next, sizeof(next))) return false;
      strcpy(message, next); pending = false; complete = false; return true;
    }
    if (!strcmp(phase, "begin")) {
      if (!doc["count"].is<uint8_t>() || doc["count"].as<uint8_t>() > ROWS ||
          !doc["pages"].is<uint16_t>() || !doc["pages"].as<uint16_t>() ||
          !doc["page"].is<uint16_t>() ||
          doc["page"].as<uint16_t>() >= doc["pages"].as<uint16_t>() ||
          !copyText(doc["view"], nextView, sizeof(nextView)) ||
          (strcmp(nextView, "home") && strcmp(nextView, "profiles") &&
           strcmp(nextView, "groups") && strcmp(nextView, "nodes")) ||
          !copyText(doc["rev"], nextRevision, sizeof(nextRevision)) || !nextRevision[0]) return false;
      nextCount = doc["count"]; nextPages = doc["pages"]; received = 0;
      nextPage = doc["page"];
      for (Row& row : staging) row = Row{};
      return true;
    }
    if (!strcmp(phase, "row")) {
      if (!nextRevision[0] || !doc["index"].is<uint8_t>()) return false;
      const uint8_t index = doc["index"];
      if (index >= nextCount || !doc["current"].is<bool>() || !doc["enabled"].is<bool>()) return false;
      Row row;
      if (!copyText(doc["name"], row.name, sizeof(row.name))) return false;
      row.current = doc["current"]; row.enabled = doc["enabled"];
      staging[index] = row; received |= static_cast<uint8_t>(1u << index); return true;
    }
    if (strcmp(phase, "end") || received != (1u << nextCount) - 1u ||
        !nextRevision[0] || !copyText(doc["message"], message, sizeof(message))) return false;
    memcpy(rows, staging, sizeof(rows));
    count = nextCount; pages = nextPages; selected = 0;
    page = nextPage; strcpy(view, nextView);
    strcpy(revision, nextRevision); nextRevision[0] = 0;
    pending = false; complete = true; return true;
  }
};
static_assert(sizeof(ClashPanel) < 2048, "Clash page memory budget");
}  // namespace xte
