# 设备面（whale-mobile 接入，contracts/v2）

> 协议 canonical 事实源：**whale-mobile** 的 `contracts/v2/`（PROTOCOL.md +
> messages/*.schema.json + objects/*.schema.json + examples/*.json）；本仓
> `contracts/v2/` 是整体覆盖同步的镜像，不允许在本仓单独修改 schema。
> v1（`contracts/v1/`）已随 §18 破坏性变更整体下线并从本仓删除。
> 漂移校验：`python3 scripts/contract_sync_check.py --core . --mobile ../whale-mobile`。

## 组件

| 模块 | 职责 |
| --- | --- |
| `devices/registry.py` | `DeviceRegistry`：注册 / 6 位配对码（10min）/ 批准 / 吊销 / scope 上限；能力全集直读 `contracts/v2` §5.1 枚举；持久化走 MemoryStore kv（`device:{id}`），审计写 `audit:studio:*`（只元数据） |
| `devices/embedded_wss.py` | `EmbeddedWssGateway`：`/api/v2/devices/ws` 长连（websockets sync server，独立端口），auth.challenge→hello 验签、逐帧 schema 校验、心跳/离线、experience/action/notification 上下行、§7.2 五终态回执状态机；同时实现 `DeviceWssPort`（NotificationGateway 首选送达口） |
| `devices/gateway_v2.py` | `DeviceGatewayV2`：多 transport 编排器（embedded_wss/adb/ssh 选路 + 幂等） |
| `devices/transports/embedded_wss.py` | `EmbeddedWssTransport`：embedded 通道的 `DeviceTransport` 实现（注册进编排器） |
| `devices/schema.py` | `ContractValidator`：纯 stdlib 的 v2 schema 子集校验器（不引 jsonschema；遇到子集外关键字 fail closed） |
| `devices/perception.py` | `DevicePerceptionAdapter`：实现 `perception/adapters.py` 的 `PerceptionAdapter` 协议，内存队列中转 `experience.event` |
| `api/devices_api.py` | 管理端 REST 路由（控制台 Bearer 鉴权；REST 仍随全站 `/api/v1` 前缀，设备**协议**为 v2） |

可选依赖：`whale-core[devices]` = websockets + cryptography（registry 与
校验器本身纯 stdlib，缺依赖时管理端点回 503）。

## 端点清单

管理 REST（`Authorization: Bearer <console-token>`）：

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/v1/devices/pair` | body `{label, pubkey_hex, scopes?}` → `{code, device_id, expires_in}`；`device_id` = SHA256(公钥)前16字节 hex（§3，32-hex） |
| `POST /api/v1/devices/pair/approve` | body `{code}` → 设备转 `active`；过期回 410 |
| `GET /api/v1/devices` | 列表 + 在线状态 + `ws_port`/`ws_path`（视图隐去配对码；capabilities 为 v2 §5.1 全集） |
| `GET /api/v1/devices/{id}` | 单设备详情（含 session 状态） |
| `DELETE /api/v1/devices/{id}` / `POST .../revoke` | 吊销：状态即时生效 + `kick()` 在线连接（先回 `error.device_revoked` 再关闭，§4） |
| `POST /api/v1/devices/{id}/scopes` | body `{scopes: string[]}` → 收窄/重设注册 scope（active）；编排循环据此停止自主体 |
| `POST /api/v1/devices/{id}/query` | `{operation, filters?, max_items?, timeout?}` → 下发 `sensor.query`（§5.3 查询子集），deadline 内等 `sensor.result`；离线 409 `unavailable`，越权 403 `scope_denied`，超时 504 `expired` |
| `POST /api/v1/devices/{id}/action` | `{capability, arguments?, idempotency_key?, task_id?, run_id?, authorized_scope?, approval_ref?, timeout?}` → `action.request`；**重复幂等键回已记录状态（`duplicate=true`，绝不重放，§7.1）**，等待**终态**回执 |
| `POST /api/v1/devices/{id}/action/status` | `{idempotency_key}` → 只读对账；终态 `receipt` 保留 `started_at`/`completed_at`、`error_code`、`provider_ref` 与 `result` |
| `POST /api/v1/devices/{id}/confirm` | 设备确认挑战（§14 device.confirm.request/response；challenge_id 为 ULID，approved=true 时设备签名必填） |

等待中的 sensor.result、action.receipt 和 device.confirm.response 均绑定到发起请求的
已认证 device_id；其他设备即使提交相同 request_id / action_id / challenge_id，
也不能消费或完成该 pending 请求。超时清理只移除原请求对应的等待项。

动作首次回执、重复动作重放、Core 重启后的状态对账均保留结果摘要和不透明
输出引用；不应为补结果自动重执行动作。大输出经 `operation="device.output.read"`
（sensor.query 专属，§5.3）+ `filters.output_ref` 分页拉取。

设备 WSS：`ws://<host>:<gateway_port>/api/v2/devices/ws`（长连；TLS 由外层
反代/tailscale serve 终结，§1）。连接即下发 `auth.challenge`（64-hex +
expires_in 秒）；`device.hello` 验签（Ed25519，签名输入 = challenge hex 串的
ASCII 字节）通过前拒绝其他帧；未知 major 回 `error.protocol_version` 关闭；
schema 违例回 `error.invalid_message` 保持连接；未知类型静默忽略（§4）。

## 配对流程

1. 手机端生成/读取 Android Keystore 中的 Ed25519 密钥对，经
   `POST /devices/pair` 提交公钥 → 得 6 位配对码并上屏。
2. 主人在控制台 `POST /devices/pair/approve` 输入配对码 → 设备 `active`，
   凭据 = 公钥指纹 + scope 上限。
3. 设备连 WSS：`auth.challenge` → `device.hello`（签名 + capabilities +
   features + device + app_version/app_flavor）→ `device.hello_ack`
   （core 按设备 scope 上限收窄 `accepted_capabilities`）。
4. 吊销：`DELETE /devices/{id}`，即时生效——在线连接立即收到
   `error.device_revoked` 并关闭；新握手回 `device_revoked`；每帧还会复查
   registry 状态（绕过 kick 的吊销同样在下一帧断线）。

## scope 矩阵（D3）

常量落在 `life/conversation_context.py`（`SCOPE_SCREEN_*` / `SCOPE_PHONE_*` /
`SCOPE_DEVICE_*`，与 contracts/v2 §5.1 能力枚举逐字一致）；裁决在
`life/context_resolver.py`：

- `screen.*` **不在**任何基础 scope 集里（默认最小权限），只能经临时租约
  （`temporary_auth`，已纳入 `GRANTABLE_SCOPES`）显式授予；
- 租约注入时 `screen.*` 仅 **owner 私聊 / API owner** 生效；**群聊硬上限拒绝**；
- `derive_kind` 认识 `channel="mobile"` → `ConversationKind.PHONE`
  （基础 scope 仍最小，`chat.basic`）。

检索标记约定：上行事件经 `DevicePerceptionAdapter` 转入 perception 时 payload
透传事件自带 `source`（§9 枚举）、`device_id`、`privacy_class`、`occurred_at`；
**不做默认全局召回**（本层只入队，召回策略归 perception/reasoner）。

## 回执与幂等（§7/§16）

- 回执状态机五终态：`succeeded`/`failed`/`timeout`/`cancelled`/`unknown`；
  `received` 是唯一非终态（「已收到」≠「已执行」）。
- `action_id` = 每次请求实例；`idempotency_key` = 逻辑动作。重复键回已记录
  的真实状态，绝不重放；终态 first-wins，落库不可翻案。
- 非终态记录过 `expires_at` + 24h 宽限 → 落 `unknown` 标记；终态记录保留
  7 天后有界清理（`sweep_action_retention`）。
- 通知面（§13）：`notification.event` 下行首选本 WSS（`DeviceWssPort`），
  离线留队等 `notification.sync` 补传；`notification.receipt/action` 上行进
  `NotificationGateway` 送达台账；`notification.revoke` 支持撤回。

## GUI 自主体

v1 的 `devices/agent_loop.py`（device.agent.* 三能力编排循环）已随 v1 下线
删除；GUI 自主体由 `devices/gui_agent/` 承担（contracts/v2 能力名 +
§7.2 五终态 + §8 元素绑定/过期检测），经 `DeviceGatewayV2` 选路
（embedded_wss 优先于 adb/ssh，GUI 类能力）。

## 停止任务

- **端侧**：UI / 桥紧急停止（estop）保留；`estop_active` 错误码入 §15 枚举。
- **core 侧等价**：
  - `DELETE|POST /api/v1/devices/{id}/revoke` 吊销并踢线；
  - `POST /api/v1/devices/{id}/scopes` 收窄注册 scope。编排循环每步
    scope/状态复查，命中即中止且不重放待执行动作；
  - `device.stop` 能力（§5.1）可经 action.request 下发。
