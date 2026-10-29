"""云端 shell 权限分档策略：只做分类与裁决，绝不执行命令。

档位（env ``WHALE_SHELL_TIER``，默认 managed，未知取值回落最保守档）：

- **managed**：仅固定白名单工具（与 call-bridge ``BRIDGE_TOOLS`` 对齐），无自由命令；
- **safe**：managed + 只读检索类工具（网页搜索等），仍无自由命令；
- **personal**：完全个人使用的单 owner 宿主机——在高危模式之外放开自由命令，
  且仅认证 owner 的入站事件可触发。

所有档位共享 ``HIGH_RISK_PATTERNS`` 硬拒绝清单；不存在「无限制」档。

诚实边界：字符串拒绝清单**可被绕过**（编码、变量拼接、自写脚本），personal 档的
前提是「单一受信 owner + 已认证渠道」；真正的隔离边界在宿主机执行器侧
（非 root 专用用户 + systemd 沙箱 + 超时强杀），本策略只是防手滑护栏。
执行永远发生在基础设施侧（cloud host executor / bridge_tools），本模块不起子进程。
"""

from __future__ import annotations

import os
import re
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

# 高危硬拒绝（personal 档也拒绝）。label 用于审计，不含命令本身以外的信息。
HIGH_RISK_PATTERNS: tuple[tuple[str, str], ...] = (
    # 破坏性文件系统
    (r"\brm\s+(?:-\w*[rf]\w*\s+)+(?:/|~|\$HOME\b)(?:\s|$|\*|/)", "destructive_rm"),
    (r"\b(?:rm|mv)\b[^\n]*\s/(?:etc|usr|bin|sbin|boot|dev|sys|proc)\b", "system_dir_mutation"),
    (r"\b(?:mkfs(?:\.\w+)?|fdisk|parted|sgdisk)\b", "disk_partition_format"),
    (r"\bdd\b[^\n]*\bof=/dev/", "dd_to_device"),
    (r">\s*/dev/(?:sd|nvme|vd|mmcblk)", "device_write"),
    (r"\bshred\b[^\n]*/dev/", "shred_device"),
    # 电源 / init
    (r"\b(?:shutdown|poweroff|halt|reboot)\b", "power_state"),
    (r"\bsystemctl\s+(?:poweroff|reboot|halt|suspend|hibernate)\b", "power_state"),
    (r"\binit\s+[06]\b", "power_state"),
    (r"\bkill\s+(?:-\w+\s+)?1\b", "kill_init"),
    # fork bomb
    (r":\(\s*\)\s*\{", "fork_bomb"),
    # 提权 / 权限破坏
    (r"\b(?:sudo|doas|pkexec)\b", "privilege_escalation"),
    (r"\bsu\b(?:\s|$)", "privilege_escalation"),
    (r"\bchmod\s+(?:-R\s+)?(?:[0-7]*777|a\+rwx)\s+/", "world_writable_root"),
    (r"\bchown\s+(?:-R\s+)?\S+\s+/(?:\s|$)", "chown_root"),
    # 凭据 / 隐私材料
    (r"/etc/(?:shadow|sudoers|gshadow)\b", "credential_file"),
    (r"(?:~|\$HOME\b|/root\b|/home/\w+)/\.ssh\b", "ssh_material"),
    (r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b", "ssh_key"),
    (r"\.aws/credentials|\.kube/config|\.netrc\b|\.git-credentials\b", "cloud_credentials"),
    (r"authorized_keys", "ssh_authorized_keys"),
    (r"/proc/\d+/environ|\bprintenv\b", "env_dump"),
    (r"\$\{?\w*(?:API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY)\w*\}?", "secret_variable_expansion"),
    # 远程代码管道执行
    (r"\b(?:curl|wget|fetch)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|fi)?sh\b", "piped_remote_code"),
    (r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:python\d?|perl|ruby|node)\b", "piped_remote_code"),
    # 容器 / 命名空间逃逸面（等价 root）
    (r"\b(?:docker|podman|kubectl|nerdctl)\b", "container_engine"),
    (r"\b(?:nsenter|chroot)\b", "namespace_escape"),
    # 自身服务 / 审计破坏
    (r"\bsystemctl\s+(?:stop|disable|mask|kill)\b", "service_takedown"),
    (r"\bcrontab\s+-r\b", "cron_wipe"),
    (r"\bhistory\s+-c\b|\.(?:bash|zsh)_history\b", "audit_wipe"),
    (r"\bjournalctl\s+--(?:rotate|vacuum)", "journal_wipe"),
    # 防火墙 / 远程访问削弱
    (r"\bufw\s+disable\b|\biptables\s+-F\b|\bnft\s+flush\s+ruleset\b|\bfirewall-cmd\s+--panic-on\b", "firewall_off"),
    # shell rc 持久化写入
    (r">{1,2}\s*\S*\.(?:bashrc|zshrc|profile|bash_profile)\b", "shell_rc_write"),
    # git 不可逆远程操作
    (r"\bgit\s+push\b[^\n]*(?:--force\b|--force-with-lease\b|\s-f\b|--delete\b|--mirror\b)", "git_force_push"),
)


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
    risk: str = ""

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
        self._compiled = tuple((re.compile(p), label) for p, label in HIGH_RISK_PATTERNS)

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
        """自由命令裁决：仅 personal 档 + 认证 owner；高危模式硬拒绝。"""
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
        for pattern, label in self._compiled:
            if pattern.search(cmd):
                return ShellVerdict(False, f"high-risk pattern: {label}", self.tier, risk=label)
        return ShellVerdict(True, "within personal non-dangerous scope", self.tier)

    def audit(self, subject: str, verdict: ShellVerdict, *, source: str = "") -> dict:
        """台账记录：只记命令/工具文本（截断）与裁决；**不记命令输出**（可能含密钥）。"""
        return {
            "subject": subject[: self.max_command_chars],
            "source": source,
            "at": datetime.now(timezone.utc).isoformat(),
            **verdict.as_dict(),
        }
