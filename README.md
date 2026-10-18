# XTE-Link

初代 **Xteink X4（ESP32-C3）** 的 USB／局域网墨水屏状态面板，横屏 800×480。
支持电脑状态、系统音量、助手状态、设置、TF 文件传输和长按电源休眠。
页头右上角显示电池图标，插着 USB 时图标左侧显示闪电（GPIO20 只表示接电，不区分是否充满）。
休眠时显示 TF 卡上的 4 级灰度图片，可固定、随机或顺序轮换；没有图片时显示内置像素画。
设置开发版分为显示、图库、启动、连接、助手提醒、设备与电源六类，包含设备端图片预览、
自动清残影、Clash 启动与上次应用恢复、连接诊断、审批角标/跳页和电池自动休眠。
默认菜单启动、审批仅角标，自动清残影与自动休眠关闭；确认后保存到 NVS，保留旧版设置以便回退。
支持 DSH 实时审批、Codex 桌面审批、`agents.json` 助手桥接、独立心跳和 AI 事件即时推送。
Codex 通过 Windows 桌面 IPC 同步状态和命令／文件审批，设备 ✓ 批准、顶部 → 拒绝；
复杂问题和权限表单在电脑处理。Codex 内部 IPC 已核对桌面版 `26.1013.4894.0`，更新后需重新验证兼容性。

## 直接使用

当前 Windows 托盘驱动：
[XTE-Link.exe](artifacts/pc-driver/installer-20261019-r1/XTE-Link.exe)（2026-10-19，单文件安装与维护版）。
首次运行自动打开安装窗口，提供“一键完整安装／更新”、刷入配套固件、下载构建并安装字库、安装内置图片素材。
EXE 内置固件、30 个 UI 图标／Logo、两张屏保、刷机与字体转换工具，无需安装开发环境或准备仓库文件。
字库首次从小米官方下载约 228 MB 的 MiSans 包，在本机生成并缓存，也可导入已有官方 ZIP。
安装前核对设备身份与真实分区／OTA 状态；首次保存当前完整 Flash，后续复用匹配备份；仅更新应用，保留恢复区。
目前仅支持已验证的 Pixel 3.0.12 恢复布局及沿用该布局的 XTE-Link；未知原厂／其他布局拒绝刷写。
字体、图片安装复用 YMODEM-1K 和 TF 完整回读摘要，同内容跳过，替换时保留旧文件，保留用户图片与旧字库。
详见[安装、重试与支持范围](pc_driver/INSTALLER.md)。说明、许可和对应源码也可直接从 EXE 窗口导出。
双击即可运行，无需 Python；右键托盘图标可配置 WiFi、连接方式、设备、推送间隔和休眠图片。
默认自动连接唯一的 ESP32 USB-Serial/JTAG 串口，多设备时手动选择。
先退出旧驱动和串口监视器，避免占用串口。
配置和日志保存在用户目录 `.xte-link/`，程序不设置开机自启。
使用说明与 SHA-256 随 EXE 放在同一目录。
Codex 桌面保持运行，驱动自动连接；断线、请求已处理或增量丢失后，旧审批按键失效。
驱动只发送单次批准／拒绝，不授予整个会话权限；不使用 Codex 键盘兜底。
原生请求不提供拒绝选项时，顶部 → 使用“取消本轮”。
CLI 独立会话不在本次接入范围。桌面订阅与状态同步已实测，用户已确认 X4 显示 Codex 正在运行；
X4 ✓ 回传真实 Codex 命令审批已实测成功，拒绝／取消的实体按键仍待验证。

首次配置局域网时，通过 USB 连接，在驱动窗口填写 2.4 GHz WiFi 名称和密码。
固件验证后保存至 TF 卡 `/xte-link/network.json`（含明文密码，需要可用 TF 卡）；
电脑仅保存设备身份与配对密钥。然后在设备设置中手动选择“局域网连接”，
驱动默认自动跟随设备的连接方式。局域网自动发现失败时，在驱动设置填写设备 IP。
驱动也可固定选择串口直连或局域网连接，两侧选择须一致。

驱动设置中的常规推送间隔为 5～3600 秒，默认 5 秒；AI 事件立即发送，
心跳独立每 5 秒发送，长间隔不会导致固件误判离线。助手页的事件刷新跳过普通刷新间隔，
仍等待屏幕可用；默认不切页，新设置可选审批时切到助手。设备“屏幕更新间隔”只控制普通刷新。
局域网复用 TCP，mDNS 只发布设备身份，配对握手与消息采用 HMAC-SHA256 校验；
消息内容不加密。恢复默认切回串口，保留 TF WiFi 配置及字库。

