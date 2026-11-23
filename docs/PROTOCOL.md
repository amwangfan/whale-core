# Whale Link 协议 v1

Core 与所有客户端（控制台、手机 App、眼镜桥、IO 渠道桥、脚本）之间的唯一协议。一个信封、一套方法、一套事件、一套错误。

## 1. 传输

| 通道 | 地址 | 用途 |
| --- | --- | --- |
| WebSocket | `ws(s)://<host>/api/link` | 长连接：RPC 双向调用 + 事件推送 + 能力声明 |
| HTTP | `POST /api/rpc/{method}` | 与 WS 相同的方法，适合脚本和无长连接的端 |
| SSE | `GET /api/events?topics=run.*,notify` | 只读事件流 |
| SSE | `POST /api/chat/stream` | 单轮对话流式输出（`run` / `step` / `delta` / `done` 事件；鉴权同 Link：Bearer 或 `X-Whale-Link-Ticket` 票头，票据来源默认 channel=mobile） |
| HTTP | `GET /api/schema` | 全部方法与参数 JSON Schema，客户端可据此生成类型 |
| HTTP | `GET /api/artifacts/{id}` · `POST /api/artifacts` · `DELETE /api/artifacts/{id}` | 产物/附件（需鉴权：Bearer 或 Link ticket；POST raw body + `X-Whale-Name`/`X-Whale-Content-Type` 头 → 201 含 sha256） |
| WebSocket | `/api/devices/ws` | whale-mobile 设备身体（设备线协议，Ed25519 设备身份；见 `contracts/device/`） |

鉴权：`Authorization: Bearer <token>`；WS 在 hello 的 `token` 字段或 `?token=` 携带。token 由 `whale token` 查看/轮换。
手机聊天面另可用 **Link ticket**（主 token 不上机）：设备线先验 → `POST /api/devices/link_ticket`
（§11 同构签名头，见 `contracts/device/`）→ `{ticket, expires_in:900}`；hello 带 `ticket` 字段
（固定 role=mobile、绑 device_id、撤销设备即拒绝+在线踢线）。artifacts 面（`GET/POST/DELETE
/api/artifacts`）同样接受 ticket（`X-Whale-Link-Ticket` 头或 `?ticket=`）。

约束：Link 单帧 ≤ 1 MiB（超限回 `error{code:"invalid_params", message:"frame too large"}`，连接保持）；
大负载一律走 `/api/artifacts/{id}` 引用拉取，不在控制通道串送。服务端无强制心跳：客户端自行
30s `ping` 保活，断线即离线（不重放事件；重连后用 `conversations.messages` / `events.query` 补齐）。

## 2. 信封

```json
{"v": "1.0", "id": "01JD…", "type": "rpc.request", "ts": 1795400000000, "corr": null, "payload": {}}
```

- `id`：发送方生成的 ULID；`corr`：响应/回执关联的请求 `id`。
- `type`：`hello` · `welcome` · `rpc.request` · `rpc.response` · `event` · `ping` · `pong` · `error`。
- 同一连接上的帧**严格有序**：处理某个请求期间产生的事件一定先于该请求的响应送达。

## 3. 握手

```json
→ {"type": "hello", "id": "h1", "payload": {
     "token": "…", "role": "mobile", "name": "Pixel 9",
     "subscribe": ["run.*", "notify", "approval.*"],
     "capabilities": [{"name": "phone.vibrate", "description": "震动手机", "risk": "external",
                       "params": {"type": "object", "properties": {"ms": {"type": "integer"}}}}]}}
← {"type": "welcome", "corr": "h1", "payload": {"client_id": "cli_…", "protocol": "1.0",
     "server": {"name": "鲸鱼", "version": "1.0.0"}, "methods": ["chat.send", …]}}
```

- `role`：`console` | `mobile` | `glasses` | `bridge` | `cli`（用作对话渠道名）。
- **role 方法列管**：`glasses` 角色只能调用白名单方法（`chat.send/cancel`、
  `conversations.list/messages`、`runs.get/list`、`link.subscribe/clients`、`approvals.list/resolve`、
  `system.status/schema`、`events.query`、`glasses.*`）；`mobile` 角色白名单为 `chat.send/cancel`、
  `conversations.*`、`runs.get/list`、`link.subscribe/clients`、`approvals.list/resolve`、
  `system.status/schema`、`events.query`、`personas.list`、`tasks.list/get`、`memory.search`；
  其余一律 `forbidden`。不在列管表内的角色不受此限（owner/client scope 检查独立于此、先生效）。
- 客户端声明的能力注册为 `client.<name>.<capability>`，Agent 可直接调用；调用时服务端向客户端发 `rpc.request {method: "capability.invoke", params: {name, args, run_id}}`，客户端回 `rpc.response`。断线即注销。
- 订阅可随时用 `link.subscribe {patterns}` 替换。

## 4. RPC

```json
→ {"type": "rpc.request", "id": "r1", "payload": {"method": "chat.send", "params": {"text": "你好"}}}
← {"type": "rpc.response", "corr": "r1", "payload": {"ok": true, "result": {…}}}
← {"type": "rpc.response", "corr": "r1", "payload": {"ok": false, "error": {"code": "not_found", "message": "…", "retryable": false}}}
```

方法按领域分组（完整参数见 `/api/schema`）：

