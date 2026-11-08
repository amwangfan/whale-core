# Whale 端云协议 V2（控制通道）

> 状态：**开发中** · 版本：`2.0-dev` · 2026-11-07
> 本目录是 whale-mobile 与 whale-core 之间的**唯一协议事实源**（canonical source）。两侧各自实现校验器，不允许通过导入对方代码共享协议。V1 代码与契约已从当前运行树移除；历史演进差异见本文 §18。
> V2 相对 V1 的破坏性变更汇总见 §18。

## 1. 传输

- **Control**：WSS 双向长连，`wss://<core-host>/api/v2/devices/ws`。手机端主动出站，不开公网入站端口。TLS 由 core 宿主（tailscale serve / 反代）终结。
- **REST**：附件上传/下载、设备管理、任务管理走 HTTPS REST（§11、§14）。REST 鉴权与 V1 相同（控制台 Bearer / 设备签名，不在本文重复）。
- **Media**：独立 TCP/UDP 实时音频通道（见 `server/audio-bridge`），信令经 Control 协商。**禁止在 Control 通道串送大音频/截图/视频二进制**；大负载一律经 output_ref / evidence_ref / attachment_ref 引用，另走 REST 或查询拉取。
- **ADB / SSH**：是 Core 侧 Device Gateway 的 transport 实现，不走本 WSS 协议；但同一设备的能力命名、动作语义、回执状态机与本协议完全一致（§5、§7）。

约束：单帧 ≤ 256 KiB；每条请求类消息必须带超时（deadline/expires_at/timeout_ms 之一）；心跳 30s，90s 无心跳判定离线（标离线，不踢线；撤销才踢线）。

## 2. 信封（所有 WSS 消息）

```json
{
  "v": "2.0",
  "id": "01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "type": "experience.event",
  "ts": 1794052800000,
  "seq": 412,
  "dir": "uplink",
  "payload": { }
}
```

| 字段 | 说明 |
| --- | --- |
| `v` | 协议版本 `major.minor`，V2 消息必须匹配 `^2\.[0-9]+$`。同 major 向后兼容新增可选字段；未知 major 拒绝并回 `error.protocol_version` 后关闭。 |
| `id` | 消息 ID，26 字符 ULID（Crockford Base32，不含 I/L/O/U）。 |
| `seq` | 每方向各自单调递增序号，用于丢帧检测与乱序识别。 |
| `dir` | `uplink`（机→云）/ `downlink`（云→机）。 |
| `ts` | 发送端毫秒时间戳（Unix ms）。**事件真实发生时间在 payload 内**（`occurred_at`），与发送时间分离。 |

时间戳一律 Unix 毫秒整数，范围 0..4102444800000（2100-01-01 UTC）。**全协议仅两个例外使用秒**：`auth.challenge.expires_in`（相对秒）与 `device.hello_ack.heartbeat_interval`（秒）。

## 3. 统一 ID 规则

V2 所有实体 ID 为「前缀 + 26 字符 ULID」，前缀指明实体类型；ULID 字符集 `[0-9A-HJKMNP-TV-Z]`。正则中的 `{26}` 即 ULID 本体。

