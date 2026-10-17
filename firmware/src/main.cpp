#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <BatteryMonitor.h>
#include <EInkDisplay.h>
#include <InputManager.h>
#include <algorithm>
#include <driver/gpio.h>
#include <esp_sleep.h>

#include "protocol.h"
#include "sleep_art.h"
#include "sleep_image.h"
#include "tf_transfer.h"
#include "tf_font.h"
#include "settings.h"
#include "network.h"

namespace {
// SDK requires constructor pins; these values follow the verified X4 contract.
EInkDisplay display(8, 10, 21, 4, 5, 6);
InputManager input;
// GPIO0 follows the X4 contract; the SDK 2.0 divider still awaits calibration.
BatteryMonitor battery(0);
uint16_t batteryMv = 0;
uint32_t batterySampledAt = 0;
xte::LineReceiver receiver;
xte::Status status;
xte::TfTransfer tf;
xte::TfFont font;
xte::Network network;
bool handleUsbLine(char* line, size_t length);
void handleNetworkLine(char* line, size_t length);
constexpr uint32_t OFFLINE_MS = 15000;
uint32_t lastReceived = 0;
uint32_t lastContact = 0;
uint32_t lastRendered = 0;
bool haveStatus = false;
bool dirty = false;
bool wasOnline = false;
bool forceFull = false;
bool halfRefresh = false;  // App transitions; normal content updates stay fast.
bool renderNow = false;
uint32_t volumeSequence = 0;
uint32_t volumeRequestedAt = 0;
bool volumePending = false;
float confirmedVolume = NAN;
using xte::Page;
Page page = Page::STATUS;
bool appMenu = true;  // page also remembers the selected launcher tile.
uint8_t selectedAgent = 0;
xte::Settings settings;
enum class SettingsMode : uint8_t { LIST, EDIT, INFO, RESET };
SettingsMode settingsMode = SettingsMode::LIST;
uint8_t selectedSetting = 0;
uint8_t draftSetting = 0;
const char* settingsMessage = nullptr;
constexpr const char* FIRMWARE_VERSION = "app-menu-half-20261017";
constexpr uint8_t SETTING_COUNT = 6;
constexpr uint32_t POWER_HOLD_MS = 1500;
// The pinned SDK has no power-latch API; GPIO13 follows the verified X4 contract.
constexpr gpio_num_t POWER_LATCH = GPIO_NUM_13;
// GPIO20 is driven high while USB is present (verified 2026-10-16; no pull needed).
constexpr uint8_t USB_DETECT = 20;
bool usbPowered = false;
uint32_t usbPolledAt = 0;
uint32_t powerPressedAt = 0;
bool powerGesture = false;
bool ignoreWakeRelease = false;

// Draw directly into the SDK buffer, without a second GFX canvas.
class FrameGraphics : public Adafruit_GFX {
 public:
  FrameGraphics() : Adafruit_GFX(EInkDisplay::DISPLAY_WIDTH, EInkDisplay::DISPLAY_HEIGHT) {}
  void drawPixel(int16_t x, int16_t y, uint16_t color) override {
    if (x < 0 || y < 0 || x >= width() || y >= height()) return;
    uint8_t& value = display.getFrameBuffer()[y * EInkDisplay::DISPLAY_WIDTH_BYTES + x / 8];
    const uint8_t mask = 0x80 >> (x & 7);
    if (color) value |= mask;
    else value &= ~mask;
  }
};
FrameGraphics graphics;

void text(int16_t x, int16_t y, uint8_t size, const char* value) {
  graphics.setCursor(x, y);
  graphics.setTextSize(size);
  graphics.print(value);
}

void uiText(int16_t x, int16_t y, const char* value, uint16_t maxWidth = 0) {
  font.draw(graphics, x, y, value, maxWidth ? maxWidth : graphics.width() - x);
}

void renderButtonHints() {
  // Landscape symbols: top Left/Right; right Up/Down/Confirm/Back.
  // Firmware key definitions and input behavior stay unchanged.
  // 16x16 monochrome symbols occupy existing whitespace and need no TF glyphs.
  static constexpr uint8_t icons[6][32] = {
    { // Up
      0x00, 0x00, 0x00, 0x00, 0x01, 0x80, 0x03, 0xc0,
      0x07, 0xe0, 0x0d, 0xb0, 0x19, 0x98, 0x31, 0x8c,
      0x01, 0x80, 0x01, 0x80, 0x01, 0x80, 0x01, 0x80,
      0x01, 0x80, 0x01, 0x80, 0x00, 0x00, 0x00, 0x00,
    },
    { // Down
      0x00, 0x00, 0x00, 0x00, 0x01, 0x80, 0x01, 0x80,
      0x01, 0x80, 0x01, 0x80, 0x01, 0x80, 0x01, 0x80,
      0x31, 0x8c, 0x19, 0x98, 0x0d, 0xb0, 0x07, 0xe0,
      0x03, 0xc0, 0x01, 0x80, 0x00, 0x00, 0x00, 0x00,
    },
    { // Right
      0x00, 0x00, 0x00, 0x00, 0x00, 0x80, 0x00, 0xc0,
      0x00, 0x60, 0x00, 0x30, 0x00, 0x18, 0x3f, 0xfc,
      0x3f, 0xfc, 0x00, 0x18, 0x00, 0x30, 0x00, 0x60,
      0x00, 0xc0, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00,
    },
    { // Left
      0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x03, 0x00,
      0x06, 0x00, 0x0c, 0x00, 0x18, 0x00, 0x3f, 0xfc,
      0x3f, 0xfc, 0x18, 0x00, 0x0c, 0x00, 0x06, 0x00,
      0x03, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00,
    },
    { // Confirm
      0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x06,
      0x00, 0x0c, 0x00, 0x18, 0x00, 0x18, 0x00, 0x30,
      0x30, 0x60, 0x18, 0xc0, 0x0c, 0xc0, 0x07, 0x80,
      0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    },
    { // Back
      0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x18, 0x0c,
      0x0c, 0x18, 0x06, 0x30, 0x03, 0x60, 0x01, 0xc0,
      0x01, 0xc0, 0x03, 0x60, 0x06, 0x30, 0x0c, 0x18,
      0x18, 0x0c, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    },
  };
  graphics.drawBitmap(342, 8, icons[3], 16, 16, 0);
  graphics.drawBitmap(442, 8, icons[2], 16, 16, 0);
  const int16_t right = graphics.width() - 20;
  graphics.drawBitmap(right, 104, icons[0], 16, 16, 0);
  graphics.drawBitmap(right, 192, icons[1], 16, 16, 0);
  graphics.drawBitmap(right, 280, icons[4], 16, 16, 0);
  graphics.drawBitmap(right, 368, icons[5], 16, 16, 0);
}

uint8_t batteryBars(uint16_t millivolts) {
  return (BatteryMonitor::percentageFromMillivolts(millivolts) * 22 + 50) / 100;
}

void sampleBattery() {
  uint16_t samples[5];
  for (uint16_t& sample : samples) sample = battery.readMillivolts();
  std::sort(samples, samples + 5);
  // Only a visible change in the icon schedules a normal-interval repaint.
  if (batteryMv && batteryBars(samples[2]) != batteryBars(batteryMv)) dirty = true;
  batteryMv = samples[2];
  batterySampledAt = millis();
}

void renderBattery() {
  // Below the 42 px connection label, above the header rule and right-side hints.
  graphics.drawRect(744, 62, 26, 14, 0);
  graphics.fillRect(770, 66, 3, 6, 0);
  graphics.fillRect(746, 64, batteryBars(batteryMv), 10, 0);
  if (!usbPowered) return;
  static constexpr uint8_t bolt[13] = {0x07, 0x0e, 0x1c, 0x38, 0x70, 0xff, 0xff,
                                       0x0e, 0x1c, 0x38, 0x70, 0x60, 0x40};
  graphics.drawBitmap(732, 62, bolt, 8, 13, 0);
}

void pollUsb() {
  usbPolledAt = millis();
  const bool powered = digitalRead(USB_DETECT) == HIGH;
  if (powered == usbPowered) return;
  usbPowered = powered;
  sampleBattery();  // Charging lifts the reading; refresh it with the plug state.
  renderNow = true;
}

void metric(int16_t x, int16_t y, const char* label, const char* value) {
  uiText(x, y, label, 312);
  text(x, y + 54, 3, value);
}

// Icons use the existing GFX primitives and the SDK's single framebuffer.
void renderAppMenu() {
  constexpr const char* labels[] = {"电脑状态", "系统音量", "助手控制", "设置"};
  for (uint8_t i = 0; i < 4; ++i) {
    const int16_t x = 40 + (i % 2) * 368;
    const int16_t y = 104 + (i / 2) * 144;
    const bool selected = i == static_cast<uint8_t>(page);
    graphics.drawRect(x, y, 348, 132, 0);
    if (selected) graphics.drawRect(x + 3, y + 3, 342, 126, 0);
    const int16_t ix = x + 24, iy = y + 32;
    if (i == 0) {
      graphics.drawRect(ix, iy, 64, 44, 0);
      graphics.fillRect(ix + 30, iy + 44, 4, 10, 0);
      graphics.fillRect(ix + 16, iy + 54, 32, 3, 0);
      for (int j = 0; j < 4; ++j)
        graphics.fillRect(ix + 10 + j * 12, iy + 32 - j * 6, 6, 8 + j * 6, 0);
    } else if (i == 1) {
      graphics.drawRect(ix + 2, iy + 18, 18, 24, 0);
      graphics.drawRect(ix + 20, iy + 8, 18, 44, 0);
      graphics.drawFastVLine(ix + 48, iy + 16, 28, 0);
      graphics.drawFastVLine(ix + 58, iy + 6, 48, 0);
    } else if (i == 2) {
      graphics.drawRect(ix, iy + 8, 64, 44, 0);
      graphics.fillRect(ix + 14, iy + 22, 8, 8, 0);
      graphics.fillRect(ix + 42, iy + 22, 8, 8, 0);
      graphics.drawFastHLine(ix + 20, iy + 40, 24, 0);
      graphics.drawFastVLine(ix + 32, iy, 8, 0);
    } else {
      for (int j = 0; j < 3; ++j) {
        graphics.drawFastHLine(ix, iy + 10 + j * 20, 64, 0);
        graphics.fillRect(ix + (j == 1 ? 40 : 12), iy + 4 + j * 20, 10, 13, 0);
      }
    }
    font.draw(graphics, x + 112, y + 42, labels[i], 220);
    if (selected) graphics.fillRect(x + 112, y + 99, 64, 4, 0);
  }
}

void renderStatusPage() {
  graphics.drawFastVLine(400, 104, 250, 0);
  graphics.drawFastHLine(28, 230, 744, 0);
  char value[48];
  if (haveStatus && std::isfinite(status.cpu)) snprintf(value, sizeof(value), "%.1f %%", status.cpu);
  else strcpy(value, "--");
  metric(48, 110, "CPU负载", value);
  if (haveStatus && std::isfinite(status.gpuTemp)) snprintf(value, sizeof(value), "%.1f C", status.gpuTemp);
  else strcpy(value, "--");
  metric(440, 110, "GPU温度", value);
  if (haveStatus && std::isfinite(status.memoryUsed) && std::isfinite(status.memoryTotal)) {
    snprintf(value, sizeof(value), "%.1f / %.1f GiB", status.memoryUsed / 1024, status.memoryTotal / 1024);
  } else strcpy(value, "--");
  metric(48, 270, "内存用量", value);
  if (haveStatus && std::isfinite(status.gpu)) snprintf(value, sizeof(value), "%.1f %%", status.gpu);
  else strcpy(value, "--");
  metric(440, 270, "GPU负载", value);
}

void renderVolumePage() {
  uiText(48, 116, "系统音量");
  char value[24];
  if (haveStatus && std::isfinite(status.volume)) snprintf(value, sizeof(value), "%.0f %%", status.volume);
  else strcpy(value, "--");
  text(48, 174, 5, value);
  graphics.drawRect(48, 268, 704, 34, 0);
  if (haveStatus && std::isfinite(status.volume)) {
    const int width = static_cast<int>(status.volume * 698.0f / 100.0f);
    if (width > 0) graphics.fillRect(51, 271, width, 28, 0);
  }
  snprintf(value, sizeof(value), "下键 -%d", settings.volumeStep());
  uiText(48, 338, value, 300);
  snprintf(value, sizeof(value), "上键 +%d", settings.volumeStep());
  uiText(502, 338, value, 250);
}

void renderAgentPage() {
  uiText(48, 108, "助手控制");
  if (status.agentCount == 0) {
    uiText(48, 180, "暂无连接");
    return;
  }
  if (selectedAgent >= status.agentCount) selectedAgent = 0;
  const xte::Agent& agent = status.agents[selectedAgent];
  text(48, 170, 5, agent.tool);
  const char* stateLabel = strcmp(agent.state, "ask") == 0 ? "等待确认" :
                           strcmp(agent.state, "running") == 0 ? "运行中" :
                           strcmp(agent.state, "idle") == 0 ? "空闲" :
                           strcmp(agent.state, "offline") == 0 ? "离线" :
                           strcmp(agent.state, "done") == 0 ? "已完成" :
                           strcmp(agent.state, "error") == 0 ? "错误" : nullptr;
  if (stateLabel) uiText(520, 174, stateLabel, 232);
  else text(520, 186, 3, agent.state);
  if (strcmp(agent.state, "ask") == 0) {
    uiText(48, 258, "请求确认");
    if (agent.text[0]) text(48, 312, 2, agent.text);
    else uiText(48, 306, "等待批准");
    uiText(48, 354, "确认：批准", 380);
    uiText(492, 354, "右键：拒绝", 260);
  } else {
    if (agent.text[0]) text(48, 294, 2, agent.text);
    else uiText(48, 294, "暂无待处理请求");
    uiText(48, 354, "上下键切换助手");
  }
}

const char* settingLabel(uint8_t index) {
  constexpr const char* labels[] = {"音量步长", "屏幕更新间隔", "菜单默认项", "连接方式", "设备信息", "恢复默认值"};
  return labels[index];
}

uint8_t settingIndex(uint8_t index) {
  return index == 0 ? settings.volumeIndex : index == 1 ? settings.intervalIndex :
         index == 2 ? settings.startupPage : network.isLan();
}

void settingValue(uint8_t index, uint8_t choice, char* value, size_t size) {
  if (index == 0) {
    constexpr int steps[] = {1, 5, 10};
    snprintf(value, size, "%d %%", steps[choice]);
  } else if (index == 1) {
    constexpr int seconds[] = {5, 10, 30};
    snprintf(value, size, "%d 秒", seconds[choice]);
  } else if (index == 2) {
    constexpr const char* pages[] = {"状态页", "音量页", "助手页"};
    snprintf(value, size, "%s", pages[choice]);
  } else if (index == 3) {
    snprintf(value, size, "%s", choice ? "局域网连接" : "串口直连");
  } else {
    snprintf(value, size, "%s", index == 4 ? "查看" : "重置");
  }
}

void renderSettingsPage() {
  uiText(48, 96, "设置", 200);
  if (settingsMessage) uiText(480, 96, settingsMessage, 272);
  if (settingsMode == SettingsMode::INFO) {
    char value[64];
    uiText(48, 146, "固件版本", 240);
    text(400, 158, 2, FIRMWARE_VERSION);
    uiText(48, 196, "协议版本", 240);
    text(400, 208, 2, network.isLan() ? "v2 / LAN TCP" : "v2 / USB CDC");
    uiText(48, 246, "TF卡状态", 260);
    uiText(400, 246, SdMan.ready() ? "已挂载" : "未挂载", 352);
    uiText(48, 296, "可用内存", 240);
    snprintf(value, sizeof(value), "%u bytes", static_cast<unsigned>(ESP.getFreeHeap()));
    text(400, 308, 2, value);
    uiText(48, 346, network.isLan() ? "局域网地址" : "WiFi配置", 240);
    if (network.isLan() && WiFi.status() == WL_CONNECTED)
      text(400, 358, 2, WiFi.localIP().toString().c_str());
    else uiText(400, 346, network.ready() ? "已配置" : "请用USB配置", 352);
    return;
  }
  if (settingsMode == SettingsMode::RESET) {
    uiText(48, 174, "恢复默认设置？", 704);
    uiText(48, 242, "音量步长 5%，更新间隔 5 秒", 704);
    uiText(48, 300, "菜单默认状态，连接改为串口", 704);
    return;
  }
  for (uint8_t i = 0; i < SETTING_COUNT; ++i) {
    const int16_t y = 138 + i * 42;
    const bool selected = i == selectedSetting;
    const uint16_t color = selected ? 1 : 0;
    if (selected) graphics.fillRect(28, y - 2, 744, 46, 0);
    font.draw(graphics, 48, y, settingLabel(i), 440, color);
    char value[32];
    const uint8_t choice = settingsMode == SettingsMode::EDIT && i == selectedSetting ?
                           draftSetting : settingIndex(i);
    settingValue(i, choice, value, sizeof(value));
    font.draw(graphics, 520, y, value, 232, color);
  }
}

void render(bool online, bool full, bool half = false) {
  const uint32_t started = millis();
  font.beginRender();
  display.clearScreen();
  graphics.setTextColor(0);
  graphics.setTextWrap(false);
  text(28, 20, 3, "XTE-Link");
  const char* connection = online ? "在线" : haveStatus ? "离线" : "等待连接";
  if (!online && !haveStatus && network.isLan())
    connection = !network.ready() ? "缺少WiFi配置" : WiFi.status() != WL_CONNECTED ? "WiFi连接中" : "等待驱动";
  uiText(570, 8, connection, 202);
  renderBattery();
  text(28, 58, 2, haveStatus ? status.host : network.isLan() ? "LAN TCP" : "USB SERIAL");
  graphics.drawFastHLine(28, 88, 744, 0);
  if (appMenu) renderAppMenu();
  else if (page == Page::STATUS) renderStatusPage();
  else if (page == Page::VOLUME) renderVolumePage();
  else if (page == Page::AGENTS) renderAgentPage();
  else renderSettingsPage();
  graphics.drawFastHLine(28, 400, 744, 0);
  if (appMenu) {
    uiText(28, 412, "方向键选择  确认打开", 744);
  } else if (page == Page::SETTINGS) {
    const char* hint = settingsMode == SettingsMode::LIST ? "上下选择  确认打开  返回菜单" :
                       settingsMode == SettingsMode::EDIT ? "左右调整  确认保存  返回取消" :
                       settingsMode == SettingsMode::RESET ? "确认恢复  返回取消" : "返回：设置列表";
    uiText(28, 412, hint, 744);
  } else {
    uiText(28, 412, "返回菜单", 180);
    text(230, 424, 2, haveStatus ? status.time : "--");
  }
  renderButtonHints();
  font.endRender();
  display.displayBuffer(full ? EInkDisplay::FULL_REFRESH :
                        half ? EInkDisplay::HALF_REFRESH : EInkDisplay::FAST_REFRESH);
  lastRendered = started;
  Serial.printf("XTE: rendered %s in %lu ms, heap=%u battery=%umV\n", full ? "full" : half ? "half" : "fast",
                static_cast<unsigned long>(millis() - started), static_cast<unsigned>(ESP.getFreeHeap()),
                static_cast<unsigned>(batteryMv));
  if (!font.healthy()) Serial.printf("XTE: font boxes: %s\n", font.error());
}

void sendVolumeDelta(int delta) {
  if (!haveStatus || millis() - lastContact >= OFFLINE_MS) return;
  renderNow = true;
  if (haveStatus && std::isfinite(status.volume)) {
    status.volume = std::max(0.0f, std::min(100.0f, status.volume + delta));
  }
  if (++volumeSequence == 0) ++volumeSequence;
  volumePending = true;
  volumeRequestedAt = millis();
  char message[64];
  snprintf(message, sizeof(message), "XTE: volume_delta %+d %lu\n", delta, static_cast<unsigned long>(volumeSequence));
  if (network.isLan()) network.send(message); else Serial.print(message);
  // Show the prediction before draining old status frames or PC confirmations.
  render(haveStatus && millis() - lastContact < OFFLINE_MS, false);
  renderNow = false;
}

void sendAgentAction(const char* action) {
  if (!haveStatus || millis() - lastContact >= OFFLINE_MS) return;
  if (status.agentCount == 0 || selectedAgent >= status.agentCount) return;
  const xte::Agent& agent = status.agents[selectedAgent];
  if (strcmp(agent.state, "ask") != 0 || agent.requestId[0] == '\0') return;
  char message[96];
  snprintf(message, sizeof(message), "XTE: agent_action %s %s %s\n", action, agent.tool, agent.requestId);
  if (network.isLan()) network.send(message); else Serial.print(message);
  renderNow = true;
}

bool commitSettings(const xte::Settings& next) {
  if (next.encode() == settings.encode()) {
    settingsMessage = "未修改";
    return true;
  }
  if (!next.save()) {
    settingsMessage = "保存失败";
    Serial.println("XTE: settings save failed");
    return false;
  }
  settings = next;
  settingsMessage = "已保存";
  Serial.printf("XTE: settings saved volume_step=%d update_ms=%lu startup_page=%u\n",
                settings.volumeStep(), static_cast<unsigned long>(settings.updateMs()),
                static_cast<unsigned>(settings.startupPage));
  return true;
}

void handleSettingsButtons() {
  // Consume navigation here; Back cannot fall through to Agent-page rejection.
  if (input.wasPressed(InputManager::BTN_BACK)) {
    if (settingsMode == SettingsMode::LIST) { appMenu = true; halfRefresh = true; }
    else settingsMode = SettingsMode::LIST;
    settingsMessage = nullptr;
    renderNow = true;
    return;
  }
  if (settingsMode == SettingsMode::LIST) {
    // Landscape side Up/Down are the SDK's Right/Left buttons.
    if (input.wasPressed(InputManager::BTN_RIGHT)) {
      selectedSetting = (selectedSetting + SETTING_COUNT - 1) % SETTING_COUNT;
      settingsMessage = nullptr;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_LEFT)) {
      selectedSetting = (selectedSetting + 1) % SETTING_COUNT;
      settingsMessage = nullptr;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      settingsMessage = nullptr;
      if (selectedSetting < 4) {
        draftSetting = settingIndex(selectedSetting);
        settingsMode = SettingsMode::EDIT;
      } else {
        settingsMode = selectedSetting == 4 ? SettingsMode::INFO : SettingsMode::RESET;
      }
      renderNow = true;
    }
  } else if (settingsMode == SettingsMode::EDIT) {
    // Landscape top Left/Right are the SDK's Up/Down buttons.
    if (input.wasPressed(InputManager::BTN_UP) || input.wasPressed(InputManager::BTN_DOWN)) {
      const int delta = input.wasPressed(InputManager::BTN_UP) ? -1 : 1;
      const int count = selectedSetting == 3 ? 2 : 3;
      draftSetting = static_cast<uint8_t>((draftSetting + delta + count) % count);
      settingsMessage = nullptr;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      if (selectedSetting == 3) {
        if (!network.setMode(draftSetting != 0)) {
          settingsMessage = network.ready() ? "保存失败" : "请先用USB配置WiFi";
        } else {
          receiver.reset(); haveStatus = false; lastContact = 0; volumePending = false;
          status = xte::Status{};
          settingsMode = SettingsMode::LIST;
          settingsMessage = "已保存";
          network.info();
        }
        renderNow = true;
        return;
      }
      xte::Settings next = settings;
      if (selectedSetting == 0) next.volumeIndex = draftSetting;
      else if (selectedSetting == 1) next.intervalIndex = draftSetting;
      else next.startupPage = draftSetting;
      if (commitSettings(next)) settingsMode = SettingsMode::LIST;
      renderNow = true;
    }
  } else if (settingsMode == SettingsMode::RESET && input.wasPressed(InputManager::BTN_CONFIRM)) {
    if (network.setMode(false) && commitSettings(xte::Settings{})) {
      receiver.reset(); haveStatus = false; lastContact = 0; volumePending = false;
      status = xte::Status{};
      settingsMode = SettingsMode::LIST;
      network.info();
    } else settingsMessage = "保存失败";
    renderNow = true;
  }
}

