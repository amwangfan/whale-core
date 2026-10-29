"""云端入站事件契约（消息 / 工具结果）。真实 QQ/OneBot 适配器外置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class InboundEvent:
    """入站事件：消息或工具回执。不含 Token / 密钥。"""

    kind: str  # "message" | "tool_result"
    text: str = ""
    source: str = ""  # 渠道标签，如 qq / bridge_tool / webhook
    tool_name: str = ""  # kind=tool_result 时填写白名单工具名
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("message", "tool_result"):
            raise ValueError(f"unsupported inbound kind: {self.kind!r}")
