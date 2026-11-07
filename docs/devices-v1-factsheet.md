> **历史快照（V1，2026-11-07 前）。** 当前设备协议已切换为 V2，V1 目录和运行时已删除。此文只用于理解旧实现，不是当前代码导航；请以 [docs/devices.md](devices.md) 和 [devices-v2-design.md](devices-v2-design.md) 为准。

# Devices 域事实底稿（whale-core-dev v1）—— Device Gateway v2 + ADB/SSH Transport 参考

> 事实源：`src/whale_core/devices/*.py`、`src/whale_core/api/devices_api.py`、`tests/`。所有行号对应上述文件。

## 0. 总览

- 协议事实源：`contracts/v1/PROTOCOL.md` + `contracts/v1/messages/*.schema.json`（共 16 个 schema + envelope）。设备身份 = Ed25519 密钥对，`device_id = SHA-256(pubkey)[:16].hex()`（32 hex 字符）。
- 持久化：**不开独立 SQLite 表**，全部走 `MemoryStore` kv 抽象。生产后端 `SqliteMemoryStore`（`memory/sqlite_store.py`）：`kv(key TEXT PRIMARY KEY, value TEXT NOT NULL)`（WAL，JSON 字符串值）+ `records` 表（记忆记录，devices 不用）。测试用 `InMemoryStore`，双后端语义一致。
- WS 线程模型：`websockets.sync.server`，每连接一线程 + 后台 sweeper 线程。

## 1. 模块清单

### 1.1 `registry.py`（282 行）——设备注册/配对/审批

常量：
- `DEVICE_CAPABILITIES: tuple[str,...]`（16 项）：`screen.context.read`、`screen.history.search`、`screen.evidence.read`、`screen.live.subscribe`、`phone.notification.post`、`phone.notification.read`、`phone.call.ring`、`phone.call.audio`、`phone.device.safe_action`、`glasses.card.show`、`glasses.card.update`、`glasses.card.dismiss`、`device.agent.control`、`device.agent.observe`、`device.agent.shell`、`device.agent.output.read`（最后一项 query-only，不隐含 control/observe/shell）。
- `PAIR_CODE_TTL_S = 600`；`DEVICE_KEY_PREFIX = "device:"`；`AUDIT_PREFIX = "audit:studio:"`。
- `_PUBKEY_RE = ^[0-9a-fA-F]{64}$`；`_DEVICE_ID_RE = ^[0-9a-f]{32}$`。

函数：
- `now_ms() -> int`；`_now_iso() -> str`
- `device_id_from_pubkey_hex(pubkey_hex: str) -> str`（64 hex → 32 hex，非法抛 ValueError）

类：
- `DeviceRegistryError(ValueError)`：`.code` + message。
- `DeviceRegistry(store: MemoryStore, *, actor="owner:api-token")`：
  - `audit(action, *, outcome="ok", reason="", **meta)` → 写 `audit:studio:{ts}:{uuid8}`（只元数据，值被截断/字符串化）
  - `get(device_id) -> dict|None`；`list_devices() -> list[dict]`（只收 `device:<32hex>` 本体键，排除 `device:action:*` 等子域，按 created_at 排序）
  - `public_view(meta)`：隐去 `pairing_code`/`code_expires_at`
  - `request_pairing(*, label, pubkey_hex, scopes=None, code_ttl_s=600) -> {"code","device_id","expires_in"}`：label 1..64；pubkey 64 hex；active 重配对 → `conflict`；revoked → `conflict`（须先 purge）；6 位码全局唯一重摇 8 次；状态机落 `pending`
  - `approve(code) -> dict`：码须 6 位数字；找不到/非 pending → `bad_request`；过期 → `expired`；置 active、清码
  - `revoke(device_id) -> dict`：幂等；踢线由 API 层触发
  - `purge(device_id) -> bool`：仅 revoked 可删；删后须重新配对
  - `status_of(device_id) -> str`；`scopes_of(device_id) -> tuple[str,...]`
  - `set_scopes(device_id, scopes) -> dict`：仅 active；去重、校验白名单；core 侧"停止自主体"等价手段
  - `dumps_scopes(meta)` 静态助手

