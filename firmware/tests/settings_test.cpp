// Appended to actual production functions by verify_settings.py.
#include <cassert>
constexpr uint8_t SCREEN_UP = InputManager::BTN_RIGHT;
constexpr uint8_t SCREEN_DOWN = InputManager::BTN_LEFT;
constexpr uint8_t SCREEN_LEFT = InputManager::BTN_UP;
constexpr uint8_t SCREEN_RIGHT = InputManager::BTN_DOWN;

void press(uint8_t key) {
  input.pressed = static_cast<uint8_t>(1 << key);
  renderNow = false;
  handlePageButtons();
  input.pressed = 0;
}

void pushClashFrame(JsonDocument& frame) {
  frame["v"] = 2; frame["cmd"] = "clash";
  char line[768];
  const size_t length = serializeJson(frame, line, sizeof(line));
  assert(handlePush(line, length));
}

void edit(Setting item) {
  appMenu = false; page = Page::SETTINGS; settingsMode = SettingsMode::LIST;
  for (uint8_t group = 0; group < GROUP_COUNT; ++group)
    for (uint8_t row = 0; row < GROUP_SIZES[group]; ++row)
      if (GROUP_ITEMS[group][row] == item) selectedGroup = group;
  selectedSetting = item;
  press(InputManager::BTN_CONFIRM);
  assert(settingsMode == SettingsMode::EDIT);
}

std::vector<uint8_t> sampleBmp() {
  std::vector<uint8_t> data(70 + 400 * 480, 0x12);
  std::fill(data.begin(), data.begin() + 70, 0);
  const auto u32 = [&](size_t at, uint32_t value) {
    for (int i = 0; i < 4; ++i) data[at + i] = static_cast<uint8_t>(value >> (8 * i));
  };
  data[0] = 'B'; data[1] = 'M'; u32(2, static_cast<uint32_t>(data.size()));
  u32(10, 70); u32(14, 40); u32(18, 800); u32(22, 480); data[26] = 1; data[28] = 4; u32(46, 4);
  for (size_t i = 0; i < 4; ++i)
    for (size_t channel = 0; channel < 3; ++channel) data[54 + i * 4 + channel] = static_cast<uint8_t>(85 * i);
  return data;
}

