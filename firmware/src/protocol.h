#pragma once

#include <ArduinoJson.h>
#include <cmath>
#include <cstring>

namespace xte {
constexpr size_t MAX_PAYLOAD_BYTES = 384;
constexpr size_t MAX_AGENTS = 2;

struct Agent {
  char tool[8] = {};
  char state[8] = {};
  char requestId[37] = {};
  char text[49] = {};
};

struct Status {
  char host[33] = {};
  char time[20] = {};
  float cpu = NAN;
  float memoryUsed = NAN;
  float memoryTotal = NAN;
  float gpu = NAN;
  float gpuTemp = NAN;
  float volume = NAN;
  uint32_t volumeAck = 0;
  Agent agents[MAX_AGENTS] = {};
  uint8_t agentCount = 0;
  bool hasAgents = false;
};

struct JsonReader {
  JsonReader(const char* input, size_t size) : data(input), length(size) {}
  const char* data;
  size_t length;
  size_t position = 0;
  int read() { return position < length ? static_cast<unsigned char>(data[position++]) : -1; }
  size_t readBytes(char* output, size_t count) {
    const size_t remaining = length - position;
    if (count > remaining) count = remaining;
    memcpy(output, data + position, count);
    position += count;
    return count;
  }
};

inline bool readMetric(JsonVariantConst value, float& result, float maximum, float minimum = 0) {
  if (value.isNull()) {
    result = NAN;
    return true;
  }
  if (!value.is<float>()) return false;
  result = value.as<float>();
  return std::isfinite(result) && result >= minimum && result <= maximum;
}

inline bool readAgentText(JsonVariantConst value, char* output, size_t capacity, bool required) {
  if (value.isNull()) {
    if (!required) {
      output[0] = '\0';
      return true;
    }
    return false;
  }
  const char* text = value.as<const char*>();
  if (!text) return false;
  if (strlen(text) >= capacity || value.as<JsonString>().size() != strlen(text)) return false;
  for (const char* ch = text; *ch; ++ch) {
    if (*ch < 32 || *ch > 126) return false;
  }
  strcpy(output, text);
  return true;
}

inline bool readAgents(JsonVariantConst value, Agent* output, uint8_t& count) {
  JsonArrayConst agents = value.as<JsonArrayConst>();
  if (agents.isNull() || agents.size() > MAX_AGENTS) return false;
  count = 0;
  for (JsonObjectConst source : agents) {
    Agent& target = output[count];
    if (!readAgentText(source["tool"], target.tool, sizeof(target.tool), true) ||
        !readAgentText(source["state"], target.state, sizeof(target.state), true) ||
        !readAgentText(source["id"], target.requestId, sizeof(target.requestId), false) ||
        !readAgentText(source["text"], target.text, sizeof(target.text), false)) return false;
    if (strcmp(target.tool, "dsh") && strcmp(target.tool, "codex")) return false;
    bool known = false;
    for (const char* state : {"offline", "idle", "running", "ask", "done", "error"})
      if (!strcmp(target.state, state)) known = true;
    if (!known || (!strcmp(target.state, "ask") && !target.requestId[0])) return false;
    for (uint8_t i = 0; i < count; ++i) if (!strcmp(output[i].tool, target.tool)) return false;
    ++count;
  }
  return true;
}

inline bool readControl(char* payload, size_t length, JsonDocument& doc) {
  if (!length || length > MAX_PAYLOAD_BYTES || memchr(payload, '\0', length)) return false;
  JsonReader reader{payload, length};
  if (deserializeJson(doc, reader, DeserializationOption::NestingLimit(4))) return false;
  while (reader.position < length) {
    const char ch = payload[reader.position++];
    if (ch != ' ' && ch != '\t' && ch != '\r') return false;
  }
  return doc.is<JsonObject>() && doc["v"].is<int>() && doc["v"].as<int>() == 2 &&
         doc["cmd"].is<const char*>();
}

inline bool parseStatus(char* payload, size_t length, Status& result) {
  if (length == 0 || length > MAX_PAYLOAD_BYTES || memchr(payload, '\0', length)) return false;
  // Agent entries add two bounded objects to the v2 snapshot. Keep the
  // document on the stack; the ESP32-C3 has ample heap but limited PSRAM.
  StaticJsonDocument<1024> doc;
  JsonReader reader{payload, length};
  if (deserializeJson(doc, reader, DeserializationOption::NestingLimit(4))) return false;
  while (reader.position < length) {
    const char ch = payload[reader.position++];
    if (ch != ' ' && ch != '\t' && ch != '\r') return false;
  }
  if (!doc.is<JsonObject>() || !doc["v"].is<int>() || doc["v"].as<int>() != 2) return false;
  for (const char* key : {"host", "time", "cpu", "cpu_temp_c", "gpu", "gpu_temp_c", "mem_used_mib", "mem_total_mib"}) {
    if (!doc.containsKey(key)) return false;
  }
  const char* host = doc["host"];
  const char* stamp = doc["time"];
  if (!host || strlen(host) == 0 || strlen(host) > 32 || !stamp || strlen(stamp) != 19) return false;
  if (doc["host"].as<JsonString>().size() != strlen(host) || doc["time"].as<JsonString>().size() != 19) return false;
  for (const char* ch = host; *ch; ++ch) {
    if (*ch < 32 || *ch > 126) return false;
  }
  for (size_t i = 0; i < 19; ++i) {
    const char separator = (i == 4 || i == 7) ? '-' : i == 10 ? ' ' : (i == 13 || i == 16) ? ':' : '\0';
    if (separator ? stamp[i] != separator : (stamp[i] < '0' || stamp[i] > '9')) return false;
  }
  Status candidate;
  if (doc.containsKey("volume_ack")) {
    if (!doc["volume_ack"].is<uint32_t>()) return false;
    candidate.volumeAck = doc["volume_ack"].as<uint32_t>();
  }
  float ignoredCpuTemp = NAN;
  if (!readMetric(doc["cpu"], candidate.cpu, 100) ||
      !readMetric(doc["cpu_temp_c"], ignoredCpuTemp, 150, -50) ||
      !readMetric(doc["gpu"], candidate.gpu, 100) ||
      !readMetric(doc["gpu_temp_c"], candidate.gpuTemp, 150, -50) ||
      !readMetric(doc["mem_used_mib"], candidate.memoryUsed, 1000000) ||
      !readMetric(doc["mem_total_mib"], candidate.memoryTotal, 1000000) ||
      !readMetric(doc["volume"], candidate.volume, 100)) return false;
  if (std::isfinite(candidate.memoryTotal) && candidate.memoryTotal <= 0) return false;
  if (std::isfinite(candidate.memoryUsed) && std::isfinite(candidate.memoryTotal) &&
      candidate.memoryUsed > candidate.memoryTotal) return false;
  if (doc.containsKey("agents")) {
    candidate.hasAgents = true;
    if (!readAgents(doc["agents"], candidate.agents, candidate.agentCount)) return false;
  }
  strcpy(candidate.host, host);
  strcpy(candidate.time, stamp);
  result = candidate;
  return true;
}

class LineReceiver {
 public:
  void reset() { length = 0; overflow = false; }
  bool push(char ch, Status& result, bool (*handleLine)(char*, size_t) = nullptr) {
    if (ch == '\n') {
      const bool handled = !overflow && handleLine && handleLine(buffer, length);
      const bool valid = !overflow && !handled && parseStatus(buffer, length, result);
      length = 0;
      overflow = false;
      return valid;
    }
    if (overflow) return false;
    if (length == MAX_PAYLOAD_BYTES) {
      overflow = true;
      return false;
    }
    buffer[length++] = ch;
    return false;
  }

 private:
  char buffer[MAX_PAYLOAD_BYTES + 1] = {};
  size_t length = 0;
  bool overflow = false;
};
}
