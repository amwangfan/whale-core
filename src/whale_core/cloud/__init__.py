"""云端行动闭环：入站事件 → 选行动 → Actions 网关（stub / mock HTTP）。

shell 权限分档（managed / safe / personal）裁决在 ``shell_policy``，
闭环只产意图、不执行；默认档 managed，无自由命令。
"""

from whale_core.cloud.events import InboundEvent
from whale_core.cloud.loop import (
    DEFAULT_SHELL_ALLOWLIST,
    CloudActionLoop,
    RuleActionSelector,
)
from whale_core.cloud.shell_policy import (
    MANAGED_TOOLS,
    SAFE_EXTRA_TOOLS,
    ShellPolicy,
    ShellTier,
    ShellVerdict,
)

__all__ = [
    "InboundEvent",
    "CloudActionLoop",
    "RuleActionSelector",
    "DEFAULT_SHELL_ALLOWLIST",
    "MANAGED_TOOLS",
    "SAFE_EXTRA_TOOLS",
    "ShellPolicy",
    "ShellTier",
    "ShellVerdict",
]
