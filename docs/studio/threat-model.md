# Whale Studio · 威胁模型（S0）

> 日期：2026-10-31 · 每条威胁标注**现状证据**与**处置阶段**。
> 资产：主人私人记忆（records 24 条/未来更多）、QQ 账号发送能力、工具执行能力（shell/media/search）、API token、一次性口令、审批决策。

## 威胁主体
- T-A：群聊普通成员（含好奇者、提示注入者）
- T-B：陌生私聊账号（可能伪装主人）
- T-C：第三方角色卡/插件/网页内容（不可信文本）
- T-D：能接触管理 API 的调用方（token 泄漏后）
- T-E：模型自身幻觉/被注入后的行为

## 威胁清单（现状 → 处置）

| # | 威胁 | 现状证据 | 当前暴露 | 处置 |
| --- | --- | --- | --- | --- |
| TH-01 | 越权检索私人记忆 | companion.py:34-35 无授权召回；curation.py:82 全表扫描 | **高**：任何能触发对话的会话都获得全局记忆 | S2 查询前 SQL 限域 + ResolvedContext 硬闸 |
| TH-02 | 伪造 owner | InputEnvelope.owner_id 盲信（runtime.py:310）；identity.py 死代码 | 中：QQ 入口有配对门，但 API/内部路径无复核 | S1 服务端解析，client 字段作废 |
| TH-03 | 群里主人提权全群 | channels.py 群私聊不分，OwnerEvent 丢 sender | 高（accept_group=True 时） | S1 ConversationKind 显式化 + 群权限上限 |
| TH-04 | 固定暗号/自称主人提权 | 无 OTP 机制；口令在 chat 文本里会进记忆与日志 | 未实现功能，设计期阻断 | S4 一次性口令 + 原子核销 + 明文拦截 |
| TH-05 | Prompt 注入改策略 | 角色卡/网页/工具结果未与权限隔离（但 policy.py:109 非 owner 全拒兜底） | 中：工具层 fail-closed，记忆层无防 | S1-S2 上下文与权限解耦（INV-04/07） |
| TH-06 | 隐式检索回退跨域 | DynamicHybridCurator 能力降级回退（semantic.py:26-34） | 设计后风险：限域后回退不得跨空间 | S2 回退只在已授权空间集合内 |
| TH-07 | 缓存越域 | embedding 缓存键 sha256(text) 无命名空间（embedding.py:42） | 高（S2 引入空间后必须解决） | S2 缓存键加 (space_id, version) |
| TH-08 | 后台任务持失效授权 | 任务台账无授权快照；resume 不重验 | 中（现任务均为 owner 发起） | S1+S4 授权快照 + 敏感动作前重验（INV-09） |
| TH-09 | UI 绕过 API 直攻 | 无 Studio API；现有 API 单 token 全权 | 设计期约束 | S5 服务端单一事实源 + 负向测试 |
| TH-10 | 口令重放/并发双花 | 无机制 | 设计期阻断 | S4 SQLite 单事务原子核销（INV-05） |
| TH-11 | 口令明文泄漏 | 聊天记录/记忆/日志管道已存在 | 设计期阻断 | S4 入 LLM/记忆/日志前拦截；建议管理页提交 |
| TH-12 | API token 泄漏=全权 | server.py 单 token 全权，无 scope | 中（接受现状：本地 token=owner 管理通道） | S5 起 Studio 写操作加主体解析；token 仍 0600 |
| TH-13 | 审批跨上下文重放 | pending_fingerprint 只覆盖 payload（runtime.py:1036） | 低（现单主体） | S1 指纹扩展 conversation+principal |
| TH-14 | 备份/导出越界 | backup 整库覆盖、export 无空间过滤（export.py:36） | S2 后风险 | S2 备份/导出按空间过滤或仅限管理通道 |
| TH-15 | 语音链路无身份 | RealtimeTranscriptRecorder 无 owner 概念 | 中 | S7 语音会话挂 ResolvedContext |

## 已确认"不构成威胁"的现有防线（不要拆）
- gateway.py verifier 注入 + fail-closed（L62-69）
- policy.py 非 owner 全拒 + transaction 强制人审（L109-149）
- API Bearer 恒定时间比较 + Origin 检查 + token 必填启动
- QQ 入站 transport_authenticated + 配对门（channels.py:36-39）
- 审批单次消费 + 金额上限 + 日限串行预留

## 红队测试映射（S0 空壳 → 点亮阶段）
`tests/test_studio_invariants.py` 已建：INV-01/03/05 契约级 4 项现役绿；8 项 skip 占位随 S1-S5 点亮。goal-chain F2 的 N01-N17 逐项对应测试，发布前全绿是硬 Gate。

## 政策修订（owner 决策 2026-11-01）
- **MEM-02 修订**：成员记忆默认开启——成员与鲸鱼私聊即视为同意，无需显式授权；
  「别记我了」持久退出（opt-out），撤销/删除仍可用。memories 只记本人自述，
  敏感内容仍整条不抽，空间隔离与私聊归属校验不变。
- **MEM-HARD-01 修订**：protected_original 允许连接**主人本人大号私聊/控制台**
  （paired owner 且 QQ_PRIVATE/API 会话）；群聊/成员/陌生人/未配对依旧硬拒。
  存储层 include_protected 默认 False，仅该路径显式开启（双钥匙）。
- 群消息 sudo 明文不落时间线（先于截获的持久化已堵）。