托盘“上传休眠图片（USB）”可一次选多张图片，拖动预览调整取景、滚轮缩放，
预览即设备上的 4 级灰度效果；上传时转为 800×480 的 4 位灰度 BMP，写入 TF 卡 `/.sleep/`，
当前真机每张约 2.5 秒（192070 字节，含完整 TF 回读校验），可勾选先删除已有图片。
新版固件自动采用 YMODEM-1K，旧固件自动使用原协议；传输期间设备 UI 和网络轮询暂停。
新版设备“设置 → 屏保图库”可预览并选固定/随机/顺序；
随机避开上一张，顺序按路径循环，预览不推进轮换记录。图库包含根目录 `/sleep.bmp` 与 `/.sleep/`，
最多 200 项；从旧版迁移时，已有根目录图片先沿用为固定屏保。固件只读取 800×480、未压缩、1/4/8 位调色板 BMP，
其他格式或读取失败时显示内置像素画。灰度显示流程参考 CrossPoint Reader（MIT）。

## 随仓库附带的息屏皮肤

两套图片素材已内置于 [assets/sleep-skins/](assets/sleep-skins/README.md)，包含可直接使用的
800×480 四级灰度 BMP、PNG 预览、高分辨率原图、生成提示词与 SHA-256。
将 BMP 复制到 TF 卡 `/.sleep/`，或使用电脑驱动的“上传休眠图片（USB）”；
然后在设备“设置 → 屏保图库 → 选择与预览”中预览并确认固定，也可选择随机或顺序。

| 硬件实验室 | 静默终端 |
| --- | --- |
| ![硬件实验室](assets/sleep-skins/hardware-lab.png) | ![静默终端](assets/sleep-skins/quiet-terminal.png) |
| [设备 BMP](assets/sleep-skins/hardware-lab.bmp) | [设备 BMP](assets/sleep-skins/quiet-terminal.bmp) |

## 固件与验证

当前应用：[firmware.bin](artifacts/firmware/installer-20261019/firmware.bin)，版本 `installer-20261019`。
已通过安装器刷入现有验证设备；独立 verify_flash、完整应用读回、系统区与恢复 app0 摘要检查通过。
保留新 YMODEM 传输；补充卡内文件摘要、空间查询、无覆盖改名、版本握手与资源安装后刷新。
电脑端 112 项回归、设备资源重复安装、文件替换与独立下载对比通过；单 EXE 的内置运行库和四份字库生成已验证。
备份、打包方式及实测范围见[安装器验收记录](artifacts/firmware/installer-20261019/VERIFICATION.md)。

此前应用：[firmware.bin](artifacts/firmware/usb-tf-20261019/firmware.bin)，版本 `usb-tf-20261019`，
已刷入验证设备 app1；完整应用读回、系统区和恢复 app0 摘要检查通过，保留 MiSans 与灰度 UI。
1 MiB 上传约 8.34 秒，原协议约 31.41 秒；4 MiB 约 30.2 秒，新路径均含完整 TF 回读 SHA-256。
CRC/丢 ACK/序号回绕/取消/超时/哈希错误恢复、旧协议回退和独立下载通过。
接收器复用固定版本的 LoBo ESP32 YMODEM，通过 SDK `FsFile` 写卡；
来源、硬件范围、实测数据与限制见 [验收记录](artifacts/firmware/usb-tf-20261019/VERIFICATION.md)。

MiSans 多字号已上传 TF 并刷入设备：[接入与验收说明](firmware/FONTS.md)、
[转换工具与官方获取方式](tools/misans/README.md)。XTE-Link 使用小米提供的 MiSans 字体，
保留其[许可原文](tools/misans/LICENSE-MiSans.txt)；字体不适用本仓库代码的 MIT 许可。
24/32/40px 中文与 64px 数字字库由电脑本地生成到 TF 资源目录，不作为独立字体包公开分发。
此前 MiSans 应用：[firmware.bin](artifacts/firmware/misans-20261018/firmware.bin)，版本 `misans-20261018`。
四份字库的完整 SD 重读 SHA-256、应用独立完整读回、系统区与恢复槽校验通过；
设备启动正常，灰度素材无缺失，电脑驱动已恢复。字号观感和实体操作仍待观察。

灰度扁平 UI 素材第一版见 [图标与 Logo](assets/ui/gray-flat-v1/README.md)：
26 枚图标、4 种尺寸的小 Logo，包含 SVG 源图、PNG 预览、TF 用 BMP 与来源记录。
已接入下列固件并上传 TF：页头 Logo、两页菜单、设置分类、连接/供电状态与横屏按键提示。
实时电量仍动态绘制；缺卡或素材读取失败时回退到原图标。

