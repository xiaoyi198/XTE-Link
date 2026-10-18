# ESP32 YMODEM 接收适配

上游：<https://github.com/loboris/ESP32_ymodem_example>，固定
`efb1466d00bc8f4f591c770cd349819675e83b83`，通过 Git submodule 保留。
`ymodem.c` 和 `ymodem.h` 文件头中的 LoBo 2017 许可原文保留；不是 MIT。
许可允许使用、修改和分发，要求保留版权、许可及免责声明，且不得未经许可用作者名宣传。

克隆后执行 `.venv/Scripts/python.exe scripts/apply_ymodem_patch.py`。
PlatformIO 通过 `symlink://third_party/esp32-ymodem/components/ymodem` 只构建接收器。
不构建上游示例、UART 驱动、SPIFFS、镜像、LED 或发送器。

补丁保留上游接收流程和 CRC 算法；以同步回调接入 ESP32-C3 原生 USB 与 SDK 已打开的
`FsFile`，不复制 SD 驱动。新增路径只接受控制协议预先验证的单个文件，YMODEM 文件名不参与路径选择。
修补：有界元数据及整包读取、声明长度匹配（含空文件）、重复块/重复文件头 ACK 而不重复写入、
序号回绕、完整长度才接受 EOT、严格的空批次尾、尾 ACK 丢失重传，以及 USB/存储写入错误上报。
传输在现有 Arduino loop 任务内运行，避免和显示抢 SPI；传输期间 UI 与网络轮询暂停。
文件保留在 `.xte-part`，必须经过 `finish` 的同步、完整 TF 重读 SHA-256 和原子改名才发布。

XYmodem 候选未接入：存储耦合、无写入结果检查、重复文件头重开文件及 purge 越界等问题使适配成本更高。
PC 使用 <https://github.com/alexwoo1900/ymodem> 的 PyPI `ymodem==1.5.3`（MIT）；
薄封装补上其发送后未等待批次尾 ACK、留下 EOT 后的 `C` 未读取的行为，并保留原 JSON 上传作为回退。
只有发送过 EOT 后才识别批次尾，避免把回绕到序号 0 的数据误当作尾包。

Arduino 2.0.17 原生 HWCDC 在本机出现长操作后整包/半包 TX 停滞。固件整帧输出并等待/flush；
PC 控制请求停滞时复用现有只读 `hello` 查询唤醒队列，不重放写入/删除/提交命令。
`put` 的短就绪回包阶段禁用此查询，避免进入二进制接收后插入 JSON。
不修改 Arduino 核心、USB 驱动或 SDK 引脚；此规避办法的验证范围是本记录中的 Windows/ESP32-C3 组合。
托盘图片在临时目录生成 BMP，调用文件发送器并自动清理；WiFi 配置继续使用内存中的旧上传路径。
