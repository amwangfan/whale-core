# Whale-Core v1 架构

> 2026-11-23 重构。本文是 v1 的设计事实源：先讲为什么推倒重来，再讲新的系统形态、模块边界、数据流和协议。
> 旧实现整体移入 `legacy/`，仅作移植参考，不参与运行。

## 0. 对旧系统的判断

旧代码约 6.3 万行 Python、两套 React 控制台、23 万行测试。能力面很广，但问题不在代码风格，而在**没有主干**：

| 现象 | 根因 | 影响 |
| --- | --- | --- |
| `core/agent.py`、`cloud/loop.py`、`perception/`、`initiative/`、`actions/` 多为 stub，从未接线 | 按阶段（G1–G5、S0–S5、PR-xx、issue #xx）叠加，阶段结束不回收 | 新人与编程 Agent 无法判断哪条路径是真的 |
| 对话至少有四条入口：`ConversationOrchestrator`（6 个 mixin）、`life/qq_workflow.py`（1144 行）、`api/glasses.py` + `glasses_agent.py`（3200 行，自带 HTTP 服务）、`api/routes/mobile_chat.py` | 每接一个客户端就复制一遍"召回记忆 → 拼人格 → 调模型 → 投递" | 同一个 Agent 在不同客户端表现不一致；修一个 bug 要改四处 |
| 主动行为有 5 套循环：`initiative/`、`life/initiative.py`、`proactive_greeting.py`、`schedule_remind.py`、`service/worker.py` | 没有统一调度器 | 各自配额、各自静默时段、各自日志 |
| 审批有 4 套：runtime policy、IM approval_bridge、gui_agent approval、autonomy ApprovalBook | 没有统一能力/权限模型 | 用户看不到"一个待办审批"视图 |
| 投递有 3 套：`notifications/`、`integrations/im/delivery`、`life/result_delivery` | 客户端不是一等公民 | 结果"在哪、送到没有"难以回答 |
| 记忆有 12 个模块，底层是通用 KV blob；`owner_facts.py` 1300 行 | 每个场景单独设计记忆 | 没有统一检索、没有关系、无法可视化 |
| `http.server` 手写路由；多个版本前缀混用 | 协议跟着客户端走 | 客户端之间无法共享数据结构 |
| 可观测（trace、task_timeline、diagnostics）各自补丁式 | 没有统一事件流 | 用户看不到 Agent "为什么这么做" |

旧系统里值得保留的是**原则**而不是代码：真实回执（succeeded/failed/unknown 不混淆）、幂等键、设备 Ed25519 身份与 scope、观察内容不构成授权、有证据的记忆。设备线协议本身设计良好且已真机验证，v1 把它的信封思想推广为全系统协议。

## 1. 系统形态

Whale 是一个**常驻的智能体操作系统**。一切都围绕四个一等对象：

```
           ┌──────────── Link 协议（WS + HTTP，同一套消息） ────────────┐
 Console ──┤  Mobile ── Device(Android/ADB/SSH) ── IO 渠道(QQ…) ── CLI  │
           └───────────────────────────┬───────────────────────────────┘
                                       │ 客户端 = 会话入口 + 能力提供者
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Kernel：EventBus（带持久事件日志）· Store（SQLite）· Config · Scheduler      │
├──────────────┬──────────────┬───────────────┬───────────────┬───────────────┤
│ Agent        │ Memory       │ Capabilities  │ Tasks         │ Autonomy      │
│ 人格·上下文   │ 提取·整理    │ 工具/插件/设备 │ Goal·子 Agent │ 探索·主动消息  │
│ Run/Step 循环 │ 关联·检索·遗忘│ 统一策略与审批 │ 计划·调度      │ 日记·候选知识  │
└──────────────┴──────────────┴───────────────┴───────────────┴───────────────┘
                     Models：OpenAI 兼容 provider · 角色路由（chat/fast/vision/embed）
```

1. **Run**：Agent 的每一次行动（回复一条消息、执行一个目标、一次探索）都是一个 Run，由 Step 组成（think / recall / tool / reply / delegate / wait_approval）。Run 和 Step 实时以事件流出，并持久化。**可观测性不是附加功能，而是执行模型本身。**
2. **Capability**：模型能调用的一切——内置工具、插件动作、设备动作、渠道发送——都是 Capability：名字、描述、JSON Schema 参数、风险等级、提供者。设备上线时声明能力，即注册为 Capability；下线即注销。Agent 不区分"工具"和"设备"。
3. **Memory**：统一的记忆条目（fact / preference / episode / knowledge / diary / skill）+ 条目间的关系边 + 证据来源。所有场景共享一个存储与一个检索器。
4. **Client**：一切外部参与者都通过 Link 协议连接，在握手时声明 `role` 与 `capabilities`。控制台、手机、眼镜、QQ 桥都是 Client。