void handlePageButtons() {
  if (appMenu) {
    uint8_t selected = static_cast<uint8_t>(page);
    // Landscape Left/Right = SDK Up/Down; landscape Up/Down = SDK Right/Left.
    if (input.wasPressed(InputManager::BTN_UP) || input.wasPressed(InputManager::BTN_DOWN))
      selected ^= 1;
    else if (input.wasPressed(InputManager::BTN_RIGHT) || input.wasPressed(InputManager::BTN_LEFT))
      selected ^= 2;
    else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      appMenu = false;
      settingsMode = SettingsMode::LIST;
      settingsMessage = nullptr;
      halfRefresh = true;
      renderNow = true;
      return;  // Opening an app must never also trigger its action.
    }
    if (selected != static_cast<uint8_t>(page)) {
      page = static_cast<Page>(selected);
      renderNow = true;
    }
    return;
  }
  if (page == Page::SETTINGS) {
    handleSettingsButtons();
    return;
  }
  if (input.wasPressed(InputManager::BTN_BACK)) {
    appMenu = true;
    halfRefresh = true;
    renderNow = true;
    return;
  }
  if (page == Page::VOLUME) {
    if (input.wasPressed(InputManager::BTN_LEFT)) sendVolumeDelta(-settings.volumeStep());
    else if (input.wasPressed(InputManager::BTN_RIGHT)) sendVolumeDelta(settings.volumeStep());
  } else if (page == Page::AGENTS) {
    if (input.wasPressed(InputManager::BTN_LEFT)) {
      if (status.agentCount > 0) selectedAgent = selectedAgent == 0 ? status.agentCount - 1 : selectedAgent - 1;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_RIGHT)) {
      if (status.agentCount > 0) selectedAgent = (selectedAgent + 1) % status.agentCount;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) sendAgentAction("approve");
    else if (input.wasPressed(InputManager::BTN_DOWN)) sendAgentAction("reject");
  }
}

