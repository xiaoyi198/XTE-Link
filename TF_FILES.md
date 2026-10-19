# TF 文件传输

固件启动时通过固定版本 SDK 的 `SDCardManager` 挂载 TF。底层使用
[SdFat 2.3.1](https://github.com/greiman/SdFat/tree/2.3.1)，支持 FAT16/FAT32/exFAT。
文件存取、MiSans 字库和灰度图片已接入固件，现状与入口见 [README](README.md)。
本页保留原 JSON 文件协议；新版另有 YMODEM-1K 路径，详见 [接收适配说明](patches/README-ymodem.md)。

## 电脑端使用

需先安装并验证兼容的 XTE-Link 固件。运行目录为项目根目录，使用已有
pyserial 3.5，无新电脑端依赖。退出正在运行的状态驱动和串口监视器，
同一 COM 口只能由一个程序占用；文件操作结束后可重新启动状态驱动。

```powershell
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 mount
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 ls /
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 mkdir /fonts
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 mkdir /assets
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 put .\font.bin /fonts/font.bin
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 get /fonts/font.bin .\font-copy.bin
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 rm /fonts/font.bin
.\.venv\Scripts\python.exe -m pc_driver.tf --port COM3 abort
```

`rm` 会删除指定文件。上传和下载均拒绝覆盖已有目标；需要替换时先明确删除
旧文件或使用新文件名。上例的 `rm` 不属于校验步骤，请仅在确实需要删除时运行。
路径使用 `/` 开头的绝对路径，固件限制为 96 个 UTF-8 字节。当前构建启用
SdFat 的 `USE_UTF8_LONG_NAMES=1`，路径可包含中文；电脑端 JSON 用转义形式传输。
中文文件名读取仍需这版固件真机确认。路径
拒绝 `.`、`..`、重复 `/`、尾随 `/`（根目录除外）及 FAT 非法字符。
二进制内容无字符集限制，包括字库、位图、零字节文件。

## 协议和资源

继续使用 USB CDC、115200 设置、逐行 JSON，请求不超过原有 384 字节限制。
文件请求字段为 `v:2, cmd:"tf", id:uint32, op:字符串`，返回
`XTE: tf {"id":...,"ok":true,...}`；失败返回 `ok:false,error:错误码`。
电脑端忽略 SDK 日志和其他请求 ID；每块收到确认后才发送下一块。
不自动重发写操作，连接故障后重新开始传输。

| op | 请求字段 | 成功响应 |
| --- | --- | --- |
| mount | 无 | ready:true, chunk:128；重新挂载 |
| list | path | 打开目录 |
| list_next | 无 | name, dir, size；末尾 eof:true |
| mkdir | path | 创建目录，可包含父目录 |
| remove | path | 删除文件；不删除目录 |
| put | path, size:uint32, sha256:64位小写十六进制 | 打开新上传 |
| write | offset:uint32, data:小写十六进制 | offset:下一位置 |
| get | path | size:uint32 |
| read | offset:uint32 | data:最多128字节十六进制, offset:下一位置 |
| finish | 无 | sha256；完整传输才允许结束 |
| abort | 无 | 关闭会话，清理本会话未完成上传 |

每块最多 128 字节，SHA-256 使用 ESP32 框架内置 mbedTLS。固件只保留一个
打开的传输会话，不将整个文件加载到 RAM；目录逐条返回。
上传先写 `目标路径.xte-part`，结束时检查长度、SHA-256 和文件同步结果，
再通过 SdFat 改名为目标文件。下载校验成功后才把电脑端临时文件改名。
SHA-256 校验传输内容，不代表逐扇区读回或掉电原子性保证。

30 秒未收到合法文件请求会中止会话。正常中止、超时和长按休眠会关闭文件、
删除本会话临时上传。突发掉电/复位可能留下 `.xte-part`，下次上传不会截断它；
可用 `ls` 找到并用 `rm` 明确删除。`.xte-part` 名称禁止作为上传目标。
传输期间不拔卡，不长按休眠；更换卡后运行 `mount`。

共享 SPI 使用已核实引脚：SCLK 8、MISO 7、MOSI 10、EPD CS 21、SD CS 12。
SDK 没有统一 SPI 引脚接口，项目先初始化带 MISO 的 Arduino SPI，随后 SDK
复用该总线；SD CS 和 EPD CS 预先置高。SdFat 使用 SHARED_SPI，每个操作
和屏幕访问均在同一个 Arduino 主循环内执行。文件会话期间暂停显示及页面操作，
电源键仍可中止会话进入休眠；结束后恢复刷新。
SDK 的 SD SPI 上限为 40 MHz，其稳定性需目标设备实测；本次未修改 SDK。

## 验证和刷写边界

2026-10-11：PlatformIO 构建通过；电脑端协议测试覆盖二进制/空文件、
分块边界、错误响应、SHA-256 不一致和本地文件保护。测试使用串口模拟器，
不能证明 TF 硬件、共享 SPI 或真实传输速率。

后续已完成写入、大文件 YMODEM 与完整回读验证，实测范围及恢复布局见
[归档中的 USB TF 与安装器验收记录](docs/归档说明.md)。
构建产物为 `.pio/build/xteink_x4/firmware.bin`；不要运行 PlatformIO 默认 upload。
真机验收使用专用测试目录，并检查传输后屏幕和按键恢复。
