#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <EInkDisplay.h>
#include <InputManager.h>
#include <algorithm>
#include <driver/gpio.h>
#include <esp_sleep.h>

#include "protocol.h"
#include "sleep_art.h"
#include "tf_transfer.h"
#include "tf_font.h"
#include "settings.h"

namespace {
// SDK requires constructor pins; these values follow the verified X4 contract.
EInkDisplay display(8, 10, 21, 4, 5, 6);
InputManager input;
xte::LineReceiver receiver;
xte::Status status;
xte::TfTransfer tf;
xte::TfFont font;
bool handleTfLine(char* line, size_t length) { return tf.handle(line, length); }
constexpr uint32_t OFFLINE_MS = 15000;
uint32_t lastReceived = 0;
uint32_t lastRendered = 0;
bool haveStatus = false;
bool dirty = false;
bool wasOnline = false;
bool forceFull = false;
bool renderNow = false;
uint32_t volumeSequence = 0;
uint32_t volumeRequestedAt = 0;
bool volumePending = false;
float confirmedVolume = NAN;
using xte::Page;
Page page = Page::STATUS;
uint8_t selectedAgent = 0;
xte::Settings settings;
enum class SettingsMode : uint8_t { PREVIEW, LIST, EDIT, INFO, RESET };
SettingsMode settingsMode = SettingsMode::PREVIEW;
uint8_t selectedSetting = 0;
uint8_t draftSetting = 0;
const char* settingsMessage = nullptr;
constexpr const char* FIRMWARE_VERSION = "hints2-20261013";
constexpr uint8_t SETTING_COUNT = 5;
constexpr uint32_t POWER_HOLD_MS = 1500;
// The pinned SDK has no power-latch API; GPIO13 follows the verified X4 contract.
constexpr gpio_num_t POWER_LATCH = GPIO_NUM_13;
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

void metric(int16_t x, int16_t y, const char* label, const char* value) {
  uiText(x, y, label, 312);
  text(x, y + 54, 3, value);
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
  snprintf(value, sizeof(value), "左键 -%d", settings.volumeStep());
  uiText(48, 338, value, 300);
  snprintf(value, sizeof(value), "右键 +%d", settings.volumeStep());
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
    uiText(492, 354, "返回：拒绝", 260);
  } else {
    if (agent.text[0]) text(48, 294, 2, agent.text);
    else uiText(48, 294, "暂无待处理请求");
    uiText(48, 354, "左右键切换助手");
  }
}

const char* settingLabel(uint8_t index) {
  constexpr const char* labels[] = {"音量步长", "屏幕更新间隔", "启动页面", "设备信息", "恢复默认值"};
  return labels[index];
}

uint8_t settingIndex(uint8_t index) {
  return index == 0 ? settings.volumeIndex : index == 1 ? settings.intervalIndex : settings.startupPage;
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
  } else {
    snprintf(value, size, "%s", index == 3 ? "查看" : "重置");
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
    text(400, 208, 2, "v2 / USB CDC");
    uiText(48, 246, "TF卡状态", 260);
    uiText(400, 246, SdMan.ready() ? "已挂载" : "未挂载", 352);
    uiText(48, 296, "可用内存", 240);
    snprintf(value, sizeof(value), "%u bytes", static_cast<unsigned>(ESP.getFreeHeap()));
    text(400, 308, 2, value);
    uiText(48, 346, "字库状态", 240);
    uiText(400, 346, font.healthy() ? "读取正常" : "读取失败", 352);
    return;
  }
  if (settingsMode == SettingsMode::RESET) {
    uiText(48, 174, "恢复默认设置？", 704);
    uiText(48, 242, "音量步长 5%，更新间隔 5 秒", 704);
    uiText(48, 300, "启动进入状态页", 704);
    return;
  }
  for (uint8_t i = 0; i < SETTING_COUNT; ++i) {
    const int16_t y = 142 + i * 50;
    const bool selected = settingsMode != SettingsMode::PREVIEW && i == selectedSetting;
    const uint16_t color = selected ? 1 : 0;
    if (selected) graphics.fillRect(28, y - 2, 744, 48, 0);
    font.draw(graphics, 48, y, settingLabel(i), 440, color);
    char value[32];
    const uint8_t choice = settingsMode == SettingsMode::EDIT && i == selectedSetting ?
                           draftSetting : settingIndex(i);
    settingValue(i, choice, value, sizeof(value));
    font.draw(graphics, 520, y, value, 232, color);
  }
}