| 实体 | 格式 | 示例 | 生成方 |
| --- | --- | --- | --- |
| `device_id` | Ed25519 公钥指纹 SHA-256 前 16 字节小写 hex，`^[0-9a-f]{32}$` | `ba4bb14c75917cfc0e69fb7a1caf8ba3` | 设备 |
| 消息 `id` | 26 字符 ULID | `01J9XK3EM8TQ2ZP0VN6W4RA7YC` | 发送方 |
| `session_id` | 26 字符 ULID | 同上 | core |
| `task_id` | `task_` + ULID | `task_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | core |
| `run_id` | `run_` + ULID | `run_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | core |
| `action_id` | `act_` + ULID | `act_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | core |
| `event_id` | `evt_` + ULID | `evt_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | 设备 |
| `observation_id` | `obs_` + ULID | `obs_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | 观察产生方（core transport 或设备） |
| `approval_id` | `aprv_` + ULID | `aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | core |
| `attachment_id` | `att_` + ULID | `att_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | core |
| `request_id` | 26 字符 ULID | 同消息 id | core |
| `challenge_id` | 26 字符 ULID | 同消息 id | core |
| `notification_id` | `notif_` + ULID | `notif_01J9XK3EM8TQ2ZP0VN6W4RA7YC` | core |
| `idempotency_key` | `^[0-9A-Za-z:_-]{8,128}$` | `notify:leaf5:evt_01J…` | core（下行）/设备（上行事件即 event_id） |

**device_id 全协议一律严格 32-hex**（修复 V1 中 confirm 消息退化为任意字符串的不一致）。

## 4. 认证与设备身份

- 设备身份 = **Ed25519 密钥对**，私钥存 Android Keystore（硬件背书可用时）。`device_id` 仅是标识，不构成认证。
- 配对：设备上屏显示配对码 → 主人在 core 可信管理端输入确认 → core 签发设备凭据（绑定公钥指纹 + scope 上限）。撤销即时生效：已在线连接立即关闭，新连接拒绝并回 `error.device_revoked`。
- 连接建立后 core 首帧下发 `auth.challenge`，payload `{challenge: 32字节随机数小写hex（^[0-9a-f]{64}$）, expires_in: 相对秒}`。设备须在有效期内以 **challenge 十六进制字符串的 UTF-8 字节**（ASCII 等价）为输入做 Ed25519 签名，签名值编码为 64 字节签名的小写 hex（128 字符），随 `device.hello` 提交；签名通过前连接仅接受 hello，其余一律拒绝。hello 的 `challenge` 字段回填该 64-hex 串。
- 设备认证 ≠ 主人认证：任何敏感动作同时要求「可信设备 + 可信会话/主体 + 具体 scope + 审批」。
- 违例处理：帧未过 schema 校验 → 回 `error.invalid_message` 并保持连接；未知消息类型 → 静默忽略（前向兼容）；未知 major 版本 → 回 `error.protocol_version` 并关闭。

## 5. 设备描述与能力

### 5.1 能力命名空间（全协议唯一事实源）

V2 只维护**一份**能力枚举，hello / hello_ack / action.request / sensor.query / device.confirm.request 均引用它；各消息可用子集见 §5.3 表。

```
screen.context.read        screen.history.search      screen.evidence.read
screen.live.subscribe
phone.notification.post    phone.notification.read
phone.call.ring            phone.call.audio           phone.device.safe_action
glasses.card.show          glasses.card.update        glasses.card.dismiss
device.observe             device.tap                 device.long_press
device.swipe               device.input_text          device.key
device.wait                device.app.launch          device.app.list
device.app.install         device.app.uninstall
device.shell               device.file.read           device.file.write
device.file.list           device.logcat              device.stop
device.output.read         device.screen.record       device.status
```

### 5.2 DeviceDescriptor / DeviceCapabilities（objects/device-descriptor）

设备在 `device.hello` 中声明完整描述：

| 字段 | 说明 |
| --- | --- |
| `device_id` | §3 |
| `capabilities` | §5.1 枚举子集（1..64，unique） |
| `features` | 布尔特征集：`root_shell`、`accessibility`、`media_projection`、`telecom`、`ocr_mlkit`。feature=false 的能力即使声明也会被 core 忽略 |
| `device` | `{model, android_release, api_level}` |
| `app_version` / `app_flavor` | 版本号与构建变体（`daily`/`root`） |

`CapabilityRegistry`（端侧）按构建变体注册能力：Root 版独有能力（device.shell、device.file.*、device.app.install/uninstall、device.logcat、device.screen.record 等）在日常版构建中**不注册、不声明**（不靠 UI 隐藏）。

### 5.3 各消息可用能力子集

| 消息 | 可用子集 |
| --- | --- |
| `device.hello` / `hello_ack` | 全部 |
| `action.request.capability` | 全部，**除** `device.output.read`（仅 sensor.query） |
| `sensor.query.operation` | `screen.context.read`、`screen.history.search`、`screen.evidence.read`、`screen.live.subscribe`、`device.output.read`、`device.app.list`、`device.status` |
| `device.confirm.request.scopes` | 全部 |

`device.status` 为 V2 新增只读查询（电量、网络、前台应用、Root 可用性），不产生副作用。

## 6. 消息目录（V2）

| 消息 | 方向 | 关键 payload 字段 | 语义 |
| --- | --- | --- | --- |
| `auth.challenge` | down | challenge, expires_in | 见 §4 |
| `device.hello` | up | protocol_version, device_id, capabilities[], features{}, device{}, app_version, app_flavor, signature, challenge | 见 §4/§5 |
| `device.hello_ack` | down | session_id, server_time, heartbeat_interval, accepted_capabilities[] | core 可收窄能力（可为空数组） |
| `device.heartbeat` | up | device_id, observed_at, status(online/connected_only/paused), battery?, network?, modules{} | 区分在线/仅连接/功能暂停 |
| `experience.event` | up | ExperienceEvent 对象（§9） | 上行经历事件；幂等键=event_id |
| `event.ack` | down | event_ids[]（每项须符合 event_id 格式） | 事件确认（含去重命中），端侧据此停发；批量 ≤200 |
| `sensor.query` | down | request_id, operation, filters, max_items, deadline | core 权限检查后下发；端侧再按本地授权判断 |
| `sensor.result` | up | request_id, status(ok/denied/unavailable/paused/error), data, truncated, source | 仅返回限定字段；不泄露本地路径 |
| `action.request` | down | action_id, task_id?, run_id?, capability, arguments, observation_id?, timeout_ms, expires_at, idempotency_key, approval_ref?, correlation_id?, authorized_scope[] | §7 |
| `action.receipt` | up | action_id, task_id?, status, result?, error_code?, provider_ref?, started_at?, completed_at? | §7 |
| `device.confirm.request` | down | challenge_id, device_id, principal_id, channel, scopes[], expires_at, conversation_id?, action_fingerprint? | 可信确认挑战（无私人内容） |
| `device.confirm.response` | up | challenge_id, approved, signature?, reason?, responded_at | approved=true 时 signature=Ed25519(challenge_id UTF-8 字节)，必填 |
| `run.trace.event` | up | RunTraceEvent 对象（§12） | Agent 执行轨迹步 |
| `recording.control` | down | run_id, op(start/pause/resume/stop), options? | MediaProjection 录制控制 |
| `recording.state` | up | run_id, state, file_ref?, duration_ms?, error_code? | 录制状态回报 |
| `call.invite` / `call.answer` / `call.hangup` / `call.state` | both | 同 V1（number pattern 修正为 `^\+?[0-9*#]{1,23}$`；answer.accepted、hangup.reason、state.state 枚举正式入文档） | 挂断即撤销会话临时 scope |
| `notification.event` | down | event_id, notification_id, device_id, category, title, body, priority, created_at, expires_at, target?, actions?, group_key?, collapse_key? | 原生通知事件（§13） |
| `notification.receipt` | up | event_id, notification_id, device_id, status, received_at | 送达/展示回执（§13） |
| `notification.action` | up | event_id, notification_id, device_id, action_id, submitted_at | 通知按钮操作（§13） |
| `notification.sync` | up | device_id, last_event_id?, synced_at | 重连后同步位置上报（§13） |
| `notification.revoke` | down | event_id, notification_id, device_id, reason? | 通知撤回（§13） |
| `error` | both | ref_id, code, message（脱敏） | 协议级错误 |

## 7. 动作请求与回执

### 7.1 action.request

```json
{
  "action_id": "act_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "task_id": "task_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "run_id": "run_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "capability": "device.tap",
  "arguments": {"element_id": 23},
  "observation_id": "obs_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "timeout_ms": 10000,
  "expires_at": 1794052860000,
  "idempotency_key": "agent:run_01J…:step8",
  "authorized_scope": ["device.tap"],
  "approval_ref": "aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "correlation_id": "evt_01J9XK3EM8TQ2ZP0VN6W4RA7YC"
}
```

- `task_id` / `run_id` 可选；GUI Agent 序列动作必须携带，便于审计与轨迹对齐。一次性动作可省略。
- `observation_id` 当 arguments 引用 `element_id` 时必填（§8 过期检测）；坐标类参数不需要。
- `action_id` 与 `idempotency_key` 关系（V1 未定义，V2 明确）：action_id 标识**每一次请求实例**；idempotency_key 标识**同一逻辑动作**。重发/重试同一逻辑动作必须复用同一幂等键；core 幂等表记录 key→action_id 映射，重复键回执已记录的 action_id 的真实状态，绝不重复执行。
- `timeout_ms` 为端侧执行超时上限（不含排队）；`expires_at` 为动作有效期（过期不补执行）。

### 7.2 action.receipt 状态机

| status | 终态？ | 语义 |
| --- | --- | --- |
| `received` | 否 | 端侧已收到并入队/在飞。**仅用于重复幂等键在飞时的重发**，不作为结果 |
| `succeeded` | 是 | 真实执行完成，result 携带结果摘要 |
| `failed` | 是 | 明确失败，error_code 必填 |
| `timeout` | 是 | 超过 timeout_ms 未终结。**超时不等于未执行**（副作用可能已发生），core 不得自动重放 |
| `cancelled` | 是 | 执行前/执行中被取消（device.stop、紧急停止、撤销） |
| `unknown` | 是 | 请求可能已执行但回执丢失/跨重启未终结。不得当作失败处理 |

「first terminal wins」：终态一旦落库不可翻案；在飞重复键重发 `received`，已终结重复键重发真实终态。

### 7.3 result 与 verification

```json
"result": {
  "summary": "已点击搜索按钮",
  "output_ref": "agent:out:act_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "verification": {"status": "passed", "summary": "搜索页已打开"}
}
```

- `summary` ≤2000 字符；大输出不落回执本体，写 `output_ref`，经 `device.output.read` 查询分页拉取。
- `verification.status` ∈ `passed` / `failed` / `skipped`。坐标类动作若端侧未做验证则标 `skipped`，禁止伪报 `passed`。
- `provider_ref`：诚实性注解（如 `corr:<event_id>`、`degraded:phone_notification`）。

### 7.4 device.output.read 字节分页

`sensor.query.filters` 使用 `output_ref`、`offset`（非负字节偏移，默认 0）、`max_bytes`（正整数，默认且每页最多 49152 字节）。端侧以文件随机读取返回单页，文本也按原始 UTF-8 字节编码，避免跨页切断字符时丢失数据。

`sensor.result.data[0]` 返回 `bytes_b64`、`offset`、`next_offset`、`total_bytes`、`truncated`；外层 `truncated` 与本页一致。`next_offset = offset + 解码字节数`，`truncated = next_offset < total_bytes`。到达或超过 EOF 返回空页，偏移收敛到文件长度。单页二进制 48 KiB，base64 后仍为 64 KiB，为 256 KiB 帧上限预留信封空间。

旧输出源的 `text` / `data_base64` 由运行时转换为相同分页格式；如果旧源本身已经截断，附加 `source_truncated=true`，Core 不得把拼接结果当成完整输出。读取期间文件长度变化、页不前进或超过 Core 总读取预算均应报错；接收端先拼接字节，再解码文本。本节规定查询数据对象，不新增消息类型或信封字段。

## 8. GUI 观察与元素绑定

- 一次观察产生 `observation_id`；观察结果中的语义元素携带**观察内局部**整数 `element_id`（不跨观察稳定）。
- 元素另带跨观察稳定 `fingerprint` = `sha256(className|text|resourceId|contentDescription|bounds|clickable|scrollable|editable)` 前 16 hex。
- 引用元素的动作（`device.tap` 等带 `element_id`）必须携带产出该元素的 `observation_id`；执行方（core agent loop 或端侧执行器）执行前必须在 fresh 观察中按稳定键（className+resourceId+contentDescription+bounds，**不含 text**）重校验**恰好一个**匹配，否则以 `failed/stale_observation` 终结，不沿用过期坐标。
- 观察负载（截图引用 + 元素表）本体经 `device.observe` 回执的 `output_ref` 拉取，不占用 256 KiB 帧预算。

## 9. Experience 事件

`experience.event` 的 payload 即 ExperienceEvent 对象（objects/experience-event）：

```json
{
  "event_id": "evt_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "kind": "screen.segment_ready",
  "occurred_at": 1794052800000,
  "device_id": "ba4bb14c75917cfc0e69fb7a1caf8ba3",
  "source": "screen_memo",
  "summary": "浏览了一个关于 AI 机器人的视频",
  "privacy_class": "owner_private",
  "evidence_ref": "capture:segment_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "task_id": null,
  "run_id": null,
  "schema_version": "2.0",
  "extras": {"package": "com.example.video"}
}
```

- `source` 枚举：`screen_memo` / `device_action` / `agent_experience` / `notification` / `user` / `system`。
- `kind` 点分小写（`^[a-z0-9_.]+$`），如 `screen.segment_ready`、`device.action.completed`、`agent.experience`。
- `task_id` / `run_id` 可空（schema 中为可选字段）；AI 操作与任务相关经历必须携带以便轨迹对齐。
- 幂等与 V1 相同：event_id 即幂等键，core 去重，端侧加密队列至少一次送达，暂停期不补传私人历史。
- 默认不含原始截图/完整 OCR；core 凭 `evidence_ref` 按需索取。

## 10. Evidence 引用

`evidence_ref` 为不透明引用（objects/evidence-ref），格式 `^[a-z][a-z0-9_]*:[A-Za-z0-9._/-]{1,128}$`，命名空间：

| 前缀 | 含义 | 读取途径 |
| --- | --- | --- |
| `capture:` | 端侧采集证据（截图/片段） | sensor.query `screen.evidence.read` |
| `agent:out:` | 动作/Shell 大输出 | sensor.query `device.output.read` |
| `agent:ui:` / `agent:shot:` | 最新 UI 树/截图快照 | sensor.query `device.output.read` |
| `run:` | Agent run 轨迹步骤 | core 侧 run 存储 |
| `recording:` | 录制文件/帧 | §12 |
| `att:` | 附件（§11） | REST |

引用绝不泄露设备本地绝对路径；端侧对引用做格式与类别白名单校验，拒绝路径形态、未知类别或畸形引用（沿用 V1 `agent:` 引用规则）。

## 11. 附件（AttachmentRef）

聊天与媒体结果使用稳定资源引用（objects/attachment-ref）：

```json
{
  "attachment_id": "att_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "type": "image",
  "mime": "image/webp",
  "size": 123456,
  "sha256": "9f86d0…(64 hex)",
  "width": 512,
  "height": 512,
  "duration_ms": null
}
```

- `type` ∈ `image` / `video` / `audio` / `file`。
- REST：`POST /api/v2/chat/attachments`（multipart）→ `201` AttachmentRef；`GET /api/v2/attachments/{attachment_id}` 下载；`DELETE` 同路径删除。消息体只携带 attachment_id，**不在 JSON 中反复传 Base64**。
- Core 生成的图片/视频同样落地为附件，以 attachment_id 引用，不返回临时 URL。

## 12. 执行轨迹与录制

### 12.1 RunTraceEvent（objects/run-trace-event + run.trace.event 消息）

```json
{
  "run_id": "run_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
  "seq": 8,
  "timestamp_ms": 1794052801000,
  "kind": "action_completed",
  "task_summary": "在视频应用中搜索指定内容",
  "action": {"type": "device.tap", "label": "搜索按钮", "target": "element:23"},
  "result": "succeeded",
  "verification": {"status": "passed", "summary": "搜索页面已打开"},
  "frame_ref": "recording:run_01J9XK3EM8TQ2ZP0VN6W4RA7YC/frame_000008"
}
```

- `kind` 枚举：`task_started` / `observation` / `action_started` / `action_completed` / `action_failed` / `verification` / `approval_requested` / `approval_resolved` / `task_completed` / `task_failed` / `note`。
- `seq` 自 1 单调递增；`timestamp_ms` 与录屏时间轴对齐的基准。
- 轨迹记录**可理解的操作说明**（打开设置/搜索应用），不记录模型思维链。

### 12.2 录制控制

- `recording.control`（down）：`{run_id, op: start|pause|resume|stop, options?: {profile?, max_duration_ms?, exclude_packages?}}`。
- `recording.state`（up）：`{run_id, state: idle|recording|paused|stopped|failed|uploaded, file_ref?, duration_ms?, error_code?}`。
- 两条路径：ADB+scrcpy（core 侧采集，第一阶段优先）；MediaProjection（Root 版内置，能力 `device.screen.record`）。默认不录制；录制期必须有可见状态与停止入口。

### 12.3 RecordingManifest（objects/recording-manifest）

`{run_id, device_id, recorder: scrcpy|media_projection, started_at, stopped_at, video_ref, trace_ref, resolution?, frame_rate?, profile, status: completed|partial|failed}`。录制与轨迹共用 run_id，保证视频与步骤可按时间戳对齐。

## 13. 通知子协议（原生通知）

> 依据《Whale 原生通知系统修正方案 2026-11-07》：**Whale 原生通知是产品能力**，
> ntfy/Bark/PushDeer 仅为可选外部出口，不承担默认链路。通知展示由 Mobile 原生层
> （Android NotificationManager）负责；远程送达首选 WSS（本协议），FCM/厂商 Push
> 为可选适配器。同一事件经多通道送达必须只展示一次（端侧按 event_id 去重）。

### 13.1 notification.event（downlink）

Core → Mobile 的通知事件。关键字段见 §6；`category` 枚举：

```
chat.message      task.started      task.progress     task.completed
task.failed       approval.requested（高优先级，带操作按钮）
device.offline    device.online     memory.updated    reminder.triggered
```

- `priority` ∈ `low` / `normal` / `high`；仅 `approval.requested` 等确需及时处理的用 `high`，不滥用。
- `expires_at`：过期不再弹出，可按业务保留在通知历史。聊天/审批/任务完成适用**不同 TTL**（core 侧策略，§13.4）。
- `target`：点击跳转目标（type ∈ task/conversation/device/approval/chat/none）。
- `actions`：操作按钮（如审批的 批准/拒绝），最多 4 个。
- `group_key` 分组聚合；`collapse_key` 同键新通知替换旧通知。

### 13.2 notification.receipt（uplink）

`status` 枚举：`received`（已收到）/ `displayed`（已提交 Android 通知系统）/
`opened` / `dismissed` / `action_submitted` / `expired` / `failed`。

**诚实性**：`displayed` 不等于用户已看到——Android 可能因权限、渠道关闭或系统
策略不展示；渠道被用户关闭时端侧应回 `failed`（permission_missing）而非伪报
displayed。

### 13.3 notification.action / sync / revoke

- `notification.action`（up）：通知按钮操作回传。**通知按钮不是本地授权旁路**——
  审批类动作的真正裁决在 core（approval_id 一次性核销，§14）。
- `notification.sync`（up）：重连后端侧上报 `last_event_id`；core 补发仍在有效
  期内的事件；端侧按 event_id 去重，已展示不重复弹出，已过期不再弹出。
- `notification.revoke`（down）：按 notification_id 撤回（更新/失效）。

### 13.4 Core 侧送达

Core 业务只调 `notification_service.notify(device_id, category, title, body, target=...)`；
Notification Gateway 按设备连接状态选 Provider（WSS 首选，FCM/OEM Push 可选适配器，
ntfy 等外部出口独立开关）。送达队列持久化，断线保留未过期事件。

## 14. 审批模型

两级审批，互不替代：

1. **设备确认**（device.confirm.request/response）：主人持机确认，Ed25519 签名核销挑战。每挑战独立 nonce，一次性。
2. **IM 审批**（task.approval，core ↔ IM Gateway 对象模型，不走设备 WSS）：`{approval_id, task_id, device_id, action, summary, expires_at, reply_options}`。一次性、有有效期、旧 approval_id 不得批准新动作；群聊可接收通知但不作为主人审批入口。

低风险动作（读取、打开应用、滚动、截图）不需要逐步审批；不可逆/对外产生影响的动作（发送、发布、删除、安装、支付）采用一次性审批，而不是永久禁用。

## 15. 错误码（全协议单一事实源）

`error.code` 与 `action.receipt.error_code` 使用同一枚举：

```
protocol_version   auth_failed        device_revoked     scope_denied
capability_unknown expired            duplicate          rate_limited
unavailable        permission_missing invalid_message    internal
background_restricted                 timeout            cancelled
stale_observation  element_not_found  approval_denied    estop_active
```

- `timeout` / `cancelled` 同时也是 receipt.status 终态；作为 error_code 仅用于 error 消息或 result 细化。
- `stale_observation` / `element_not_found`：§8 过期元素与元素丢失。
- `estop_active`：紧急停止激活期间的拒绝码。
- V1 三处口径分裂（12/13/14 个）在 V2 统一为此表；新增错误码必须先改本节与 error.schema.json。

## 16. 幂等与离线

- 上行事件：端侧加密队列持久化，至少一次送达 + core 按 event_id 去重；暂停期标记 drop_on_pause 不补传私人历史。
- 下行动作：端侧按 idempotency_key 持久去重（跨重启），过期不补执行；跨重启未终结一律回 `unknown`，绝不重放；设备撤销后队列清空。
- 无执行器且无 UI 委托时立即 `failed/unavailable`，不悬挂 `received`。

## 17. 隐私分级

`public` / `owner_private` / `owner_sensitive` / `device_secret`。`device_secret` 永不进入任何协议消息（刻意不在任何 schema 枚举内）；owner_sensitive 默认不上镜、不进 IM、不进群聊。群聊/非主人会话对 screen.* 一律硬上限拒绝。

## 18. V1 → V2 破坏性变更汇总

1. 信封 `v` 收紧为 `^2\.[0-9]+$`；WSS 路径 `/api/v2/devices/ws`。
2. 回执状态机：`received/accepted/executed/failed/unknown` → `received`（非终态）+ `succeeded/failed/timeout/cancelled/unknown`（终态）。**映射：executed→succeeded；accepted 取消（不再作为可谎报中间态）**。
3. `sensor.event` → `experience.event`（增 source/task_id/run_id/extras）；`sensor.ack` → `event.ack`，且 event_ids 项须符合 event_id 格式。
4. ID 全面改为「前缀+ULID」；device_id 各处严格 32-hex（含 confirm）。
5. 能力枚举单一名称空间，新增 device.* 动作族 17 项 + device.status；hello 增 features/device/app_flavor。
6. action.request：`args` → `arguments`，增 task_id/run_id/observation_id/timeout_ms；capability 语义扩展为 §5.1 动作名。
7. 错误码统一为 §15 单表；hello.challenge 回填 auth.challenge 的 64-hex 格式；call.invite.number pattern 修正。
8. 新增消息：run.trace.event、recording.control、recording.state；新增对象合同：device-descriptor、experience-event、evidence-ref、attachment-ref、run-trace-event、recording-manifest、approval-request。
9. action_id 与 idempotency_key 关系明确定义（§7.1）。

## 19. Schema 与示例

- `messages/*.schema.json` 为逐消息 JSON Schema（draft 2020-12）；`objects/*.schema.json` 为共享对象合同；`examples/*.json` 为黄金示例。
- **schema 子集约束**：core 侧校验器只实现 draft 2020-12 小子集，**禁止使用** `$ref`、`$defs`、`if/then/else`、`oneOf/anyOf/allOf`、`patternProperties`、相对 `$id`。条件必填（如 confirm.response.approved=true 时 signature 必填）写在文档与 description，由实现侧强制。
- 校验器要求（V2 加强，tests/contract_validate.py 实现）：
  - 每个消息 schema 必须有同名黄金示例（反向检查）；
  - 正例过逐消息 schema 后再过 envelope.schema.json；
  - 负例覆盖：版本不符、删必填字段、错误 dir、ts 越界、ID 格式错误（device_id 非 32-hex、event_id 无前缀等）；
  - 错误码枚举在 error.schema.json 与 action.receipt.schema.json 中逐字一致（校验器交叉比对）。
