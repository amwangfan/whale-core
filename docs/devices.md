# 设备面（whale-mobile 接入，D1–D4 范围说明）

> 协议 canonical 事实源：**whale-mobile** 的 `contracts/v1/`（PROTOCOL.md +
> messages/*.schema.json + examples/*.json）；本仓 `contracts/v1/` 是整体覆盖
> 同步的镜像，不允许在本仓单独修改 schema。漂移校验：
> `python3 scripts/contract_sync_check.py --core . --mobile ../whale-mobile`，
> 或手动触发 CI workflow `contract-sync` 指定两侧候选 ref 联合验收（issue #46）。
> 参考实现：whale-mobile `server/dev_gateway.py`。

## 组件

| 模块 | 职责 |
| --- | --- |
| `devices/registry.py` | `DeviceRegistry`：注册 / 6 位配对码（10min）/ 批准 / 吊销 / scope 上限；持久化走 MemoryStore kv（`device:{id}`，对齐 `memory/spaces.py` 模式），审计写 `audit:studio:*`（只元数据，对齐 studio.py 约定） |
| `devices/gateway.py` | `DeviceGateway`：`/api/v1/devices/ws` 长连（websockets sync server，独立端口），挑战-应答验签、逐帧 schema 校验、心跳/离线、上下行 |
| `devices/schema.py` | `ContractValidator`：纯 stdlib 的 V1 schema 子集校验器（不引 jsonschema；遇到子集外关键字 fail closed） |
| `devices/perception.py` | `DevicePerceptionAdapter`：实现 `perception/adapters.py` 的 `PerceptionAdapter` 协议，内存队列中转 `sensor.event` |
| `api/devices_api.py` | 管理端 REST 路由（全部走控制台 Bearer 鉴权） |

可选依赖：`whale-core[devices]` = websockets + cryptography（pyproject 注明理由；
registry 与校验器本身纯 stdlib，缺依赖时管理端点回 503）。

## 端点清单

管理 REST（`Authorization: Bearer <console-token>`，与既有控制台一致）：

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/v1/devices/pair` | body `{label, pubkey_hex, scopes?}` → `{code, device_id, expires_in}`；`device_id` = SHA256(公钥)前16字节 hex（§3） |
| `POST /api/v1/devices/pair/approve` | body `{code}` → 设备转 `active`；过期回 410 |
| `GET /api/v1/devices` | 列表 + 在线状态 + `ws_port`/`ws_path`（视图隐去配对码） |
| `GET /api/v1/devices/{id}` | 单设备详情（含 session 状态） |
| `DELETE /api/v1/devices/{id}` / `POST /api/v1/devices/{id}/revoke` | 吊销：状态即时生效 + `kick()` 在线连接（先回 `error.device_revoked` 再关闭，§3） |
| `POST /api/v1/devices/{id}/query` | `{operation, filters?, max_items?, timeout?}` → 下发 `sensor.query`，deadline 内等 `sensor.result`；设备离线 409 `unavailable`，越权 403 `scope_denied`，超时 504 `expired` |
| `POST /api/v1/devices/{id}/action` | `{capability, args?, idempotency_key?, authorized_scope?, approval_ref?, timeout?}` → `action.request`；幂等键查重（重复 409 `duplicate`），等待**终态**回执 |

设备 WSS：`ws://<host>:<api_port+2>/api/v1/devices/ws`（长连，不套 voice.py 的
60s 票据/会话超时；TLS 由外层反代终结，§1）。连接即下发 `auth.challenge`；
`device.hello` 验签（Ed25519，签名输入 = challenge hex 串的 ASCII 字节）通过前
拒绝其他帧；未知 major 回 `error.protocol_version` 关闭；schema 违例回
`error.invalid_message` 保持连接；未知类型静默忽略。

## 配对流程

1. 手机端生成/读取 Android Keystore 中的 Ed25519 密钥对，经
   `POST /devices/pair` 提交公钥 → 得 6 位配对码并上屏。
2. 主人在控制台（Whale Studio/管理 API）`POST /devices/pair/approve` 输入配对码
   → 设备 `active`，凭据 = 公钥指纹 + scope 上限。
3. 设备连 WSS：`auth.challenge` → `device.hello`（签名）→ `device.hello_ack`
   （core 按设备 scope 上限收窄 `accepted_capabilities`）。
4. 吊销：`DELETE /devices/{id}`，即时生效——在线连接立即收到
   `error.device_revoked` 并关闭；新握手回 `device_revoked`；每帧还会复查
   registry 状态（绕过 kick 的吊销同样在下一帧断线）。

## scope 矩阵（D3）

常量落在 `life/conversation_context.py`（`SCOPE_SCREEN_*` / `SCOPE_PHONE_*`，
与 §5 能力枚举逐字一致）；裁决在 `life/context_resolver.py`：

- `screen.*` **不在**任何基础 scope 集里（默认最小权限），只能经临时租约
  （`temporary_auth`，已纳入 `GRANTABLE_SCOPES`）显式授予；
- 租约注入时 `screen.*` 仅 **owner 私聊 / API owner** 生效；**群聊硬上限拒绝**
  （INV-03 一致），非主人会话同样拒绝；
- `screen.evidence.read` 的「逐笔/短时租约二次授权」由租约机制本身承担
  （TTL + max_uses），证据本体永不上行进感知事件（`evidence_ref` 只是引用）；
- `derive_kind` 新增认识 `channel="mobile"` → `ConversationKind.PHONE`
  （基础 scope 仍最小，`chat.basic`）。

检索标记约定：上行事件经 `DevicePerceptionAdapter` 转入 perception 时 payload
固定带 `source="screenmemo"`、`device_id`、`privacy_class`、`occurred_at`；
**不做默认全局召回**（本层只入队，召回策略归 perception/reasoner）。

## 与 dev_gateway 的语义差异

| 点 | dev_gateway（参考实现） | whale-core（本实现） |
| --- | --- | --- |
| 运行模型 | asyncio（`websockets.serve`） | 线程式 `websockets.sync.server`（对齐 api/voice.py） |
| 持久化 | SQLite（`dev_gateway.db`） | MemoryStore kv（`device:*` / `device:event:*` / `device:action:*` / `device:receipts:*`），双后端一致 |
| 管理通道 | 回环 JSON-lines（8766） | 控制台 Bearer REST（`/api/v1/devices/*`），复用既有鉴权 |
| 审计 | 仅日志 | `audit:studio:*` 留痕（pair/approve/revoke/hello，不记正文） |
| 配对码 | 不查撞码 | 撞码重摇（避免批准错设备） |
| 过期判定 | `expires_at < now` | `expires_at <= now`（到期瞬间即失效） |
| schema 校验 | jsonschema 库 | 纯 stdlib 子集校验器（devices/schema.py） |
| 踢线 | 仅每帧复查 | kick 主动关 + 每帧复查双保险 |
| 上行事件 | 只落库 | 落库去重 + 默认 `DurableDevicePerceptionAdapter` 持久待处理队列（自定义 adapter 仍可选） |
| call.* 消息 | 无处理器，忽略 | 同样忽略（schema 先校验；信令落地属媒体通道后续） |

心跳/离线、challenge TTL、ack 幂等闭环、回执状态机、错误码集合与参考实现
**完全一致**（§2/§3/§4/§6）。

## 留给后续（D4+ / 阶段二）

- **D4**：`temporary_auth.verify()` 的 `proof_method="device"`（租约确认推送到
  已配对设备，主人在手机上点确认；本次未动 QQ 私聊限制）；
- **D5**：Studio 设备节点卡（`_graph` 增 `kind:"device"`，数据源 = registry）；
- **D6**：设备面指标汇总接口（当前审计已留痕，聚合视图未做）；
- 媒体通道（call.* 信令处理、audio-bridge 对接）与 `screen.live.subscribe`
  的推送流；
- 感知事件进入长期记忆/召回的策略：默认待处理事件现可重启后重放；
  消费者必须在处理成功后调用 `adapter.ack(device_id=…, event_id=…)`，
  否则 `poll()` 会继续返回以保证至少一次交付。后续需要接入实际消费端并制定保留／清理策略。
