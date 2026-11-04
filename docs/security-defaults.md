# 安全默认（拒绝默认 / grant 边界）

状态：网关与 Shell ACL 已 fail-closed；本页固化「分类 ≠ 授权」与开关面。

## 复用什么

- `plugins/gateway.py` + `plugins/policy.py`：非 owner / 无 grant → denied
- `cloud/shell_policy.py` + `life/temporary_auth.py`：Shell 与 sudo 租约
- Studio `GrantsPanel`：临时口令与租约 UI（不含支付/删库等永不可授 scope）

## 自研补齐（本回合）

- 只读 API：`GET /api/v1/security/defaults`（`security_defaults_snapshot()`）
- 本文档 + GrantsPanel 拒绝默认提示
- **不改** Shell ACL 裁决矩阵

## 纪律

| 规则 | 说明 |
|------|------|
| 分类 ≠ 授权 | `intent_router` 命中 `shell.safe` 只选路线；执行仍过 grant/owner + 沙箱 |
| 拒绝默认 | 未认证 / 无匹配租约 → 拒绝；transaction 永不 auto |
| 群聊禁 sudo | `TemporaryAuthService.verify` 拒绝非私聊 |
| 低熵口令 | 仅 `LOW_ENTROPY_SCOPES` |

## 相关开关

见 snapshot 的 `switches` 字段；主动问候默认 off + dry-run。