**设备记录字段**（kv 值 dict）：`device_id, pubkey_hex, label, scopes(list), status(pending|active|revoked), pairing_code, code_expires_at, created_at, updated_at`（毫秒）。

### 1.2 `schema.py`（166 行）——contracts 镜像加载校验

- 常量：`SUPPORTED_MAJOR=1`，`PROTOCOL_VERSION="1.0"`，环境变量 `WHALE_CONTRACTS_DIR`。
- `_KEYWORDS` 白名单（draft 2020-12 子集）：type/const/enum/pattern/minLength/maxLength/minimum/maximum/minItems/maxItems/uniqueItems/items/properties/required/additionalProperties + 元信息 $schema/$id/title/description。出现子集外关键字 → `SchemaUnsupported`（**fail closed**）。
- `contracts_dir() -> Path`：env 优先，否则从 `__file__` 向上找 `contracts/v1/messages`；找不到抛 FileNotFoundError。
- `parse_major(version) -> int|None`。
- `class _Validator`：`__init__` 校验关键字白名单；`validate(instance) -> str|None`（返回第一条违例，递归 `_check`）。
- `class ContractValidator(messages_dir=None)`：加载目录下全部 `*.schema.json`，按 `properties.type.const` 索引到 `self.validators: dict[str,_Validator]`；title=="envelope" 存 `self.envelope`。`validate(msg_type, message)`：未知 type 返回 None（调用方决定忽略）。

### 1.3 `gateway.py`（752 行）——WSS 长连网关

常量：`WS_PATH="/api/v1/devices/ws"`；`MAX_FRAME_BYTES=256KiB`；`REF_ID_FALLBACK="0"*16`；心跳 30s/超时 90s；challenge TTL 30s；`TERMINAL_RECEIPT_STATES={"executed","failed","unknown"}`；`ACTION_RETENTION_MS=7d`；`ACTION_PENDING_GRACE_MS=24h`。

类：
- `GatewayError(ValueError)`：`.code`（§4 错误码 + `unavailable` 管理面补充）。
- `ConnectionState(ws)`：全内存运行时态：`ws, device_id, authed, challenge, challenge_deadline, session_id, down_seq, expected_up_seq, last_heartbeat(monotonic), offline_marked, status`。
- `_Pending`：`threading.Event` + `result`，跨线程请求-应答对。
- `DeviceGateway(registry, store, *, host="127.0.0.1", port=0, adapter=None, heartbeat_interval=30, heartbeat_timeout=90, challenge_ttl=30, log_event=None)`：
  - 构造即 `serve()` 监听（websockets 缺失抛 ImportError）；默认 adapter = `DurableDevicePerceptionAdapter(store)`；`ping_interval=None`（心跳由协议层承担）；两个 daemon 线程：`serve_forever` + `_sweep_loop`。
  - 状态：`online: dict[device_id, ConnectionState]`；`pending: dict[request_id|action_id, _Pending]`；`pending_confirm: dict[challenge_id, _Pending]`（AUTH-01）；`stopping: Event`。

**连接流程**（`_handle`）：
1. 路径校验非 `WS_PATH` → close(1008)。
2. 连接即下行 `auth.challenge`（`secrets.token_hex(32)`，`expires_in=challenge_ttl`），`ws.recv(timeout=ttl+5)` 等首帧。
3. `_handle_hello`：JSON 解析 → major 版本检查 → type 必须 `device.hello` → schema 校验 → challenge 未过期且匹配 → registry 查设备（未知→`auth_failed`/not_paired；revoked→`device_revoked`；非 active→`auth_failed`）→ `_verify_signature`（Ed25519 verify，签名输入 = challenge hex 字符串的 ASCII 字节）→ 按注册 scopes 收窄 `accepted_capabilities` → 置 authed、`session_id=uuid4hex`、`expected_up_seq=msg.seq+1` → 同设备新连接顶替旧连接（close 1000）→ 发 `device.hello_ack{session_id, server_time, heartbeat_interval, accepted_capabilities}`。
4. 主循环逐帧 `_handle_frame`：JSON/major 校验（major 不符回 `error.protocol_version` 并关）→ **每帧复查 revoked**（回 `error.device_revoked` 并关）→ 未知 type 静默忽略 → schema 校验失败回 `invalid_message` 但不断线 → seq 跳变只记日志不拒绝 → 按 `_on_{type.replace('.','_')}` 分派。
5. 断开：从 `online` 摘除、`_emit("device:offline")`。

