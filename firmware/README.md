# X4 固件

当前版本 `maintenance-20261019`，源码位于 `firmware/src/`，由根目录 `platformio.ini` 选择。
已完成软件验证，尚未刷入设备；交付与验证入口见[项目 README](../README.md#固件与验证)。

## 当前功能

横屏 800×480，两页应用菜单包含电脑状态、系统音量、助手控制、设置和 Clash 面板。
状态经 USB CDC 或局域网 TCP 接收；独立心跳 15 秒无有效通信判离线。
六分类[设置中心](../docs/设置中心.md)控制启动、刷新、图库、连接、提醒和电池闲置休眠。
按键按[横屏映射](../docs/按键方向约定.md)转换，不改 SDK 键名或 ADC 解码。

当前 UI 从 TF 读取 MiSans 字库和灰度图标；SDK EInkDisplay 只使用一份 framebuffer。
字体按需读取，绘制后关闭文件，再串行刷新屏幕；共享 SPI 不并发访问 SD 和 EPD。
普通更新使用保存的间隔，菜单与数值操作快刷，进入/退出应用使用半刷并恢复灰度差分基线。
短按电源全刷；长按约 1.5 秒显示 TF 屏保或内置像素画、使屏幕休眠并进入深睡，再次长按唤醒。
深睡不接收消息；电池锁存和唤醒仍按[硬件契约](../docs/硬件契约-核实版.md)及已记录边界处理。

USB 文件传输采用 YMODEM-1K，兼容旧 JSON 上传；协议、摘要回读及限制见[TF_FILES.md](../TF_FILES.md)。
WiFi 配置保存于 TF，局域网消息以 HMAC 校验；细节见[通信协议](../PROTOCOL.md)。
字体读取器及 Legacy20 兼容见[FONTS.md](FONTS.md)。

## 构建与验证

按[开发环境](../docs/开发环境.md)初始化固定子模块、依赖和补丁后运行：

```powershell
.\.venv\Scripts\python.exe -m platformio run -e xteink_x4
.\.venv\Scripts\python.exe -B firmware/tests/verify_settings.py
```

当前构建保存在 `.pio/build/`，自动应用归档先写入 `backups/firmware/`，工作结束后迁入外部备份。
不要运行默认 PlatformIO upload。刷机仍须核对设备、真实分区、OTA、应用摘要及恢复槽。
原厂完整备份缺失，已有 Pixel 备份与历史验收记录均保留在[归档目录](../docs/归档说明.md)。