int main() {
  // Legacy migration never writes; every old preference combination survives.
  assert(!settings.load() && settings.volumeStep() == 5 && nvs_host::writes == 0);
  for (uint32_t invalid : {0u, 0x02000001u, 0x01000003u, 0x01000301u, 0x01030001u}) {
    nvs_host::present = true; nvs_host::value = invalid;
    assert(!settings.load() && settings.sameAs(xte::Settings{}));
  }
  for (uint8_t v = 0; v < 3; ++v)
    for (uint8_t i = 0; i < 3; ++i)
      for (uint8_t p = 0; p < 3; ++p) {
        nvs_host::value = 0x01000000u | (uint32_t(p) << 16) | (uint32_t(i) << 8) | v;
        assert(settings.load() && settings.volumeIndex == v && settings.intervalIndex == i && settings.startupPage == p);
      }
  assert(nvs_host::writes == 0);
  const uint32_t legacy = nvs_host::value;
  settings.startupPage = 4; settings.startupMode = 2; settings.ghostIndex = 3;
  settings.imageMode = 1; strcpy(settings.sleepFile, "/.sleep/固定.bmp");
  settings.reminderMode = 2; settings.sleepTimeoutIndex = 3;
  assert(settings.save() && nvs_host::value == legacy);
  xte::Settings restarted; assert(restarted.load() && restarted.sameAs(settings));
  nvs_host::blob[0] = 99;
  assert(!restarted.load() && restarted.sameAs(xte::Settings{}));
  assert(settings.save());
  nvs_host::blob.pop_back();
  assert(!restarted.load() && restarted.sameAs(xte::Settings{}));
  assert(settings.save());
  for (const char* bad : {"/../a.bmp", "/.sleep/../a.bmp", "/.sleep/.hidden.bmp", "/.sleep/a.txt", "/other/a.bmp"})
    assert(!xte::Settings::imagePathValid(bad));
  xte::Settings invalid; invalid.imageMode = 1; assert(!invalid.valid() && !invalid.save());
  invalid = xte::Settings{}; memset(invalid.sleepFile, 'a', sizeof(invalid.sleepFile)); assert(!invalid.valid());
  assert(nvs_host::opens == 0);

  // Startup restores a valid app, including the second menu page, without an action.
  settings = xte::Settings{}; settings.startupPage = 4;
  initializeStartup(); assert(appMenu && menuPage == 1 && menuSelection[1] == 1);
  settings.startupMode = 1;
  initializeStartup(); assert(!appMenu && page == Page::CLASH && clash.pending);
  const uint32_t initialRequest = clash.sequence;
  xte::Status connected; strcpy(connected.host, "PC");
  acceptStatus(connected); assert(clash.pending && clash.sequence == initialRequest + 1);
  nvs_host::lastApp = 2; settings.startupMode = 2;
  initializeStartup(); assert(!appMenu && page == Page::AGENTS);
  const unsigned resumeWrites = nvs_host::writes;
  openApp(Page::VOLUME); assert(nvs_host::lastApp == 1 && nvs_host::writes == resumeWrites + 1);
  openApp(Page::VOLUME); openApp(Page::SETTINGS); assert(nvs_host::writes == resumeWrites + 1);
  initializeStartup(); assert(page == Page::VOLUME);
  nvs_host::lastApp = 99;
  initializeStartup(); assert(page == Page::CLASH);
  settings = xte::Settings{}; clash = xte::ClashPanel{};
  for (Page app : {Page::STATUS, Page::VOLUME, Page::AGENTS, Page::SETTINGS}) {
    menuPage = 0; menuSelection[0] = static_cast<uint8_t>(app);
    appMenu = true; forceFull = false; halfRefresh = false;
    press(InputManager::BTN_CONFIRM);
    assert(!appMenu && page == app && renderNow && halfRefresh);
    render(false, false, halfRefresh); assert(display.lastMode == EInkDisplay::HALF_REFRESH);
    halfRefresh = false; press(InputManager::BTN_BACK);
    assert(appMenu && page == app && renderNow && halfRefresh);
  }
  appMenu = true; menuPage = 0; menuSelection[0] = 0; menuSelection[1] = 0;
  press(SCREEN_UP); assert(menuSelection[0] == 3);
  press(SCREEN_DOWN); assert(menuSelection[0] == 0);
  press(SCREEN_LEFT); assert(menuPage == 1);
  for (uint8_t tile : {0, 2, 3}) {
    menuSelection[1] = tile; press(InputManager::BTN_CONFIRM); assert(appMenu && !renderNow);
  }
  menuSelection[1] = 1; press(InputManager::BTN_CONFIRM); assert(page == Page::CLASH && clash.pending);
  const size_t requests = Serial.lines.size();
  press(InputManager::BTN_CONFIRM); assert(Serial.lines.size() == requests);
  const uint32_t waitingRead = clash.sequence;
  press(InputManager::BTN_BACK); assert(appMenu && menuPage == 1 && clash.pending);
  press(InputManager::BTN_CONFIRM);
  assert(!appMenu && clash.pending && clash.sequence == waitingRead && Serial.lines.size() == requests);

  // Home categories and editors keep the landscape direction contract.
  openApp(Page::SETTINGS); selectedGroup = 0;
  press(SCREEN_LEFT); assert(selectedGroup == 0 && !renderNow);
  press(SCREEN_UP); assert(selectedGroup == 5);
  press(SCREEN_DOWN); assert(selectedGroup == 0);
  press(InputManager::BTN_CONFIRM); assert(settingsMode == SettingsMode::LIST && selectedSetting == UPDATE);
  press(SCREEN_UP); assert(selectedSetting == CLEAN);
  press(SCREEN_DOWN); assert(selectedSetting == UPDATE);
  press(InputManager::BTN_BACK); assert(settingsMode == SettingsMode::HOME);
  const unsigned beforeEdit = nvs_host::writes;
  edit(VOLUME); press(SCREEN_RIGHT); assert(draftSetting == 2 && settings.volumeIndex == 1);
  press(SCREEN_UP); assert(draftSetting == 2 && !renderNow);
  press(InputManager::BTN_BACK); assert(settingsMode == SettingsMode::LIST && nvs_host::writes == beforeEdit);
  edit(VOLUME); press(InputManager::BTN_CONFIRM); assert(nvs_host::writes == beforeEdit);
  edit(VOLUME); press(SCREEN_RIGHT); press(InputManager::BTN_CONFIRM);
  assert(settings.volumeStep() == 10 && restarted.load() && restarted.volumeStep() == 10);
  edit(UPDATE); press(SCREEN_RIGHT);
  const auto stored = nvs_host::blob;
  nvs_host::failOpen = true; press(InputManager::BTN_CONFIRM);
  assert(settingsMode == SettingsMode::EDIT && settings.updateMs() == 5000 && nvs_host::blob == stored);
  nvs_host::failOpen = false; nvs_host::failWrite = true; press(InputManager::BTN_CONFIRM);
  assert(settingsMode == SettingsMode::EDIT && settings.updateMs() == 5000 && nvs_host::blob == stored);
  nvs_host::failWrite = false; press(InputManager::BTN_CONFIRM);
  assert(settings.updateMs() == 10000 && settingsMode == SettingsMode::LIST);
  edit(START_APP);
  for (int i = 0; i < 4; ++i) press(SCREEN_RIGHT);
  press(InputManager::BTN_CONFIRM); assert(settings.startupPage == 4);
  edit(IMAGE_MODE); press(SCREEN_RIGHT); press(InputManager::BTN_CONFIRM);
  assert(settingsMode == SettingsMode::EDIT && settings.imageMode == 0);
  press(InputManager::BTN_BACK);
  const xte::Settings beforeTransport = settings;
  edit(TRANSPORT); press(SCREEN_RIGHT); press(InputManager::BTN_CONFIRM);
  assert(!network.lan && settingsMode == SettingsMode::EDIT);
  network.configured = true; network.fail = true; press(InputManager::BTN_CONFIRM);
  assert(!network.lan && settingsMode == SettingsMode::EDIT);
  network.fail = false; press(InputManager::BTN_CONFIRM);
  assert(network.lan && settings.sameAs(beforeTransport));
  selectedSetting = RECONNECT; selectedGroup = 3;
  press(InputManager::BTN_CONFIRM); assert(network.reconnects == 1 && !haveStatus);
  selectedSetting = CONNECTION; press(InputManager::BTN_CONFIRM);
  assert(settingsMode == SettingsMode::CONNECTION);
  const unsigned readOnlyWrites = nvs_host::writes;
  press(InputManager::BTN_CONFIRM); press(InputManager::BTN_BACK);
  assert(nvs_host::writes == readOnlyWrites);

  // Preview uses the real BMP parser and gray display path, without advancing history.
  host::files["/.sleep/a.bmp"] = sampleBmp(); host::files["/.sleep/b.BMP"] = sampleBmp();
  host::files["/.sleep/invalid.bmp"] = {0, 1, 2}; host::files["/.sleep/.hidden.bmp"] = {};
  host::files["/.sleep/note.txt"] = {};
  {
    xte::SleepImage image;
    assert(image.openPath("/.sleep/a.bmp"));
    assert(image.draw(display.getFrameBuffer(), xte::SleepImage::BASE) && display.bytes[0] == 0x00);
    assert(image.draw(display.getFrameBuffer(), xte::SleepImage::LSB) && display.bytes[0] == 0xaa);
    assert(image.draw(display.getFrameBuffer(), xte::SleepImage::MSB) && display.bytes[0] == 0xff);
  }
  selectedGroup = 1; selectedSetting = GALLERY; settingsMode = SettingsMode::LIST;
  press(InputManager::BTN_CONFIRM); assert(settingsMode == SettingsMode::GALLERY && gallery.size() == 3);
  const auto previewWrites = nvs_host::writes;
  press(InputManager::BTN_CONFIRM); assert(settingsMode == SettingsMode::PREVIEW);
  render(false, false); assert(display.grayDisplays && nvs_host::writes == previewWrites);
  press(SCREEN_RIGHT); assert(gallerySelection == 1); render(false, false);
  press(InputManager::BTN_BACK); assert(settingsMode == SettingsMode::GALLERY && settings.imageMode == 0);
  press(InputManager::BTN_CONFIRM); render(false, false); press(InputManager::BTN_CONFIRM);
  assert(settings.imageMode == 1 && !strcmp(settings.sleepFile, "/.sleep/b.BMP"));
  assert(restarted.load() && restarted.sameAs(settings));
  press(SCREEN_DOWN); press(InputManager::BTN_CONFIRM); render(false, false);
  assert(settingsMode == SettingsMode::GALLERY && !strcmp(settingsMessage, "图片无效或已删除"));
  press(InputManager::BTN_BACK); assert(gallery.empty() && host::opens == 0);
  host::files.erase("/.sleep/invalid.bmp");
  nvs_host::strings.clear(); settings.imageMode = 2;
  for (const char* expected : {"/.sleep/a.bmp", "/.sleep/b.BMP", "/.sleep/a.bmp"}) {
    xte::SleepImage image; assert(image.open(settings));
    assert(nvs_host::strings["sleepLast"] == String(expected));
  }
  settings.imageMode = 0;
  for (int i = 0; i < 12; ++i) {
    const String previous = nvs_host::strings["sleepLast"];
    xte::SleepImage image; assert(image.open(settings));
    assert(nvs_host::strings["sleepLast"] != previous);
  }
  host::files.clear();
  for (int i = 0; i < 205; ++i) host::files["/.sleep/" + std::to_string(i) + ".bmp"] = {};
  assert(xte::SleepImage::catalog().size() == 200);
  host::files.clear();

  // All editable additions cycle, save, reload and preserve unrelated fields.
  assert(settings.save());
  for (Setting item : {START_MODE, GHOST, REMINDER, SLEEP_TIMEOUT, IMAGE_MODE}) {
    for (uint8_t choice = 0; choice < settingChoices(item); ++choice) {
      edit(item);
      while (draftSetting != choice) press(SCREEN_RIGHT);
      press(InputManager::BTN_CONFIRM);
      assert(settingsMode == SettingsMode::LIST && settingIndex(item) == choice);
      assert(restarted.load() && restarted.sameAs(settings));
    }
  }

  // Every Nth fast refresh clears ghosting. Half transitions don't count.
  settings = xte::Settings{}; settings.ghostIndex = 1; appMenu = true; fastRefreshes = 18;
  render(false, false); assert(display.lastMode == EInkDisplay::FAST_REFRESH && fastRefreshes == 19);
  render(false, false, true); assert(display.lastMode == EInkDisplay::HALF_REFRESH && fastRefreshes == 19);
  render(false, false); assert(display.lastMode == EInkDisplay::FULL_REFRESH && fastRefreshes == 0);
  render(false, true); assert(fastRefreshes == 0);

  // New requests may switch pages, but cannot consume an opening key or interrupt editing/TF.
  settings = xte::Settings{}; settings.reminderMode = 2;
  haveStatus = true; status = xte::Status{}; hostNow = 1000; lastContact = 1000; lastUserActivity = 900;
  appMenu = true; attentionQueued = false;
  char askEvent[] = "{\"v\":2,\"cmd\":\"agents\",\"agents\":[{\"tool\":\"codex\",\"state\":\"ask\",\"id\":\"request-a\"}]}";
  assert(handlePush(askEvent, strlen(askEvent)) && attentionQueued && pendingApprovals() == 1);
  const size_t noApproval = Serial.lines.size();
  applyAgentAttention(1000); assert(appMenu);
  input.held = 1 << InputManager::BTN_CONFIRM; applyAgentAttention(2000); assert(appMenu);
  input.held = 0; tf.busy = true; applyAgentAttention(2000); assert(appMenu);
  tf.busy = false; page = Page::SETTINGS; appMenu = false; applyAgentAttention(2000);
  assert(page == Page::SETTINGS && attentionQueued);
  page = Page::CLASH; clash.confirming = true; applyAgentAttention(2000); assert(page == Page::CLASH);
  clash.confirming = false; clash.pending = false; applyAgentAttention(2000);
  assert(page == Page::AGENTS && !attentionQueued && Serial.lines.size() == noApproval);
  press(InputManager::BTN_BACK); assert(appMenu);
  assert(handlePush(askEvent, strlen(askEvent)) && !attentionQueued);
  applyAgentAttention(3000); assert(appMenu);

  // Battery idle timeout is wrap-safe and pauses for connected power and active work.
  settings = xte::Settings{}; settings.sleepTimeoutIndex = 1;
  haveStatus = false; status = xte::Status{}; appMenu = true; usbPowered = false; lastUserActivity = 0;
  assert(!idleSleepDue(299999) && idleSleepDue(300000));
  usbPowered = true; assert(!idleSleepDue(300000) && lastUserActivity == 300000);
  usbPowered = false; assert(!idleSleepDue(300001));
  tf.busy = true; assert(!idleSleepDue(700000) && lastUserActivity == 700000); tf.busy = false;
  hostNow = 800000; haveStatus = true; lastContact = hostNow; status.agentCount = 1;
  strcpy(status.agents[0].state, "ask"); strcpy(status.agents[0].requestId, "pending");
  assert(!idleSleepDue(hostNow) && lastUserActivity == hostNow);
  haveStatus = false; appMenu = false; page = Page::SETTINGS;
  assert(!idleSleepDue(1200000) && lastUserActivity == 1200000);
  appMenu = true; lastUserActivity = UINT32_MAX - 1000;
  assert(!idleSleepDue(298998) && idleSleepDue(298999));
  settings.sleepTimeoutIndex = 0; assert(!idleSleepDue(2000000));

  // Restore preferences without changing legacy data, TF, pairing or last-app storage.
  selectedGroup = 5; selectedSetting = RESET; settingsMode = SettingsMode::LIST;
  appMenu = false; page = Page::SETTINGS; settings.volumeIndex = 2;
  press(InputManager::BTN_CONFIRM); assert(settingsMode == SettingsMode::RESET);
  nvs_host::failWrite = true; press(InputManager::BTN_CONFIRM);
  assert(settingsMode == SettingsMode::RESET && settings.volumeIndex == 2);
  nvs_host::failWrite = false; press(InputManager::BTN_CONFIRM);
  assert(settingsMode == SettingsMode::LIST && settings.sameAs(xte::Settings{}) && !network.lan);
  assert(restarted.load() && restarted.sameAs(settings) && nvs_host::value == legacy);
  press(InputManager::BTN_BACK); assert(settingsMode == SettingsMode::HOME);
  press(InputManager::BTN_BACK); assert(appMenu);
  appMenu = false; page = Page::STATUS; halfRefresh = false; forceFull = false;
  haveStatus = false; status = xte::Status{}; hostNow = 0;

  // Saved interval governs background rendering, with immediate connection changes and wrap-safe timing.
  settings.intervalIndex = 2; dirty = true; wasOnline = true; lastRendered = 1000;
  assert(!backgroundRenderDue(30999, true));
  assert(backgroundRenderDue(31000, true));
  assert(backgroundRenderDue(1001, false));
  page = Page::SETTINGS;
  assert(!backgroundRenderDue(100000, true) && backgroundRenderDue(1001, false));
  page = Page::STATUS; settings.intervalIndex = 0; lastRendered = UINT32_MAX - 2000;
  assert(!backgroundRenderDue(2998, true) && backgroundRenderDue(2999, true));

  appMenu = true; page = Page::AGENTS;
  assert(!backgroundRenderDue(100000, true));
  appMenu = false;

  // Volume uses the saved step, stays clamped, and keeps the existing sequence/confirmation contract.
  haveStatus = true; status.volume = 97; page = Page::VOLUME; settings.volumeIndex = 2;
  size_t volumeLog = Serial.lines.size();
  press(InputManager::BTN_RIGHT); assert(status.volume == 100 && volumePending);
  assert(Serial.lines[volumeLog].find("volume_delta +10") != std::string::npos);
  volumeLog = Serial.lines.size();
  status.volume = 0; settings.volumeIndex = 0; press(InputManager::BTN_LEFT); assert(status.volume == 0);
  assert(Serial.lines[volumeLog].find("volume_delta -1") != std::string::npos);

  // Back returns to the menu without rejecting; landscape Right explicitly rejects.
  status.agentCount = 1; strcpy(status.agents[0].tool, "codex");
  strcpy(status.agents[0].state, "ask"); strcpy(status.agents[0].requestId, "request-1");
  page = Page::SETTINGS; settingsMode = SettingsMode::LIST;
  const size_t logs = Serial.lines.size(); press(InputManager::BTN_BACK);
  assert(Serial.lines.size() == logs);
  page = Page::AGENTS; menuSelection[0] = static_cast<uint8_t>(page);
  appMenu = false; press(InputManager::BTN_BACK);
  assert(appMenu && Serial.lines.size() == logs);
  press(InputManager::BTN_CONFIRM); assert(!appMenu && Serial.lines.size() == logs);
  press(SCREEN_RIGHT);
  assert(Serial.lines.back().find("agent_action reject codex request-1") != std::string::npos);
  const size_t rejected = Serial.lines.size();
  press(InputManager::BTN_BACK); assert(appMenu);
  renderNow = false;
  xte::Status menuSample; menuSample.volume = 43;
  acceptStatus(menuSample); assert(!renderNow);
  assert(!backgroundRenderDue(100000, wasOnline));
  press(InputManager::BTN_CONFIRM);
  assert(!appMenu && Serial.lines.size() == rejected + 1); // accepted only, no approval
  assert(nvs_host::opens == 0 && host::opens == 0);
  // AI frames bypass the normal 30-second repaint interval without moving the
  // metric timestamp. A later metric-only frame preserves the event state.
  page = Page::AGENTS; settings.intervalIndex = 2; hostNow = 500;
  renderNow = false;
  char event[] = "{\"v\":2,\"cmd\":\"agents\",\"agents\":[{\"tool\":\"codex\",\"state\":\"ask\",\"id\":\"new\",\"text\":\"Approve\"}]}";
  assert(handlePush(event, strlen(event)) && renderNow && lastContact == 500);
  assert(!strcmp(status.agents[0].requestId, "new"));
  xte::Status sample; strcpy(sample.host, "PC"); strcpy(sample.time, "2026-10-14 01:00:00");
  acceptStatus(sample);
  assert(status.agentCount == 1 && !strcmp(status.agents[0].requestId, "new"));
  hostNow = 20000;
  char heartbeat[] = "{\"v\":2,\"cmd\":\"heartbeat\"}";
  assert(handlePush(heartbeat, strlen(heartbeat)) && lastContact == 20000 && lastReceived == 500);
  assert(!strcmp(status.time, "2026-10-14 01:00:00"));
  renderNow = false; page = Page::SETTINGS;
  char nextEvent[] = "{\"v\":2,\"cmd\":\"agents\",\"agents\":[{\"tool\":\"codex\",\"state\":\"done\"}]}";
  assert(handlePush(nextEvent, strlen(nextEvent)) && renderNow);
  network.lan = true;
  const auto contacts = lastContact;
  char usbHeartbeat[] = "{\"v\":2,\"cmd\":\"heartbeat\"}";
  hostNow = 25000;
  assert(handleUsbLine(usbHeartbeat, strlen(usbHeartbeat)) && lastContact == contacts);
  network.lan = false;
  // The actual Clash receiver commits only a complete matching page.
  appMenu = false; page = Page::CLASH;
  clash = xte::ClashPanel{};
  assert(clash.begin(30000));
  StaticJsonDocument<768> frame;
  frame["id"] = clash.sequence + 1; frame["phase"] = "error"; frame["message"] = "旧请求";
  assert(!clash.accept(frame) && clash.pending);
  frame.clear(); frame["id"] = clash.sequence; frame["phase"] = "begin";
  frame["view"] = "nodes"; frame["page"] = 0; frame["pages"] = 2;
  frame["rev"] = "1234567890abcdef"; frame["count"] = 2;
  assert(clash.accept(frame) && clash.pending && !clash.complete);
  frame.clear(); frame["id"] = clash.sequence; frame["phase"] = "row";
  frame["index"] = 0; frame["name"] = "日本节点"; frame["enabled"] = true; frame["current"] = true;
  assert(clash.accept(frame));
  frame["phase"] = "end"; frame["message"] = "手选节点，仅本次运行";
  assert(!clash.accept(frame) && !clash.complete);  // Missing row cannot produce a partial page.
  frame["phase"] = "row"; frame["index"] = 1; frame["name"] = "香港节点"; frame["current"] = false;
  assert(clash.accept(frame));
  frame["phase"] = "end";
  assert(clash.accept(frame) && clash.complete && !clash.pending && clash.count == 2);
  assert(!strcmp(clash.view, "nodes") && clash.pages == 2 && clash.rows[0].current);
  press(SCREEN_UP); assert(clash.selected == 1);
  const size_t beforeConfirm = Serial.lines.size();
  press(InputManager::BTN_CONFIRM);
  assert(clash.confirming && Serial.lines.size() == beforeConfirm);
  press(InputManager::BTN_BACK); assert(!clash.confirming && !appMenu);
  press(InputManager::BTN_CONFIRM); press(InputManager::BTN_CONFIRM);
  assert(clash.pending && clash.writeRequest && Serial.lines.size() == beforeConfirm + 1);
  assert(Serial.lines.back().find("\"op\":\"activate\"") != std::string::npos);
  frame.clear(); frame["id"] = clash.sequence; frame["phase"] = "error"; frame["message"] = "结果待确认，请刷新";
  assert(clash.accept(frame) && !clash.pending && !clash.complete);
  press(InputManager::BTN_CONFIRM);
  assert(Serial.lines.back().find("\"op\":\"list\"") != std::string::npos);
  const uint32_t waitingNodes = clash.sequence;
  press(InputManager::BTN_BACK);
  assert(appMenu && !strcmp(clash.view, "nodes") && clash.pending);
  openApp(Page::CLASH);
  assert(!strcmp(clash.view, "nodes") && clash.pending && clash.sequence == waitingNodes);
  frame.clear(); frame["id"] = clash.sequence; frame["phase"] = "error"; frame["message"] = "读取失败";
  pushClashFrame(frame);
  press(InputManager::BTN_BACK);
  assert(!strcmp(clash.view, "groups") && clash.pending);
  frame["id"] = clash.sequence; pushClashFrame(frame);
  press(InputManager::BTN_BACK); assert(!strcmp(clash.view, "home") && clash.pending);
  frame["id"] = clash.sequence; pushClashFrame(frame);
  press(InputManager::BTN_BACK); assert(appMenu && !clash.pending);
  // Both home switches require a second confirmation; cancel emits no action.
  appMenu = false; page = Page::CLASH;
  for (uint8_t index : {2, 3}) {
    strcpy(clash.view, "home"); clash.complete = true; clash.pending = false;
    clash.count = 5; clash.selected = index; clash.rows[index].enabled = true;
    const size_t beforeSwitch = Serial.lines.size();
    press(InputManager::BTN_CONFIRM);
    assert(clash.confirming && Serial.lines.size() == beforeSwitch);
    press(InputManager::BTN_BACK);
    assert(!clash.confirming && Serial.lines.size() == beforeSwitch);
    press(InputManager::BTN_CONFIRM); press(InputManager::BTN_CONFIRM);
    assert(clash.pending && clash.writeRequest && Serial.lines.size() == beforeSwitch + 1);
  }

  // A pending node write survives exit, reentry and a fresh telemetry connection.
  clash = xte::ClashPanel{}; appMenu = false; page = Page::CLASH; hostNow = 40000;
  strcpy(clash.view, "nodes"); strcpy(clash.revision, "1234567890abcdef");
  clash.complete = true; clash.count = 1; clash.rows[0].enabled = true;
  press(InputManager::BTN_CONFIRM); press(InputManager::BTN_CONFIRM);
  const uint32_t writingNode = clash.sequence;
  const size_t sentNode = Serial.lines.size();
  assert(clash.pending && clash.writeRequest);
  assert(!clash.begin(hostNow) && clash.sequence == writingNode);
  sendClashRequest(); sendClashRequest(true);
  assert(Serial.lines.size() == sentNode && clash.sequence == writingNode);
  press(InputManager::BTN_BACK);
  assert(appMenu && clash.pending && !strcmp(clash.view, "nodes"));
  openApp(Page::CLASH);
  assert(clash.pending && clash.sequence == writingNode && Serial.lines.size() == sentNode);
  haveStatus = false; acceptStatus(connected);
  assert(clash.pending && clash.sequence == writingNode && Serial.lines.size() == sentNode + 1);
  press(InputManager::BTN_BACK); openApp(Page::STATUS); renderNow = false;

  // Matching fragmented results commit off-page without repainting that page.
  frame.clear(); frame["id"] = writingNode + 1; frame["phase"] = "error"; frame["message"] = "错误 ID";
  const size_t ignoredFrames = Serial.lines.size();
  pushClashFrame(frame);
  assert(clash.pending && !renderNow && Serial.lines.size() == ignoredFrames);
  frame.clear(); frame["id"] = writingNode; frame["phase"] = "begin";
  frame["view"] = "nodes"; frame["page"] = 0; frame["pages"] = 1;
  frame["rev"] = "abcdef1234567890"; frame["count"] = 1;
  pushClashFrame(frame);
  assert(clash.pending && !renderNow && !clash.unseenResult);
  frame.clear(); frame["id"] = writingNode; frame["phase"] = "end"; frame["message"] = "已切换";
  pushClashFrame(frame);
  assert(clash.pending && !clash.complete && !renderNow);  // Incomplete frame set stays pending.
  frame["phase"] = "row"; frame["index"] = 0; frame["name"] = "已选节点";
  frame["current"] = true; frame["enabled"] = true;
  pushClashFrame(frame);
  frame["phase"] = "end"; pushClashFrame(frame);
  assert(!clash.pending && clash.complete && clash.unseenResult && !renderNow);
  assert(clash.rows[0].current && !strcmp(clash.message, "已切换"));
  const size_t receivedNode = Serial.lines.size();
  openApp(Page::CLASH);
  assert(clash.complete && !clash.unseenResult && clash.sequence == writingNode);
  assert(Serial.lines.size() == receivedNode && !strcmp(clash.message, "已切换"));

  // Off-page write timeouts remain uncertain; reentry only refreshes on explicit confirmation.
  for (const char* view : {"home", "nodes", "profiles"}) {
    clash = xte::ClashPanel{}; appMenu = false; page = Page::CLASH; hostNow = UINT32_MAX - 1000;
    strcpy(clash.view, view); clash.complete = true; clash.count = 5;
    clash.selected = !strcmp(view, "home") ? 3 : 0; clash.rows[clash.selected].enabled = true;
    press(InputManager::BTN_CONFIRM); press(InputManager::BTN_CONFIRM);
    const uint32_t pendingWrite = clash.sequence;
    press(InputManager::BTN_BACK); renderNow = false;
    expireClashRequest(hostNow + 69999); assert(clash.pending && !renderNow);
    expireClashRequest(hostNow + 70000);
    assert(!clash.pending && !clash.complete && clash.unseenResult && !renderNow);
    assert(!strcmp(clash.message, "结果待确认，请刷新"));
    const size_t timedOut = Serial.lines.size();
    frame.clear(); frame["id"] = pendingWrite; frame["phase"] = "error"; frame["message"] = "迟到回执";
    pushClashFrame(frame);
    assert(Serial.lines.size() == timedOut && !strcmp(clash.message, "结果待确认，请刷新"));
    openApp(Page::CLASH);
    assert(clash.sequence == pendingWrite && !clash.pending && Serial.lines.size() == timedOut);
    haveStatus = false; acceptStatus(connected);
    assert(!clash.pending && clash.sequence == pendingWrite && Serial.lines.size() == timedOut + 1);
    press(InputManager::BTN_CONFIRM);
    assert(clash.pending && !clash.writeRequest && clash.sequence == pendingWrite + 1);
    assert(Serial.lines.back().find("\"op\":\"list\"") != std::string::npos);
    renderNow = false; pushClashFrame(frame);
    assert(clash.pending && !renderNow && clash.sequence == pendingWrite + 1);
  }

  // Off-page driver errors survive reentry, while read timeouts keep the shorter deadline.
  clash = xte::ClashPanel{}; strcpy(clash.view, "nodes");
  assert(clash.begin(50000, true));
  openApp(Page::SETTINGS); renderNow = false;
  frame.clear(); frame["id"] = clash.sequence; frame["phase"] = "error"; frame["message"] = "操作失败，请刷新";
  pushClashFrame(frame);
  assert(!clash.pending && clash.unseenResult && !renderNow && !clash.complete);
  const size_t failedWrite = Serial.lines.size(); openApp(Page::CLASH);
  assert(Serial.lines.size() == failedWrite && !strcmp(clash.message, "操作失败，请刷新"));
  press(InputManager::BTN_CONFIRM); assert(clash.pending && !clash.writeRequest);
  renderNow = false; expireClashRequest(hostNow + 9999); assert(clash.pending && !renderNow);
  expireClashRequest(hostNow + 10000); assert(!clash.pending && renderNow && !clash.complete);
  assert(!strcmp(clash.message, "驱动未响应，请更新电脑驱动"));
  press(InputManager::BTN_CONFIRM); assert(clash.pending && !clash.writeRequest);
  const uint32_t expiringRead = clash.sequence;
  press(InputManager::BTN_BACK); renderNow = false;
  expireClashRequest(hostNow + 10000);
  assert(!clash.pending && clash.unseenResult && !renderNow);
  const size_t readTimedOut = Serial.lines.size(); openApp(Page::CLASH);
  assert(!clash.pending && clash.sequence == expiringRead && Serial.lines.size() == readTimedOut);
  assert(!strcmp(clash.message, "驱动未响应，请更新电脑驱动"));
  puts("PASS: legacy migration, all settings editors, startup/resume, gallery/gray preview, image rotation, save failure/retry, ghost cadence, approval focus isolation, battery sleep guards, volume and Clash regressions");
  return 0;
}
