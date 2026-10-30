# Whale Studio · 架构设计（S0 基线审计后的实施版）

> 日期：2026-10-31 · 基线：`whale-core@d13b818`（+ S0 契约）
> 本文档只写**经代码审计确认的事实**与**落到具体文件的设计**，不复述 PRD 全文。

## 1. 现状审计结论（事实，均有 file:line 证据）

### 1.1 身份与运行时
- `life/identity.py` 的 `OwnerRegistry` **是死代码**：全仓仅 tests 引用，生产零接线。owner_id 为调用方自由字符串，无签发验证。
- `life/channels.py`：`OwnerEvent` **丢弃 sender account**，群/私聊不区分（kind 由适配器拼进 `private:{uid}`/`group:{gid}` 字符串）。
- `core/runtime.py`：`InputEnvelope.owner_id/source` 由调用方自填、核心盲信（L220 注释与 L310 现实矛盾）。
- **实证摩擦**：`runtime_owner_verifier` 的 source 白名单为 `{"api","cli","runtime"}`（runtime.py:104），QQ 用 `"qq:owner"`；且编排器构造的工具 intent 无 draft_id（runtime.py:1028-1030）→ **QQ 编排路径非只读工具实际全被拒**，现网只有只读工具可达。这就是"权限问题一直没炸"的原因——敏感路径事实上从没被触发过。
- `core/companion.py`：记忆检索（L34-35）、历史注入（L50）、写回（L60-67）**零授权检查**。
- `plugins/policy.py`：非 owner 一律 denied（L109-116）；`audit()` 死代码。
- `plugins/gateway.py`：身份只从注入 verifier 查，fail-closed（设计正确，是 Studio 权限扩展的挂点）。

### 1.2 记忆子系统
- 两张全局表 `kv` / `records`，只有主键无索引；**零迁移机制**（无 user_version、无 ALTER 先例——inbox 除外）。
- **"先全库查后过滤"确认存在**：`curation.py:82-83`、`hybrid.py:122-124` 全表拉出 EPISODE+PREFERENCE 后 Python 侧按 tag 过滤。
- embedding 缓存键 = `sha256(text)`，**无任何命名空间**（plugins/embedding.py:42-43）。
- `governance.py` 分层/去重/过期/supersede **全部是死代码**（仅 tests 调用）；过期治理从未自动运行。
- `backup.py:26-33` `_validate` 硬编码列集——加列能过、缺列拒，迁移时必须同步。
- **装配不一致**：profiles.py 用 `DynamicHybridCurator`，qq_workflow.py 用裸 `MemoryCurator`（QQ 永不走向量召回）。
- 删除全部物理 DELETE（符合 owner"真删"要求），唯一软删是 supersede tag。

### 1.3 角色卡 / API / 前端
- `persona:active_card` 全局单值；`persona:default` 快照被**每条对话热读**（companion.py:32、agent.py:88、server.py:550）——会话级改造必须保留全局兜底。
- **卡切换/导入没有任何 HTTP/CLI 入口**，纯库代码。
- API：单 Bearer token = 全权，**无调用者身份概念**，无版本/乐观锁；`POST /chat` 压根不设 owner_id。
- 前端 React18+Vite5，无 xyflow/路由/状态库；token 存 localStorage。
- `ctx.lock` 纪律：ThreadingHTTPServer 每请求一线程，写路由须沿用。

## 2. 目标架构（落到文件）

```
可信渠道适配器（qq_onebot/wq_push/voice/api）
      │  Incoming{channel, kind, conversation_id, sender_id, transport_authenticated}
      ▼
ConversationContextResolver  ←── OwnerRegistry（复活）+ TemporaryGrantStore（S4）
      │  输出不可变 ResolvedContext（已冻结于 life/conversation_context.py）
      ▼
┌─────────────────────────────────────────────────┐
│ InputEnvelope 增加 context 字段（服务端注入，只读）│
│  env.owner_id 保留兼容但不再作为权限证据          │
└─────────────────────────────────────────────────┘
      │                    │                    │
      ▼                    ▼                    ▼
PersonaResolver      MemoryAccessPolicy      CapabilityPolicy
（会话→卡绑定，       （查询前限域，SQL 下沉）   （scope 上限计算，
 全局卡兜底）          vault 硬拒）             不超 policy.py 基线）
      │                    │                    │
      └──────────┬─────────┴────────────────────┘
                 ▼
     既有 ConversationOrchestrator / companion / PluginGateway
     （扩展而非替换：gateway verifier 改读 ResolvedContext）
```

### 2.1 模块规划（与 goal-chain §G 对齐，按现状修正）