## 2. 模块与职责

```
src/whale/
  kernel/        ids · errors · events(EventBus+EventLog) · db(SQLite+迁移) · config · app(组装与生命周期)
  models/        ChatModel 协议 · openai_compat（流式 + tool calling）· scripted（测试/演示）· router
  agent/         persona · context(提示词预算) · runtime(Run 循环) · runs(Run/Step 存储)
  memory/        store(FTS5) · retrieval(混合召回) · pipeline(提取/整理/遗忘) · embeddings
  capabilities/  spec · registry · policy(风险→allow/ask/deny) · approvals · builtin/*
  plugins/       manifest · loader（插件 = 一组 Capability + 可选生命周期）
  tasks/         goals(目标与子 Agent) · scheduler(cron/interval/once 唯一调度器)
  autonomy/      explorer(兴趣→搜索→阅读→日记/候选知识) · proactive(主动消息策略)
  link/          envelope · rpc(方法注册，REST 与 WS 共用) · hub(连接、订阅、能力声明)
  devices/       registry(配对/Ed25519/scope) · v2(与 whale-mobile V2 线协议的边缘适配)
  channels/      Channel 抽象 · QQ 渠道（backend：onebot / weq，config 驱动）
  groups/        群聊记忆空间：本机归档 · 手动/定时更新 · 话题段与关键词 · 成员画像与喜好（带消息依据）
  setup.py       首次设置：模型预设与一步连接 · 局域网访问 · 进程内重启
  server/        Starlette 应用：/api/rpc、/api/link(WS)、/api/events(SSE)、静态控制台
web/             无构建步骤的控制台（ES Modules + Preact/htm，随 Python 包分发）
```

依赖方向严格单向：`kernel ← models ← memory ← capabilities ← agent ← tasks/autonomy ← link/devices/channels ← server`。下层不 import 上层；跨层通信只经过 EventBus 或注入的接口。

## 3. 关键数据流

### 3.1 一条消息

```
Client ──chat.send──▶ AgentRuntime.start_run(input)
  Run(created) → Step recall：memory.retrieve(query, scope) ──▶ 使用了哪些记忆（事件）
              → Step think：model.complete(context, tools=registry.visible(scope))
              → Step tool×N：policy.check → (ask → Approval → 等待) → capability.invoke → 结果
              → Step reply：流式 token → 客户端
  Run(completed) → memory.pipeline.observe(turn)（后台：提取事实/偏好/经历，关联，去重）
```

所有入口（控制台、手机、QQ、眼镜）调用同一个 `start_run`，区别只在 `Conversation` 的 `channel` 与回复投递目标。

### 3.2 一个目标（Goal 模式）

`tasks.create(goal)` → Goal 持久化 → 调度器启动 Run（mode=goal，带计划工具 `plan.update`、`task.spawn` 子 Agent、`task.complete`）→ 每步可观测 → 需审批时 Run 进入 `waiting`，审批后恢复 → 结束写结果与经验记忆。子 Agent 是子 Run，独立预算，父 Run 等待其完成。

### 3.3 自主探索

调度器按间隔触发 `autonomy.explore` → 从兴趣（记忆中的 preference/knowledge + 显式兴趣列表）选题 → Run(mode=explore，只开放只读能力) → 搜索/阅读 → 产出日记（diary）与候选知识（knowledge，status=candidate）→ 用户在控制台确认后转 accepted。主动消息由 `autonomy.proactive` 统一裁决（静默时段、日额度、冷却），通过 Channel/Client 投递。

## 4. 记忆模型

```
MemoryItem(id, kind, content, summary, subject, status, importance, confidence,
           strength, created_at, updated_at, last_access_at, access_count,
           source{run_id, conversation_id, quote}, tags[], embedding?)
MemoryLink(src, dst, relation, weight)   relation ∈ related | supersedes | derived_from | about
```

