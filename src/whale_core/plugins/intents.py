"""泛化插件动作意图（kind ``plugin_action``）：新输出点无需改核心意图目录。

分层：插件框架契约（framework，issue #31）。

动作意图只描述 plugin/action/target/args；不携带 owner 或审批布尔值。
``PluginGateway`` 必须通过宿主注入的可信 verifier 查询 owner 会话和审批台账；
默认 verifier 全拒绝。风险级从 manifest 解析，不能由意图自声明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from whale_core.core.types import ActionIntent


@dataclass(frozen=True)
class PluginAction:
    """声明由某插件执行一个动作（发信息 / 写邮件 / 买东西 / 桌面通知……）。"""

    plugin_id: str
    action: str
    target: str = ""  # 语义目标：收件人 / 商品 id / 应用名等，由插件解释
    args: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="plugin_action",
            payload={
                "plugin_id": self.plugin_id,
                "action": self.action,
                "target": self.target[:500],
                "args": dict(self.args),
                "executor": f"plugin:{self.plugin_id}",
            },
            priority=self.priority,
            reason="plugin.action",
        )