bool backgroundRenderDue(uint32_t now, bool online) {
  // Menu and settings keep receiving snapshots without repainting on every sample.
  // Connection changes bypass the selected interval so stale data is visibly offline.
  return online != wasOnline ||
         (!appMenu && page != Page::SETTINGS && dirty && now - lastRendered >= settings.updateMs());
}

void replyActive(const char* message) {
  if (network.isLan()) network.send(message); else Serial.print(message);
}

void acceptStatus(xte::Status incoming) {
  const float previousVolume = status.volume;
  confirmedVolume = incoming.volume;
  if (volumePending && incoming.volumeAck != volumeSequence) incoming.volume = previousVolume;
  else volumePending = false;
  if (!incoming.hasAgents) {
    incoming.agentCount = status.agentCount;
    memcpy(incoming.agents, status.agents, sizeof(status.agents));
  } else if (memcmp(incoming.agents, status.agents, sizeof(status.agents)) && !appMenu && page == Page::AGENTS) {
    renderNow = true;
  }
  status = incoming;
  if (!appMenu && page == Page::VOLUME &&
      (std::isfinite(previousVolume) != std::isfinite(status.volume) ||
       (std::isfinite(status.volume) && previousVolume != status.volume))) renderNow = true;
  lastReceived = lastContact = millis();
  haveStatus = true;
  dirty = true;
  replyActive("XTE: accepted\n");
}

