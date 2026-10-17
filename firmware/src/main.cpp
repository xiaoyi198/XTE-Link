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
#include "clash_panel.h"

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
bool appMenu = true;
constexpr uint8_t MENU_PAGE_COUNT = 2;
constexpr uint8_t MENU_TILE_COUNT = 4;
uint8_t menuPage = 0;
uint8_t menuSelection[MENU_PAGE_COUNT] = {};
uint8_t selectedAgent = 0;
xte::ClashPanel clash;
xte::Settings settings;
enum class SettingsMode : uint8_t { HOME, LIST, EDIT, INFO, RESET, GALLERY, PREVIEW, CONNECTION };
enum Setting : uint8_t {
  UPDATE, GHOST, CLEAN, IMAGE_MODE, GALLERY, START_MODE, START_APP, VOLUME,
  TRANSPORT, CONNECTION, RECONNECT, REMINDER, INFO, SLEEP_TIMEOUT, RESET
};
SettingsMode settingsMode = SettingsMode::HOME;
constexpr uint8_t GROUP_COUNT = 6;
constexpr Setting GROUP_ITEMS[GROUP_COUNT][3] = {
  {UPDATE, GHOST, CLEAN}, {IMAGE_MODE, GALLERY, GALLERY},
  {START_MODE, START_APP, VOLUME}, {TRANSPORT, CONNECTION, RECONNECT},
  {REMINDER, REMINDER, REMINDER}, {INFO, SLEEP_TIMEOUT, RESET}
};
constexpr uint8_t GROUP_SIZES[GROUP_COUNT] = {3, 2, 3, 3, 1, 3};
uint8_t selectedGroup = 0;
uint8_t selectedSetting = 0;
uint8_t draftSetting = 0;
const char* settingsMessage = nullptr;
constexpr const char* FIRMWARE_VERSION = "settings-center-20261018";
std::vector<String> gallery;
uint16_t gallerySelection = 0;
uint16_t fastRefreshes = 0;
uint32_t lastUserActivity = 0;
Page lastApp = Page::STATUS;
bool attentionQueued = false;
bool renderGalleryPreview();
uint8_t pendingApprovals();
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
  constexpr const char* labels[MENU_PAGE_COUNT][MENU_TILE_COUNT] = {
    {"电脑状态", "系统音量", "助手控制", "设置"},
    {"快捷功能", "Clash Verge", "占位二", "占位三"},
  };
  for (uint8_t i = 0; i < MENU_TILE_COUNT; ++i) {
    const int16_t x = 40 + (i % 2) * 368;
    const int16_t y = 104 + (i / 2) * 144;
    const bool selected = i == menuSelection[menuPage];
    graphics.drawRect(x, y, 348, 132, 0);
    if (selected) graphics.drawRect(x + 3, y + 3, 342, 126, 0);
    const int16_t ix = x + 24, iy = y + 32;
    if (menuPage == 1) {
      if (i == 0) {
        // Lightning: shortcut placeholder, using the existing GFX primitives.
        graphics.fillRect(ix + 30, iy, 20, 6, 0);
        for (int j = 0; j < 5; ++j) {
          graphics.fillRect(ix + 26 - j * 4, iy + 6 + j * 5, 20, 5, 0);
          graphics.fillRect(ix + 26 - j * 4, iy + 31 + j * 5, 20, 5, 0);
        }
      } else if (i == 1) {
        // Connected nodes identify the Clash Verge controller entry.
        graphics.drawRect(ix, iy + 20, 20, 20, 0);
        graphics.drawRect(ix + 44, iy, 20, 20, 0);
        graphics.drawRect(ix + 44, iy + 40, 20, 20, 0);
        graphics.drawFastHLine(ix + 20, iy + 30, 15, 0);
        graphics.drawFastVLine(ix + 34, iy + 10, 41, 0);
        graphics.drawFastHLine(ix + 34, iy + 10, 10, 0);
        graphics.drawFastHLine(ix + 34, iy + 50, 10, 0);
      } else if (i == 2) {
        graphics.drawRect(ix + 10, iy, 44, 58, 0);
        for (int j = 0; j < 3; ++j)
          graphics.drawFastHLine(ix + 20, iy + 16 + j * 12, 24, 0);
      } else {
        graphics.drawRect(ix + 6, iy + 2, 52, 52, 0);
        graphics.fillRect(ix + 29, iy + 14, 6, 28, 0);
        graphics.fillRect(ix + 18, iy + 25, 28, 6, 0);
      }
    } else if (i == 0) {
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
    if (menuPage == 1 && i == 1)
      text(x + 112, y + 50, 3, labels[menuPage][i]);  // 198 px; TF text would be 250 px.
    else font.draw(graphics, x + 112, y + 42, labels[menuPage][i], 220);
    if (selected) graphics.fillRect(x + 112, y + 99, 64, 4, 0);
    if (menuPage == 0 && i == 2 && settings.reminderMode && pendingApprovals()) {
      char count[4]; snprintf(count, sizeof(count), "%u", static_cast<unsigned>(pendingApprovals()));
      graphics.fillRect(x + 290, y + 12, 38, 32, 0);
      graphics.setCursor(x + 303, y + 21); graphics.setTextSize(2); graphics.setTextColor(1);
      graphics.print(count); graphics.setTextColor(0);
    }
  }
  for (uint8_t i = 0; i < MENU_PAGE_COUNT; ++i) {
    const int16_t x = 382 + i * 28;
    if (i == menuPage) graphics.fillRect(x, 386, 8, 8, 0);
    else graphics.drawRect(x, 386, 8, 8, 0);
  }
  char indicator[8];
  snprintf(indicator, sizeof(indicator), "%u/%u", static_cast<unsigned>(menuPage + 1),
           static_cast<unsigned>(MENU_PAGE_COUNT));
  text(716, 382, 2, indicator);
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

const char* groupLabel(uint8_t index) {
  constexpr const char* labels[] = {"显示与刷新", "屏保图库", "启动与操作", "连接与配对", "助手提醒", "设备与电源"};
  return labels[index];
}

const char* settingLabel(uint8_t index) {
  constexpr const char* labels[] = {
    "屏幕更新间隔", "自动清残影", "立即清屏", "屏保模式", "选择与预览",
    "启动方式", "默认应用", "音量步长", "连接方式", "连接诊断", "重新连接",
    "审批提醒", "设备信息", "电池自动休眠", "恢复默认值"
  };
  return labels[index];
}

const char* settingHelp(uint8_t index) {
  constexpr const char* help[] = {
    "普通数据刷新；审批事件即时更新", "累计快刷后全刷；周期需实屏调整", "与短按电源键相同",
    "固定图片在图库中选择", "预览后按确认可设为固定屏保", "开机与唤醒时生效",
    "用于菜单选中项或直接启动", "每次按键调整的音量百分比", "局域网需先用USB配置WiFi",
    "区分供电、WiFi和电脑连接", "重新握手，不修改配对资料", "编辑设置时不会自动跳页",
    "字库与TF状态在打开时检查", "仅电池闲置；休眠停止接收消息", "重置偏好，保留WiFi和TF文件"
  };
  return help[index];
}

uint8_t settingIndex(uint8_t index) {
  switch (index) {
    case UPDATE: return settings.intervalIndex;
    case GHOST: return settings.ghostIndex;
    case IMAGE_MODE: return settings.imageMode;
    case START_MODE: return settings.startupMode;
    case START_APP: return settings.startupPage;
    case VOLUME: return settings.volumeIndex;
    case TRANSPORT: return network.isLan();
    case REMINDER: return settings.reminderMode;
    case SLEEP_TIMEOUT: return settings.sleepTimeoutIndex;
    default: return 0;
  }
}

uint8_t settingChoices(uint8_t index) {
  if (index == START_APP) return 5;
  if (index == GHOST || index == SLEEP_TIMEOUT) return 4;
  return index == TRANSPORT ? 2 : 3;
}

void settingValue(uint8_t index, uint8_t choice, char* value, size_t size) {
  const char* label = "打开";
  switch (index) {
    case UPDATE: {
      constexpr int seconds[] = {5, 10, 30};
      snprintf(value, size, "%d 秒", seconds[choice]); return;
    }
    case VOLUME: {
      constexpr int steps[] = {1, 5, 10};
      snprintf(value, size, "%d %%", steps[choice]); return;
    }
    case GHOST: {
      constexpr int counts[] = {0, 20, 50, 100};
      if (choice) { snprintf(value, size, "%d 次快刷", counts[choice]); return; }
      label = "关闭"; break;
    }
    case IMAGE_MODE: {
      constexpr const char* modes[] = {"随机", "固定一张", "顺序"}; label = modes[choice]; break;
    }
    case START_MODE: {
      constexpr const char* modes[] = {"应用菜单", "指定应用", "上次应用"}; label = modes[choice]; break;
    }
    case START_APP: {
      constexpr const char* apps[] = {"电脑状态", "系统音量", "助手控制", "设置", "Clash Verge"}; label = apps[choice]; break;
    }
    case TRANSPORT: label = choice ? "局域网连接" : "串口直连"; break;
    case REMINDER: {
      constexpr const char* modes[] = {"关闭", "仅角标", "切到助手"}; label = modes[choice]; break;
    }
    case SLEEP_TIMEOUT: {
      constexpr int minutes[] = {0, 5, 15, 30};
      if (choice) { snprintf(value, size, "%d 分钟", minutes[choice]); return; }
      label = "关闭"; break;
    }
    case RESET: label = "重置"; break;
    default: break;
  }
  snprintf(value, size, "%s", label);
}

void settingsRow(int16_t y, const char* label, const char* value, bool selected = false) {
  if (selected) graphics.fillRect(28, y, 744, 42, 0);
  const uint16_t color = selected ? 1 : 0;
  font.draw(graphics, 48, y, label, 342, color);
  font.draw(graphics, 410, y, value, 342, color);
}

uint8_t pendingApprovals() {
  if (!haveStatus || millis() - lastContact >= OFFLINE_MS) return 0;
  uint8_t count = 0;
  for (uint8_t i = 0; i < status.agentCount; ++i)
    if (!strcmp(status.agents[i].state, "ask") && status.agents[i].requestId[0]) ++count;
  return count;
}

void renderSettingsPage() {
  uiText(48, 96, settingsMode == SettingsMode::HOME ? "设置" : groupLabel(selectedGroup), 342);
  char value[96];
  if (settingsMode == SettingsMode::HOME) {
    for (uint8_t i = 0; i < GROUP_COUNT; ++i) {
      if (i == 0) snprintf(value, sizeof(value), "%lu 秒更新", static_cast<unsigned long>(settings.updateMs() / 1000));
      else if (i == 1) settingValue(IMAGE_MODE, settings.imageMode, value, sizeof(value));
      else if (i == 2) settingValue(START_MODE, settings.startupMode, value, sizeof(value));
      else if (i == 3) snprintf(value, sizeof(value), "%s / %s", network.isLan() ? "局域网" : "USB",
                               haveStatus && millis() - lastContact < OFFLINE_MS ? "在线" : "等待");
      else if (i == 4) settingValue(REMINDER, settings.reminderMode, value, sizeof(value));
      else snprintf(value, sizeof(value), "%s", SdMan.ready() ? "TF已挂载" : "TF未挂载");
      settingsRow(138 + i * 42, groupLabel(i), value, i == selectedGroup);
    }
    return;
  }
  if (settingsMode == SettingsMode::INFO) {
    settingsRow(146, "固件版本", ""); text(410, 158, 2, FIRMWARE_VERSION);
    settingsRow(188, "TF卡状态", SdMan.ready() ? "已挂载" : "未挂载");
    settingsRow(230, "字库状态", font.healthy() ? "读取正常" : "读取失败");
    snprintf(value, sizeof(value), "%u bytes", static_cast<unsigned>(ESP.getFreeHeap()));
    settingsRow(272, "可用内存", ""); text(410, 284, 2, value);
    snprintf(value, sizeof(value), "%u mV", static_cast<unsigned>(batteryMv));
    settingsRow(314, "电池参考电压", ""); text(410, 326, 2, value);
    return;
  }
  if (settingsMode == SettingsMode::CONNECTION) {
    settingsRow(138, "USB供电", usbPowered ? "已接电" : "电池供电");
    settingsRow(180, "电脑驱动", haveStatus && millis() - lastContact < OFFLINE_MS ? "在线" : "等待连接");
    settingsRow(222, "WiFi名称", network.ready() ? network.ssid() : "未配置");
    settingsRow(264, "WiFi状态", !network.isLan() ? "未启用" :
                WiFi.status() == WL_CONNECTED ? "已连接" : network.ready() ? "连接中" : "未配置");
    settingsRow(306, "局域网地址", "");
    text(410, 318, 2, WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "--");
    if (WiFi.status() == WL_CONNECTED) snprintf(value, sizeof(value), "%d dBm / v2", WiFi.RSSI());
    else snprintf(value, sizeof(value), "-- / v2");
    settingsRow(348, "信号与协议", ""); text(410, 360, 2, value);
    return;
  }
  if (settingsMode == SettingsMode::RESET) {
    uiText(48, 160, "恢复默认设置？", 704);
    uiText(48, 220, "重置偏好，连接改为串口", 704);
    uiText(48, 276, "保留WiFi配对、图片与字库", 704);
    uiText(48, 332, settingsMessage ? settingsMessage : "自动休眠关闭，屏保改为随机", 704);
    return;
  }
  if (settingsMode == SettingsMode::GALLERY) {
    snprintf(value, sizeof(value), "%u / %u", gallery.empty() ? 0u : static_cast<unsigned>(gallerySelection + 1),
             static_cast<unsigned>(gallery.size()));
    text(610, 108, 2, value);
    if (gallery.empty()) {
      uiText(48, 190, "暂无图片，请用电脑驱动上传", 704);
      uiText(48, 256, "休眠时使用内置像素画", 704);
    } else {
      const uint16_t start = (gallerySelection / 4) * 4;
      for (uint16_t i = start; i < gallery.size() && i < start + 4; ++i) {
        const int16_t y = 146 + (i - start) * 44;
        const bool selected = i == gallerySelection;
        if (selected) graphics.fillRect(28, y, 744, 42, 0);
        const char* path = gallery[i].c_str();
        font.draw(graphics, 48, y, strrchr(path, '/') + 1, 660, selected ? 1 : 0);
        if (settings.imageMode == 1 && !strcmp(path, settings.sleepFile))
          font.draw(graphics, 710, y, "*", 40, selected ? 1 : 0);
      }
    }
    if (settingsMessage) uiText(48, 348, settingsMessage, 704);
    return;
  }
  if (settingsMode == SettingsMode::EDIT) {
    uiText(48, 166, settingLabel(selectedSetting), 704);
    settingValue(selectedSetting, draftSetting, value, sizeof(value));
    uiText(96, 236, value, 608);
    uiText(48, 326, settingsMessage ? settingsMessage : settingHelp(selectedSetting), 704);
    return;
  }
  for (uint8_t i = 0; i < GROUP_SIZES[selectedGroup]; ++i) {
    const Setting item = GROUP_ITEMS[selectedGroup][i];
    settingValue(item, settingIndex(item), value, sizeof(value));
    settingsRow(152 + i * 54, settingLabel(item), value, item == selectedSetting);
  }
  uiText(48, 336, settingsMessage ? settingsMessage : settingHelp(selectedSetting), 704);
}

void renderClashPage() {
  text(48, 108, 3, "Clash Verge");
  const char* title = !strcmp(clash.view, "profiles") ? "订阅列表" :
                      !strcmp(clash.view, "groups") ? "策略组" :
                      !strcmp(clash.view, "nodes") ? "节点选择" : "控制面板";
  uiText(310, 96, title, 350);
  char indicator[16];
  snprintf(indicator, sizeof(indicator), "%u/%u", static_cast<unsigned>(clash.page + 1),
           static_cast<unsigned>(clash.pages));
  text(668, 108, 2, indicator);
  if (clash.confirming) {
    const bool isSwitch = !strcmp(clash.view, "home") && (clash.selected == 2 || clash.selected == 3);
    uiText(48, 160, isSwitch ? "确认切换此开关？" : "确认执行此操作？", 704);
    const char* name = clash.rows[clash.selected].name;
    if (isSwitch && (strstr(name, "开启") || strstr(name, "关闭"))) {
      const bool enabled = strstr(name, "开启") != nullptr;
      const char* target = clash.selected == 3 ? (enabled ? "TUN：开启 -> 关闭" : "TUN：关闭 -> 开启") :
                                              (enabled ? "系统代理：开启 -> 关闭" : "系统代理：关闭 -> 开启");
      uiText(48, 224, target, 704);
    } else uiText(48, 224, name, 704);
    uiText(48, 300, "确认执行  返回取消", 704);
  } else if (clash.pending || !clash.complete) {
    uiText(48, 182, clash.message[0] ? clash.message : "确认刷新状态", 704);
    if (!clash.pending) uiText(48, 270, "确认重试  返回上级", 704);
  } else {
    for (uint8_t i = 0; i < clash.count; ++i) {
      const int16_t y = 142 + i * 42;
      const bool selected = i == clash.selected;
      const uint16_t color = selected ? 1 : 0;
      if (selected) graphics.fillRect(28, y - 2, 744, 42, 0);
      const xte::ClashPanel::Row& row = clash.rows[i];
      graphics.setTextColor(color);
      text(40, y + 12, 2, row.current ? "*" : row.enabled ? ">" : "-");
      font.draw(graphics, 68, y, row.name, 680, color);
    }
    graphics.setTextColor(0);
    if (!clash.count) uiText(48, 190, "暂无条目", 704);
    uiText(48, 354, clash.message, 704);
  }
}

void render(bool online, bool full, bool half = false) {
  const uint32_t started = millis();
  if (!appMenu && page == Page::SETTINGS && settingsMode == SettingsMode::PREVIEW) {
    if (renderGalleryPreview()) { lastRendered = started; fastRefreshes = 0; return; }
    full = true; // Failed preview may have modified the shared framebuffer.
  }
  if (!half && settings.ghostEvery() && fastRefreshes >= settings.ghostEvery() - 1) full = true;
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
  char hostLabel[31];
  snprintf(hostLabel, sizeof(hostLabel), "%.30s", haveStatus ? status.host : network.isLan() ? "LAN TCP" : "USB SERIAL");
  text(28, 58, 2, hostLabel);
  if (settings.reminderMode && pendingApprovals() && !(appMenu && menuPage == 0)) {
    char count[32]; snprintf(count, sizeof(count), "待审批 %u", static_cast<unsigned>(pendingApprovals()));
    uiText(400, 44, count, 312);
  }
  graphics.drawFastHLine(28, 88, 744, 0);
  if (appMenu) renderAppMenu();
  else if (page == Page::STATUS) renderStatusPage();
  else if (page == Page::VOLUME) renderVolumePage();
  else if (page == Page::AGENTS) renderAgentPage();
  else if (page == Page::CLASH) renderClashPage();
  else renderSettingsPage();
  graphics.drawFastHLine(28, 400, 744, 0);
  if (appMenu) {
    uiText(28, 412, "左右翻页  上下选择  确认打开", 744);
  } else if (page == Page::CLASH) {
    uiText(28, 412, clash.pending ? "正在读取  返回取消等待" :
                               "上下选择  左右翻页  确认进入", 744);
  } else if (page == Page::SETTINGS) {
    const char* hint = settingsMode == SettingsMode::HOME ? "上下选择  确认打开  返回菜单" :
                       settingsMode == SettingsMode::LIST ? "上下选择  确认打开  返回分类" :
                       settingsMode == SettingsMode::EDIT ? "左右调整  确认保存  返回取消" :
                       settingsMode == SettingsMode::GALLERY ? "上下选择  确认预览  返回列表" :
                       settingsMode == SettingsMode::RESET ? "确认恢复  返回取消" : "确认刷新  返回列表";
    uiText(28, 412, hint, 744);
  } else {
    uiText(28, 412, "返回菜单", 180);
    text(230, 424, 2, haveStatus ? status.time : "--");
  }
  renderButtonHints();
  font.endRender();
  display.displayBuffer(full ? EInkDisplay::FULL_REFRESH :
                        half ? EInkDisplay::HALF_REFRESH : EInkDisplay::FAST_REFRESH);
  if (full) fastRefreshes = 0;
  else if (!half && fastRefreshes < UINT16_MAX) ++fastRefreshes;
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
  if (next.sameAs(settings)) {
    settingsMessage = "未修改";
    return true;
  }
  if (!next.save()) {
    settingsMessage = "保存失败";
    Serial.println("XTE: settings save failed");
    return false;
  }
  if (next.ghostIndex != settings.ghostIndex) fastRefreshes = 0;
  if (next.reminderMode != settings.reminderMode) attentionQueued = false;
  settings = next;
  lastUserActivity = millis();
  settingsMessage = "已保存";
  Serial.printf("XTE: settings saved volume_step=%d update_ms=%lu startup_page=%u\n",
                settings.volumeStep(), static_cast<unsigned long>(settings.updateMs()),
                static_cast<unsigned>(settings.startupPage));
  return true;
}

void handleSettingsButtons() {
  // Consume navigation here; Back cannot fall through to Agent-page rejection.
  if (input.wasPressed(InputManager::BTN_BACK)) {
    if (settingsMode == SettingsMode::HOME) { appMenu = true; halfRefresh = true; }
    else if (settingsMode == SettingsMode::LIST) settingsMode = SettingsMode::HOME;
    else if (settingsMode == SettingsMode::PREVIEW) { settingsMode = SettingsMode::GALLERY; forceFull = true; }
    else {
      if (settingsMode == SettingsMode::GALLERY) std::vector<String>().swap(gallery);
      settingsMode = SettingsMode::LIST;
    }
    settingsMessage = nullptr;
    renderNow = true;
    return;
  }
  if (settingsMode == SettingsMode::HOME) {
    if (input.wasPressed(InputManager::BTN_RIGHT) || input.wasPressed(InputManager::BTN_LEFT)) {
      const int delta = input.wasPressed(InputManager::BTN_RIGHT) ? -1 : 1;
      selectedGroup = (selectedGroup + GROUP_COUNT + delta) % GROUP_COUNT;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      selectedSetting = GROUP_ITEMS[selectedGroup][0];
      settingsMode = SettingsMode::LIST;
      settingsMessage = nullptr;
      renderNow = true;
    }
  } else if (settingsMode == SettingsMode::LIST) {
    // Landscape side Up/Down are the SDK's Right/Left buttons.
    if (input.wasPressed(InputManager::BTN_RIGHT)) {
      uint8_t row = 0;
      while (GROUP_ITEMS[selectedGroup][row] != selectedSetting) ++row;
      selectedSetting = GROUP_ITEMS[selectedGroup][(row + GROUP_SIZES[selectedGroup] - 1) % GROUP_SIZES[selectedGroup]];
      settingsMessage = nullptr;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_LEFT)) {
      uint8_t row = 0;
      while (GROUP_ITEMS[selectedGroup][row] != selectedSetting) ++row;
      selectedSetting = GROUP_ITEMS[selectedGroup][(row + 1) % GROUP_SIZES[selectedGroup]];
      settingsMessage = nullptr;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      settingsMessage = nullptr;
      if (selectedSetting == INFO) settingsMode = SettingsMode::INFO;
      else if (selectedSetting == CONNECTION) settingsMode = SettingsMode::CONNECTION;
      else if (selectedSetting == RESET) settingsMode = SettingsMode::RESET;
      else if (selectedSetting == CLEAN) forceFull = true;
      else if (selectedSetting == GALLERY) {
        gallery = xte::SleepImage::catalog();
        gallerySelection = 0;
        for (uint16_t i = 0; i < gallery.size(); ++i)
          if (!strcmp(gallery[i].c_str(), settings.sleepFile)) gallerySelection = i;
        settingsMode = SettingsMode::GALLERY;
      } else if (selectedSetting == RECONNECT) {
        network.reconnect();
        receiver.reset(); haveStatus = false; lastContact = 0; volumePending = false;
        status = xte::Status{}; attentionQueued = false;
        settingsMessage = "正在重新连接";
        network.info();
      } else {
        draftSetting = settingIndex(selectedSetting);
        settingsMode = SettingsMode::EDIT;
      }
      renderNow = true;
    }
  } else if (settingsMode == SettingsMode::EDIT) {
    // Landscape top Left/Right are the SDK's Up/Down buttons.
    if (input.wasPressed(InputManager::BTN_UP) || input.wasPressed(InputManager::BTN_DOWN)) {
      const int delta = input.wasPressed(InputManager::BTN_UP) ? -1 : 1;
      const int count = settingChoices(selectedSetting);
      draftSetting = static_cast<uint8_t>((draftSetting + delta + count) % count);
      settingsMessage = nullptr;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      if (selectedSetting == TRANSPORT) {
        if (!network.setMode(draftSetting != 0)) {
          settingsMessage = network.ready() ? "保存失败" : "请先用USB配置WiFi";
        } else {
          receiver.reset(); haveStatus = false; lastContact = 0; volumePending = false; attentionQueued = false;
          status = xte::Status{};
          settingsMode = SettingsMode::LIST;
          settingsMessage = "已保存";
          network.info();
        }
        renderNow = true;
        return;
      }
      xte::Settings next = settings;
      switch (selectedSetting) {
        case UPDATE: next.intervalIndex = draftSetting; break;
        case GHOST: next.ghostIndex = draftSetting; break;
        case IMAGE_MODE:
          if (draftSetting == 1 && !settings.sleepFile[0]) {
            settingsMessage = "请先在图库选择固定图片"; renderNow = true; return;
          }
          next.imageMode = draftSetting; break;
        case START_MODE: next.startupMode = draftSetting; break;
        case START_APP: next.startupPage = draftSetting; break;
        case VOLUME: next.volumeIndex = draftSetting; break;
        case REMINDER: next.reminderMode = draftSetting; break;
        case SLEEP_TIMEOUT: next.sleepTimeoutIndex = draftSetting; break;
        default: return;
      }
      if (commitSettings(next)) settingsMode = SettingsMode::LIST;
      renderNow = true;
    }
  } else if (settingsMode == SettingsMode::GALLERY || settingsMode == SettingsMode::PREVIEW) {
    if (gallery.empty()) return;
    const bool preview = settingsMode == SettingsMode::PREVIEW;
    const uint8_t previous = preview ? InputManager::BTN_UP : InputManager::BTN_RIGHT;
    const uint8_t next = preview ? InputManager::BTN_DOWN : InputManager::BTN_LEFT;
    if (input.wasPressed(previous) || input.wasPressed(next)) {
      const int delta = input.wasPressed(previous) ? -1 : 1;
      gallerySelection = static_cast<uint16_t>((gallerySelection + gallery.size() + delta) % gallery.size());
      settingsMessage = nullptr; renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      if (!preview) settingsMode = SettingsMode::PREVIEW;
      else {
        xte::Settings nextSettings = settings;
        nextSettings.imageMode = 1;
        snprintf(nextSettings.sleepFile, sizeof(nextSettings.sleepFile), "%s", gallery[gallerySelection].c_str());
        const bool saved = commitSettings(nextSettings);
        settingsMode = SettingsMode::GALLERY;
        if (saved) settingsMessage = "已设为固定屏保";
        forceFull = true;
      }
      renderNow = true;
    }
  } else if ((settingsMode == SettingsMode::INFO || settingsMode == SettingsMode::CONNECTION) &&
             input.wasPressed(InputManager::BTN_CONFIRM)) renderNow = true;
  else if (settingsMode == SettingsMode::RESET && input.wasPressed(InputManager::BTN_CONFIRM)) {
    // Save preferences first; report partial transport failure and allow retry.
    if (!commitSettings(xte::Settings{})) { renderNow = true; return; }
    if (network.setMode(false)) {
      receiver.reset(); haveStatus = false; lastContact = 0; volumePending = false;
      status = xte::Status{}; attentionQueued = false;
      settingsMode = SettingsMode::LIST;
      network.info();
    } else settingsMessage = "串口切换失败，请重试";
    renderNow = true;
  }
}