**上行处理器**：
- `_on_device_heartbeat`：device_id 须与连接身份一致；刷 `last_heartbeat`/`status`；曾被标离线则恢复并发 `device:online`。
- `_on_sensor_event`：幂等键 `device:event:{device}:{event_id}`；新事件落 kv（字段见下）→ `adapter.offer()` → `_emit("device:sensor_event")`；**重复也回 `sensor.ack`**（幂等闭环，端侧停发）。
- `_on_sensor_result`：按 `request_id` resolve `pending`。
- `_on_action_receipt`：先查反向索引 `device:action-id:{device}:{action_id}`（未知 action_id 直接丢、不写数据，防洪）；回执流水 `device:receipts:{device}:{action_id}` 追加 `{state,ts}` 保留最近 32 条；终态回执写回 `device:action:{device}:{idem_key}` 记录（**first terminal wins**，迟到冲突终态不改写；终态时附 `receipt{state,executed_at,error_code,provider_ref,result}`）；终态才 resolve `pending[action_id]` 并 `_emit("device:action_receipt")`。
- `_on_device_confirm_response`：按 `challenge_id` resolve `pending_confirm`，结果 `{approved, reason, signature}`。
- `_on_error`：记日志。

**下行 API（管理面线程调用）**：
- `build_message(msg_type, dir_, payload, seq) -> dict`：构造 `{v,id,ts,seq,dir,type,payload}` 并过出站 schema（失败即实现 bug，raise）。
- `send_msg(st, msg_type, payload)`；`send_error(st, ref_id, code, message)`（message 截 500）。
- `online_state(device_id)` / `is_online(device_id)`（排除 offline_marked）。
- `kick(device_id) -> bool`：吊销即时踢线（error.device_revoked + close）。
- `_check_scope(device_id, capability)`：非 active → `scope_denied`；能力不在注册 scopes → `scope_denied`。
- `send_query(device_id, *, operation, filters=None, max_items=10, timeout_s=15.0) -> dict`：operation 即能力名（先做 `_check_scope`）；max_items 1..100；`request_id=uuid4hex`；`deadline=now+timeout*1000`；等 `sensor.result`，超时 → `GatewayError("expired")`。
- `send_action(device_id, *, capability, args=None, idempotency_key=None, authorized_scope=None, approval_ref=None, correlation_id=None, timeout_s=15.0) -> dict`：
  - **幂等**：`idem_key` 缺省 `idem-{uuid}`，长度 8..128；`store.transact(reserve)` 原子占键 `device:action:{device}:{idem_key}`（字段：`device_id,idempotency_key,action_id,capability,created_at,expires_at,state="pending"`）+ 反向索引 `device:action-id:{device}:{action_id} -> idem_key`；已占 → `GatewayError("duplicate")`。
  - payload：`action_id, capability, args, authorized_scope(默认[capability]), expires_at, idempotency_key` + 可选 `approval_ref/correlation_id`；等终态回执，超时 → `expired`（**幂等键仍占着**，供 `action_status` 对账）。
- `action_status(device_id, idempotency_key) -> dict`：只读对账，不重发；非终态且过 expires_at → 视图态置 `unknown`。
- `sweep_action_retention(*, now=None, max_deletes=500) -> {"scanned","deleted","marked_unknown"}`：终态过 7d 删（动作记录+反向索引+回执流水）；非终态过 expires+24h 宽限 → 标 `unknown` 不删；有效重试窗口内不动；孤儿回执流水清；单趟删除封顶。搭 `_sweep_loop` 每小时一趟。
- `_sweep_loop`：心跳超时（默认 90s）标 `offline_marked` + `_emit("device:heartbeat_timeout")`（**不踢线**，心跳恢复自动回 online）。
- `close()`：置 stopping、关全部连接（1001）、server.shutdown、join 两线程。

### 1.4 `perception.py`（136 行）

