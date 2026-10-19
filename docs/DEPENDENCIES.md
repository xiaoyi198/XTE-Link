# 依赖与资源来源

项目自己的代码采用根目录 [MIT 许可证](../LICENSE)。
第三方代码遵循上游各自许可证；Git 子模块保留上游仓库及其许可文件，
PlatformIO 和 pip 获取的包不复制进主仓库。

| 依赖 | 项目配置 | 来源 |
| --- | --- | --- |
| OpenX4 Community SDK | `7d86603ad27709a9a766bb5ad893cfc39e60777e` | [仓库](https://github.com/open-x4-epaper/community-sdk)，MIT |
| LoBo ESP32 YMODEM | `efb1466d00bc8f4f591c770cd349819675e83b83` | [仓库](https://github.com/loboris/ESP32_ymodem_example)，保留文件头许可及接收适配补丁 |
| Espressif32 PlatformIO 平台 | 6.12.0 | [仓库](https://github.com/platformio/platform-espressif32) |
| Arduino-ESP32 | 2.0.17 | [仓库](https://github.com/espressif/arduino-esp32) |
| WiFi / ESPmDNS / mbedTLS | Arduino-ESP32 2.0.17 内置 | 复用既有框架，不复制实现 |
| python-zeroconf | 0.151.5 | [仓库](https://github.com/jstasiak/python-zeroconf)，局域网发现 |
| ifaddr | 0.2.0 | [仓库](https://github.com/pydron/ifaddr)，zeroconf 网络接口依赖 |
| Phosphor Icons core | 2.1.1，`2b75f3ad12b420c9504ef05df8d2564a28f8500e` | [仓库](https://github.com/phosphor-icons/core)，MIT；仅 TF 图标素材，无设备运行库 |
| SdFat | 2.3.1 | [仓库](https://github.com/greiman/SdFat) |
| ArduinoJson | 6.21.5 | [仓库](https://github.com/bblanchon/ArduinoJson) |
| Adafruit GFX | 1.11.11 | [仓库](https://github.com/adafruit/Adafruit-GFX-Library) |
| Adafruit BusIO | 1.17.0（显式配置） | [仓库](https://github.com/adafruit/Adafruit_BusIO) |

SDK 的 InputManager、EInkDisplay、SDCardManager 通过 `symlink://` 接入；
Preferences 由 Arduino-ESP32 提供。构建工具包的完整固定字符串见
[`platformio.ini`](../platformio.ini)，传递依赖和缓存情况见 [开发环境](开发环境.md)。
Python 开发依赖在 [`requirements-dev.txt`](../requirements-dev.txt)，
驱动依赖以 [`pc_driver/requirements.txt`](../pc_driver/requirements.txt) 为准。

SDK 本地变更通过 [SDCardManager 补丁](../patches/README.md)保存，YMODEM 使用[接收适配补丁](../patches/README-ymodem.md)，
并附上游许可全文，不复制显示、按键或 SD 驱动实现。

当前页面采用 MiSans 4.009 Medium，保留[官方许可快照](../tools/misans/LICENSE-MiSans.txt)、
[本地转换工具与参考摘要](../tools/misans/README.md)。字体数据由使用者获取并在本机生成，不公开分发。
`Zheng17/20/25号.epdfont` 仅用于旧格式验证与 Legacy20 回退；读取方式见[字库说明](../firmware/FONTS.md)。
完整 Flash、TF 字体、生成预览与历史验收材料保存在[本机归档](归档说明.md)。
