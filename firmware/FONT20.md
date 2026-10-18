# 20 号 TF 字库固件（2026-10-13）

已实现原始 `Zheng20号.epdfont` 按需读取和现有页面中文显示。
2026-10-13 用户授权后已刷入 app1，独立读回校验通过。
本轮不增加设置页或设置参数；后续页面可直接调用 `uiText()`。

## 使用的字库与接口

TF 路径固定为 `/fonts/Zheng20号.epdfont`，使用用户提供的文件原样读取，
不转换、不缩放、不写入 TF。若卡内已有原文件，无须重复复制。

- 文件大小：7,958,602 字节。
- SHA-256：`b62eba931c899cb676b71cc6759d805f65e67ba79bc94180fdb5ce7431c9cb59`。
- 原始 20 号：汉字步进 42 像素，“中”字形 28×41 像素；不是 20 像素高。
- 覆盖 GB2312 全部 6763 个汉字；7445 个 GB2312 字符中覆盖 7063 个，
  另有 382 个符号缺失，缺字显示方框。明细见本机 `artifacts/font-preview/` 预览报告。
- 固件字符串采用 UTF-8，解码后按字库 Unicode 区间索引查找。
  GB2312 是本次约定的字符覆盖范围，不把 UTF-8 字符串当成 GB2312 字节读取。
- 原始 2-bit 像素连续打包、MSB 在前；灰度 2/3 画黑，0/1 留白。
  继续使用 SDK 单色 framebuffer，没有新增灰度刷新驱动。

`src/tf_font.h` 提供 `beginRender()`、`measure()`、`draw()`、`endRender()`。
`draw()` 接收顶端坐标、UTF-8 文本、最大宽度和颜色，支持黑字或反白；
超过宽度时在完整字符边界停止。行高与基线采用原始指标，调用者安排各行位置。
已处理原字库负 bearing 被零扩展的问题，例如“秒”的左偏移是 -1。

## 内存与文件访问

读取器在 ESP32-C3 构建中大小为 **5868 字节**（ELF 符号核对），包括：
128 个区间的固定容量、8 个字形缓存（每个 bitmap 最多 512 字节）、
文件对象及少量状态。编译时限制总大小不超过 8 KiB。
原字库 bitmap 的最大单字长度实测为 492 字节。
没有把约 278 KB 的字形表或约 7.7 MB 的 bitmap 加载进 RAM。
该数字不包括 SDK 原有 SD 缓冲区、渲染调用栈或运行时文件分配。

每次渲染打开一次文件，检查 48 字节头及区间表，按需 seek/read 单字。
当前帧内重复字可命中缓存，下一帧清空缓存，避免换卡或文件替换后显示旧字。
结束绘制后关闭字体文件，再调用屏幕刷新；TF 串口会话期间沿用既有暂停绘制逻辑。
显示、字体和 TF 文件协议继续在同一个主循环串行访问 SDK 的共享 SPI。

按用户要求，无卡、缺文件、坏头/区间/字形、短读和缺字均显示方框，
不增加整页英文回退。读取错误后该帧余下文字画方框，下次渲染重新尝试。
没有无限重试或等待字体成功的循环；实际 SD 超时仍由 SDK/SdFat 决定。
读失败会输出 `XTE: font boxes: ...`；缺失的单个字符直接画方框。

## 页面改动

- 状态页：CPU/GPU 负载、GPU 温度、内存用量、连接状态、更新时间。
- 音量页：系统音量、左右键提示；音量值保留原 GFX 大数字。
- 助手页：状态、审批提示、确认/返回及切换提示。
- 休眠画面：“休眠中”使用同一字库。

按原始字号调整了文字间距，使用既有画布。
设备名、时间、助手名称和电脑端请求正文继续按现有 ASCII 协议绘制。
页面切换、音量 ±5、审批动作、电源长短按和刷新节流逻辑保持原样。

## 为什么需要小适配器