- `MAX_QUEUED_EVENTS=1000`。
- `DevicePerceptionAdapter(*, max_queued)`：线程安全 deque；`offer(*, device_id, payload) -> PerceptionEvent`（固定 `source="screenmemo"`，payload 带 device_id/privacy_class/summary/evidence_ref/schema_version/occurred_at_ms；溢出丢最旧并计 `dropped`）；`poll() -> Iterator`（取走并清空）。
- `DurableDevicePerceptionAdapter(store, *, max_queued)`：at-least-once 持久 inbox。`offer` 只做转换不入队（网关已落 `device:event:*`）；`poll` 扫 `device:event:*` 排除已 ack（`device:processed:{device}:{event_id}`），按 received_at 排序，非破坏性；`ack(*, device_id, event_id)` 写 processed 键（不存在的事件 raise）。

### 1.5 `bridge.py`（334 行）——模拟闭环 + 生产端口适配

- `BridgeError(ValueError, .code)`。
- `default_projector(device_id, payload, *, subject_id="owner", privacy_space="") -> dict`：sensor.event → SituationStore 事实记录 spec（`source=f"device:{device_id}"`，summary 截 400）。
- `SimulatedActionPort(registry, *, online=None, receipts=None, now_fn=None)`：可重放脚本化动作端口。
  - `send_action(device_id, *, capability, args, idempotency_key, timeout_s) -> Mapping`：revoked→`DeviceUnavailable("device_revoked")`；非 active→`auth_failed`；scope 缺→`capability_unknown`；离线→`unavailable`；先 `_reserve_pending`（内存 dict `{(device,idem): rec}`），脚本化 receipt 支持 dict/Exception/callable；异常时保留 pending；终态回写 rec 供对账。
  - `action_status(device_id, idempotency_key)`：对齐 gateway 语义（过期待定置 unknown）。
- `GatewayActionPort(gateway)`：生产适配，`GatewayError → DeviceUnavailable(code)`；`send_action` / `action_status`。
- `SimulatedDeviceBridge(store, registry, *, situations=None, controller=None, outbox=None, prefs=None, approvals=None, action_port=None, subject_id="owner", privacy_space="", contracts=None)`：
  - `ingest_sensor_event(device_id, payload, *, ctx=None, projector=None, busy=False) -> dict`：鉴权（revoked→`device_revoked`；非 active→`auth_failed`）→ 套 `sensor.event` schema → kv 去重（重复只回 `{"ack":[id],"duplicate":True}`）→ 落库 → projector → `situations.record` → `controller.evaluate` → `controller.execute(...)`，返回 `{"ack","duplicate","situation_id"?,"decision"?,"execution"?}`。

### 1.6 `agent_loop.py`（570 行）——DeviceAgentLoop（core#58 安卓自主体）