| 领域 | 方法 |
| --- | --- |
| system | `system.status` · `system.schema` · `events.query` · `system.network` · `system.set_lan` · `system.restart` |
| setup | `setup.status` · `setup.quick_model` · `setup.complete` · `models.presets` |
| chat | `chat.send` · `chat.cancel` · `conversations.list/messages/update/delete` |
| runs | `runs.list` · `runs.get`（含全部 Step 与子 Run） |
| memory | `memory.list/get/search/remember/update/forget/graph/stats/maintain/evidence` |
| groups | `groups.list/get/enable/update/set_paused/set_opt_out/import/refresh/messages/context/delete` |
| capabilities | `capabilities.list` · `capabilities.invoke` · `policy.set` |
| approvals | `approvals.list` · `approvals.resolve` |
| tasks | `tasks.create/list/get/start/cancel/delete` · `schedule.list/set_enabled/run_now/delete` |
| personas | `personas.list/save/import/delete/activate` |
| config | `config.get/update/set_secret` · `models.status/test/upsert_provider/remove_provider/list_remote` |
| plugins | `plugins.list/set_enabled/configure` |
| autonomy | `autonomy.status` · `autonomy.explore` |
| devices | `devices.list/get/pair_start/pair_approve/revoke/rename/set_scopes/set_role/privacy/set_privacy/action/query/notify/events/actions/search_events/digest_now` |
| link | `link.subscribe` · `link.clients` |
| glasses | `glasses.context.sync`（日历窗口快照，≤12/日、title≤120、[today,today+7]、calendarHash 幂等）· `glasses.qq_reply`（口述发绑定 owner QQ，目标不从参数推导）· `glasses.deliver`（QQ 优先/通知兜底，图片经 artifact id，音频本期如实 unsupported）· `glasses.schedule.list/candidates/candidate_action/add_item/delete_item/source`（候选两阶段 + 溯源） |

`scope`：多数管理方法要求 owner 身份；`chat.send`、`runs.get`、`system.status` 等标记为 `client` 的方法所有已认证客户端可用。

`chat.send` 可选参：`wait=false` 立即返回 `run_id`（终稿经 `run.completed` 事件到达）；
`model_role:"fast"` + `remember:false` = 轻量档（fast 角色模型，不召回不回写记忆——眼镜 quick 语义）；
`conversation_id` 由客户端生成并持有（如 `glasses:<ULID>`），幂等复用。

## 5. 错误

| code | HTTP | 含义 |
| --- | --- | --- |
| `invalid_params` | 400 | 参数不合法（`details.errors` 给出字段位置） |
| `unauthorized` | 401 | token 缺失或错误 |
| `forbidden` | 403 | 权限或策略禁止 |
| `not_found` | 404 | 对象或方法不存在 |
| `conflict` | 409 | 状态冲突（如审批已处理） |
| `rate_limited` | 429 | 超出每日模型请求预算 |
| `internal` | 500 | 未预期错误 |
| `unavailable` | 503 | 依赖不可用（模型、设备、客户端离线），`retryable` 指示能否重试 |
| `timeout` | 504 | 超时；对外部动作意味着**结果未知**，不要自动重放 |

## 6. 事件

`event.payload = {topic, data, event_id}`。主题：

| 主题 | data |
| --- | --- |
| `agent.state` | `{phase: idle|thinking|acting|waiting, active: [{run_id, mode, status, conversation_id, title, channel}]}` |
| `run.started` · `run.completed` · `run.failed` · `run.cancelled` | `{run}` |
| `run.step` | `{step: {id, run_id, seq, kind, title, status, input, output, started_at, ended_at}}`（开始与结束各一次） |
| `run.delta` | `{run_id, conversation_id, delta}`（流式文本，不持久化） |
| `run.plan` · `run.progress` · `run.status` | 计划更新、工具进度、等待审批 |
| `memory.created/updated/reinforced/superseded/forgotten/extracted/maintained` | 记忆生命周期 |
| `approval.requested` · `approval.resolved` | 审批对象 |
| `task.created/updated/deleted` · `schedule.changed/fired` | 任务与调度 |
| `device.connected/disconnected/status/event/event_filtered/action/paired/revoked/updated/digest` | 设备（`event_filtered` 只含原因，不含内容） |
| `notify` | `{title, body, meta, delivered_to}`：Agent 主动给主人的消息 |
| `plugin.changed` · `config.changed` · `clients.changed` · `capability.registered/unregistered` | 系统状态 |
| `autonomy.exploring/explored/proactive` | 自主行为 |
| `group.updated/archived/refreshed/deleted` | 群聊记忆空间 |
| `setup.completed` | 首次设置完成 |
| `group.updated/archived/refreshed/deleted` | 群聊记忆空间 |
| `setup.completed` | 首次设置完成 |

非临时事件写入事件日志，`events.query {pattern, limit, before}` 可回放。

## 7. Step 类型（可观测性）

| kind | 含义 | output |
| --- | --- | --- |
| `recall` | 检索记忆 | `{memories: [{id, kind, content, score, why[]}]}` |
| `think` | 一次模型调用 | `{latency_ms, decision: reply|tools, tools[], reasoning}` |
| `tool` | 调用能力 | `{status: ok|error|unknown, data, summary, error, latency_ms}` |
| `approval` | 等待用户批准 | `{approval_id, approved}` |
| `reply` | 最终回复 | `{text}` |

## 8. 与 whale-mobile 的关系

- **设备身体**（感知、通知、动作、Root Shell）走设备线协议 `/api/devices/ws`（信封 `v` 1.x）：已在真机验证，具备 Ed25519 身份、序号、回执五终态与幂等键。Core 在边缘把设备能力翻译成 `device.<name>.<capability>`。
- **聊天界面**（Flutter）迁移到 Link v1：一条 `/api/link` 连接 + `chat.send` + 订阅 `run.*`/`notify`，即可获得与控制台完全一致的流式回复、思维轨迹和审批。
