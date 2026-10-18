#pragma once
// Host-only framebuffer and I/O substitutes for compiling the actual render functions.
#include "tf_font.h"
#include "protocol.h"
#include "settings.h"
#include "clash_panel.h"
#include "sleep_image.h"
#include "ui_assets.h"
#include <algorithm>
#include <array>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <stdexcept>
#include <string>
struct EInkDisplay {
  static constexpr int DISPLAY_WIDTH = 800, DISPLAY_HEIGHT = 480, DISPLAY_WIDTH_BYTES = 100;
  enum Mode { FULL_REFRESH, HALF_REFRESH, FAST_REFRESH };
  Mode lastMode = FAST_REFRESH;
  std::array<uint8_t, 48000> bytes;
  std::array<uint8_t, 48000> base, grayLsb, grayMsb;
  void clearScreen() { bytes.fill(0xff); }
  uint8_t* getFrameBuffer() { return bytes.data(); }
  void displayBuffer(Mode mode) {
    if (host::fontOpens) throw std::runtime_error("font still open during refresh");
    lastMode = mode;
    base = bytes;
  }
  unsigned grayDisplays = 0;
  void copyGrayscaleLsbBuffers(uint8_t* value) { std::copy(value, value + 48000, grayLsb.begin()); }
  void copyGrayscaleMsbBuffers(uint8_t* value) { std::copy(value, value + 48000, grayMsb.begin()); }
  void displayGrayBuffer() { ++grayDisplays; }
  void cleanupGrayscaleBuffers(uint8_t*) {}
} display;
struct SerialStub {
  std::vector<std::string> lines;
  template<typename... Args> void printf(const char* format, Args... args) {
    char value[256];
    snprintf(value, sizeof(value), format, args...);
    lines.emplace_back(value);
  }
  void println(const char* line) { lines.emplace_back(line); }
  void print(const char* line) { lines.emplace_back(line); }
} Serial;
struct EspStub { unsigned getFreeHeap() { return 240680; } } ESP;
uint32_t hostNow = 0;
uint32_t millis() { return hostNow; }
struct InputManager {
  static constexpr uint8_t BTN_BACK = 0, BTN_CONFIRM = 1, BTN_LEFT = 2, BTN_RIGHT = 3,
                           BTN_UP = 4, BTN_DOWN = 5, BTN_POWER = 6;
  uint8_t pressed = 0;
  uint8_t held = 0;
  bool wasPressed(uint8_t button) const { return (pressed & (1 << button)) != 0; }
  bool isPressed(uint8_t button) const { return (held & (1 << button)) != 0; }
} input;
xte::TfFont font;
xte::Status status;
xte::LineReceiver receiver;
struct NetworkStub {
  bool lan = false, configured = false, fail = false;
  unsigned reconnects = 0;
  bool isLan() const { return lan; }
  bool ready() const { return configured; }
  bool setMode(bool next) {
    if (fail || (next && !configured)) return false;
    lan = next; return true;
  }
  void info(uint32_t = 0, const char* = nullptr, const char* = nullptr) {}
  const char* ssid() const { return "Test WiFi"; }
  void reconnect() { ++reconnects; }
  bool send(const char* line) { Serial.print(line); return true; }
  const char* commitConfig() { return nullptr; }
} network;
constexpr int WL_CONNECTED = 3;
struct WiFiStub {
  int status() const { return 0; }
  int RSSI() const { return -55; }
  WiFiStub localIP() const { return {}; }
  std::string toString() const { return "192.168.1.2"; }
} WiFi;
struct TfStub {
  bool busy = false;
  bool active() const { return busy; }
  bool handle(char*, size_t) { return false; }
} tf;
void saveFrame(const std::string& path) {
  std::ofstream output(path, std::ios::binary);
  output.write(reinterpret_cast<const char*>(display.bytes.data()), display.bytes.size());
}

// Hardware I/O substitutes; UI tests exercise menu behavior, not ADC calibration.
struct BatteryMonitor {
  static uint16_t percentageFromMillivolts(uint16_t) { return 80; }
  uint16_t readMillivolts() { return 4000; }
} battery;
uint16_t batteryMv = 4000;
uint32_t batterySampledAt = 0, usbPolledAt = 0;
bool usbPowered = false;
constexpr uint8_t USB_DETECT = 20;
constexpr int HIGH = 1;
int digitalRead(uint8_t) { return 0; }
