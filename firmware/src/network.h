#pragma once

#include <ESPmDNS.h>
#include <Preferences.h>
#include <SDCardManager.h>
#include <WiFi.h>
#include <esp_system.h>
#include <mbedtls/md.h>
#include "protocol.h"

namespace xte {
// Only transport/configuration lives here. SD and display access stay on loop().
class Network {
 public:
  static constexpr uint16_t PORT = 8765;
  static constexpr const char* PATH = "/xte-link/network.json";
  static constexpr const char* NEXT = "/xte-link/network.next.json";
  static constexpr const char* OLD = "/xte-link/network.previous.json";

  void begin() {
    snprintf(device, sizeof(device), "%012llx", ESP.getEfuseMac() & 0xffffffffffffULL);
    Preferences prefs;
    if (prefs.begin("xte-link", true)) {
      lan = prefs.getBool("lan", false);
      prefs.end();
    }
    // A power loss between renames leaves the previous file recoverable.
    if (SdMan.ready() && !SdMan.exists(PATH) && SdMan.exists(OLD)) SdMan.rename(OLD, PATH);
    configured = readConfig(PATH, config);
    if (!configured && SdMan.ready() && SdMan.exists(OLD)) configured = readConfig(OLD, config);
    WiFi.persistent(false);  // TF is the sole source of WiFi credentials.
    if (lan && configured) startWiFi();
  }

  bool isLan() const { return lan; }
  bool ready() const { return configured; }
  const char* id() const { return device; }
  const char* ssid() const { return configured ? config.ssid : ""; }
  void reconnect() {
    stop();
    if (lan && configured) startWiFi();
  }
  const char* state() const {
    if (!lan) return "usb";
    if (!configured) return "no_config";
    if (WiFi.status() != WL_CONNECTED) return "connecting";
    return authenticated ? "connected" : "waiting_driver";
  }

  bool setMode(bool next) {
    if (next && !configured) return false;
    if (next == lan) return true;
    Preferences prefs;
    if (!prefs.begin("xte-link", false)) return false;
    const bool saved = prefs.putBool("lan", next) == 1;
    prefs.end();
    if (!saved) return false;
    stop();
    lan = next;
    if (lan) startWiFi();
    return true;
  }

  const char* commitConfig() {
    Config next;
    if (!readConfig(NEXT, next)) return "invalid_config_or_no_card";
    if (SdMan.exists(OLD) && !SdMan.remove(OLD)) return "previous_remove_failed";
    const bool existed = SdMan.exists(PATH);
    if (existed && !SdMan.rename(PATH, OLD)) return "previous_rename_failed";
    if (!SdMan.rename(NEXT, PATH)) {
      if (existed) SdMan.rename(OLD, PATH);
      return "commit_failed";
    }
    stop();
    config = next;
    configured = true;
    // Failure to remove the previous copy is harmless; a later commit can retry.
    if (existed) SdMan.remove(OLD);
    if (lan) startWiFi();
    return nullptr;
  }

  void info(uint32_t requestId = 0, const char* error = nullptr, const char* firmware = nullptr) const {
    StaticJsonDocument<512> doc;
    doc["device"] = device;
    doc["mode"] = lan ? "lan" : "usb";
    doc["configured"] = configured;
    doc["events"] = 1;
    if (firmware) doc["firmware"] = firmware;
    doc["state"] = state();
    doc["ip"] = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : String("");
    doc["heap"] = ESP.getFreeHeap();
    doc["id"] = requestId;
    doc["ok"] = !error;
    if (error) doc["error"] = error;
    Serial.print("XTE: link ");
    serializeJson(doc, Serial);
    Serial.println();
  }

  void stop() {
    dropPeer();
    if (serving) {
      MDNS.end();
      server.end();
      serving = false;
    }
    WiFi.disconnect(true, false);
  }

  bool send(const char* line) {
    if (!authenticated || !peer.connected()) return false;
    const size_t size = strlen(line);
    if (!size || size > MAX_PAYLOAD_BYTES + 1 || tx == UINT32_MAX) { dropPeer(); return false; }
    uint8_t packet[6 + MAX_PAYLOAD_BYTES + 1 + 32];
    const uint32_t sequence = ++tx;
    for (uint8_t i = 0; i < 4; ++i) packet[i] = sequence >> (24 - 8 * i);
    packet[4] = size >> 8;
    packet[5] = size;
    memcpy(packet + 6, line, size);
    if (!sign('S', packet, size + 6, packet + size + 6) ||
        peer.write(packet, size + 38) != size + 38) { dropPeer(); return false; }
    return true;
  }