void render(bool online, bool full) {
  const uint32_t started = millis();
  font.beginRender();
  display.clearScreen();
  graphics.setTextColor(0);
  graphics.setTextWrap(false);
  text(28, 20, 3, "XTE-Link");
  uiText(570, 8, online ? "在线" : haveStatus ? "离线" : "等待连接", 202);
  text(28, 58, 2, haveStatus ? status.host : "USB SERIAL");
  graphics.drawFastHLine(28, 88, 744, 0);
  if (page == Page::STATUS) renderStatusPage();
  else if (page == Page::VOLUME) renderVolumePage();
  else if (page == Page::AGENTS) renderAgentPage();
  else renderSettingsPage();
  graphics.drawFastHLine(28, 400, 744, 0);
  if (page == Page::SETTINGS) {
    const char* hint = settingsMode == SettingsMode::PREVIEW ? "左右切页  确认进入" :
                       settingsMode == SettingsMode::LIST ? "上下选择  确认打开  返回退出" :
                       settingsMode == SettingsMode::EDIT ? "左右调整  确认保存  返回取消" :
                       settingsMode == SettingsMode::RESET ? "确认恢复  返回取消" : "返回：设置列表";
    uiText(28, 412, hint, 744);
  } else {
    uiText(28, 412, "更新", 120);
    text(150, 424, 2, haveStatus ? status.time : "--");
  }
  renderButtonHints();
  font.endRender();
  display.displayBuffer(full ? EInkDisplay::FULL_REFRESH : EInkDisplay::FAST_REFRESH);
  lastRendered = started;
  Serial.printf("XTE: rendered %s in %lu ms, heap=%u\n", full ? "full" : "fast",
                static_cast<unsigned long>(millis() - started), static_cast<unsigned>(ESP.getFreeHeap()));
  if (!font.healthy()) Serial.printf("XTE: font boxes: %s\n", font.error());
}

void sendVolumeDelta(int delta) {
  renderNow = true;
  if (haveStatus && std::isfinite(status.volume)) {
    status.volume = std::max(0.0f, std::min(100.0f, status.volume + delta));
  }
  if (++volumeSequence == 0) ++volumeSequence;
  volumePending = true;
  volumeRequestedAt = millis();
  Serial.printf("XTE: volume_delta %+d %lu\n", delta, static_cast<unsigned long>(volumeSequence));
  // Show the prediction before draining old status frames or PC confirmations.
  render(haveStatus && millis() - lastReceived < OFFLINE_MS, false);
  renderNow = false;
}

void sendAgentAction(const char* action) {
  if (status.agentCount == 0 || selectedAgent >= status.agentCount) return;
  const xte::Agent& agent = status.agents[selectedAgent];
  if (strcmp(agent.state, "ask") != 0 || agent.requestId[0] == '\0') return;
  Serial.printf("XTE: agent_action %s %s %s\n", action, agent.tool, agent.requestId);
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
  if (settingsMode == SettingsMode::PREVIEW) {
    if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      settingsMode = SettingsMode::LIST;
      settingsMessage = nullptr;
      renderNow = true;
    }
    return;
  }
  // Consume navigation here; Back cannot fall through to Agent-page rejection.
  if (input.wasPressed(InputManager::BTN_BACK)) {
    settingsMode = settingsMode == SettingsMode::LIST ? SettingsMode::PREVIEW : SettingsMode::LIST;
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
      if (selectedSetting < 3) {
        draftSetting = settingIndex(selectedSetting);
        settingsMode = SettingsMode::EDIT;
      } else {
        settingsMode = selectedSetting == 3 ? SettingsMode::INFO : SettingsMode::RESET;
      }
      renderNow = true;
    }
  } else if (settingsMode == SettingsMode::EDIT) {
    // Landscape top Left/Right are the SDK's Up/Down buttons.
    if (input.wasPressed(InputManager::BTN_UP) || input.wasPressed(InputManager::BTN_DOWN)) {
      const int delta = input.wasPressed(InputManager::BTN_UP) ? -1 : 1;
      draftSetting = static_cast<uint8_t>((draftSetting + delta + 3) % 3);
      settingsMessage = nullptr;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      xte::Settings next = settings;
      if (selectedSetting == 0) next.volumeIndex = draftSetting;
      else if (selectedSetting == 1) next.intervalIndex = draftSetting;
      else next.startupPage = draftSetting;
      if (commitSettings(next)) settingsMode = SettingsMode::LIST;
      renderNow = true;
    }
  } else if (settingsMode == SettingsMode::RESET && input.wasPressed(InputManager::BTN_CONFIRM)) {
    if (commitSettings(xte::Settings{})) settingsMode = SettingsMode::LIST;
    renderNow = true;
  }
}