bool handlePush(char* line, size_t length) {
  StaticJsonDocument<768> doc;
  if (!xte::readControl(line, length, doc)) return false;
  if (doc["cmd"] == "heartbeat") {
    lastContact = millis();
    replyActive("XTE: pong\n");
  } else if (doc["cmd"] == "agents") {
    xte::Agent updates[xte::MAX_AGENTS] = {};
    uint8_t count = 0;
    if (!xte::readAgents(doc["agents"], updates, count) || !count) return true;
    bool changed = false;
    for (uint8_t i = 0; i < count; ++i) {
      uint8_t index = 0;
      while (index < status.agentCount && strcmp(status.agents[index].tool, updates[i].tool)) ++index;
      if (index == status.agentCount) {
        if (status.agentCount >= xte::MAX_AGENTS) return true;
        ++status.agentCount;
      }
      changed |= memcmp(&status.agents[index], &updates[i], sizeof(xte::Agent)) != 0;
      status.agents[index] = updates[i];
    }
    lastContact = millis();
    if (changed) {
      dirty = true;
      if (!appMenu && page == Page::AGENTS) renderNow = true;
    }
    replyActive("XTE: accepted\n");
  }
  return true;
}

bool handleUsbLine(char* line, size_t length) {
  if (tf.handle(line, length)) return true;
  StaticJsonDocument<256> doc;
  if (xte::readControl(line, length, doc)) {
    if (doc["cmd"] == "hello") { network.info(); return true; }
    if (doc["cmd"] == "network_commit") {
      if (!doc["id"].is<uint32_t>()) return true;
      network.info(doc["id"], tf.active() ? "busy" : network.commitConfig());
      renderNow = page == Page::SETTINGS;
      return true;
    }
  }
  // USB remains available for configuration/TF tools in LAN mode, never for a
  // second data stream. The firmware setting is the authority for routing.
  return network.isLan() || handlePush(line, length);
}

