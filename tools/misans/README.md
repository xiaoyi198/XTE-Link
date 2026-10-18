# MiSans 多字号 TF 字库工具

XTE-Link 使用 MiSans 字体，由小米提供。本工具只在电脑本地生成应用所需的显示资源，
不安装系统字体，不修改 TTF 轮廓，不将生成字库作为独立字体包公开分发。
MiSans 不适用本仓库的代码许可证；使用前阅读 [官方许可](https://hyperos.mi.com/font/zh/download/)
和本目录的 [许可原文](LICENSE-MiSans.txt)。

## 获取与生成

使用 Python 3.11+，在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r tools/misans/requirements.txt
.\.venv\Scripts\python.exe tools/misans/build_misans.py --download
```

`--download` 在本地没有 ZIP 时通过 Python 标准库下载
[官方 MiSans.zip](https://hyperos.mi.com/font-download/MiSans.zip)，约 228 MB；
已有 ZIP 可直接 `--source-zip '路径/MiSans.zip'`，不再联网。使用标准 ZIP 读取器只读取
包内 `MiSans/ttf/MiSans-Medium.ttf` 与 `MiSans-Regular.ttf`，没有实现私有下载器或字体引擎。
原包、TTF 都不会被修改。官网更新后摘要不匹配会停止，不能绕过检查后把新版本当旧版本。

默认产物在 `artifacts/misans-local/misans-4.009-medium-v1/`：

- `fonts/`：TF 卡 `/fonts/` 对应的 4 份 EPDF 文件；
- `MiSans-white-preview.png`：Regular/Medium 同文、同像素尺寸、白底 1:1 实际栅格预览；
- `manifest.json`、`SHA256SUMS.txt`：来源、版本、原包/TTF/输出摘要、尺寸与生成环境。

输出目录必须不存在，防止覆盖先前版本。复现时使用
`--output artifacts/misans-local/reproduce-4.009`；比较两次 manifest 的 `files`，应完全相同。
`reference-manifest.json` 是本次已验证的公开校验元数据，不包含字体数据。
要逐字节复现，保持所记录的 Pillow 12.3.0、FreeType 2.14.3 与 fontTools 4.60.1；
其他平台的 FreeType 构建如产生不同结果，应重新验证，不能静默替换固定摘要。

## 选择与格式

源字体为 **MiSans 4.009 Medium**，Regular 只用于对照。Medium 在 24px 下笔画更清晰；
没有人工描边、加粗、变形或重绘。数字仍使用同一个 Medium 源文件。
字号均为传给 FreeType 的 **px/em**，不表示所有字形的实际墨迹高度，也不是印刷字号。

| 文件后缀 | 用途 | 行高 / 基线 | 最大单字位图 | 文件字节数 |
| --- | --- | --- | --- | --- |
| 24 | 提示、设置与列表 | 35 / 27 | 91 B | 2,347,398 |
| 32 | 正文与一般标题 | 46 / 36 | 160 B | 3,863,692 |
| 40 | 菜单标题、音量标题 | 57 / 45 | 245 B | 5,729,819 |
| 64 | 音量大数字 | 51 / 49 | 325 B | 3,511 |

24/32/40px 包含原字体支持的 29,366 个 BMP 字符（排除控制字符及代理项），250 个区间。
保留全部 GB2312 的 6763 个汉字；GB2312 非汉字符号仅 `U+30FB` 缺失，继续显示方框，
不拿别的字体替换。64px 仅包含 `0123456789 .%+-:/` 共 17 个字符。
四份文件共 11,944,420 字节，旧 `/fonts/Zheng20号.epdfont` 必须继续保留。

栅格化复用 Pillow/FreeType：单字 BASIC 布局、整数像素尺寸、灰度覆盖率 >=128 为黑。
没有字符间 kerning 或复杂排版。位图为单色、连续打包、MSB 在前，最后一字节低位补零，
不是每行补齐；EPDF 头部 `is_2bit=0`。固件继续通过 SDK 的单色 framebuffer 绘制文字，
灰度图标使用已有独立灰度流程。

沿用仓库已验证的 **48 字节头 + 12 字节区间 + 13 字节字形记录**，
仅封装 FreeType 结果。MiSans bearing 为真正的有符号 int16；旧 Zheng 的负 int8 零扩展
兼容仅用于 Legacy20。格式不是 `.cpfont`，也不宣称兼容所有名为 EPDF 的工具。

已复核 [社区转换器](https://github.com/eunchurn/x4-epdfont-converter/tree/adf0ede98d7c42ceea1c4413b85b161c87caedce)：
其转换器源码使用 32 字节头/16 字节字形，直接接入会更换现有解析器并增加 Web/WASM 工具链；
因此只增加现有格式的打包脚本，栅格化与字体解析复用成熟库。
SDK 固定 `7d86603ad27709a9a766bb5ad893cfc39e60777e`，当前没有相应的 TF 字库转换/解码 API。

## 验证

先准备 PlatformIO 的项目依赖，再运行（Windows 需要 MSVC C++ Build Tools；其他系统用 g++/clang++）：

```powershell
.\.venv\Scripts\python.exe -m platformio run -e xteink_x4
.\.venv\Scripts\python.exe tools/misans/verify_misans.py
.\.venv\Scripts\python.exe firmware/tests/verify_settings.py
.\.venv\Scripts\python.exe firmware/tests/verify_ui_assets.py --misans-dir artifacts/misans-local/misans-4.009-medium-v1/fonts
.\.venv\Scripts\python.exe firmware/tests/verify_tf_font.py --font '旧字库路径/Zheng20号.epdfont'
```

主机验证直接编译生产读取器，按单字输出画布，与独立 `ImageDraw.text` 的阈值像素比较，
并验证摘要、全部区间/位图边界、字号切换、缓存清空、反白、裁切、无卡/缺文件、
坏头/区间/字形、短读和下一帧恢复。生产页面截图和 `verification.json` 在
`.pio/misans-host-test/`，带灰度素材的组合预览在 `.pio/font-host-test/ui-fixtures/`。
这些是软件结果，不代表墨水屏观感、残影、刷新速度或 TF 真机验收。

## 许可边界

官网核对日期：2026-10-18。官方 [FAQ](https://hyperos.mi.com/font/zh/faq/)
允许免费商用、嵌入，并要求在软件中特别注明使用 MiSans；固件在“设置 → 设备与电源”
的字库状态和启动日志中注明 MiSans，仓库 README 与本说明也保留来源。

许可同时禁止改编/二次开发，并禁止将字体或其组件单独进一步分发；用字体创作的其他作品
有另行的分发条款。**注明来源不是修改字体或独立发布转换字库的授权。**
原文没有单列 TTF→EPDF 栅格格式转换条款。本项目将标准字号栅格化作为应用显示/嵌入资源，
保持源轮廓原样；这是实施方式及对嵌入条款的理解，不能宣称小米另行批准了任何格式转换。
公开仓库只保留代码、说明、许可与摘要；每位使用者从官方获取后在自己的电脑本地生成。
如需单独对外分发 EPDF 或对转换授权取得确定结论，应先向小米取得明确许可；
在此之前沿用官方获取、本地生成这条路径，不公开字体 ZIP、TTF 或生成的 TF 字库。

本目录许可证快照来自官方许可页面正文；原始 HTML 保存在本机
`artifacts/misans-local/source/official-download.html`，供核对。再次使用时以官方当前条款为准。
