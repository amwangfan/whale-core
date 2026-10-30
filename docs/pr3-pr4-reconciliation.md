# PR #3 / PR #4 并行实现整合审阅

日期：2026-10-30

两项 PR 均从相同旧基线开发：[#3](https://github.com/amwangfan/whale-core/pull/3) 已先合入 main，[#4](https://github.com/amwangfan/whale-core/pull/4) 的实现不能当作对 #3 的增量补丁。

## 重叠范围与选择

- #3：104 个修改文件；#4：74 个修改文件；有 **48 个路径重叠**。
- 受影响的核心包括 `core/runtime.py`、`plugins/manager.py`、`profiles.py`、`api/server.py`、`cloud/shell_executor.py`、`pyproject.toml` 及多数 `web/src` 控制台组件。
- #3 的主 Runtime 已有持久任务、幂等/审批恢复、工具超时/回执、可继续执行任务等逻辑；#4 的主 Runtime 为另一套结构，不应整文件覆盖。
- #3 已验证 Python 932 例、前端 32 例，另有 Linux/macOS 包装与 Docker/Compose release-smoke CI；#4 在独立分支报告 Python 837 例（1 skipped）及前端 15 例，未经合入 #3 后的组合验证。
- 两项分支测试成功 **不代表** 将 48 个重叠文件直接合并不会回归。不要因为 GitHub 显示可合并就忽略 API/执行语义的差异。

## 去重结论

| 模块/能力 | 当前 main（来源 #3） | #4 的差异 | 决策 |
| --- | --- | --- | --- |
| 对话与任务 | 持久任务、审批/取消/恢复、工具执行时限 | 独立的 `core/jobs.py` 与另一个运行时 | 保留 #3；禁止并存两套任务状态机 |
| 插件与配置 | `PluginManager`、配置快照、可管理能力 | `plugins/builtin.py`、独立热更新及管理 API | 不整套替换；后续如需某项行为再移植到已有契约 |
| 搜索与 provider 网络 | `plugins/provider_http.py` 已做 URL 校验、阻止凭据重定向 | `core/network.py` + `plugins/http_provider.py` | 保留已落地的主实现，避免第二套 HTTP 客户端契约 |
| 图片/视频/语音 | `capability_setup.py` + ArtifactStore + 异步视频任务 + realtime session bridge | `core/artifacts.py`、`core/jobs.py`、`plugins/pcm_voice.py` | 不复制重叠后端；独有原生 PCM/WebSocket 浏览器传输另列后续任务 |
| Shell | Linux bubblewrap 隔离、输出与资源上限 | `cloud/container_executor.py` 另一执行器 | 保留主 ShellExecutor，避免两个安全边界不一致 |
| 控制台 | 当前 10 页设计、会话/任务/媒体与 realtime | `api/console.py` + `web/Life.tsx` 另一套界面 | 不覆盖现有控制台；可独立提取有实质价值的交互 |
| 设备桥接 | `life/weq_send.py` 等渠道与 `realtime_bridge.py` | `plugins/whale_io.py`、`api/voice.py`、`pcm_voice.py` | **待独立评审与真实协议核对**；尤其是 whale-io PCM WebSocket 入口 |
| 单宿主目录互斥 | 未见等价的进程级排他锁 | `core/lease.py` 的 POSIX advisory flock | **本整合 PR 吸收并实际接入** CLI、API、桌面入口及离线恢复 |

## 本整合 PR 的实际贡献

从 #4 选取可独立落地、不会改变 #3 的领域接口的唯一能力：**运行宿主排他锁**。将其接入 `whale run`、`whale api`、Linux/macOS 桌面宿主以及 `whale memory restore`。这避免两个独立服务同时使用同一数据目录、旧版任务被另一宿主意外重放、以及服务运行中恢复 SQLite 的显而易见风险。

实现特点：
- POSIX `fcntl.flock`、非阻塞申请，锁文件权限 `0600`。
- 在打开数据库/装配服务前获取，宿主退出和异常时释放。
- 在线 SQLite 备份不受影响；离线恢复必须取得排他锁。
- 不替代 SQLite 事务，不保护未使用 RuntimeLease 的第三方代码；不会跨机器锁定同一 NFS/网络存储。

## 待另行移植而不应无验证合入的 #4 特有功能

1. 原生 AudioWorklet/浏览器 WebSocket → whale-io PCM TCP 中继（`api/voice.py`, `plugins/pcm_voice.py`, `web/RealtimeCall.tsx`）。与 #3 现有 realtime session HTTP 流程的能力和延迟差异较大；需先核对目标 whale-io 的 wire protocol、认证票据和设备采样率，并明确与现有 realtime 的选择策略。
2. 外部原生设备桥 `plugins/whale_io.py`、`plugins/bridge.py` 的具体渠道消息映射。部分代码带有“接口尚需设备验证”的声明，不可未经实测当作真实发送能力。
3. 专项请求数预算与 `core/budget.py`。#3 已具备提供者诊断与使用计数，但这不等于严格的跨宿主请求预算。若实施需要明确日界线、预算默认值与用户配置、原子预留和失败计数语义，不能直接复用第二套 kv 计数器覆盖现有统计。
4. QQ/WeQ 的独立 host 与探索/主动生活控制台差异。应复用 #3 现有出站台账、全局暂停与 WorkflowsService，不建立第二套 sender 或队列。

**原则：** 将 #4 视为已审阅的替代方案与功能来源。不能直接整合两个完整后端；如确有需要，针对上述独有能力单独提 PR、测试和验收。#4 可以标记为被 #3 加本整合 PR 取代，但应保留其分支/提交供后续移植参考。

## 安全/验收约束

- 不修改生产部署或外部账号。
- 本次 POSIX 锁不扩大 Shell/插件的任何权限。
- 若没有官方 whale-io PCM wire-protocol 的现场证据，不宣称浏览器原生电话能力已可用。
- 将 #4 任何非独立文件整套替换 #3 的主实现之前，必须重新做 API 契约与安全审查。
