"""Initiative：打扰控制与主动提醒策略。"""

from __future__ import annotations

from typing import Any

from whale_core.initiative.policy import (
    AlwaysAllowPolicy,
    DisturbancePolicy,
    InitiativePolicy,
    ProactiveCallPolicy,
    QuietHoursPolicy,
)

__all__ = [
    "InitiativePolicy",
    "QuietHoursPolicy",
    "AlwaysAllowPolicy",
    "DisturbancePolicy",
    "ProactiveCallPolicy",
    "ProactiveCallPlanner",
    "suggest_proactive_call",
    "PhoneActionPlanner",
    "suggest_phone_action",
]


def __getattr__(name: str) -> Any:
    """延迟加载策划器，避免与 actions 包初始化环依赖。"""
    if name in ("ProactiveCallPlanner", "suggest_proactive_call"):
        from whale_core.initiative import call_planner as _cp

        return getattr(_cp, name)
    if name in ("PhoneActionPlanner", "suggest_phone_action"):
        from whale_core.initiative import phone_planner as _pp

        return getattr(_pp, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