  void tick(void (*receive)(char*, size_t)) {
    if (!lan || !configured) return;
    const uint32_t now = millis();
    if (WiFi.status() != WL_CONNECTED) {
      if (serving) {
        dropPeer(); MDNS.end(); server.end(); serving = false;
      }
      if (now - attempted >= 30000) startWiFi();
      return;
    }
    if (!serving) {
      server.begin();
      server.setNoDelay(true);
      char hostname[24];
      snprintf(hostname, sizeof(hostname), "xte-%s", device);
      if (MDNS.begin(hostname)) {
        MDNS.addService("xte-link", "tcp", PORT);
        MDNS.addServiceTxt("xte-link", "tcp", "device", static_cast<const char*>(device));
      }
      serving = true;
    }
    if (havePeer && (!peer.connected() || now - touched >= (authenticated ? 15000u : 5000u))) dropPeer();
    if (server.hasClient()) {
      WiFiClient incoming = server.available();
      if (havePeer) incoming.stop();  // Never replace an active paired driver.
      else {
        peer = incoming;
        havePeer = true;
        peer.setTimeout(1);
        peer.setNoDelay(true);
        uint8_t random[16];
        esp_fill_random(random, sizeof(random));
        hex(random, sizeof(random), nonce);
        char challenge[128];
        snprintf(challenge, sizeof(challenge), "{\"device\":\"%s\",\"nonce\":\"%s\"}\n", device, nonce);
        touched = now;
        if (peer.print(challenge) != strlen(challenge)) dropPeer();
      }
    }
    for (size_t budget = 0; budget < 512 && havePeer && peer.available(); ++budget) {
      const int ch = peer.read();
      if (ch < 0) break;
      if (!authenticated) {
        if (ch == '\n') { authenticate(); used = 0; }
        else if (used < MAX_PAYLOAD_BYTES) packet[used++] = ch;
        else dropPeer();
        continue;
      }
      packet[used++] = ch;
      if (used == 6) {
        const size_t size = (size_t(packet[4]) << 8) | packet[5];
        if (!size || size > MAX_PAYLOAD_BYTES + 1) { dropPeer(); continue; }
        needed = size + 38;
      }
      if (needed && used == needed) {
        const size_t size = needed - 38;
        const uint32_t sequence = (uint32_t(packet[0]) << 24) | (uint32_t(packet[1]) << 16) |
                                  (uint32_t(packet[2]) << 8) | packet[3];
        uint8_t expected[32];
        if (rx == UINT32_MAX || sequence != rx + 1 || packet[5 + size] != '\n' ||
            !sign('C', packet, size + 6, expected) || !equal(expected, packet + size + 6, 32)) {
          dropPeer(); continue;
        }
        rx = sequence;
        touched = now;
        receive(reinterpret_cast<char*>(packet + 6), size - 1);
        used = needed = 0;
      }
    }
  }

 private:
  struct Config { char ssid[33] = {}, password[65] = {}; uint8_t key[32] = {}; } config;
  WiFiServer server{PORT, 1};
  WiFiClient peer;
  bool lan = false, configured = false, serving = false, authenticated = false, havePeer = false;
  char device[13] = {}, nonce[33] = {};
  uint32_t attempted = 0, touched = 0, tx = 0, rx = 0;
  uint8_t packet[6 + MAX_PAYLOAD_BYTES + 1 + 32] = {};
  size_t used = 0, needed = 0;

