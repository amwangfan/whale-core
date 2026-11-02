"""云端 shell 权限分档策略：只做分类与裁决，绝不执行命令。

档位（env ``WHALE_SHELL_TIER``，默认 managed，未知取值回落最保守档）：

- **managed**：仅固定白名单工具（与 call-bridge ``BRIDGE_TOOLS`` 对齐），无自由命令；
- **safe**：managed + 只读检索类工具（网页搜索等），仍无自由命令；
- **personal**：完全个人使用的单 owner 宿主机——认证 owner 的入站事件可触发
  自由命令。

本模块的裁决边界是「档位 + owner 身份 + 命令形状」（issue #59）：不再维护
字符串黑名单——黑名单可被编码/拼接绕过，却拦得住正常命令，假装不了安全边界。
真正的隔离在执行器侧：Bubblewrap 命名空间、cwd 白名单、rlimit 资源限制、
超时强杀进程组、环境擦洗与审计（``cloud/shell_executor``）。沙箱不可用时
执行器 fail-closed，本模块的任何放行都不会落到宿主机上。

执行永远发生在基础设施侧（cloud host executor / bridge_tools），本模块不起子进程。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping

# 与 call-bridge BRIDGE_TOOLS / cloud.loop 既有白名单对齐（canonical 定义在此）
MANAGED_TOOLS: frozenset[str] = frozenset(
    {
        "what_time",
        "bridge_status",
        "ring_fake_call",
        "list_workspace",
        "tail_bridge_log",
        "restart_bridge",
    }
)

# safe 档新增：只读、无本机副作用的检索类工具（执行适配器在云端外置，默认关）
SAFE_EXTRA_TOOLS: frozenset[str] = frozenset(
    {
        "web_search",
        "web_fetch",
        "weather_now",
        "calendar_today",
    }
)

MAX_COMMAND_CHARS = 2000


class ShellTier(str, Enum):
    """权限档位；顺序即包含关系（managed ⊂ safe ⊂ personal）。"""

    MANAGED = "managed"
    SAFE = "safe"
    PERSONAL = "personal"


TIER_ORDER: tuple[ShellTier, ...] = (ShellTier.MANAGED, ShellTier.SAFE, ShellTier.PERSONAL)


@dataclass(frozen=True)
class ShellVerdict:
    allowed: bool
    reason: str
    tier: ShellTier
    risk: str = ""  # 兼容字段：黑名单已移除（issue #59），恒为空串

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "tier": self.tier.value,
            "risk": self.risk,
        }


class ShellPolicy:
    """分档裁决：工具名或自由命令 → ShellVerdict。纯函数式，无副作用。"""

    def __init__(
        self,
        *,
        tier: ShellTier | str = ShellTier.MANAGED,
        managed_tools: frozenset[str] | None = None,
        safe_tools: frozenset[str] | None = None,
        max_command_chars: int = MAX_COMMAND_CHARS,
    ) -> None:
        self.tier = ShellTier(tier)  # 非法取值直接 ValueError（from_env 负责回落）
        self.managed_tools = MANAGED_TOOLS if managed_tools is None else frozenset(managed_tools)
        self.safe_tools = SAFE_EXTRA_TOOLS if safe_tools is None else frozenset(safe_tools)
        self.max_command_chars = max_command_chars

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ShellPolicy":
        """``WHALE_SHELL_TIER``：缺省 / 未知值一律回落 managed（最保守）。"""
        source = os.environ if env is None else env
        raw = (source.get("WHALE_SHELL_TIER") or "").strip().lower()
        if not raw:
            return cls()
        try:
            return cls(tier=ShellTier(raw))
        except ValueError:
            return cls()

    def tier_at_least(self, tier: ShellTier) -> bool:
        return TIER_ORDER.index(self.tier) >= TIER_ORDER.index(tier)

    def tool_verdict(self, tool_name: str) -> ShellVerdict:
        """白名单工具裁决：managed 工具全档可用；safe 工具需 safe 档以上。"""
        name = (tool_name or "").strip()
        if not name:
            return ShellVerdict(False, "empty tool name", self.tier)
        if name in self.managed_tools:
            return ShellVerdict(True, "managed tool", self.tier)
        if name in self.safe_tools:
            if self.tier_at_least(ShellTier.SAFE):
                return ShellVerdict(True, "safe read-only tool", self.tier)
            return ShellVerdict(
                False,
                f"tool {name!r} requires safe tier (current: {self.tier.value})",
                self.tier,
            )
        return ShellVerdict(False, f"unknown tool {name!r}", self.tier)

    def command_verdict(self, command: str, *, is_owner: bool = False) -> ShellVerdict:
        """自由命令裁决：仅 personal 档 + 认证 owner + 基本形状检查。

        放行的命令在执行器侧进入 Bubblewrap 沙箱（cwd 白名单 / rlimit /
        超时 / 环境擦洗）；策略层不做字符串黑名单——那不是安全边界。
        """
        if not self.tier_at_least(ShellTier.PERSONAL):
            return ShellVerdict(
                False,
                f"free-form commands require personal tier (current: {self.tier.value})",
                self.tier,
            )
        if not is_owner:
            return ShellVerdict(False, "personal commands require authenticated owner", self.tier)
        cmd = command or ""
        if not cmd.strip():
            return ShellVerdict(False, "empty command", self.tier)
        if len(cmd) > self.max_command_chars:
            return ShellVerdict(False, f"command longer than {self.max_command_chars} chars", self.tier)
        if "\x00" in cmd:
            return ShellVerdict(False, "NUL byte in command", self.tier)
        return ShellVerdict(True, "owner command in personal tier; sandbox is the boundary", self.tier)

    def audit(self, subject: str, verdict: ShellVerdict, *, source: str = "") -> dict:
        """台账记录：只记命令/工具文本（截断）与裁决；**不记命令输出**（可能含密钥）。"""
        return {
            "subject": subject[: self.max_command_chars],
            "source": source,
            "at": datetime.now(timezone.utc).isoformat(),
            **verdict.as_dict(),
        }