void handleNetworkLine(char* line, size_t length) {
  if (handlePush(line, length)) return;
  xte::Status incoming;
  if (xte::parseStatus(line, length, incoming)) acceptStatus(incoming);
}

bool renderSleepImage() {
  xte::SleepImage image;
  uint8_t* frame = display.getFrameBuffer();
  if (!image.open() || !image.draw(frame, xte::SleepImage::BASE)) return false;
  display.displayBuffer(EInkDisplay::FULL_REFRESH);
  if (image.hasGray() && image.draw(frame, xte::SleepImage::LSB)) {
    display.copyGrayscaleLsbBuffers(frame);
    if (image.draw(frame, xte::SleepImage::MSB)) {
      display.copyGrayscaleMsbBuffers(frame);
      display.displayGrayBuffer();
    }
  }
  image.close();
  return true;
}

void renderSleep() {
  if (renderSleepImage()) return;
  display.clearScreen();
  graphics.setTextColor(0);
  graphics.setTextWrap(false);
  constexpr int scale = 10;
  constexpr int x = (EInkDisplay::DISPLAY_WIDTH - xte::SLEEP_ART_WIDTH * scale) / 2;
  constexpr int y = 42;
  for (size_t row = 0; row < xte::SLEEP_ART_HEIGHT; ++row) {
    for (size_t col = 0; xte::SLEEP_ART[row][col]; ++col) {
      if (xte::SLEEP_ART[row][col] == '#') {
        graphics.fillRect(x + static_cast<int16_t>(col) * scale,
                          y + static_cast<int16_t>(row) * scale, scale, scale, 0);
      }
    }
  }
  font.beginRender();
  uiText(337, 408, "休眠中", 126);
  font.endRender();
  display.displayBuffer(EInkDisplay::FULL_REFRESH);
}

