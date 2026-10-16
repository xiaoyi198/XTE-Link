# XTE-Link

初代 **Xteink X4（ESP32-C3）** 的 USB／局域网墨水屏状态面板，横屏 800×480。
支持电脑状态、系统音量、助手状态、设置、TF 文件传输和长按电源休眠。
页头右上角显示电池图标，插着 USB 时图标左侧显示闪电（GPIO20 只表示接电，不区分是否充满）。
休眠时显示 TF 卡上的 4 级灰度图片，每次随机轮换；没有图片时显示内置像素画。
设置包含音量步长、刷新间隔、启动页、连接方式和恢复默认值，确认后保存到 NVS。
支持 DSH 实时审批、`agents.json` 助手桥接、独立心跳和 AI 事件即时推送。
Codex 的具体审批／完成状态需要桥接数据，进程检测只判断运行与离线。

## 直接使用

当前 Windows 托盘驱动：
[XTE-Link.exe](artifacts/pc-driver/20261016-233355/XTE-Link.exe)（2026-10-16）。
双击即可运行，无需 Python；右键托盘图标可配置 WiFi、连接方式、设备、推送间隔和休眠图片。
默认自动连接唯一的 ESP32 USB-Serial/JTAG 串口，多设备时手动选择。
先退出旧驱动和串口监视器，避免占用串口。
配置和日志保存在用户目录 `.xte-link/`，程序不设置开机自启。
使用说明与 SHA-256 随 EXE 放在同一目录。

首次配置局域网时，通过 USB 连接，在驱动窗口填写 2.4 GHz WiFi 名称和密码。
固件验证后保存至 TF 卡 `/xte-link/network.json`（含明文密码，需要可用 TF 卡）；
电脑仅保存设备身份与配对密钥。然后在设备设置中手动选择“局域网连接”，
驱动默认自动跟随设备的连接方式。局域网自动发现失败时，在驱动设置填写设备 IP。
驱动也可固定选择串口直连或局域网连接，两侧选择须一致。

驱动设置中的常规推送间隔为 5～3600 秒，默认 5 秒；AI 事件立即发送，
心跳独立每 5 秒发送，长间隔不会导致固件误判离线。助手页的事件刷新跳过普通刷新间隔，
仍等待屏幕可用；不自动切换页面。设备“屏幕更新间隔”只控制普通刷新。
局域网复用 TCP，mDNS 只发布设备身份，配对握手与消息采用 HMAC-SHA256 校验；
消息内容不加密。恢复默认切回串口，保留 TF WiFi 配置及字库。

托盘“上传休眠图片（USB）”可一次选多张图片，拖动预览调整取景、滚轮缩放，
预览即设备上的 4 级灰度效果；上传时转为 800×480 的 4 位灰度 BMP，写入 TF 卡 `/.sleep/`，
每张约 10 秒，可勾选先删除已有图片。每次休眠随机显示一张，并避开上一次的图片；
TF 根目录存在 `/sleep.bmp` 时只显示它。固件只读取 800×480、未压缩、1/4/8 位调色板 BMP，
其他格式或读取失败时显示内置像素画。灰度显示流程参考 CrossPoint Reader（MIT）。

最新可用应用：[firmware.bin](artifacts/firmware/sleep-image-20261016-225639/firmware.bin)，
版本 `sleep-image-20261016`，SHA-256 与构建来源见同目录校验文件和 `manifest.json`。
本版本在 `lan-push-20261014` 基础上增加电池图标、USB 接电闪电和 TF 休眠图片，
已在当前验证设备刷入 app1 并通过独立镜像校验；GPIO20 插拔电平、4 级灰度色阶、
渐变和唤醒后无残影已实测。
电量使用 SDK `BatteryMonitor` 默认分压倍数，成品无法拆测，百分比仅供参考。
构建静态 RAM 为 102,716 字节，应用镜像 898,768 字节。
局域网认证通信、心跳、即时事件、断连恢复及重启持久化已在上一版 `lan-push-20261014` 验证，
该次测试可用 heap 最低 152,956 字节。屏幕观感仍需使用者目视确认。
中文界面需自行准备 TF 字库 `/fonts/Zheng20号.epdfont`；
匹配文件为 7,958,602 字节，SHA-256：
`b62eba931c899cb676b71cc6759d805f65e67ba79bc94180fdb5ce7431c9cb59`。
字体文件不随仓库分发，缺失或读取失败显示方框。

**不要使用 PlatformIO 默认 upload。** 刷写前核对备份、设备、分区和 OTA 启动槽，
只更新已确认的应用槽并独立校验，保留引导器、分区和恢复更新器。
现有实测设备的 app1 为 `0x650000`，不能直接套用到其他设备。
已有完整备份来自 Pixel 3.0.12，原厂备份缺失。

## 横屏操作

| 图标 / 位置 | SDK 原键名 | 设置页用途 |
| --- | --- | --- |
| 顶部 ←、→ | `BTN_UP`、`BTN_DOWN` | 预览切页；编辑调整值 |
| 右侧 ↑、↓ | `BTN_RIGHT`、`BTN_LEFT` | 列表选择 |
| 右侧 ✓、× | `BTN_CONFIRM`、`BTN_BACK` | 打开／保存；返回／取消 |

其他页面保留原有按键分配。短按电源全刷，长按约 1.5 秒休眠；休眠画面没有按键提示，再次长按唤醒。

## 从源码构建

在仓库根目录执行，需要 Git、Python 3.11；首次安装依赖需联网。

```powershell
git submodule update --init --recursive
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/apply_sdk_patch.py
.\.venv\Scripts\python.exe -m platformio run -e xteink_x4
```

固件输出 `.pio/build/xteink_x4/firmware.bin`，成功构建自动备份至 `backups/firmware/`。
SDK 固定 commit `7d86603ad27709a9a766bb5ad893cfc39e60777e`；
补丁脚本给已有 SDCardManager 接入格式化接口，可重复执行。
工具链和固件库版本固定在 `platformio.ini`，SDK 通过子模块和 `symlink://` 引用。

Windows EXE 构建：

```powershell
.\.venv\Scripts\python.exe -m pip install -r pc_driver/requirements-build.txt
.\.venv\Scripts\python.exe scripts/build_driver.py
```

生成产物位于 `artifacts/pc-driver/<构建时间>/`。
也可运行 `.\.venv\Scripts\python.exe -m pc_driver.driver --port COM3`，
将 `COM3` 替换为实际串口。传感器不可用时返回缺失值。

## 仓库范围与许可

保留固件与驱动运行源码、必要构建脚本和依赖、SDK 子模块与补丁、
当前 EXE/固件及其校验文件、本 README 和 [MIT 许可证](LICENSE)。
完整备份、历史产物、测试、预览工具、截图和详细开发文档只在本地保留。
更新公开版本时同步调整 `.gitignore` 中的产物目录。

项目自己的代码采用 MIT；SDK 及其他依赖遵循各自许可证，
SDK 补丁附带 [上游许可](patches/LICENSE-open-x4-sdk)。
