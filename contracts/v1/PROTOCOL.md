# Whale 端云协议 V1（控制通道）

> 状态：**草案冻结候选** · 版本：`1.0-dev` · 2026-10-31
> 本目录是 whale-mobile 与 whale-core 之间的**唯一协议事实源**。两侧各自实现校验器，不允许通过导入对方代码共享协议。

## 1. 传输

- **Control**：WSS 双向长连，`wss://<core-host>/api/v1/devices/ws`。手机端主动出站，不开公网入站端口。当前部署经 Tailscale 组网，TLS 由 core 宿主（tailscale serve / 反代）终结。
- **Media**：独立 TCP/UDP 实时音频通道（见 `server/audio-bridge`），信令经 Control 协商，媒体帧协议见 §9。**禁止在 Control 通道串送大音频/截图二进制**。
- **Local Sensor**：ScreenMemo MCP 仅回环（127.0.0.1:37621）或受控私网；不对公网开放。

## 2. 信封（所有消息）

```json
{
  "v": "1.0",
  "id": "01J…ULID",
  "type": "sensor.event",
  "ts": 1730000000000,
  "seq": 412,
  "dir": "uplink",
  "payload": { }
}
```

| 字段 | 说明 |
| --- | --- |
| `v` | 协议版本 `major.minor`。同 major 向后兼容新增可选字段；未知 major 拒绝并回 `error.protocol_version`。 |
| `id` | 消息 ID（ULID/UUID）。 |
| `seq` | 每方向各自单调递增序号，用于丢帧检测与乱序识别。 |
| `dir` | `uplink`（机→云）/ `downlink`（云→机）。 |
| `ts` | 发送端毫秒时间戳；**事件真实发生时间在 payload 内**（`occurred_at`），与发送时间分离。 |

约束：单帧 ≤ 256 KiB；每条请求类消息必须带 deadline/超时与幂等键；心跳 30s，90s 无心跳判定离线。

## 3. 认证与设备身份

- 设备身份 = **Ed25519 密钥对**，私钥存 Android Keystore（硬件背书可用时）。`device_id` = 公钥指纹（SHA-256 前 16 字节 hex），仅是标识，不构成认证。
- 配对：手机上屏显示配对码 → 主人在 core 可信管理端（Whale Studio/控制台）输入确认 → core 签发设备凭据（绑定公钥指纹 + scope 上限）。撤销即时生效。
- `device.hello` 必须携带对 `challenge`（服务端在 WS 握手响应头/首帧下发）的 Ed25519 签名；签名通过前连接仅接受 hello，其余一律拒绝。**签名输入 = challenge 十六进制字符串的 ASCII 字节**（非解码后的 32 字节）；签名值编码为 64 字节签名的小写 hex（128 字符）。
- 设备认证 ≠ 主人认证：任何敏感动作同时要求「可信设备 + 可信会话/主体 + 具体 scope + 审批」。
- 撤销语义：core 撤销设备后，**已在线连接须立即关闭**，新连接拒绝并回 `error.device_revoked`。
- 违例处理：帧未过 schema 校验 → 回 `error.invalid_message` 并保持连接；未知消息类型 → 静默忽略（前向兼容）；未知 major 版本 → 回 `error.protocol_version` 并关闭。
- 补充消息：`auth.challenge`（downlink，连接建立后首帧下发），payload 为 `{challenge: 32字节随机数的小写hex, expires_in: 秒}`，结构见 `messages/auth.challenge.schema.json`；设备须在有效期内以其为签名输入完成 `device.hello`。

## 4. 消息目录（V1）

| 消息 | 方向 | 关键 payload 字段 | 语义 |
| --- | --- | --- | --- |
| `device.hello` | up | protocol_version, device_id, capabilities[], app_version, signature, challenge | 见 §3 |
| `device.hello_ack` | down | session_id, server_time, heartbeat_interval, accepted_capabilities[] | core 可收窄能力 |
| `device.heartbeat` | up | device_id, observed_at, status(online/connected_only/paused), battery?, modules{} | 区分在线/仅连接/功能暂停 |
| `sensor.event` | up | event_id, kind, occurred_at, privacy_class, summary, evidence_ref?, schema_version | 默认不含截图/完整 OCR；幂等键=event_id |
| `sensor.ack` | down | event_ids[] | 事件确认（含去重命中），端侧据此停发；批量 ≤200 |
| `sensor.query` | down | request_id, operation, filters, max_items, deadline | core 权限检查后下发；手机再按本地授权判断 |
| `sensor.result` | up | request_id, status(ok/denied/unavailable/error), data, truncated, source | 仅返回限定字段；不泄露本地路径 |
| `action.request` | down | action_id, capability, args, authorized_scope, approval_ref?, expires_at, idempotency_key | 端侧校验白名单+有效期+scope |
| `action.receipt` | up | action_id, state(received/accepted/executed/failed/unknown), executed_at?, error_code? | 「接受」≠「已执行」，禁止谎报 |
| `call.invite` / `call.answer` / `call.hangup` / `call.state` | both | session_id, media_params, expires_at | 挂断即撤销会话临时 scope |
| `error` | both | ref_id, code, message（脱敏） | 协议级错误 |

错误码（V1）：`protocol_version` `auth_failed` `device_revoked` `scope_denied` `capability_unknown` `expired` `duplicate` `rate_limited` `unavailable` `permission_missing`（端侧 Android 运行时权限缺失）`invalid_message` `internal`。完整定义见 `messages/error.schema.json`。

## 5. 数据等级与能力命名

- 隐私级：`public` / `owner_private` / `owner_sensitive` / `device_secret`（永不进感知事件）。
- 能力命名：`screen.context.read` `screen.history.search` `screen.evidence.read` `screen.live.subscribe` `phone.notification.post` `phone.notification.read` `phone.call.ring` `phone.call.audio` `phone.device.safe_action`。
- 默认最小权限；群聊/非主人会话对 screen.* 一律硬上限拒绝。

## 6. 幂等与离线

- 上行事件持久化于端侧加密队列：至少一次送达 + core 按 event_id 去重；暂停期间不补传私人历史（队列标记 drop_on_pause）。
- 下行动作：端侧按 idempotency_key 去重，过期（expires_at）不补执行；撤销设备后队列清空。

## 7. 媒体通道（概要）

PCM s16le mono 16kHz，帧 `[u32 BE len][u8 type][payload]`（type0=PCM，type1=控制文本）。独立 BRIDGE_TOKEN（与设备凭据分离）。Realtime 模型链路优先，ASR→LLM→TTS 级联为可显示降级的后备。

## 8. Schema 与示例

`messages/*.schema.json` 为逐消息 JSON Schema（draft 2020-12）；`examples/*.json` 为黄金示例。`tests/` 内校验器对两侧实现做契约测试：示例必须通过 schema；未知 major 版本必须被拒。