常量：`CAP_OBSERVE/CONTROL/SHELL/OUTPUT_READ`；`DEFAULT_MAX_STEPS=8`；`DEFAULT_TIMEOUT_S=15.0`；`DEFAULT_TIMEOUT_RETRIES=1`；`_SUMMARY_MAX=2000`；`_OUTPUT_REF_RE`（`agent:ui:latest|agent:out:<1..128>|agent:shot:shot-N.jpg`）。
- `device_agent_space_id(device_id) -> "device-agent:{device_id}"`（设备私有记忆命名空间）。
- `@dataclass(frozen) AgentDecision{kind: observe|control|shell|stop, args, reason, done}`；`AgentStepRecord{step, capability, outcome, detail, summary, output_ref, idempotency_key}`；`AgentLoopResult{status: succeeded|stopped|failed, reason, steps, last_summary}`。
- `DecisionPort(Protocol).decide(*, goal, observation, history) -> AgentDecision`
- `OutputReadPort(Protocol).read_output(device_id, *, output_ref, timeout_s) -> Mapping`
- `ScriptedDecisionPort(decisions)`：队列吐决策，耗尽 → stop("script-exhausted")。
- `DeepSeekDecisionPort(companion, *, system="")`：调 `companion.reply(prompt, persona=system)` 期望 JSON `{kind,args,reason,done}`；`_parse_decision_json` 容错提取 ```json 块；解析失败 → stop("unparseable-decision")；模型异常 → stop("model-error:*")。
- `GatewayOutputReadPort(gateway)`：`output_ref` 过 `_OUTPUT_REF_RE` 后走 `gateway.send_query(operation="device.agent.output.read", filters={"output_ref":...}, max_items=1)`；`GatewayError→DeviceUnavailable`。
- `SimulatedOutputReadPort(payloads=None)`：脚本化/默认 `{"status":"ok","data":[{"text":f"sim:{ref}"}]}`。
- `DeviceAgentLoop(registry, device_port, *, decision, output_port=None, store=None, max_steps=8, timeout_s=15.0, timeout_retries=1, remember=True, stop_check=None)`：
  - `run(device_id, *, goal) -> AgentLoopResult`：**同步循环** observe→决策→control/shell，最多 max_steps。
  - `_preflight(device_id, *, required)`：revoked/非 active → `failed/unavailable`；scope 缺 → `failed/scope_denied`（每步复查 → 吊销/收窄 scope 即中止且不重放）。
  - `_dispatch_action(...)`：`idem = f"agent-{uuid4hex[:24]}"`；`device_port.send_action(...)`；`DeviceUnavailable` → `classify_exception`；其它异常 → `unknown`。executed + observe → 观察落 `{"summary","output_ref","raw_result"}`，有 output_ref 且持 `device.agent.output.read` scope 则 `_safe_pull_output`（截 1500，jpeg 只记长度）；`remember_episode` 写入 `device-agent:{device}` 空间。
  - 重试纪律：仅 `device.agent.observe` 的 `failed/timeout` 自动有界重试（新幂等键）；control/shell 超时先 `_reconcile_after_timeout`（用原幂等键查 `device_port.action_status`）：`not_found`→`retry_safe` 才允许新键重试；`executed`→记成功不重发；pending/received/accepted/unknown/无 status API → 升 `unknown` **禁止重放**；终态 failed → 归一编排码不重试。

### 1.7 `agent_failures.py`（108 行）

- 稳定编排码：`FAILED_TIMEOUT="failed/timeout"`、`FAILED_UNAVAILABLE="failed/unavailable"`、`FAILED_SCOPE_DENIED="failed/scope_denied"`、`UNKNOWN="unknown"`；`ORCH_FAILURE_CODES`；`SAFE_TIMEOUT_RETRY_CAPABILITIES={"device.agent.observe"}`。
- `classify_gateway_code(code) -> str`：`expired/timeout`→timeout；`scope_denied/capability_unknown`→scope_denied；`unavailable/device_revoked/auth_failed/permission_missing/background_restricted/rate_limited/internal`→unavailable；未识别保守 → unavailable。
- `classify_receipt(payload, *, expect_action_id="") -> (outcome, detail)`：先 `life.autonomy.validate_action_receipt`；`executed`/`unknown`/`deferred`（received/accepted 非终态）；failed 按 error_code 归一。
- `classify_exception(exc)`；`should_auto_retry(orch_code, *, capability)`；`should_abort_loop(orch_code)`（unavailable/scope_denied/unknown → 停）。

### 1.8 `event_consumer.py`（220 行）——事件→通知消费者（轮询式，宿主驱动）

- `DeviceEventConsumer(store, adapter, *, gateway=None, registry=None, outbox=None, prefs=None, now_fn=None)`：
  - `process_pending(*, max_events=32) -> dict[str,int]`（计数键：`polled/dropped_unauthorized/dropped_private/dropped_low_value/dropped_merged/notified/suppressed_by_policy/failed/unknown/acked`）。
  - 管道：`adapter.poll()` → 同 `(device,kind)` 10 分钟窗口合并只留最新（旧直接 ack）→ `_device_allowed`（registry active 且 `capabilities` 含 `phone.notification.post`；注意：读的是 `capabilities` 字段，而 registry 存的字段名是 `scopes` —— **潜在字段不一致点**）→ `privacy_class=="private"` 丢 → `_should_notify`（关键词正则：截止/deadline/会议/缴费/提醒…；忽略 `device.heartbeat/battery/state` 前缀）→ `_deliver`：
    - 第二道幂等：`device:notify-state:{device}:{event_id}` status==executed 直接返回 notified；
    - `outbox.propose(ProactiveDraft(draft_id=f"device-notify:{device}:{event}", text=f"注意到：{summary}", channel="phone_notice"))` → `outbox.dispatch(draft_id, prefs, port=_NullPort())` 策略裁决（静默/冷却/配额）→ ready/accepted 才 `gateway.send_action(capability="phone.notification.post", idempotency_key=f"notify:{device}:{event}")`；异常 → state 置 `unknown` 不重试；
    - 终态落 `device:notify-state:*`；到达任何终态即 `adapter.ack`。
  - `_sanitize_summary`：剥离控制字符、压空白、截 160（摘要按不可信数据处理）。
  - 缺省 prefs：`ProactivePrefs(enabled=True, allowed_channels={"phone_notice"})`。

### 1.9 `initiative_loop.py`（152 行）——EV-02 活接线（默认关）

- `CONFIG_KEY="config:device_initiative"`（kv 存 `{"enabled":bool,"rules":{"<situation.key>":{"contains","text","title"?,"reason"?}}}`）。
- `_owner_ctx() -> ResolvedContext`：合成 owner API 上下文（channel=api，scopes=(SCOPE_DEVICE_NOTIFY, SCOPE_PHONE_NOTIFICATION_POST)）。
- `DeviceInitiativeAdapter(DevicePerceptionAdapter)`：内包 `DurableDevicePerceptionAdapter`；`offer` 落库后若 `enabled` 则 `_drive`：projector → `SituationStore.record` → `AutonomyController.evaluate` → remind/action → `controller.execute(outbox=ProactiveOutbox(store), device_port=GatewayActionPort(gateway), approvals=ApprovalBook(store))`；闭环异常静默不影响落库。规则命中 → `LEVEL_ACT_LOW_RISK` + `KIND_ACTION`，幂等键 `init-{event_id}`。
- `device_id_of(situation)` / `split_device_id(*, source)`。

### 1.10 `glasses.py`（104 行）——眼镜 HUD 策略（纯函数层）

- `GLASSES_CAPABILITIES`（含 `glasses.feedback`）；上限 `MAX_TITLE_CHARS=40/MAX_BODY_CHARS=200/MAX_CARD_ID_CHARS=64/MAX_TTL_S=3600`；`ALLOWED_PRIORITIES={low,normal,high}`。
- `GlassesPolicyError(ValueError,.code)`；`validate_card(*, card_id, title, body, priority="normal", ttl_s=300) -> dict`（白名单+截断+控制字符剥离）。
- 回执分层：`DISPLAY_STATES=("requested","phone_handled","bridge_sent","shown","degraded","unknown")`；`_RECEIPT_MAP`（executed→bridge_sent，displayed→shown，failed→degraded）；`DisplayVerdict{state,evidence,simulated}`；`classify_receipt(receipt)`：**只有 receipt 显式 `display_proof=True` 且 status=executed 才报 shown**；无回执 → unknown。

### 1.11 `__init__.py`
导出 `DEVICE_CAPABILITIES, DevicePerceptionAdapter, DeviceRegistry, DeviceRegistryError, device_id_from_pubkey_hex`。

## 2. 存储布局（全部 kv，单表 `kv(key TEXT PRIMARY KEY, value TEXT)`，值为 JSON）

| 键模式 | 值 | 写入方 |
|---|---|---|
| `device:{device_id}` | 设备记录（§1.1 字段） | registry |
| `device:event:{device}:{event_id}` | `{device_id,event_id,kind,occurred_at,privacy_class,summary,evidence_ref,schema_version,received_at}` | gateway/bridge |
| `device:processed:{device}:{event_id}` | `{processed:True}` | DurableAdapter.ack |
| `device:action:{device}:{idem_key}` | `{device_id,idempotency_key,action_id,capability,created_at,expires_at,state,receipt?}` | gateway.send_action / receipt 回写 |
| `device:action-id:{device}:{action_id}` | `idem_key`（str，反向索引） | gateway.send_action |
| `device:receipts:{device}:{action_id}` | `[{state,ts}...]` 最多 32 条 | gateway._on_action_receipt |
| `device:notify-state:{device}:{event_id}` | `{status,reason?/receipt?}` | event_consumer |
| `audit:studio:{ts}:{uuid8}` | `{at,action,actor,outcome,reason,...}` | registry.audit |
| `config:device_initiative` | `{enabled,rules}` | initiative_loop |
| `device-agent:{device}` 记忆空间 | remember_episode 观察记录 | agent_loop（records 表） |

SQLite 后端：`SqliteMemoryStore`（WAL、文件 chmod 600、`transact(fn)` 原子读写）。**v2 若要独立 SQLite 表结构，当前没有任何现成 DDL 可继承，只有 kv 约定。**

## 3. WSS 端点消息处理流程（gateway）

连接：`auth.challenge`↓ → `device.hello`↑（验签 Ed25519、配对状态、challenge）→ `device.hello_ack`↓（session_id/心跳节奏/收窄能力）→ 长连循环。上行：`device.heartbeat`、`sensor.event`（→`sensor.ack`↓）、`sensor.result`、`action.receipt`、`device.confirm.response`、`error`。下行：`sensor.query`、`action.request`、`device.confirm.request`、`error`。心跳 90s 超时标离线（不踢线）；吊销即时 `kick()` + 每帧复查。单帧 ≤256KiB；未知 type 静默忽略；schema 违例回 error 不断线；major 版本不符断线。

## 4. REST API 清单（`api/devices_api.py`，前缀 `/api/v1`，全部控制台 Bearer）

| 方法/路径 | 参数（JSON body） | 说明 |
|---|---|---|
| GET `/devices/ws` | — | 400 提示 WS 在独立端口（`gateway.port`） |
| POST `/devices/pair` | `label, pubkey_hex, scopes?[str]` | → `{code, device_id, expires_in}` |
| POST `/devices/pair/approve` | `code` | → public_view |
| GET `/devices` | — | 列表+`capabilities`+`ws_path/ws_port`，每台附 `online` |
| GET `/devices/{id}` | — | public_view + `online` + `session{session_id,status}` |
| DELETE `/devices/{id}` 或 POST `/devices/{id}/revoke` | — | 吊销 + `gateway.kick`，返 `{status, kicked}` |
| POST `/devices/{id}/scopes` | `scopes:[str]` | 收窄 scope（停止自主体手段） |
| POST `/devices/{id}/purge` | — | 硬删除（须先 revoked）+ kick |
| POST `/devices/{id}/query` | `operation, filters?, max_items?, timeout?` | → `gateway.send_query` |
| POST `/devices/{id}/action` | `capability, args?, idempotency_key?, authorized_scope?, approval_ref?, correlation_id?, timeout?` | → `gateway.send_action` 等终态 |
| POST `/devices/{id}/action/status` | `idempotency_key` | 只读对账 |
| POST `/devices/{id}/confirm` | `scopes[], principal_id?, channel?, conversation_id?, ttl_s?, grant_ttl_s?, grant_max_uses?, action_fingerprint?` | AUTH-01：TemporaryAuthService 挑战 → `send_confirm_request` → 验签（Ed25519 over challenge_id UTF-8）→ 原子核销发限时租约 grant |

错误码映射：registry `bad_request→400, not_found→404, conflict→409, expired→410`；gateway `invalid_message→400, scope_denied→403, not_found→404, duplicate→409, unavailable→409, expired→504`。`device_id` 路由参数须匹配 `^[0-9a-f]{32}$`；timeout 钳制 1..60s。装配：`ensure_device_services(ctx, server)` 懒构建 registry/gateway/`DeviceInitiativeAdapter`；websockets 缺失 → 503。

另有独立 `api/glasses.py`（whale-glasses-api 独立进程，`/v1/chat`、`/v1/qq/reply` 等，token 鉴权），不在 `/api/v1/devices` 面内。

## 5. 测试覆盖（tests/）

- `test_devices_registry.py`：配对码形状/TTL、非法 pubkey、未知 scope、approve 生命周期、revoke 幂等、purge 前置 revoked。
- `test_devices_ws.py`：`DeviceSim`（Ed25519 真签名）+ 真实 HTTP API：Bearer 必填、pair/approve/list/revoke、握手成功与能力收窄、首帧必须 hello、坏签名/challenge 不符/未知/未批/吊销设备拒绝、major 不符关连、schema 违例不断线、未知 type 忽略、sensor 去重 ack、action 幂等/回执终态、心跳离线恢复等。
- `test_device_agent_loop.py`：失败码归一、unknown 不重试且中止、吊销/收窄 scope 停环、observe 超时有界重试、control/shell 超时 reconcile（pending→不重发、not_found→可重试）、私有记忆空间写入。
- `test_device_event_consumer.py`：happy path、未授权/吊销丢弃、private 不进管道、低价值/合并窗口、注入文本不成指令、静默时段抑制、网关失败标 unknown 不盲重发。
- `test_durable_devices.py`：inbox 跨重启存活至 ack；ack 不存在事件报错。
- `test_device_confirm_auth.py`：scoped grant 签发、单次核销防重放、并发只有一个赢、错设备/过期/拒绝、verifier 必填、挑战不带私密内容。
- `test_device_output_query.py`：`device.agent.output.read` 独立 scope、不隐含 observe/shell、吊销后不可用、未知 operation 传输前拒绝。
- `test_device_receipt_results.py`：终态 receipt 结果跨重启可对账（双后端参数化）。
- `test_device_retention.py`：7d 保留、重试窗口内不动、过宽限标 unknown、孤儿流水清理、单趟删除封顶。
- `test_devices_scopes.py`：能力常量与协议一致、scope 与 grant 体系集成（群聊硬拒、owner 私聊/API 放行）。
- `test_glasses_policy.py` / `test_glasses_api.py`：卡片校验、回执分层绝不伪报 shown；独立 glasses API 进程行为。
- `test_arch_channels.py`：白名单冻结 bridge.py 允许的 life 依赖。

## 6. v2 复用 vs 重写评估

**可直接复用（与 transport 解耦良好）**：
- `registry.py` 全部：配对状态机、scope 上限、审计约定。v2 换存储时只需替换 MemoryStore 实现或加设备表适配层。
- `schema.py`：contracts 校验器（纯 stdlib、fail closed），ADB/SSH transport 复用同一信封/能力契约时零改动。
- `agent_failures.py`：编排失败码与重试纪律，与 transport 无关。
- `bridge.py` 的 `GatewayActionPort` 模式：v2 只需让新 gateway 暴露同签名 `send_action(device_id, *, capability, args, idempotency_key, timeout_s)` + `action_status(device_id, idempotency_key)`，`SimulatedDeviceBridge`/`DeviceAgentLoop`/`event_consumer` 全部不感知。
- `perception.py` 两个 adapter、`event_consumer.py`、`initiative_loop.py`、`glasses.py`：纯上层，只依赖 adapter/store/gateway 窄接口。
- `agent_loop.py` 的 `DeviceAgentLoop` 本体：device_port/output_port 已是 Protocol 注入。

**需要重写/改造**：
- `gateway.py` 的 transport 绑定部分：`_handle/_handle_hello/_handle_frame/send_msg/kick/ConnectionState` 与 websockets sync server 强耦合。ADB/SSH transport 下"连接"语义不同（无 challenge-应答 WS 握手、心跳模型不同、可能无长连），应抽象 `DeviceTransport` 接口（send/close/online 枚举），把 **幂等预约、回执状态机、retention sweep、scope 校验**（`_on_action_receipt`、`send_action` 的 transact reserve、`action_status`、`sweep_action_retention`、`_check_scope`）抽成 transport 无关的 `ActionDispatcher` 复用——这些是 v1 最有价值的部分。
- `DeviceGateway.__init__` 构造即监听的形态：v2 应将 listener 生命周期与 dispatcher 分离（否则 ADB 轮询/SSH 会话无法套用）。
- Ed25519 challenge-应答认证：ADB/SSH 的认证事实源是 ADB key / SSH host key，`_verify_signature` 流程需按 transport 替换，但"注册公钥 → device_id"指纹机制可保留。
- `devices_api.py` 的 `_downlink/_confirm`：`_confirm` 强绑 `send_confirm_request` 的 WS 帧往返；ADB/SSH 下设备端确认交互需要新机制。REST 路由外壳（路径/错误码映射/装配）可保留。
- 已知小坑：`event_consumer._device_allowed` 读 `meta["capabilities"]` 而 registry 实际存 `scopes` 字段——v2 顺手修正（测试里用 FakeRegistry 所以没暴露）。
- kv 键域约定（`device:action:*`、`device:receipts:*` 等）是跨模块契约，v2 若改表结构需提供迁移或保持 kv 语义等价。