此前灰度 UI 应用：[firmware.bin](artifacts/firmware/gray-flat-ui-20261018/firmware.bin)，
版本 `gray-flat-ui-20261018`。此前已刷入验证设备 app1，独立镜像校验、系统区与恢复槽摘要检查通过。
30 个 TF BMP 已全部读回校验；启动加载 13 项素材、无缺失，灰度刷新正常，电脑状态与心跳回执正常。
图标复用 Phosphor 2.1.1（MIT），通过现有 BMP 读取器与 SDK 灰度接口绘制；不新增整屏缓冲。
灰度每次需追加灰度波形，首屏约 2.3 秒；屏幕观感、连续操作与残影仍需实体设备验收。

此前设置中心应用：[firmware.bin](artifacts/firmware/settings-center-20261018/firmware.bin)，
版本 `settings-center-20261018`，六分类设置中心，完成编译、生产代码主机测试与 UI 渲染；
已于 2026-10-18 刷入当前验证设备 app1；独立校验通过，系统前 64 KiB 和恢复 app0 摘要不变。
设备正常启动、TF/字库正常，电脑驱动已恢复，局域网状态与心跳回执正常。
新版不需要更改协议；原驱动仍可使用，新 EXE 更新图片上传说明。
自动休眠仅在电池供电时生效；按键、TF 传输、有效待审批、设置页与 Clash 等待/确认暂停计时。
深睡停止接收消息，仍通过长按电源唤醒。助手自动跳页等待按键松开，设置编辑期间不抢页面。
固件 SHA-256、源码摘要、SDK commit 和软件验收范围见同目录 `manifest.json`。

此前已刷入并验证的应用：[firmware.bin](artifacts/firmware/clash-controls-20261017-232833/firmware.bin)，
版本 `clash-controls-20261017`，SHA-256 与构建来源见同目录校验文件和 `manifest.json`。
已刷入当前验证设备 app1；独立镜像校验通过，系统前 64 KiB 和恢复程序摘要不变。
本版接通第二页 Clash Verge：真实订阅列表、策略组与节点分页、当前节点标记、
手选节点、代理及 TUN 开关二次确认、超时和兼容性提示。用户已确认前版实屏进入与列表显示正常。
需搭配上述新电脑驱动，旧驱动会显示“驱动未响应，请更新电脑驱动”。
现有 Mihomo 命名管道可读数据和切换运行时节点；选择不保证重启或配置重载后保留。
代理开关通过 Windows WinINet，TUN 开关通过当前 Mihomo 内核管道；不再因缺少 Verge 补丁禁用。
这些兼容动作不写 Verge 保存配置，Verge 自己的开关可能不同步，重启/重载或操作其界面可能恢复。
代理守卫、自动检测或其他程序正在管理的代理拒绝此兼容路径，避免反复覆盖。
TUN 功能保留给用户，开发验证只做隔离测试，真实 TUN 写入请求为 0。
订阅更新已按能力接线，但当前官方 Verge 仍需完整兼容补丁；未安装时只读列表并说明原因。
系统代理关闭/开启并恢复原状态已实测，TUN 和 Verge 配置文件不变；不冒充完整四项交付。
上述已刷入 Clash 版本静态 RAM 为 103900 字节，应用镜像 907168 字节；7540 字形、实际渲染函数和
按钮状态机回归通过，电脑驱动 91 项测试通过。
电量仍使用 SDK 默认分压倍数，百分比仅供参考。
中文界面需自行准备 TF 字库 `/fonts/Zheng20号.epdfont`；
匹配文件为 7,958,602 字节，SHA-256：
`b62eba931c899cb676b71cc6759d805f65e67ba79bc94180fdb5ce7431c9cb59`。
字体文件不随仓库分发，缺失或读取失败显示方框。

**不要使用 PlatformIO 默认 upload。** 刷写前核对备份、设备、分区和 OTA 启动槽，
只更新已确认的应用槽并独立校验，保留引导器、分区和恢复更新器。
现有实测设备的 app1 为 `0x650000`，不能直接套用到其他设备。
已有完整备份来自 Pixel 3.0.12，原厂备份缺失。

## 横屏操作

当前 `settings-center-20261018` 沿用两页 2×2 应用菜单：
第一页为电脑状态、系统音量、助手控制、设置；第二页为快捷功能、Clash Verge、占位二、占位三
（闪电、网络节点、便签、加号）。Clash Verge 可进入控制面板，其余三项仍是占位。
底部方点与页码显示位置。进入/退出应用半刷，选择与翻页快刷。
旧版开机及唤醒进入第一页，沿用已有菜单默认项；设置中心开发版可选菜单、指定应用或上次应用，
默认应用支持 Clash，并定位到菜单第二页。

以下操作适用于两页菜单版，方向均采用当前横屏视角：

