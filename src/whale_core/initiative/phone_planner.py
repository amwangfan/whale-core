"""手机控制意图策划 stub：监测（PhoneForeground）≠ 控制；本策划填补控制侧提议。

产出 PhoneAction，由 PhoneControlGateway / 未来控制桥执行；不写 ADB。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from whale_core.actions.intents import PhoneAction
from whale_core.core.types import Observation

if TYPE_CHECKING:
    from whale_core.perception.reasoner import SituationBrief


def _situation_dict(
    observation: Observation, situation: SituationBrief | Mapping[str, Any] | None
) -> dict[str, Any]:
    if situation is None:
        raw = (observation.context or {}).get("situation") or {}
        return dict(raw) if isinstance(raw, Mapping) else {}
    if hasattr(situation, "as_dict"):
        return dict(situation.as_dict())  # type: ignore[union-attr]
    return dict(situation)


class PhoneActionPlanner:
    """规则：显式 suggest_phone_action / phone_action 字段，或 ambient 摘要触发通知。"""

    def suggest_phone_action(
        self,
        observation: Observation,
        situation: SituationBrief | Mapping[str, Any] | None = None,
    ) -> PhoneAction | None:
        ctx = dict(observation.context or {})
        sit = _situation_dict(observation, situation)

        # 显式指定：context["phone_action"] = {"action": "open_app", "target": "..."}
        explicit = ctx.get("phone_action")
        if isinstance(explicit, Mapping) and explicit.get("action"):
            return PhoneAction(
                action=str(explicit.get("action")),
                target=str(explicit.get("target") or ""),
                extras=dict(explicit.get("extras") or {}),
                priority=int(explicit.get("priority") or 0),
            )

        if ctx.get("suggest_phone_action"):
            action = str(ctx.get("phone_action_kind") or "open_app")
            target = str(ctx.get("phone_action_target") or "com.example.app")
            return PhoneAction(action=action, target=target, priority=3)

        # ambient 摘要触发：发一条本地通知（仍是意图 stub）
        ambient = str(sit.get("ambient_note") or "")
        if ambient and ctx.get("ambient_to_notification"):
            return PhoneAction(
                action="post_notification",
                target="ambient",
                extras={"body": ambient[:120], "planner": "PhoneActionPlanner"},
                priority=2,
            )
        return None


def suggest_phone_action(
    observation: Observation,
    situation: SituationBrief | Mapping[str, Any] | None = None,
    *,
    planner: PhoneActionPlanner | None = None,
) -> PhoneAction | None:
    return (planner or PhoneActionPlanner()).suggest_phone_action(
        observation, situation
    )