- **提取**：每轮结束后，后台用 fast 模型抽取自述的事实/偏好/经历，必须附原话 quote；无 quote 的推断不入库。
- **整理**：新条目与近邻（FTS + 向量）比较：重复→强化（strength↑）；冲突→新条目 `supersedes` 旧条目，旧条目 `status=superseded`；相关→建边。
- **检索**：FTS5 BM25 + 可选向量余弦 + 时间衰减 + 重要度 + 强度的加权融合；返回带 `why`（命中原因）的结果，供可观测。
- **遗忘**：强度按艾宾浩斯式指数衰减，访问即强化；低于阈值且不重要的 episode 归档；用户显式遗忘 → 墓碑（清空内容，保留 id 防复活）。
- **受众与隔离**：每条记忆有作用域（owner / shared / group:* / 其他人）。主人为中心：群聊记忆单向流入主人会话，其他人的私聊完全隔离，只有鲸鱼自我总结的性格与口癖（`style`）共享。手机分为鲸鱼专用机（不过滤）和主人日常机（系统隐私界面与排除应用不保存），经历按片段整理成记忆。详见 [MEMORY.md](MEMORY.md)。

## 5. 能力与权限

```
CapabilitySpec(name, title, description, params_schema, risk, provider, tags, timeout_s)
risk ∈ read | write | external | dangerous
Policy: (risk, provider, caller_scope, 用户规则) → allow | ask | deny
```

- 默认：read → allow；write → allow（Agent 自己的数据）；external（对外发消息、操作设备）→ ask，可设为 allow；dangerous（shell、安装应用）→ ask。用户可按能力覆盖。
- `ask` 生成 Approval（持久化、事件广播、任意客户端可批准/拒绝），Run 在 `wait_approval` Step 挂起。
- 外部动作结果三态：`ok | error | unknown`。unknown 不自动重试。

## 6. Link 协议（v1）

一个信封，所有客户端：

```json
{"v": "1.0", "id": "01J…", "type": "rpc.request", "ts": 1794052800000, "corr": null, "payload": {}}
```

| type | 方向 | 说明 |
| --- | --- | --- |
| `hello` / `welcome` | C→S / S→C | 客户端声明 `role`、`name`、`capabilities[]`、`subscribe[]`；服务端回 `client_id`、服务器能力、协议版本 |
| `rpc.request` / `rpc.response` | 双向 | `{method, params}` / `{ok, result | error}`；`corr` 关联请求。服务端也可以向客户端发 rpc（调用客户端声明的能力）|
| `event` | S→C | `{topic, data}`，按订阅推送：`run.*`、`memory.*`、`task.*`、`approval.*`、`device.*`、`agent.state` |
| `ping` / `pong` | 双向 | 心跳 |

- 同一组 RPC 方法通过 `POST /api/rpc/{method}` 暴露给 HTTP 客户端；`GET /api/schema` 输出全部方法与事件的 JSON Schema，客户端据此生成类型。
- 错误统一：`{code, message, retryable, details}`；`code` 为稳定枚举（`invalid_params`、`not_found`、`unauthorized`、`forbidden`、`conflict`、`unavailable`、`timeout`、`internal`）。
- 能力声明：客户端在 hello 中声明的能力自动注册为 `client.<name>.<capability>` Capability，Agent 可调用；调用走服务端→客户端 rpc。
- 设备：whale-mobile 设备线协议（`/api/devices/ws`，信封 `v` 1.x）由 `devices/gateway` 在边缘翻译成同一模型（DeviceNode + Capability + 回执），核心不感知线协议差异。

## 7. 控制台信息架构

不是 12 个平铺页面，而是"一个 Agent 的驾驶舱"：

- **总览（Bridge）**：Agent 当前状态（空闲/思考/执行/等待审批）、正在进行的 Run、能力/记忆/任务/设备四块平级磁贴；点击磁贴原地展开为焦点区，其余收为侧栏（动态聚焦）。
- **对话**：消息流 + 右侧"思维轨迹"抽屉：本轮召回的记忆、调用的能力、每一步耗时与理由。
- **记忆**：关系图（力导向）与列表双视图；按类型/状态筛选；条目详情显示证据与关系；候选知识审核。
- **任务**：目标列表 + 选中目标的流程视图（Run → Step 有向图，子 Agent 分支）；调度表。
- **能力**：按提供者分组的卡片（内置/插件/设备/客户端），风险与策略可就地调整。
- **设备**：可视化控制面板：在线状态、电量、能力开关、快捷动作、最近回执。
- **人格**、**设置**：分组卡片。
- 全局：审批托盘（任意页面可处理）、命令面板（Ctrl+K）、实时连接指示。