void sendClashRequest(bool activate = false) {
  char message[256];
  const uint8_t index = clash.selected;
  const bool hasRevision = clash.complete && clash.revision[0];
  const bool longRequest = activate && (clash.confirming || !strcmp(clash.view, "profiles"));
  clash.begin(millis());
  clash.longRequest = longRequest;
  snprintf(message, sizeof(message),
           "XTE: clash {\"id\":%lu,\"op\":\"%s\",\"view\":\"%s\",\"page\":%u,\"rev\":\"%s\",\"index\":%u}\n",
           static_cast<unsigned long>(clash.sequence), activate ? "activate" : "list",
           clash.view, static_cast<unsigned>(clash.page), hasRevision ? clash.revision : "",
           static_cast<unsigned>(index));
  if (network.isLan()) network.send(message); else Serial.print(message);
  renderNow = true;
}

void handleClashButtons() {
  if (input.wasPressed(InputManager::BTN_BACK)) {
    if (clash.confirming) { clash.confirming = false; renderNow = true; return; }
    clash.pending = false;
    if (!strcmp(clash.view, "home")) { appMenu = true; halfRefresh = true; renderNow = true; return; }
    strcpy(clash.view, !strcmp(clash.view, "nodes") ? "groups" : "home");
    clash.page = 0; sendClashRequest(); return;
  }
  if (clash.pending) return;
  if (clash.confirming) {
    if (input.wasPressed(InputManager::BTN_CONFIRM)) sendClashRequest(true);
    return;
  }
  if (input.wasPressed(InputManager::BTN_CONFIRM)) {
    if (!clash.complete || !clash.count) { sendClashRequest(); return; }
    const bool writes = !strcmp(clash.view, "nodes") || !strcmp(clash.view, "profiles") ||
                        (!strcmp(clash.view, "home") && (clash.selected == 2 || clash.selected == 3));
    if (writes && clash.rows[clash.selected].enabled) { clash.confirming = true; renderNow = true; }
    else sendClashRequest(true);
  } else if (clash.complete && clash.count &&
             (input.wasPressed(InputManager::BTN_RIGHT) || input.wasPressed(InputManager::BTN_LEFT))) {
    const int delta = input.wasPressed(InputManager::BTN_RIGHT) ? -1 : 1;
    clash.selected = (clash.selected + clash.count + delta) % clash.count;
    renderNow = true;
  } else if (clash.complete && clash.pages > 1 &&
             (input.wasPressed(InputManager::BTN_UP) || input.wasPressed(InputManager::BTN_DOWN))) {
    const int delta = input.wasPressed(InputManager::BTN_UP) ? -1 : 1;
    clash.page = (clash.page + clash.pages + delta) % clash.pages;
    sendClashRequest();
  }
}

