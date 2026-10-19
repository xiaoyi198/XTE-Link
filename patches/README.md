# OpenX4 SDK 本地补丁

基准仓库：<https://github.com/open-x4-epaper/community-sdk>。
固定 commit：`7d86603ad27709a9a766bb5ad893cfc39e60777e`。

`open-x4-sdk-sdcard-format.patch` 保存本项目已有的格式化与空间查询修改：
给 `SDCardManager` 暴露 `format()`，内部调用 SdFat 的格式化接口，随后清除挂载状态；
空间查询用于安装前确认 TF 可用容量。
字库读取不调用此接口；它用于现有 TF 命令，不实现新的 SD 驱动。

从根目录执行：

```powershell
git submodule update --init --recursive
.\.venv\Scripts\python.exe scripts/apply_sdk_patch.py
```

已应用时脚本直接成功返回。可用 `--check` 检查已应用或可应用，不改文件；
它不等于“补丁已经应用”。旧版仅 format 的工作区通过 `open-x4-sdk-sdcard-space.patch` 增量迁移。
脚本检查固定 commit，并在补丁冲突时停止，不会重置或覆盖额外本地改动。
应用后子模块显示 modified 是预期状态；主仓库以 gitlink 加补丁记录它，而非复制 SDK 源码。

正式构建前 `scripts/check_dependencies.py` 只读检查 SDK/YMODEM 的固定 commit 与补丁已应用状态。
检查失败时先保留本地修改，再执行对应应用脚本；构建钩子不自动修改子模块。
归档同时记录实际补丁源文件摘要，额外本地修改不会被伪装成纯上游 commit。

上游 SDK 采用 MIT，版权和许可全文保留在 [LICENSE-open-x4-sdk](LICENSE-open-x4-sdk)。
