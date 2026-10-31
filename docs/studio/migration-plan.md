# Whale Studio · 数据分类与迁移计划（S0）

> 日期：2026-10-31 · 基线：`whale-core@d13b818`
> 原则：**先备份、dry-run、确认后执行、可回滚**；迁移不给任何群聊自动挂私人记忆。

## 1. 生产数据盘点（实测，2026-10-31）

库：`/opt/whale/state/qq_workflow.sqlite3`（MemoryStore + kv 混用）

| 区域 | 键前缀 / 表 | 行数 | 分类 | 迁移去向 |
| --- | --- | --- | --- | --- |
| 记忆记录 | `records` 表 | 24 | **全部视为私人原始**（最小权限） | `protected_original` 默认 Vault |
| ├ 聊天片段 | `ep:chat:*`（kind=episode） | 22（12 私聊 / 10 群） | 私人原始 | Vault；会话不自动获得副本 |
| ├ 人格连续性 | `persona:default`（persona_continuity） | 1 | 私人原始 | Vault |
| └ 偏好 | `curated:*`（preference, stable） | 1 | 私人原始 | Vault |
| 角色卡 | `persona:card:*`、`persona:active_card` | 3 | 配置（非记忆） | 保留原位；active_card 降级为"未绑定回退" |
| 对话历史 | `chat:history:api`、`chat:history:private:<uid>` | 2 | 会话上下文（敏感） | 不动；后续按会话键隔离 |
| 任务台账 | `task:*`、`runtime:task:*`、`tasks:recent` | 17 | 运行时状态 | 不动；S7 加授权快照字段 |
| 生命周期草稿/账本 | `life:draft:*`、`life:ledger:*`、`life:quota:*` | 48 | 运行时状态 | 不动 |
| 产物索引 | `artifact:*` | 7 | 文件元数据（图片/语音） | 不动 |
| 插件状态/健康 | `plugin:state:*`、`provider:health:*` | 11 | 运行时状态 | 不动 |
| 预算 | `budget:*` | 2 | 运行时状态 | 不动 |

库：`/opt/whale/state/qq_inbox.sqlite3`：`inbox` 表 74 行（入站队列，含 attachments_json）——不动。

文件：`/opt/whale/state/persona-cards/*.txt`（1 张自定义卡）、`/opt/whale/state/artifacts/*`（7 个产物文件）——不动。

**结论：需要迁移的只有 `records` 表 24 行；其余全部保持原位。**

## 2. Schema 变更（已在副本 dry-run 验证）

```sql
CREATE TABLE studio_memory_spaces (
  space_id TEXT PRIMARY KEY,
  type TEXT CHECK(type IN ('protected_original','owner_working_copy','group_space','shareable_fork')),
  name TEXT NOT NULL, owner_principal TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, retired_at TEXT
);
ALTER TABLE records ADD COLUMN space_id TEXT REFERENCES studio_memory_spaces(space_id);
CREATE INDEX idx_records_space ON records(space_id);
CREATE TABLE studio_conversation_bindings (...);        -- 会话 → 角色卡（S3）
CREATE TABLE studio_conversation_spaces (...);          -- 会话 → 记忆空间 多对多（S2/S5）
```

注意：SQLite `ALTER TABLE ADD COLUMN ... REFERENCES` 仅允许默认 NULL，已验证可行。

## 3. 迁移步骤（生产执行时）

1. **冻结写入**：暂停 qq-bot（`systemctl stop qq-bot`），防迁移中写入。
2. **在线一致备份**：`sqlite3` backup API 复制两个库到 `/opt/whale/backups/studio-mig-<ts>/`（✅ 演练已通过：102+24+74 行，hash 校验一致）。
3. **事务内执行** schema 变更 + `UPDATE records SET space_id='vault-default'`（单事务，失败整体回滚；✅ 演练已通过：注入失败后回滚无残留列、行数不变）。
4. **校验**：迁移前后 `records` 原有 6 列内容 hash 一致（✅ 演练通过）；记录计数一致。
5. **启动并观察**：最小权限兼容模式——旧 `persona:active_card` 仅作回退；**不给任何群绑定 vault 或工作副本**。
6. **回滚路径**：整库从备份恢复（✅ 演练通过），代码侧 schema 变更为纯增量（新表+新列），旧版本代码不认识新列也能运行（SQLite 按列名访问）。

## 4. 回滚演练记录（2026-10-31，副本执行）

| 项目 | 结果 |
| --- | --- |
| 在线备份一致性（行数+hash） | ✅ |
| dry-run 迁移 24 行入 vault | ✅ |
| 迁移前后原列 hash 漂移 | ✅ 零漂移 |
| 中途失败事务回滚 | ✅ 无残留 |
| 整库备份恢复 | ✅ |

## 5. 明确不做的事

- 不修改 `content`、不重新分类单条记录（主人后续在管理界面 Fork）。
- 不自动创建任何 `owner_working_copy` / `group_space`。
- 不动 `kv` 内任何运行时状态；不触碰 secrets。
- 不在迁移脚本里写死任何 sender/conversation 的授权。