void openApp(Page next, bool remember = true) {
  page = next;
  menuPage = next == Page::CLASH ? 1 : 0;
  menuSelection[menuPage] = next == Page::CLASH ? 1 : static_cast<uint8_t>(next);
  appMenu = false;
  settingsMode = SettingsMode::HOME;
  settingsMessage = nullptr;
  halfRefresh = true;
  renderNow = true;
  if (next == Page::AGENTS) attentionQueued = false;
  if (next == Page::CLASH) { strcpy(clash.view, "home"); clash.page = 0; sendClashRequest(); }
  if (remember && settings.startupMode == 2 && next != Page::SETTINGS && next != lastApp) {
    Preferences prefs;
    if (prefs.begin("xte-link", false)) {
      if (prefs.putUChar("lastApp", static_cast<uint8_t>(next)) == 1) lastApp = next;
      else Serial.println("XTE: last app save failed");
      prefs.end();
    }
  }
}

void initializeStartup() {
  lastApp = static_cast<Page>(settings.startupPage);
  Preferences prefs;
  if (prefs.begin("xte-link", true)) {
    const uint8_t saved = prefs.getUChar("lastApp", settings.startupPage);
    if (saved < 5 && saved != static_cast<uint8_t>(Page::SETTINGS)) lastApp = static_cast<Page>(saved);
    prefs.end();
  }
  const Page next = settings.startupMode == 2 ? lastApp : static_cast<Page>(settings.startupPage);
  menuPage = next == Page::CLASH ? 1 : 0;
  menuSelection[menuPage] = next == Page::CLASH ? 1 : static_cast<uint8_t>(next);
  appMenu = true;
  if (settings.startupMode) openApp(next, false);
  lastUserActivity = millis();
}