void handlePageButtons() {
  if (page == Page::SETTINGS && settingsMode != SettingsMode::PREVIEW) {
    handleSettingsButtons();
    return;
  }
  // Page-switch events only switch pages; actions apply on a later key press.
  if (input.wasPressed(InputManager::BTN_UP) || input.wasPressed(InputManager::BTN_DOWN)) {
    const int delta = input.wasPressed(InputManager::BTN_UP) ? -1 : 1;
    page = static_cast<Page>((static_cast<int>(page) + delta + 4) % 4);
    settingsMode = SettingsMode::PREVIEW;
    settingsMessage = nullptr;
    renderNow = true;
    return;
  }
  if (page == Page::SETTINGS) handleSettingsButtons();
  else if (page == Page::VOLUME) {
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
    else if (input.wasPressed(InputManager::BTN_BACK)) sendAgentAction("reject");
  }
}

bool backgroundRenderDue(uint32_t now, bool online) {
  // The settings page keeps receiving snapshots without repainting on every sample.
  // Connection changes bypass the selected interval so stale data is visibly offline.
  return online != wasOnline ||
         (page != Page::SETTINGS && dirty && now - lastRendered >= settings.updateMs());
}

void renderSleep() {
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
  display.begin();
  Serial.printf("XTE: font reader bytes=%u path=%s\n", static_cast<unsigned>(sizeof(font)), xte::TfFont::PATH);
  render(false, true);
  Serial.println("XTE: ready protocol=2 max_payload=384");
}

void loop() {
  tf.tick();
  input.update();
  handlePowerButton();
  if (!tf.active()) handlePageButtons();
  // Bound each pass; coalesce queued frames into the latest valid snapshot.
  for (size_t i = 0; i < 512 && Serial.available(); ++i) {
    xte::Status incoming;
    if (receiver.push(static_cast<char>(Serial.read()), incoming, handleTfLine)) {
      const float previousVolume = status.volume;
      confirmedVolume = incoming.volume;
      if (volumePending && incoming.volumeAck != volumeSequence) {
        incoming.volume = previousVolume;
      } else {
        volumePending = false;
      }
      status = incoming;
      if (page == Page::VOLUME &&
          (std::isfinite(previousVolume) != std::isfinite(status.volume) ||
           (std::isfinite(status.volume) && previousVolume != status.volume))) renderNow = true;
      lastReceived = millis();
      haveStatus = true;
      dirty = true;
      Serial.println("XTE: accepted");
    }
  }
  const uint32_t now = millis();
  if (volumePending && now - volumeRequestedAt >= 2000) {
    volumePending = false;
    status.volume = confirmedVolume;
    if (page == Page::VOLUME) renderNow = true;
  }
  const bool online = haveStatus && now - lastReceived < OFFLINE_MS;
  if (online != wasOnline) dirty = true;
  if (!tf.active() && (forceFull || renderNow || backgroundRenderDue(now, online))) {
    render(online, forceFull);
    wasOnline = online;
    dirty = false;
    forceFull = false;
    renderNow = false;
  }
  delay(tf.active() ? 1 : 20);
}
