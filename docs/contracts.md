# 与基础设施的边界契约

智能层（本仓）**只**通过意图与事件订阅与基础设施交互。基础设施侧保留全部设备 / 网络实现。

## 依赖方向

```text
Whale Core / Memory / Perception / Initiative / Actions / Whale UI
        │
        ▼  契约（事件 in / 意图 out）
Turbo-IO·dashboard · VoIP 来电音频桥 · 前台监测 · （固件暂停）· 未来控制桥 / 采集
```

禁止反向依赖：基础设施模块不得 import `whale_core` 业务决策（可共享纯 DTO 若另抽，当前未抽）。

原设想覆盖对照见 [vision-map.md](vision-map.md) 与 whale-io 主仓的 [docs/vision-coverage.md](https://github.com/amwangfan/whale-io/blob/main/docs/vision-coverage.md)。

## 入站：感知事件（基础设施 → 智能层）

| 事件 | 语义 | 典型来源（基础设施） | 智能层不做 |
|------|------|----------------------|------------|
| `phone.foreground` | 前台应用变化 | 前台监测仓 | ADB / Accessibility / 截图 |
| `schedule.tick` | 日程到期 | 本机提醒 / 日历推送 | 厂商日历 SDK |
| `location.hint` | 粗粒度地点标签 | 可选位置源 | 精确坐标入库 |
| `ambient.speech_segment` | 环境语音片段**元数据** | 未来采集 / ASR | 原始音频字节入库、麦克风 API |
| `context.course` | 课程表条目 | 日历 / 课表源 | 厂商日历写回 |
| `context.weather` | 天气粗标签 | 天气源 | 供应商 Key / 账号 |
| `context.location` | 粗位置标签（校园等） | 位置源 | **勿存精确坐标** |
| `nav.route_progress` | 导航进度（供提醒） | 导航集成 | 地图 SDK 嵌入 |

适配器协议：`PerceptionAdapter.poll() → Iterator[PerceptionEvent]`。

## 出站：行动意图（智能层 → 基础设施）

| 意图 kind | 语义 | 落地侧 | 智能层不做 |
|-----------|------|--------|------------|
| `show_card` | HUD 卡片 | Turbo-IO / dashboard HTTPS | 像素渲染、approve、蓝牙 |
| `place_call` | VoIP 来电会话 | VoIP 来电 App + 音频桥 | TCP PCM、厂商通话 API |
| `speak` | 语音播报 | TTS / 通话桥 | API Key、供应商账号 |
| `phone_action` | 打开应用 / 通知等 | **未来手机控制桥** | ADB / Accessibility |
| `navigate_hint` | 路线提醒卡片 | dashboard / HUD | 导航引擎 |
| `ambient_listen` | 启停环境听写（仅意图） | 未来采集侧 | 麦克风权限与录音 |

Gateway 协议：`ActionGateway.dispatch(ActionIntent) → str`。

- `HttpDashboardGateway` 默认 base：`https://rayneo.wangfanlab.com/turbo`（stub，不发 HTTP）  
- `LocalBridgeGateway` 默认：`127.0.0.1:18799`（stub，不建 TCP）  
- `PhoneControlGateway`：回执 `stub-phone-control:{action}:{target}`（不启 ADB / 子进程）  
- `AmbientListenGateway`：回执 `stub-ambient-listen:on|off`（不碰麦克风）  
- `CompositeActionGateway`：按 `intent.kind` 路由到上述子网关

## QQ 渠道（W09 · OneBot v11 外部适配器 + WeQ SSE 入站，默认关闭）

实现在 `life/qq_onebot.py`（OneBot/NapCat 传输）、`life/wq_push.py`（WeQ SSE 推送入站桥）与
`life/qq_workflow.py`（入站 AI 工作流），纯 stdlib；协议端为 NapCat / LLOneBot 等 OneBot v11
实现或 WeQ 桌面端（离线模式可用），地址与 token 只经本机配置注入（宿主机 `secrets/` 目录，
已 gitignore），本仓不存任何真实凭据。实况运行器：`examples/qq_live_workflow.py`。

| 组件 | 语义 | 智能层不做 |
|------|------|------------|
| `parse_onebot_message` / `OneBotInboundBridge` | 协议端消息事件 → `Incoming(channel="qq")` → registry 配对产出 `OwnerEvent` | 群聊默认拒收；未鉴权/未配对事件不进入业务 |
| `OneBotHttpListener` | 接收协议端「HTTP POST 事件推送」；无 token 拒绝启动，默认只绑 `127.0.0.1:18798` | 不记录消息正文，不回显 token |
| `WeQSsePushListener` | 接收 WeQ「SSE 消息推送」（`{events:[...]}`，至少一次投递）：Bearer 鉴权、msgId 有界去重、自发消息过滤、立即 ACK + 单工作线程串行消费 | 未配对来件只暴露元数据；过载丢弃计数可见不静默 |
| `QqInboundWorkflow` | `OwnerEvent` → 记忆指令捷径（记住/忘掉，不调模型）→ WhaleCompanion（人格 + 多轮历史 `chat:history:{会话}` + 记忆召回 + DeepSeek + 情节回写）→ 回复草稿进 `ProactiveOutbox` 裁决 | **不发送任何消息**；策略闸门未过不调用 AI；direct_message 逐条人工批准 |
| `OneBotOutboundPort` | `direct_message` 草稿 → 绑定对端的 `send_private_msg`（默认 `enabled=False`） | 不群发、不换目标；受理 ≠ 送达，失败记 unknown 不重试 |
| `WeQWebSendPort`（`life/weq_send.py`） | `direct_message` 草稿 → WeQ 内置 Web 服务 tRPC（`account.sendElements`）真实发送；登录 token 由用户从 WeQ 设置复制注入 | 不破解、不复用助手令牌；需 WeQ 完整模式；ok=true 仅代表服务端受理 |
| `qq_weq_plugin`（`life/qq_weq_plugin.py`） | QQ/WeQ channel 插件：`qq.message` 输入适配、`send_message` communicate 动作、宿主台账 verifier 与共享 Outbox | 真实无人值守发送经 `PluginGateway`；`--auto-approve` 仍是隔离 dry-run；固定目标与被动回复预算沿用既有端口/账本 |
| `NullOneBotTransport` / `HttpOneBotTransport` | 默认 stub 只记拟调用不触网 / 真实 HTTP API 客户端（注入本机配置） | 不读环境变量自动连接；token 不进日志与异常文本 |

发送审批仍由 `ProactiveOutbox` 策略层强制：`direct_message` 渠道逐条人工批准后才触达传输端口。
运行器 `--auto-approve` 是 **dry-run 演示档**：影子账本走审批流，不触网、不动真实账本、不占额度；
无人值守真实发送需 `--live-auto-send` + `QQ_LIVE_AUTOSEND=1` 双开关，issue #12。
该无人值守 QQ/WeQ 路径经 `PluginGateway` 的宿主台账 verifier，再复用同一 `ProactiveOutbox`；
单条人工 `approve` 命令仍是显式操作。被动回复与主动行为是两套预算
（`PASSIVE_DAILY_LIMIT`/静默豁免仅作用于被动回复，issue #13）；入站 owner 消息经 SQLite 持久化
inbox（幂等/租约恢复/受控重试，issue #14）。
真实发送已通 WeQ Web 通道验证（小号→主账号，result=0）；NapCat 通道与风控权衡见
`docs/research-weq-deepseek-qq.md` §6。

## Memory

| 类型 | 语义 |
|------|------|
| `MemoryKind.episode` | 情节记忆 |
| `MemoryKind.preference` | 用户偏好 |
| `MemoryKind.persona_continuity` | 人格连续性 |
| `MemoryRecord` | 统一记录事件形状 |
| `PersonaContinuity` | 人格快照读写 stub（经 MemoryStore） |
| `ContinuityContext` | 组装人格摘要 + 近期 episode/preference，供 decide |
| `memory.history`（`chat:history:{会话}`） | 多轮会话历史环形缓冲（12 轮上限），读路径宽容降级 |
| `memory.export` | 记忆 JSONL 导出/导入（只含记忆记录；kv 账本不导出；导入默认幂等跳过） |
| `core.memory_commands` | owner 显式指令「记住：/忘掉：」——确定性处理、证据链、只删 curated 记录 |

## 对话编排（Runtime · roadmap §3.1/§6）

| 类型 | 语义 |
|------|------|
| `IntentPlan` | 结构化意图计划（goal/kind/slots/missing/capabilities/澄清/超时）；**模型提议、核心裁决**：`risk_hint` 仅参考，未定义字段（如 `approved`）解析即丢弃，不产生任何授权效果 |
| `plan_from_model_text` | 从模型自由文本提取首个 JSON 对象并强校验（`IntentPlanError`） |
| `ConversationOrchestrator` | 对话流编排：记忆指令捷径 → （无意图模型）纯对话 → 意图分流（chat/clarify/task）→ 有界工具执行 → 结果回流陪伴模型；回执如实记录，「已计划」不伪装「已执行」 |
| `ToolSpec` / `ToolRegistry` | 模型可调用的具体操作（roadmap Tool 层）：显式注册、重名拒绝；非 read_only 工具必须映射插件动作并经 `PluginGateway` 派发（执行器不能自批） |
| `TurnResult` | 一轮结果：reply + status（answered/clarify/pending_approval/denied/failed）+ plan + receipts + task_id |


## 情境推理（Perception stub）

| 类型 | 语义 |
|------|------|
| `ContextReasoner` | 规则/模板：course/weather/location/**ambient** → `SituationBrief`（非 LLM） |
| `SituationBrief` | 结构化情境摘要（含 `ambient_note`），写入 Observation.context["situation"] |
| `extract_ambient_events` / `AmbientEventTags` | 环境语音元数据标签；**不**存音频字节 |

## Initiative

| 策略 | 语义 |
|------|------|
| `QuietHoursPolicy` | 静默时段抑制（保留） |
| `DisturbancePolicy` | 提醒 vs 不打扰：quiet + 前台忙碌；`urgent` 可放行 |
| `ProactiveCallPolicy` | 何时允许主动 `place_call`（静默拦；urgent / allow / elevated+suggest 放行） |
| `ProactiveCallPlanner` | 规则提议 `PlaceCall`，再经 `ProactiveCallPolicy` 过滤 |
| `PhoneActionPlanner` | 规则提议 `PhoneAction`（监测 ≠ 控制） |

## Session / UI

| 类型 | 语义 |
|------|------|
| `SessionState` | 多设备会话：`device_id` + `conversation_id` + `last_turn`；agent 可读 |
| `WhaleLook` | 形象 / 像素画 / 惊喜 HUD **设计意图**（非像素资源） |

## Whale UI vs 上镜管道

| Whale UI（本仓 `ui/`） | 上镜管道（Turbo-IO / dashboard） |
|------------------------|----------------------------------|
| 人格、禁忌、语气 | PhoneBridge / CardTransport |
| 语音风格偏好（无 Key） | 实际 TTS / 通话音频 |
| HUD 文案与 layout_hint | 真实卡片 approve 与眼镜显示 |
| WhaleLook 形象字段 | 选图 / 动画 / 像素渲染 |

## 版本与演进

1. 先稳定本文件与 Python 类型。  
2. 再在基础设施仓加「契约消费者」适配（可选独立 DTO 包）。  
3. 真连网关仅在本地配置注入，**永不**把 Token / 真实 IP 写入本仓。