| 界面 | 顶部 ← / → | 右侧 ↑ / ↓ | ✓ | × |
| --- | --- | --- | --- | --- |
| 应用菜单 | 循环切换前 / 后一页 | 按左上→右上→左下→右下顺序循环选择，上键反向 | 打开所选应用，未实现的占位项除外 | 保持菜单 |
| Clash 面板 | 列表前/后一页 | 循环选择 | 进入；可写动作二次确认 | 取消确认/返回上级/菜单 |
| 电脑状态 | 无操作 | 无操作 | 无操作 | 返回菜单 |
| 系统音量 | 无操作 | 增加 / 减少音量 | 无操作 | 返回菜单 |
| 助手控制 | → 拒绝当前待审批请求 | 切换助手 | 批准当前请求 | 返回菜单 |
| 设置分类 | 无操作 | 选择分类 | 进入分类 | 返回菜单 |
| 设置列表 | 无操作 | 选择设置 | 打开选项 | 返回分类 |
| 设置编辑 | 调整候选值 | 无操作 | 保存 | 取消并返回列表 |
| 设置图库 | 无操作 | 选择图片 | 预览 | 返回列表 |
| 图片预览 | 上/下一张 | 无操作 | 固定该图片 | 返回图库 |
| 设置详情 / 恢复默认 | 无操作 | 无操作 | 详情刷新；重置页确认恢复 | 返回列表 |

顶部 ←/→ 对应 SDK `BTN_UP/BTN_DOWN`，右侧 ↑/↓ 对应 `BTN_RIGHT/BTN_LEFT`。
设置分类/图库操作适用于设置中心开发版。每页分别保留选中项，返回菜单保留选中应用；
打开应用的一次按键不会同时执行音量或审批动作。
新版图标读取 TF `/xte-link/ui/gray-flat-v1/`，缺失时复用原 Adafruit GFX 图元回退；文字读取原 TF 字库。
菜单翻页、选择和内容更新使用快刷，进入/退出应用使用“半刷”（SDK `HALF_REFRESH`，仍覆盖整屏）；新版额外执行灰度覆盖与差分基线恢复。
菜单继续接收数据，普通指标不重绘，审批数量变化更新角标，
连接状态变化仍立即更新。短按电源全刷，长按约 1.5 秒休眠，再次长按唤醒。

## 从源码构建

在仓库根目录执行，需要 Git、Python 3.11；首次安装依赖需联网。

```powershell
git submodule update --init --recursive
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/apply_sdk_patch.py
.\.venv\Scripts\python.exe scripts/apply_ymodem_patch.py
.\.venv\Scripts\python.exe -m platformio run -e xteink_x4
```

固件输出 `.pio/build/xteink_x4/firmware.bin`，成功构建自动备份至 `backups/firmware/`。
SDK 固定 commit `7d86603ad27709a9a766bb5ad893cfc39e60777e`；
补丁脚本给已有 SDCardManager 接入格式化接口，可重复执行。
工具链和固件库版本固定在 `platformio.ini`，SDK 通过子模块和 `symlink://` 引用。
YMODEM 子模块固定 commit `efb1466d00bc8f4f591c770cd349819675e83b83`，
必须应用上述接收适配补丁；[补丁说明](patches/README-ymodem.md)包含来源、许可及修改范围。

USB 文件上传可用 `python -m pc_driver.tf --port COM3 put 本地文件 /TF路径`，
在 `put` 参数后加 `--legacy` 使用原 JSON 上传。此工具与托盘驱动须依次使用，避免占用同一串口。

Windows EXE 构建：

```powershell
.\.venv\Scripts\python.exe -m pip install -r pc_driver/requirements-build.txt
.\.venv\Scripts\python.exe scripts/build_driver.py
```

生成产物位于 `artifacts/pc-driver/<构建时间>/`。
也可运行 `.\.venv\Scripts\python.exe -m pc_driver.driver --port COM3`，
将 `COM3` 替换为实际串口。传感器不可用时返回缺失值。

## 仓库范围与许可

保留固件与驱动运行源码、必要构建脚本和依赖、SDK 子模块与补丁、两套息屏素材、
当前 EXE/固件及其校验文件、本 README 和 [MIT 许可证](LICENSE)。
完整备份、其他历史产物、截图和详细开发文档只在本地保留；随源码保留必要的验证脚本。
更新公开版本时同步调整 `.gitignore` 中的产物目录。

项目自己的代码采用 MIT；SDK 及其他依赖遵循各自许可证，
SDK 补丁附带 [上游许可](patches/LICENSE-open-x4-sdk)。Codex IPC 适配参考
[codex-mobile](https://github.com/NathanZane/codex-mobile) commit `f79e6807ca0b9d6052afd24f822ee41b9a52e07d`，
保留其 [MIT 许可](pc_driver/LICENSE-codex-mobile)，EXE 目录也附带许可副本。
ESP32 YMODEM 接收器使用文件头的 LoBo 2017 许可，版权、许可及免责声明原文随源码和固件保留；
PC 的 `ymodem==1.5.3` 为 MIT，EXE 目录附带许可副本。
