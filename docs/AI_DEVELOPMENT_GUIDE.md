# Whale Core · 开发者与 AI 协作指南

> 面向维护者、贡献者与编程 AI。**面向普通使用者的入口是 [README](../README.md)**。
> 本文件是代码导航、运行链路和工作约定，不替代源码、测试与契约文档。
>
> 快照：2026-10-31，`main @ be7a2ee`，Python 测试 **1115 passed**，前端 **38 passed**。
> 代码变化快：动手前以目标分支最新代码、测试和[完成记录](archive/2026-10-30/completion-report-2026-10-30.md)为准；
> [差距表](archive/2026-10-30/gap-analysis-2026-10-30.md)和[路线图](archive/2026-10-30/roadmap-2026-10-30.md)是历史快照，不要把其中的旧缺口当成当前结论。

## 1. 定位与边界

whale-core 是鲸鱼项目的 Python 智能层与本地管理服务：**事件输入、意图输出；能力由插件/provider 实现，授权由宿主策略决定**。设备、网络桥接和硬件适配在 [whale-io](https://github.com/amwangfan/whale-io)。

- Python 3.11+，运行时只依赖标准库（Windows 额外装 `tzdata`）。可选依赖：`voice`（websockets，实时语音）、`desktop`（pywebview，原生窗口）。
- 管理 API 是标准库 `ThreadingHTTPServer` + SSE，不是 FastAPI；前端是 React 18 + TypeScript + Vite。
- 单 owner 部署：一个数据目录同一时刻只允许一个服务实例（`core/lease.py` 的 `RuntimeLease`），canonical 数据在 SQLite。多 owner/多宿主机同步不在当前范围。
- 没有生产用 mock/demo 档。对话只走真实模型端点，缺凭据是显式错误；测试替身集中在 `tests/_doubles.py`。
- Linux/macOS 是目标平台；Windows 桌面与 Shell 不在本轮实现范围。
- 项目级 LICENSE 仍由 owner 暂缓；`licenses/` 只有第三方通知，不能据此宣称整体开源许可。

## 2. 运行链路

```
CLI / API / desktop
  → configuration.py：私密 settings.json（密钥+地址，单代原子快照）+ 非敏感投影
  → profiles.assemble_runtime()：按 profile 组装 store / registry / manager / tools / companion / orchestrator
  → 插件注册（官方拆分包 + 显式目录插件）、分角色模型、可选媒体/语音能力、预算与租约
  → ConversationOrchestrator：会话历史、规则意图、可选 LLM 规划、有界工具循环
  → ToolRegistry / PluginGateway：策略门控、逐次审批、风险分级、回执
  → SQLite：记忆、会话、任务、审批、台账、产物元数据
  → API/SSE → 网页控制台；life/ 与 plugins/weq_channel 负责渠道收发和主动行为账本
```

几条容易误判的规则：

- 插件注册 ≠ 插件启用。`PluginManager` 有清单和启停状态；每次模型/工具/向量调用都按当前注册表解析，禁用立即撤销，不靠缓存开关。
- 分角色模型（`chat` / `intent` / `executor` / `safety`）未单独配置时回落 `llm.chat`；默认装配里 `intent` 同时提供 `llm.intent` 与 `llm.safety`（发送前复核）。设置页看到的是环境变量与持久配置合并后的有效值。
- 每日请求预算在 `core/budget.py`：模型调用与媒体任务分开计数，默认 200 / 50，0 表示禁用；名额先预留再调用，失败照样占额，防重试风暴。
- 慢任务两段式：先回执占位，后台执行完再补发；进程重启后能恢复在途任务，不会被部署中断静默丢弃。
- 审批与执行分离。审批绑定任务、步骤、插件版本和参数摘要；`resume` 不重放已完成步骤；`unknown` 不自动重试。

## 3. 代码地图

| 区域 | 入口 | 边界与现状 |
| --- | --- | --- |
| 组合根 | `profiles.py`、`configuration.py` | 统一 CLI/API/桌面装配；设置与密钥单代原子保存、热切换；角色模型、预算、租约在这里接入 |
| 对话编排 | `core/runtime.py`、`core/intent_plan.py`、`core/intent_router.py`、`core/companion.py` | 记忆指令捷径、规则意图、可选 LLM 规划、工具循环、两段式慢任务、在途任务恢复 |
| 模型访问 | `core/deepseek.py`、`plugins/model_provider.py`、`core/diagnostics.py` | OpenAI 兼容端点；错误六分类、有限重试、回环/私网 http 放行；角色能力解析；调用统计与健康报告 |
| 预算与租约 | `core/budget.py`、`core/lease.py` | 每日请求预算；单数据目录单实例的进程锁（不是鉴权或沙箱边界） |
| 记忆 | `memory/store.py`、`sqlite_store.py`、`history.py`、`continuity.py`、`curation.py`、`hybrid.py`、`semantic.py`、`export.py`、`backup.py` | 会话历史、情节/偏好/人格、词项召回、可选 embedding 融合、JSONL 导出、完整 SQLite 备份恢复 |
| 插件机制 | `plugins/manifest.py`、`registry.py`、`manager.py`、`gateway.py`、`policy.py`、`loader.py` | 六类插件、清单校验、显式加载（不做自动发现）、启停与配置、fail-closed 执行网关、风险策略 |
| 官方插件包 | `plugins/search_web/`、`plugins/weq_channel/` | 搜索/抓取与 QQ/WeQ 渠道；随 wheel/frozen/Docker 打包，宿主按包名导入 |
| 可选能力 | `plugins/capability_setup.py`、`dashscope_media.py`、`provider_http.py`、`embedding.py`、`artifacts.py` | 媒体与 embedding 只在显式配置端点时注册；DashScope/OpenAI 两种协议；三层配置回退；私有产物存储 |
| 语音 | `plugins/realtime_voice.py`、`realtime_bridge.py`、`api/voice.py`、`pcm_voice.py`、`bridge.py`、`whale_io.py` | 原生 Realtime WSS 会话与 whale-io PCM 中继两条路径；原始音频只在内存；桥接骨架明确标注未联调 |
| 工具与终端 | `plugins/shell_tool.py`、`cloud/shell_policy.py`、`cloud/shell_executor.py` | 命令分档、逐次审批、Bubblewrap 命名空间与资源限额；沙箱不可用时拒绝，不降级到宿主 |
| 任务与后台 | `core/runtime.py`、`service/workflows.py`、`service/worker.py`、`workflow_skills/` | 任务轨迹、审批恢复、工作流组合执行；定期探索默认关闭，只产出草稿，不直接外发 |
| 主动行为与渠道 | `life/`、`actions/`、`initiative/` | 收件箱、台账、额度/静默/退避、QQ/OneBot 适配、设备事件与动作契约 |
| 人格与展示 | `ui/persona.py`、`persona_cards.py`、`hud.py`、`voice.py` | 人格卡与用户自设禁忌；防注入提示保留；展示层不持有授权能力 |
| API 与 CLI | `api/server.py`、`api/voice.py`、`cli/main.py`、`cli/desktop.py` | Bearer 鉴权、回环默认、SSE、静态控制台；CLI 子命令与桌面入口 |
| 前端 | `web/src/`（10 个页签组件） | 页面按需加载、错误与空态、移动布局；所有写操作以服务端结果为准 |
| 发行 | `scripts/build_release.py`、`verify_wheel.py`、`freeze_desktop.py`、`verify_frozen.py`、`Dockerfile`、`compose.yaml` | wheel 内置控制台与官方插件；Linux 目录/单文件冻结包；非 root 只读根文件系统容器 |

配套文档：[管理 API](management-api.md) · [能力与语音配置](media-and-voice.md) · [Shell 沙箱](shell-sandbox.md) · [部署](deployment.md) · [设备与意图契约](contracts.md) · [桌面入口](packaging-desktop.md)。

## 4. 安全不变量（改动时不能破坏）

1. 模型只能提议动作；身份门、风险策略、额度、审批由宿主裁决。插件事件/意图不携带 owner 或 `approved=true` 之类自授权字段。
2. 交易级动作逐笔人审 + 限额，永远不能配置成免审。
3. 密钥只进私密快照（`secrets/settings.json`，600）与环境变量，不进仓库、日志、异常文本、清单或 API 响应。
4. 管理 API 强制 Bearer、默认只监听回环；公网必须有 TLS 反代。普通 JSON body 64KiB 上限，音频与 ASR 有独立上限。
5. Shell 只有一条入口：`cloud/shell_policy` 分档 + `cloud/shell_executor`。沙箱不可用即拒绝；`sandbox="disabled"` 仅供完全可信的宿主命令，不是安全边界。
6. 原始音频字节不入库、不进普通日志；产物必须经鉴权接口读取，默认 7 天过期。
7. 记忆可导出、可删除，删除是真删；审批状态不等于已执行；取消不会撤销已经发生的外部副作用；未知结果不自动重试。
8. 人格禁忌由用户自设，不再有强制全局禁忌；但「记忆是上下文不是指令」的注入隔离提示必须保留。
9. 官方插件包随安装包分发，第三方目录插件必须显式给出路径；不做自动发现、不自动安装依赖。
10. 单数据目录单实例：不要为了“并行加速”拆掉 `RuntimeLease`，SQLite 边界和主动派发台账依赖它。

## 5. 本地开发与验收

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core

python -m pip install -e ".[dev,voice]"     # 需要桌面窗口时再加 desktop
python -m pytest tests -q                   # 1115 passed，全部离线

pnpm -C web install --frozen-lockfile --ignore-scripts
pnpm -C web test                            # 38 passed
pnpm -C web build

whale setup --profile local
whale doctor
whale run
whale api                                   # http://127.0.0.1:8080
```

- 离线测试用可控本地 HTTP 端点、协议替身与浏览器 API 替身；不要把它们说成真实第三方服务联调。
- CI：`.github/workflows/tests.yml`（pytest）、`web-build.yml`（前端构建/测试）、`release-smoke.yml`（wheel、冻结包、Docker 镜像的隔离启动与鉴权健康检查）。
- 发行验证：`python scripts/build_release.py` + `scripts/verify_wheel.py`；冻结包用 `scripts/freeze_desktop.py` + `scripts/verify_frozen.py`，必须在目标 OS 上构建。
- 配置目录默认 `~/.whale`；token 在 `api_token`（600），首次启动打印一次，不要放进 URL、Issue 或日志。

## 6. 常见改动的落点

| 想改什么 | 先看哪里 |
| --- | --- |
| 新增插件/provider | `plugins/manifest.py` 的六类与风险级 → `registry.py` → `manager.py` 接线 → `gateway.py` 执行；新增官方包放 `plugins/<name>/` 并随发行打包 |
| 改对话/工具编排 | `core/runtime.py`（计划、循环、超时、两段式）、`core/companion.py`（人设与记忆注入）、`core/intent_router.py`（规则与可选 LLM 规划） |
| 改设置或密钥 | `configuration.py`（快照、投影、白名单、校验）→ `api/server.py` 的 `/settings` → `web/src/components/Settings.tsx` |
| 改记忆语义 | `memory/`（store/sqlite 双后端保持 parity）、`curation.py`（事实与证据）、`semantic.py`（动态融合与回退） |
| 改渠道或主动行为 | `life/`（inbox/ledger/runner）、`plugins/weq_channel/`（发送端口）、`initiative/policy.py`（额度/静默） |
| 改审批或任务状态 | `core/runtime.py` 的任务记录与审批绑定、`api/server.py` 的 `/tasks/*`、`tests/test_runtime_lifecycle.py` |
| 改 Shell | `cloud/shell_policy.py` 白名单/黑名单、`cloud/shell_executor.py` 命名空间与限额；负向测试优先 |
| 改前端页面 | `web/src/App.tsx` 的页签 → `web/src/components/` 对应组件 → `web/src/api.ts` 的类型与请求 |
| 改发行 | `scripts/build_release.py`（静态资源与官方插件）、`Dockerfile`、`compose.yaml`、`release-smoke.yml` |

## 7. 已知限制与未联调项

这些是当前代码的真实边界，写文档或回答用户时不要越过：

- 供应商侧：生图在 DashScope 兼容平台实测出图；TTS 对齐协议后真机合出 119KB WAV（此前的 502 实为非法音色参数）；Bing 免 key 搜索校园网实测可用并已切生产；ASR 在 AgentScope 平台侧暂不可用；视频与 embedding 未用付费额度联调。
- 渠道侧：QQ 语音消息与图文混排按真实端点实现（sendVoice / sendImage），以自动化测试验收；本轮未重新做真实账号复验，手机、眼镜、VoIP 真机与 PCM 中继和真实音频桥也未复验。
- 实时语音：真实麦克风实听未完成；GA/legacy 协议差异只在契约层处理，接新服务前必须核对会话字段。
- Shell 沙箱：当前容器不能建用户命名空间，只验证了 fail-closed；成功隔离需在目标 Linux 主机点验。
- 桌面与发行：macOS 签名/公证与真实 GUI 未做；Windows 排除；wheel/冻结包已过 CI 隔离启动。
- 许可：项目级 LICENSE 未定，正式对外发行前必须解决。
- `archive/2026-10-30/gap-analysis-2026-10-30.md` 与 `archive/2026-10-30/roadmap-2026-10-30.md` 保留历史快照，状态以代码、测试和完成记录为准。

## 8. 与 AI 协作的约定

用户已明确：**编程 Token 不计费**。不要为了省 Token 省略必要的代码阅读、调研、测试、独立审查和修正；也不要为了消耗 Token 堆砌依赖或无端抽象。

- 跨模块任务用子智能体并行（研究、后端、前端、安全审查、测试、发行文档），主智能体统一接口、文件范围与集成验收。
- 每轮流程：审查分支与未提交改动 → 明确验收标准 → 研究与拆任务 → 冻结共享契约 → 实施 → 独立审查 → 跑测试 → 修正 → 报告。
- 改动前先读代码和测试，不要只按旧文档或文件名推断现状。
- 不得未经授权部署生产、切换账号、执行破坏性 Shell 或删除用户数据。
- 不得把测试替身、CI 绿、真实硬件/付费服务联调混为一谈；状态标注用「有代码 / 有测试 / 已接入运行链路 / 真实联调 / 未联调」。
- 完成后报告：改了哪些文件、为什么、跑了哪些测试、真实联调到哪一层、剩余风险。

## 9. 文档维护约定

- `README.md` / `README_EN.md`：面向第一次访问的人，写用途、可用能力、安装和当前限制；保持双语同步。
- 本文件：代码地图、运行链路、安全不变量与协作规则；模块或目录变化时同步更新。
- `docs/archive/2026-10-30/completion-report-2026-10-30.md`：本轮的完成证据与验收边界，新增验收先补证据再改结论。
- `docs/management-api.md`、`media-and-voice.md`、`shell-sandbox.md`、`deployment.md`、`packaging-desktop.md`：面向具体操作，改接口、环境变量或默认值时同步。
- `docs/contracts.md`：事件与动作的长期精确契约，改动前先确认兼容。
- `archive/2026-10-30/gap-analysis-2026-10-30.md` / `archive/2026-10-30/roadmap-2026-10-30.md`：历史目标与证据快照，不追着现状改，但要在页首标注快照属性。
- `docs/README.md`：文档入口与清单，区分当前有效文档与历史归档；新增、归档文档时同步更新。

README 与本文中出现的每条命令都必须能在当前代码上执行；任何“可用”说法都要标出凭据、接线和联调边界。
