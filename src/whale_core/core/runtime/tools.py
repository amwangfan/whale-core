"""Runtime 工具层契约（issue #30 拆分）：ToolSpec/ToolRegistry 与模型协议。

- ``ToolSpec``：模型可按 Schema 调用的具体操作（roadmap §4.3 Tool 层）；
  非 read_only 必须经插件网关映射，执行器不能自批；
- ``ToolRegistry``：显式注册（与插件注册同款约定，不做自动发现）；
- ``IntentModel`` / ``ActionGatewayLike``：编排器构造期注入的协议。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from whale_core.core.types import ActionIntent
from whale_core.plugins.manifest import RiskClass

TOOL_OUTPUT_MAX_CHARS = 1200
MAX_TOOL_ROUNDS = 4


class IntentModel(Protocol):
    """意图模型契约：输入用户文本与上下文摘要，输出含 IntentPlan JSON 的文本。"""

    def plan(self, text: str, *, context: str = "") -> str:
        ...


class ActionGatewayLike(Protocol):
    def dispatch(self, intent: ActionIntent) -> str:
        ...


@dataclass(frozen=True)
class ToolSpec:
    """一个可被 Runtime 调用的工具（模型可见的具体操作，roadmap §4.3 Tool 层）。

    - ``risk_class`` 非 read_only 时，执行必须经插件网关：``plugin_id`` +
    ``action`` 把该工具映射到一个已注册插件动作（风险级由 manifest 裁决，
    不由工具自declare）；缺映射的非只读工具一律拒绝执行。
    - ``handler``：同步可调用，返回**短文本结果**（长输出由 Runtime 截断）。
    """

    name: str
    capability: str
    description: str = ""
    risk_class: str = RiskClass.READ_ONLY.value
    plugin_id: str = ""
    action: str = ""
    handler: Callable[[Mapping[str, Any]], str] | None = None

    def __post_init__(self) -> None:
        if not (self.name or "").strip():
            raise ValueError("tool requires a name")
        if not (self.capability or "").strip():
            raise ValueError("tool requires a capability id")
        RiskClass(self.risk_class)  # 非法风险级构造即失败


class ToolRegistry:
    """工具注册表：显式注册（与插件注册同款约定，不做自动发现）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name!r}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get((name or "").strip())

    def for_capability(self, capability: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.capability == capability]

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())
