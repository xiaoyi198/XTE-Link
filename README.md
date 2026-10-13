# XTE-Link

初代 **Xteink X4（ESP32-C3）** 的 USB 墨水屏状态面板，横屏 800×480。
支持电脑状态、系统音量、助手状态、设置、TF 文件传输和长按电源休眠。
设置包含音量步长、刷新间隔、启动页和恢复默认值，确认后保存到 NVS。
助手页提供状态展示及确认／拒绝事件，具体工具的审批接入需另行实现。

## 直接使用

当前 Windows 托盘驱动：
[XTE-Link.exe](artifacts/pc-driver/20261014-001309/XTE-Link.exe)（2026-10-14）。
双击即可运行，无需 Python；右键托盘图标可查看连接状态、选择串口、打开日志或退出。
默认自动连接唯一的 ESP32 USB-Serial/JTAG 串口，多设备时手动选择。
先退出旧驱动和串口监视器，避免占用串口。
配置和日志保存在用户目录 `.xte-link/`，程序不设置开机自启。
使用说明与 SHA-256 随 EXE 放在同一目录。

当前设备应用：[firmware.bin](artifacts/firmware/hints2-20261013/firmware.bin)，
版本 `hints2-20261013`，SHA-256 见同目录 `SHA256SUMS.txt`。
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

其他页面保留原有按键分配。短按电源全刷，长按约 1.5 秒休眠；休眠画面没有按键提示。

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