| 新/改 | 文件 | 职责 |
| --- | --- | --- |
| 已有 | `life/conversation_context.py` | ✅ S0 已冻结：ConversationRef/Principal/MemorySpaceRef/TemporaryGrantRef/ResolvedContext |
| 新增 | `life/context_resolver.py` | resolver 实现：渠道输入 → ResolvedContext；OwnerRegistry 复活为证据源之一 |
| 新增 | `memory/spaces.py` | MemorySpace 元信息 + ACL 判定（MemoryAccessPolicy） |
| 改 | `memory/sqlite_store.py` | records 加 space_id + 索引；list_by_kind 支持 space 过滤；引入 PRAGMA user_version 迁移机制 |
| 改 | `memory/curation.py`、`hybrid.py`、`semantic.py` | recall 签名加 `allowed_spaces`；**SQL 层限域**，消灭全表拉取后过滤 |
| 改 | `plugins/embedding.py` | 缓存键 → sha256((space_id, space_version, text)) |
| 新增 | `memory/fork.py` | ForkService：选记录→脱敏预览→确认→事务物理复制→独立空间 |
| 改 | `life/channels.py` | Incoming/OwnerEvent 保留 sender_id + conversation_kind（frozen dataclass 加字段，兼容构造） |
| 改 | `core/runtime.py` | handle_envelope 消费 context；`_intent_context` 按 ctx 限域；verifier 白名单修正（qq:owner 入列） |
| 改 | `core/companion.py` | chat 接收 ctx；recall/writeback 按授权空间；无 ctx 时走最小权限默认 |
| 新增 | `life/temporary_auth.py` | OneTimeAuthCode + TemporaryGrant（S4） |
| 新增 | `api/studio.py` | /studio/* 资源 API（S5），挂进 server._dispatch |
| 新增 | `ui/persona_resolver.py` | 会话绑定解析 + 渲染（S3） |
| 新增 | `web/src/components/Studio/` | 画布（S6，最后做） |

### 2.2 关键设计决策（已定，不再摇摆）

1. **kv 表不加空间维度**。kv 承载 task/life/history/plugin 等运行时状态，加列或前缀都会破坏现有前缀扫描（backup.py:89、history.py:94）。**空间隔离只作用于 records 表（记忆）与 embedding 缓存**；chat history 天然按 conversation_id 隔离，已满足"只含当前会话"。
2. **conversation_id 字符串格式不动**。它同时是锁键/历史键/delivery 路由；ConversationRef 仅作为解析期结构，落库仍用现有字符串（`private:{uid}`/`group:{gid}`），零迁移成本。
3. **InputEnvelope 加 `context: Optional[ResolvedContext]`**。为 None 时（旧调用方）按"未认证普通主体 + 无记忆空间 + chat.basic"最小权限运行——**这比现状严格**（现状是无检查全量召回），属于安全收紧，需要在迁移期对 QQ/控制台装配点全部显式注入，避免功能回退。
4. **verifier 体系不重写**。gateway 的 verifier 注入点设计正确；S1 只需让宿主构造能感知 ResolvedContext 的 verifier（如 `ctx_aware_owner_verifier`），并修正 source 白名单纳入 `qq:owner`。
5. **persona:default 兜底永留**。PersonaResolver：会话绑定 > `persona:active_card` > 内置 default。每次对话热读路径不改签名，只改取值来源。
6. **审批指纹扩展**。引入 ctx 后，`pending_fingerprint` 从 sha256(payload) 扩展为 sha256(payload + conversation_key + principal_id)，防跨上下文重放（审计发现的风险点）。
7. **迁移默认全部 records → protected_original**（比审计建议的 owner_working_copy 更保守，符合 PRD"最小权限兼容"）；主人后续 Fork 出工作副本。

## 3. 性能预算（先测基线再定阈值，S7 实测）

已知成本点：4000 字 persona preamble 每请求注入（多卡并发会放大 token 成本）；向量召回现场批量 embed 无持久索引（1000 条记忆时需评估）。S2 的 SQL 限域同时是性能改善（不再全表拉取）。

## 4. 与 PRD 的偏差声明

- PRD 建议 `life/access_policy.py` 独立模块；实际将 CapabilityPolicy 并入 context_resolver（ scopes 计算）+ memory/spaces（记忆 ACL），减少一个交叉引用层。
- PRD §7.2 的 `POST /auth/temporary/challenges` 首版不做（无可信管理端触发场景前不建空壳）。
- 画布用 `@xyflow/react`（PRD 推荐），S6 才引入依赖。