void sleepUntilPowerButton() {
  // A level-triggered wake source must not be armed while the button is held.
  uint32_t releasedAt = millis();
  while (millis() - releasedAt < 50) {
    if (digitalRead(InputManager::POWER_BUTTON_PIN) == LOW) releasedAt = millis();
    delay(10);
  }
  ESP_ERROR_CHECK(esp_deep_sleep_enable_gpio_wakeup(
      1ULL << InputManager::POWER_BUTTON_PIN, ESP_GPIO_WAKEUP_GPIO_LOW));
  ESP_ERROR_CHECK(gpio_hold_en(POWER_LATCH));
  gpio_deep_sleep_hold_en();
  Serial.println("XTE: sleeping; hold power to wake");
  Serial.flush();
  Serial.end();
  esp_deep_sleep_start();
}

void enterSleep() {
  tf.abort();
  network.stop();
  Serial.println("XTE: sleep requested");
  renderSleep();
  display.deepSleep();
  sleepUntilPowerButton();
}

void initializePower() {
  // Restore the output before releasing the pad hold to avoid dropping the latch.
  ESP_ERROR_CHECK(gpio_set_direction(POWER_LATCH, GPIO_MODE_OUTPUT));
  ESP_ERROR_CHECK(gpio_set_level(POWER_LATCH, 1));
  gpio_deep_sleep_hold_dis();
  ESP_ERROR_CHECK(gpio_hold_dis(POWER_LATCH));
  input.begin();
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_GPIO) return;

  const uint32_t started = millis();
  while (millis() - started < POWER_HOLD_MS) {
    if (digitalRead(InputManager::POWER_BUTTON_PIN) != LOW) {
      // Preserve the retained sleep picture on an accidental short wake press.
      sleepUntilPowerButton();
    }
    delay(10);
  }
  ignoreWakeRelease = true;
}

