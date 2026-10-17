# 息屏皮肤

仓库随附两套原创图片，使用内置 imagegen 绘制，按 XTE-Link 的硬件开发主题设计。
设备版已经导出为 **800×480、未压缩 4 位调色板 BMP、四级灰度 0/85/170/255**，
取消抖动以保持留白干净；PNG 预览与 BMP 解码后的像素一致。

| 皮肤 | 设备文件 | 预览 | 原图 |
| --- | --- | --- | --- |
| 硬件实验室 | [hardware-lab.bmp](hardware-lab.bmp) | [hardware-lab.png](hardware-lab.png) | [原始 PNG](originals/hardware-lab.png) |
| 静默终端 | [quiet-terminal.bmp](quiet-terminal.bmp) | [quiet-terminal.png](quiet-terminal.png) | [原始 PNG](originals/quiet-terminal.png) |

## 放入设备

1. 将上面的两个 BMP 下载或从克隆的仓库取出，复制到 TF 卡根目录下的 `/.sleep/`。
   也可以连接 USB，使用 XTE-Link 电脑驱动的“上传休眠图片（USB）”选择这两个 BMP。
2. 使用 `settings-center-20261018` 或更新固件，在“设置 → 屏保图库 → 选择与预览”中选图。
3. 确认预览后再次确认，设为固定屏保；顶部 ←→ 可换图。也可将屏保模式设为随机或顺序。

图片文件随仓库提供，固件从 TF 卡读取；它们不占用固件内嵌的默认像素画。
旧版固件若在根目录找到 `/sleep.bmp`，会优先显示根目录图片；新版可通过屏保模式选择。
不要勾选“先删除已有图片”，即可保留自己已有的图库。

[SHA256SUMS.txt](SHA256SUMS.txt) 包含设备 BMP、预览和原图的摘要。
[manifest.json](manifest.json) 记录格式、来源及文件映射；[PROMPTS.md](PROMPTS.md) 保留生成提示词。
