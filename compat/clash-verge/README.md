# Clash Verge 最小兼容接口（开发中）

目标：Clash Verge Rev 2.5.8，commit 4337bd85758d54d7b9044b2da3c9ac1176eae1f3。
插件以该版本 Cargo.lock 为准：tauri-plugin-mihomo 7a03d16f4d39b1bb885b7babe66b9e153e34b615。

提供补丁源、纯内存模拟器及电脑客户端；**尚未安装到运行中的 Verge**。
客户端已接入设备页面：有补丁时按能力分发四项动作；无补丁时驱动使用原生兼容路径。
原生兼容路径读取订阅、切换运行态节点/TUN、通过 Windows WinINet 切换系统代理；
不写 Verge YAML、不直接写注册表、不重启 Verge、不模拟按键。开关与节点不保证重载后保留，
Verge 自己的保存状态/界面可能不同步。完整订阅更新仍只走本补丁，原生路径不冒充实现。

## 已核实与未核实

- 现有 Mihomo 命名管道：48 次 GET 成功；2026-10-17 完成一次真实节点切换并切回原节点。
- 实机只发送两次节点 PUT；TUN、系统代理、运行配置、配置文件摘要、进程和窗口前后一致。
- 当前官方 Verge 没有完整四项控制 HTTP 接口，新增 capabilities 路由返回 404。
- Rust 共享路由 5 项测试、Clippy（warnings 为错误）、模拟器构建通过；Python 客户端 8 项隔离测试通过。
- 真实适配器按固定源码和插件类型核对，**未完成上游整包编译或真实四项写入验收**。
- 本机 rustc 为 1.98.1，上游要求至少 1.99。模拟器可用不等于真实适配器已集成。
- 用户允许在固件中提供 TUN 开关，**仅禁止开发者实机测试 TUN 切换**。2026-10-17 原生代理开关已完成往返恢复；TUN 写入 0、配置文件不变。完整补丁的系统代理/TUN 与订阅更新尚未实机测试。

## 文件

| 文件 | 用途 |
| --- | --- |
| bridge.rs | 共享路由、串行命令、去重和异步回执 |
| verge_backend.rs | 接入 Verge 业务函数、状态读回与选择持久化检查 |
| verge-2.5.8.patch | 将模块加入既有 loopback 服务的补丁 |
| make_patch.py | 检查固定 commit、生成补丁及 git apply --check；不修改调研副本 |
| harness/ | 仅支持 --simulate 的内存后端，不链接真实适配器 |
| test_client.py | 使用随机 loopback 端口与临时实例文件的隔离验证 |
| ../../pc_driver/clash_verge.py | 默认只读的可选客户端；设备面板显式启用已确认的写动作 |
| ../../pc_driver/windows_proxy.py | 无补丁时的 Windows WinINet 当前用户代理兼容路径 |

## 接口约定

复用 Verge 的 127.0.0.1 服务和 singleton-instance.json 中的端口/token。
所有新路由经现有 x-instance-token 鉴权，不开放到 LAN，不向设备传 token、订阅地址或节点连接参数。
bridge::routes 本身不鉴权；集成方必须按补丁先挂鉴权。

| 方法/路径（前缀 /xte-link/v1） | 行为 |
| --- | --- |
| GET /capabilities | 版本、就绪状态、支持动作 |
| GET /state | 当前订阅、裁剪后 remote/local 列表、组成员和开关读回 |
| POST /command | 接受一次明确目标动作并返回回执 |
| GET /requests/{id} | 查询回执，不重新执行 |

命令格式示例：

~~~json
{"v":1,"id":"unique-id","operation":{"action":"select_node","group":"组名","node":"节点名"}}
~~~

动作仅限 update_profile(uid)、select_node(group,node)、set_system_proxy(enabled)、set_tun(enabled)。
更新只允许 remote，手选只允许 Selector 的现有成员；开关使用明确布尔值。
适配器复用 update_profile、内核选择 + record_selected_node、patch_verge_config，
不把 provider 更新当作完整订阅更新。系统代理读回兼容普通代理/PAC。

回执为 pending / done / failed。失败包含固定错误码和 uncertain；结果不确定时只查询状态，不自动重放。
请求体最多 4096 字节，名称最多 512 UTF-8 字节，ID 最多 64 个 ASCII 字母数字/连字符；未知字段拒绝。
同一实例仅有一个兼容接口写任务，保留最后 32 个请求去重：同 ID/同请求返回原回执，冲突返回 409。
回执只在内存中，重启丢失，不承诺无限期去重。调用方断线不取消已接受动作，后台 panic 标为不确定。
长任务不因客户端超时取消；不自动重启、重试或回滚。串行限制不阻止用户在 Verge 自己的界面操作。

Python 默认 allow_mutations=False，写请求在联网前拒绝；显式启用后也只 POST 一次。
端口/token 轮换、回执丢失或异常会停止轮询并报告不确定。
固定 loopback，禁用环境代理及重定向；token 不进入 repr。

## 复现隔离验证

在 XTE-Link 项目根目录运行 PowerShell：

~~~powershell
$env:CARGO_HOME = Join-Path (Get-Location) '.pio/clash-bridge-cargo'
cargo test --locked --manifest-path compat/clash-verge/harness/Cargo.toml
cargo clippy --locked --manifest-path compat/clash-verge/harness/Cargo.toml --all-targets -- -D warnings
cargo build --locked --manifest-path compat/clash-verge/harness/Cargo.toml
& './.venv/Scripts/python.exe' compat/clash-verge/test_client.py
& './.venv/Scripts/python.exe' compat/clash-verge/make_patch.py
~~~

模拟器仅修改测试创建的临时实例文件和内存；最后一条只生成并检查补丁。
构建产物、缓存及调研副本不作为正式驱动发布。

## 集成门槛

1. 使用满足上游 MSRV 的环境，在独立源码副本应用补丁并构建完整 Verge，保留上游 lockfile。
2. 在不承担当前服务链路的独立实例验证四项动作、后台/轻量模式及失败读回；当前本机 TUN 禁止测试。
3. 验证节点持久化、订阅保存成功但应用失败、PAC/普通代理、权限不足、重启和回执丢失。
4. 通过后再接到既有 XTE-Link 驱动和设备有界分页协议。模拟通过不能当作实机完成。

## 许可证

XTE-Link 自编兼容模块和测试遵循项目 LICENSE（MIT）。补丁含上游文件上下文，
目标 Verge 的 Cargo.toml 标注 GPL-3.0-only。集成后的 Verge 分发遵循上游 GPL 要求并保留许可证，
不能把修改后的 Verge 整体当作 MIT 发布。