bool anyButtonHeld() {
  for (uint8_t i = 0; i <= InputManager::BTN_POWER; ++i)
    if (input.isPressed(i) || input.wasPressed(i)) return true;
  return false;
}

void applyAgentAttention(uint32_t now) {
  if (!pendingApprovals() || settings.reminderMode != 2) { attentionQueued = false; return; }
  if (!attentionQueued || tf.active() || anyButtonHeld() || now - lastUserActivity < 800 ||
      (!appMenu && (page == Page::SETTINGS || (page == Page::CLASH && (clash.confirming || clash.pending))))) return;
  for (uint8_t i = 0; i < status.agentCount; ++i) {
    if (!strcmp(status.agents[i].state, "ask") && status.agents[i].requestId[0]) {
      selectedAgent = i;
      if (appMenu || page != Page::AGENTS) openApp(Page::AGENTS, false);
      attentionQueued = false;
      renderNow = true;
      return;
    }
  }
}

bool idleSleepDue(uint32_t now) {
  if (usbPowered || tf.active() || pendingApprovals() || anyButtonHeld() ||
      (!appMenu && (page == Page::SETTINGS || (page == Page::CLASH && (clash.pending || clash.confirming))))) {
    lastUserActivity = now;
    return false;
  }
  return settings.sleepMs() && now - lastUserActivity >= settings.sleepMs();
}