  static int nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
  }
  static bool unhex(const char* value, uint8_t* out, size_t size) {
    if (!value || strlen(value) != size * 2) return false;
    for (size_t i = 0; i < size; ++i) {
      const int a = nibble(value[i * 2]), b = nibble(value[i * 2 + 1]);
      if (a < 0 || b < 0) return false;
      out[i] = (a << 4) | b;
    }
    return true;
  }
  static void hex(const uint8_t* data, size_t size, char* out) {
    constexpr char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < size; ++i) { out[2 * i] = digits[data[i] >> 4]; out[2 * i + 1] = digits[data[i] & 15]; }
    out[size * 2] = 0;
  }
  static bool equal(const uint8_t* a, const uint8_t* b, size_t size) {
    uint8_t difference = 0;
    for (size_t i = 0; i < size; ++i) difference |= a[i] ^ b[i];
    return difference == 0;
  }
  bool readConfig(const char* path, Config& out) {
    if (!SdMan.ready()) return false;
    FsFile file = SdMan.open(path);
    if (!file || file.isDirectory() || file.fileSize() > 512) { file.close(); return false; }
    char buffer[513];
    const int length = file.read(buffer, 512);
    file.close();
    if (length <= 0 || memchr(buffer, 0, length)) return false;
    StaticJsonDocument<768> doc;
    JsonReader reader{buffer, static_cast<size_t>(length)};
    if (deserializeJson(doc, reader, DeserializationOption::NestingLimit(2))) return false;
    while (reader.position < reader.length) {
      const char ch = reader.data[reader.position++];
      if (ch != ' ' && ch != '\n' && ch != '\r' && ch != '\t') return false;
    }
    const char* ssid = doc["ssid"], *password = doc["password"], *id = doc["device"], *key = doc["key"];
    if (!doc["v"].is<int>() || doc["v"] != 1 || !ssid || !password || !id || strcmp(id, device) ||
        doc["device"].as<JsonString>().size() != 12 || !key || doc["key"].as<JsonString>().size() != 64 ||
        !unhex(key, out.key, 32)) return false;
    const size_t s = strlen(ssid), p = strlen(password);
    if (!s || s > 32 || (p && (p < 8 || p > 64)) ||
        doc["ssid"].as<JsonString>().size() != s || doc["password"].as<JsonString>().size() != p) return false;
    for (const char* ch = ssid; *ch; ++ch) if (static_cast<uint8_t>(*ch) < 32) return false;
    for (const char* ch = password; *ch; ++ch) if (*ch < 32 || *ch > 126) return false;
    if (p == 64) for (const char* ch = password; *ch; ++ch)
      if (nibble(*ch) < 0 && (*ch < 'A' || *ch > 'F')) return false;
    strcpy(out.ssid, ssid); strcpy(out.password, password);
    return true;
  }
  void startWiFi() {
    attempted = millis();
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(config.ssid, config.password);
  }
  void dropPeer() {
    peer.stop(); havePeer = false; authenticated = false; used = needed = 0; tx = rx = 0;
  }
  bool hmac(const uint8_t* data, size_t length, uint8_t* out) {
    return mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), config.key, 32, data, length, out) == 0;
  }
  bool proof(const char* role, uint8_t* out) {
    char text[80];
    const int size = snprintf(text, sizeof(text), "%s:%s:%s", role, device, nonce);
    return hmac(reinterpret_cast<const uint8_t*>(text), size, out);
  }
  bool sign(char role, const uint8_t* data, size_t length, uint8_t* out) {
    uint8_t message[34 + 6 + MAX_PAYLOAD_BYTES + 1];
    message[0] = role; message[1] = ':';
    memcpy(message + 2, nonce, 32); memcpy(message + 34, data, length);
    return hmac(message, length + 34, out);
  }
  void authenticate() {
    StaticJsonDocument<192> doc;
    JsonReader reader{reinterpret_cast<char*>(packet), used};
    uint8_t supplied[32], expected[32];
    if (!used || memchr(packet, 0, used) || deserializeJson(doc, reader) || reader.position != used ||
        !doc["auth"].is<const char*>() || doc["auth"].as<JsonString>().size() != 64 ||
        !unhex(doc["auth"], supplied, 32) || !proof("client", expected) || !equal(supplied, expected, 32)) {
      dropPeer(); return;
    }
    if (!proof("server", expected)) { dropPeer(); return; }
    char digest[65], response[96]; hex(expected, 32, digest);
    snprintf(response, sizeof(response), "{\"ok\":true,\"proof\":\"%s\"}\n", digest);
    if (peer.print(response) != strlen(response)) { dropPeer(); return; }
    authenticated = true; touched = millis();
  }
};
}  // namespace xte