沿用 [OpenX4 SDK](https://github.com/open-x4-epaper/community-sdk/tree/7d86603ad27709a9a766bb5ad893cfc39e60777e)
的 SDCardManager、EInkDisplay、InputManager，以及项目已固定的 SdFat 2.3.1、
Adafruit GFX 1.11.11；没有新增固件依赖或复制驱动。
构建时 SDK 工作区已有 `SDCardManager::format()` 的本地修改（2 文件共 8 行）；
本轮未改动这些文件，字库读取不调用 format。

检查过的社区读取器不能直接加载这份旧字库：

- [CrossInk SdCardFont](https://github.com/clawlter/CrossInk/tree/cab4f24922f05811e7f44be1057f62ea2d978c52)：
  使用另一种 `.cpfont` 格式，并依赖其固件内部 HAL。
- [PapyriX EpdFontLoader](https://github.com/bigbag/papyrix-reader/tree/ee071b173e29b1e81fc7e259260077d5ba981b38/lib/EpdFont)：
  头/字形记录布局与本文件不同，流式读取仍常驻完整字形表。
- [EPDF 转换器](https://github.com/eunchurn/x4-epdfont-converter/tree/adf0ede98d7c42ceea1c4413b85b161c87caedce)：
  所述文件布局与这份实际 48 字节头、13 字节字形记录不一致。

因此仅实现已核实格式的索引和像素解码；头/记录布局的探测依据见
[字库预览工具说明](../tools/font_preview/README.md)，其余外设仍使用 SDK 接口。

## 构建与验证

```powershell
.\.venv\Scripts\python.exe -m platformio run -e xteink_x4
.\.venv\Scripts\python.exe firmware/tests/verify_tf_font.py --font '本地路径/Zheng20号.epdfont'
.\.venv\Scripts\python.exe -m unittest pc_driver.test_tf pc_driver.test_driver
```

主机验证需 Windows MSVC C++ Build Tools（本机已有），或其他平台的 g++/clang++。
测试桩只参与主机验证，不编入固件。SDK 及 Adafruit 依赖由 PlatformIO 构建准备。

2026-10-13 验证结果：

- PlatformIO 构建通过；静态 RAM 75,176/327,680 字节，应用 Flash 334,624 字节。
- 固件镜像 361,200 字节；esptool 确认 ESP32-C3、16 MB、DIO/80 MHz，checksum
  和镜像 validation hash 均有效。版本产物及 SHA-256 见发布目录 manifest。
- 7540 个 GB2312/可打印 ASCII 用例逐像素匹配独立 Python 离线预览解码，
  包括原字库的负 bearing；缺字符的方框也逐像素一致。
- 主机 C++ 测试通过：UTF-8 正常/截断/过长/代理项/超范围、整字裁切、
  反白、屏幕边界、缓存命中、无卡/缺文件/目录、坏头/区间/字形、
  bitmap 截断、读取错误与下一帧恢复、字体文件关闭和缓存重置。
- 从实际 `main.cpp` 渲染函数生成 6 个 800×480 画面，已目视检查布局。
  这些是软件 framebuffer 输出，不代表实屏灰阶、PPI、残影或刷新速度验收。
- 电脑端 32 项回归测试通过；修正了旧测试仍拒绝中文路径的过时预期，
  电脑端和协议实现未修改。

本机历史版本产物在 `firmware/releases/font20-2026-10-13/`，不随源码提交；
常规构建产物仍在 `.pio/build/xteink_x4/`。

## 真机待验收

开发构建阶段未连接设备。随后用户明确要求不新建备份、直接刷入并启动 PC 驱动；
已核对同一设备、分区 MD5、OTA CRC 和启动槽，按用户要求未新建固件备份，
只更新 app1 并独立校验，保留恢复路径。
此前原厂备份缺失及 app1 记录见 [TF 验证](../docs/verification/TF验证-2026-10-11.md)；不要使用 PlatformIO 默认 upload。

当前卡内原中文文件已上传并校验，驱动恢复后没有字体错误日志，连续三次
渲染为 545 ms、free heap 240680 字节。详见 [字库验证记录](../docs/verification/字库验证-2026-10-13.md)。
三个页面和休眠文字观感、连续切页/音量操作、按键响应及长时间堆稳定性仍待用户验收。
