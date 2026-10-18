# USB → TF 快速上传验收（2026-10-19）

已在初代 Xteink X4 / ESP32-C3 真机接入并部署 YMODEM-1K。复用
[LoBo ESP32 YMODEM](https://github.com/loboris/ESP32_ymodem_example/tree/efb1466d00bc8f4f591c770cd349819675e83b83)
的接收流程，原生 USB 作为传输回调，OpenX4 SDK `FsFile` 作为写入回调。
没有复制 SD 驱动、改变引脚或升级整个 Arduino 核心。

## 当前产物与刷写

- 应用 `firmware.bin`：932480 字节，SHA-256
  `33a945ab0ad03342f4e13c92684d05d19d7027e79af5a1ffbd7d81d75d849d90`。
- 仅写验证设备 app1 `0x650000`；设备 `a053651b5bf8`，MAC `f8:5b:1b:65:53:a0`，COM3。
  此偏移不能直接用于其他设备。
- 刷前检查分区表 MD5、OTA 记录 CRC/已选 app1、旧应用摘要、完整备份摘要和恢复 app0 摘要。
  写后 `verify_flash` 与独立完整读回均通过，前 64 KiB 系统区完全相同，恢复 app0 摘要相同。
- 正式构建环境 `xteink_x4`，不含 `XTE_TF_BENCH` 诊断命令。
  静态 RAM 105652 字节，程序统计 891654 字节；相对 MiSans 版分别增加 8、5884 字节。
  USB RX/TX 分别配置 2048 字节；额外动态缓冲共 2816 字节。
- 保留 MiSans 字库、灰度 UI 和设置。开机 `assets=13 missing=0 gray=1`，全刷约 2364 ms；无字体方框错误。
- Windows 驱动为 `artifacts/pc-driver/20261019-014048/XTE-Link.exe`，SHA-256
  `4325c23226de69662ce3ba97590393726fb7a6ba4717cdea9f17ae6a56338067`。
  已恢复运行，观察到 Codex IPC 连接/同步、设备状态 accepted 与心跳 pong；未发送真实审批/代理/TUN 动作。

已有完整 Flash 备份仍为 Pixel 3.0.12，原厂备份缺失。日常 app1 更新未重复读取整片 Flash。
每次成功构建的独立归档与历史版本继续保留。

## 速度与完整性

同一设备、TF 卡、USB 原生串口。计时包含 PC 计算摘要、传输和提交；新路径还包含
`sync`、关闭/重新打开文件、完整 SDK 读取 SHA-256 后再改名。以同一文件逐字节模式测试，
覆盖所有 256 个字节值及非整包长度。

| 文件 | 原 JSON 上传 | YMODEM-1K 上传 | 提升 |
| --- | ---: | ---: | ---: |
| 65539 字节 | 2.016 s | 1.531 s（三次中位数） | 1.32× |
| 1048589 字节 | 31.406 s | 8.344 s（三次中位数） | 3.76× |
| 4194333 字节 | 未作完整旧协议测试 | 30.187 s | — |
| 192070 字节灰度 BMP | 未重测 | 托盘实际调用方法 2.500 s | — |

最终驱动代码再次确认 64 KiB / 1 MiB / 4 MiB 分别为 1.593 / 8.344 / 30.156 s。
以上快速上传的 PC、接收流、TF 回读三个 SHA-256 均一致。
托盘图片调用 `Session.upload_sleep_images`，独立下载并逐字节比较通过；测试图片写入专用测试目录，未加入用户图库。
1 MiB 的旧 JSON 下载在 USB 队列恢复措施后连续三次校验通过，另一次记录为 36.532 s，
期间用了 49 次只读查询唤醒停滞队列。下载速度会受该核心 USB 问题影响。

单独的临时诊断构建测得 SDK 写入 `2 MiB + 13`：3.830002 s（约 535 KiB/s）；
完整读取并 SHA-256：3.039043 s，读取 API 累计 2.982203 s。诊断堆剩余 151356 字节，
记录到的任务栈最低余量 2180 字节。没有据此推断其他 TF 卡的速度。

## 错误恢复与回归

真机通过：0 / 1 / 127 / 128 / 129 / 1023 / 1024 / 1025 字节；CRC 错误重传；
文件头 ACK、序号 255 ACK、回绕到序号 0 ACK、批次尾 ACK 丢失；显式取消；错误元数据；
截断包后接收超时；声明 SHA 不匹配；已有目标不被覆盖；错误后再次上传成功。
失败文件不发布，失败 `.xte-part` 已移除。原 JSON 上传与下载继续可用。

- 实际 C 接收器主机测试 22 项：另覆盖短写/模拟满写入端、USB 写失败、畸形元数据等。
- 实际 PC `ymodem==1.5.3` 与实际 C 接收器双向联调 10 项，检查回到 JSON 时没有残余控制字节。
- 电脑驱动 96 项回归通过；目标编译器对 `main.cpp` / `ymodem.c` 的 `-Wall -Wextra -Werror` 检查通过。
- 30 张 UI BMP 三个位平面逐像素比对、缺卡/读取错误回退、真实设置/显示状态机回归通过。
- 从干净、固定 commit 的子模块应用补丁后，C/H/library.json 与当前依赖逐字节一致；补丁脚本幂等检查通过。
- 测试结束删除 41 个白名单测试文件；三个专用测试目录为空。根目录及字库/皮肤/UI 目录条目、大小保持一致。

## 协议与 USB 适配

`mount` 宣告 `upload:ymodem1k`；`put` 在验证路径、大小、目标不存在和临时文件独占创建后切换二进制接收。
YMODEM 元数据文件名不决定 TF 路径，声明长度必须与控制请求一致。
`finish` 完成摘要、同步与完整 TF 回读后才发布文件。CLI `put --legacy` 可使用原路径；旧固件自动回退。
WiFi 配置仍走内存中的原协议；托盘已转换的图片通过临时 BMP 调用上游文件发送器，退出后清理。

PC 上游发送器未等批次尾 ACK，并留下 EOT 后的 `C`；封装补齐握手，区分请求尾包与真正重传。
本机另实测 Arduino 2.0.17 HWCDC 会把整条或半条回包留在队列，后续写回包才继续发送。
固件使用整帧输出、公共连接检查/flush；PC 控制请求停滞时使用已有只读 `hello` 唤醒队列，
不重放原操作，进入二进制接收的 `put` 阶段禁用查询。上游有相关
[HWCDC 数据丢失记录](https://github.com/espressif/arduino-esp32/issues/9378)；本次现象与核心代码的关系属于代码分析推断，
实际停滞及恢复由串口轨迹验证。没有修改核心驱动。

## 可复现与限制

依赖 commit、构建标志、源码 SHA 在 `manifest.json`；具体数据、PC 源码摘要和验收范围在 `verification.json`。
SDK 固定 `7d86603ad27709a9a766bb5ad893cfc39e60777e`，接收器固定
`efb1466d00bc8f4f591c770cd349819675e83b83`；PC `ymodem==1.5.3`、`ordered-set==4.1.0`。
固件接收器使用随附 LoBo 2017 许可，PC ymodem 为 MIT。

```powershell
.\.venv\Scripts\python.exe scripts/apply_sdk_patch.py
.\.venv\Scripts\python.exe scripts/apply_ymodem_patch.py
.\.venv\Scripts\python.exe -m platformio run -e xteink_x4
.\.venv\Scripts\python.exe -m unittest pc_driver.test_tf pc_driver.test_network
.\.venv\Scripts\python.exe firmware/tests/verify_ymodem.py
.\.venv\Scripts\python.exe firmware/tests/verify_ymodem_interop.py
```

`firmware/tests/verify_tf_device.py` 为显式指定串口和设备身份的真机测试；先关闭驱动，并选新的空测试目录。
它不格式化、不刷固件、不覆盖用户文件，JSON 报告记录已创建文件，测试文件需按清单清理。

快速传输期间 UI 与网络轮询暂停；中断后重新上传，不支持断点续传。
YMODEM 双 EOT 受固定 PC 库的重试行为影响，约有 1 秒固定等待，因此小文件加速有限。
快速路径上限 `INT32_MAX`，更大文件走原 32 位大小协议；实际最大测试为 4 MiB。
满卡/短写与 USB 写失败仅在实际 C 接收器的主机回调注入；未填满、拔除 TF 卡或作物理断电/拔线测试。
仅验证本 Windows / ESP32-C3 / TF 卡组合，实体按键、冷断电持久性与长期寿命不在此次验收范围。