void handlePageButtons() {
  if (appMenu) {
    // Landscape Left/Right = SDK Up/Down; landscape Up/Down = SDK Right/Left.
    if (input.wasPressed(InputManager::BTN_UP) || input.wasPressed(InputManager::BTN_DOWN)) {
      const int delta = input.wasPressed(InputManager::BTN_UP) ? -1 : 1;
      menuPage = (menuPage + MENU_PAGE_COUNT + delta) % MENU_PAGE_COUNT;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_RIGHT) || input.wasPressed(InputManager::BTN_LEFT)) {
      const int delta = input.wasPressed(InputManager::BTN_RIGHT) ? -1 : 1;
      uint8_t& selected = menuSelection[menuPage];
      selected = (selected + MENU_TILE_COUNT + delta) % MENU_TILE_COUNT;
      renderNow = true;
    } else if (input.wasPressed(InputManager::BTN_CONFIRM)) {
      if (menuPage == 1 && menuSelection[1] != 1) return;
      openApp(menuPage == 1 ? Page::CLASH : static_cast<Page>(menuSelection[0]));
      return;  // Opening an app must never also trigger its action.
    }
    return;
  }
  if (page == Page::CLASH) { handleClashButtons(); return; }
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

void noticeAgent(const xte::Agent& previous, const xte::Agent& next) {
  if (settings.reminderMode == 2 && !strcmp(next.state, "ask") && next.requestId[0] &&
      (strcmp(previous.state, "ask") || strcmp(previous.requestId, next.requestId) || strcmp(previous.tool, next.tool)))
    attentionQueued = true;
}