void handlePowerButton() {
  if (ignoreWakeRelease) {
    if (!input.isPowerButtonPressed() &&
        digitalRead(InputManager::POWER_BUTTON_PIN) != LOW) ignoreWakeRelease = false;
    return;
  }
  if (input.wasPressed(InputManager::BTN_POWER)) {
    powerPressedAt = millis();
    powerGesture = true;
  }
  if (!powerGesture) return;
  const bool longPress = millis() - powerPressedAt >= POWER_HOLD_MS;
  if (longPress) {
    powerGesture = false;
    enterSleep();
  } else if (input.wasReleased(InputManager::BTN_POWER)) {
    powerGesture = false;
    forceFull = true;
    Serial.println("XTE: power short press; full refresh");
  }
}
}

void setup() {
  initializePower();
  if (Serial.setRxBufferSize(1024) != 1024) {
    Serial.begin(115200);
    Serial.println("XTE: USB RX allocation failed");
    while (true) delay(1000);
  }
  Serial.begin(115200);
  if (!settings.load()) Serial.println("XTE: settings defaults (missing or invalid NVS)");
  page = static_cast<Page>(settings.startupPage);
  // SDK display.begin() reuses an already initialized SPI bus. Its own begin
  // omits MISO; initialize it here from the verified hardware contract.
  digitalWrite(21, HIGH);
  pinMode(21, OUTPUT);
  digitalWrite(12, HIGH);
  pinMode(12, OUTPUT);
  SPI.begin(8, 7, 10, 21);
  SdMan.begin();
  network.begin();
  display.begin();
  Serial.printf("XTE: font reader bytes=%u path=%s\n", static_cast<unsigned>(sizeof(font)), xte::TfFont::PATH);
  pinMode(USB_DETECT, INPUT);
  usbPowered = digitalRead(USB_DETECT) == HIGH;
  sampleBattery();
  render(false, true);
  Serial.println("XTE: ready protocol=2 max_payload=384");
  network.info();
}

void loop() {
  tf.tick();
  input.update();
  handlePowerButton();
  if (!tf.active()) handlePageButtons();
  network.tick(handleNetworkLine);
  // Bound each pass; coalesce queued frames into the latest valid snapshot.
  for (size_t i = 0; i < 512 && Serial.available(); ++i) {
    xte::Status incoming;
    if (receiver.push(static_cast<char>(Serial.read()), incoming, handleUsbLine)) acceptStatus(incoming);
  }
  if (millis() - batterySampledAt >= 30000) sampleBattery();
  if (millis() - usbPolledAt >= 200) pollUsb();
  const uint32_t now = millis();
  if (volumePending && now - volumeRequestedAt >= 2000) {
    volumePending = false;
    status.volume = confirmedVolume;
    if (!appMenu && page == Page::VOLUME) renderNow = true;
  }
  const bool online = haveStatus && now - lastContact < OFFLINE_MS;
  if (online != wasOnline) dirty = true;
  if (!tf.active() && (forceFull || renderNow || backgroundRenderDue(now, online))) {
    render(online, forceFull, halfRefresh);
    wasOnline = online;
    dirty = false;
    forceFull = false;
    halfRefresh = false;
    renderNow = false;
  }
  delay(tf.active() ? 1 : 20);
}
