"""模型路由接口（stub）：以后可接百炼 / 本地 Agent，此处不写密钥。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from whale_core.core.types import Decision, Observation


@runtime_checkable
class ModelRouter(Protocol):
    """将 Observation 路由到某推理后端并得到 Decision。"""

    def route(self, observation: Observation) -> Decision:
        ...


class StubModelRouter:
    """空实现：始终返回空 Decision，便于契约与 smoke 测试。"""

    def route(self, observation: Observation) -> Decision:
        return Decision(
            intents=(),
            rationale=f"stub: no model; summary={observation.summary!r}",
            suppress=False,
        )