void acceptStatus(xte::Status incoming) {
  const bool wasConnected = haveStatus && millis() - lastContact < OFFLINE_MS;
  const uint8_t previousApprovals = pendingApprovals();
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
  if (incoming.hasAgents)
    for (uint8_t i = 0; i < incoming.agentCount; ++i) noticeAgent(status.agents[i], incoming.agents[i]);
  status = incoming;
  if (!appMenu && page == Page::VOLUME &&
      (std::isfinite(previousVolume) != std::isfinite(status.volume) ||
       (std::isfinite(status.volume) && previousVolume != status.volume))) renderNow = true;
  lastReceived = lastContact = millis();
  haveStatus = true;
  const uint8_t approvals = pendingApprovals();
  if (settings.reminderMode && approvals != previousApprovals) renderNow = true;
  if (settings.reminderMode == 2 && approvals > previousApprovals) attentionQueued = true;
  dirty = true;
  replyActive("XTE: accepted\n");
  // A direct Clash startup can precede USB/LAN driver availability. Retry only
  // its read request when fresh metrics establish the connection.
  if (!wasConnected && !appMenu && page == Page::CLASH && !clash.confirming) sendClashRequest();
}

bool handlePush(char* line, size_t length) {
  StaticJsonDocument<768> doc;
  if (!xte::readControl(line, length, doc)) return false;
  if (doc["cmd"] == "heartbeat") {
    lastContact = millis();
    replyActive("XTE: pong\n");
  } else if (doc["cmd"] == "clash") {
    if (!appMenu && page == Page::CLASH && clash.accept(doc)) {
      lastContact = millis();
      renderNow = !clash.pending;
      replyActive("XTE: clash_received\n");
    }
  } else if (doc["cmd"] == "agents") {
    const uint8_t previousApprovals = pendingApprovals();
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
      noticeAgent(status.agents[index], updates[i]);
      status.agents[index] = updates[i];
    }
    lastContact = millis();
    if (changed) {
      dirty = true;
      if (!appMenu && page == Page::AGENTS) renderNow = true;
    }
    const uint8_t approvals = pendingApprovals();
    if (settings.reminderMode && approvals != previousApprovals) renderNow = true;
    if (settings.reminderMode == 2 && approvals > previousApprovals) attentionQueued = true;
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

bool renderSleepImage(const char* previewPath = nullptr) {
  xte::SleepImage image;
  uint8_t* frame = display.getFrameBuffer();
  if (!(previewPath ? image.openPath(previewPath) : image.open(settings))) return false;
  const auto hints = [&]() {
    if (!previewPath) return;
    graphics.fillRect(0, 420, 800, 60, 1);
    font.beginRender();
    uiText(28, 428, "左右换图  确认固定  返回图库", 744);
    font.endRender();
  };
  if (!image.draw(frame, xte::SleepImage::BASE)) return false;
  hints();
  display.displayBuffer(EInkDisplay::FULL_REFRESH);
  if (image.hasGray()) {
    if (!image.draw(frame, xte::SleepImage::LSB)) return false;
    hints();
    display.copyGrayscaleLsbBuffers(frame);
    if (!image.draw(frame, xte::SleepImage::MSB)) return false;
    hints();
    display.copyGrayscaleMsbBuffers(frame);
    display.displayGrayBuffer();
  }
  image.close();
  return true;
}

bool renderGalleryPreview() {
  if (gallerySelection < gallery.size() && renderSleepImage(gallery[gallerySelection].c_str())) return true;
  settingsMode = SettingsMode::GALLERY;
  settingsMessage = "图片无效或已删除";
  return false;
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
  bool legacySettings = false;
  if (!settings.load(&legacySettings)) Serial.println("XTE: settings defaults (missing or invalid NVS)");
  // SDK display.begin() reuses an already initialized SPI bus. Its own begin
  // omits MISO; initialize it here from the verified hardware contract.
  digitalWrite(21, HIGH);
  pinMode(21, OUTPUT);
  digitalWrite(12, HIGH);
  pinMode(12, OUTPUT);
  SPI.begin(8, 7, 10, 21);
  SdMan.begin();
  // Preserve the old root-image override during migration. New random/sequential
  // modes explicitly include both the root image and the folder catalog.
  if (legacySettings && SdMan.ready() && SdMan.exists(xte::SleepImage::SINGLE)) {
    settings.imageMode = 1;
    snprintf(settings.sleepFile, sizeof(settings.sleepFile), "%s", xte::SleepImage::SINGLE);
  }
  network.begin();
  display.begin();
  Serial.printf("XTE: font reader bytes=%u path=%s\n", static_cast<unsigned>(sizeof(font)), xte::TfFont::PATH);
  pinMode(USB_DETECT, INPUT);
  usbPowered = digitalRead(USB_DETECT) == HIGH;
  sampleBattery();
  initializeStartup();
  render(false, true);
  renderNow = false; halfRefresh = false;
  Serial.println("XTE: ready protocol=2 max_payload=384");
  network.info();
}

void loop() {
  tf.tick();
  input.update();
  if (anyButtonHeld()) lastUserActivity = millis();
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
  if (!appMenu && page == Page::CLASH && clash.pending &&
      now - clash.requestedAt >= (clash.longRequest ? 70000u : 10000u)) {
    clash.pending = false; clash.complete = false;
    strcpy(clash.message, clash.longRequest ? "结果待确认，请刷新" : "驱动未响应，请更新电脑驱动");
    renderNow = true;
  }
  if (volumePending && now - volumeRequestedAt >= 2000) {
    volumePending = false;
    status.volume = confirmedVolume;
    if (!appMenu && page == Page::VOLUME) renderNow = true;
  }
  const bool online = haveStatus && now - lastContact < OFFLINE_MS;
  if (online != wasOnline) dirty = true;
  applyAgentAttention(now);
  if (idleSleepDue(now)) {
    pollUsb(); // Recheck actual power before sleeping, outside the 200 ms poll cadence.
    if (!usbPowered) { enterSleep(); return; }
    lastUserActivity = millis();
  }
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
