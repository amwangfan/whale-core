"""安全默认快照——网关/API 可读，不授予任何权限。

纪律（owner 2026-11）：
- **分类 ≠ 授权**：intent_router / 模型意图分类只选路线，不能签发 grant。
- **拒绝默认**：非 owner 且无匹配租约 → PluginGateway / ShellPolicy fail-closed。
- **Shell ACL 独立**：``tool.shell.safe`` 仍受沙箱与 personal 档约束；本模块不改裁决。
"""
from __future__ import annotations

from typing import Any

from whale_core.life.temporary_auth import GRANTABLE_SCOPES, LOW_ENTROPY_SCOPES


def security_defaults_snapshot() -> dict[str, Any]:
    """只读安全默认视图（可进 /api/v1/diagnostics 或 Studio）。"""
    return {
        "deny_by_default": True,
        "classification_is_not_authorization": True,
        "intent_router_cannot_grant": True,
        "shell_acl": {
            "unchanged": True,
            "scope": "tool.shell.safe",
            "requires": "authenticated owner OR active temporary grant",
            "still_sandboxed": True,
            "note": "Shell ACL / personal 档策略不在此面修改；分类命中 shell.safe 仍要过门控",
        },
        "grant_boundaries": {
            "grantable_scopes": sorted(GRANTABLE_SCOPES),
            "low_entropy_scopes": sorted(LOW_ENTROPY_SCOPES),
            "group_sudo_forbidden": True,
            "transaction_never_auto": True,
            "payment_delete_never_grantable": True,
        },
        "switches": {
            "WHALE_PROACTIVE_GREETING": "default off; 1 enables loop",
            "WHALE_PROACTIVE_GREETING_DRY_RUN": "default dry-run when enabled; 0 = real send (still cooldown; daily quota 0=unlimited)",
            "WHALE_PROACTIVE_GREETING_DAILY_QUOTA": "0 = unlimited; positive = Shanghai-day cap; default 2",
            "WHALE_INNER_STATE_WRITEBACK": "default on; 0 disables companion→InnerState topic write-back",
        },
        "docs": "docs/security-defaults.md",
    }
