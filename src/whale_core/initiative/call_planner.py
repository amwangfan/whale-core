"""主动呼叫策划 stub：规则提议 PlaceCall，再经 ProactiveCallPolicy 过滤。

本模块只「提议」；静默 / 紧急穿透仍由 ProactiveCallPolicy 决定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from whale_core.actions.intents import PlaceCall
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


def _trigger_tags(ctx: Mapping[str, Any], sit: Mapping[str, Any]) -> set[str]:
    tags: set[str] = set()
    for key in ("trigger_tags", "call_trigger_tags"):
        raw = ctx.get(key) or sit.get(key) or ()
        if isinstance(raw, str):
            tags.add(raw)
        elif isinstance(raw, (list, tuple, set)):
            tags.update(str(t) for t in raw)
    # 从 schedule / ambient 摘要里扫简单标签词
    for note_key in ("schedule_note", "ambient_note", "headline"):
        note = str(sit.get(note_key) or "")
        for token in ("suggest_call", "call_needed", "请回电", "需要回电"):
            if token in note:
                tags.add("suggest_call")
    return tags


class ProactiveCallPlanner:
    """轻量规则策划器：观察 + 情境 → 可选 PlaceCall。

    触发条件（任一）：
    - context.allow_proactive_call 或 urgent
    - 情境 urgency_hint==\"elevated\" 且显式 suggest_call / call_reason
    - schedule / ambient 摘要或 trigger_tags 含 suggest_call 类标签
    """

    def __init__(
        self,
        *,
        default_contact: str = "鲸鱼",
        default_mode: str = "outgoing",
    ) -> None:
        self.default_contact = default_contact
        self.default_mode = default_mode

    def suggest_proactive_call(
        self,
        observation: Observation,
        situation: SituationBrief | Mapping[str, Any] | None = None,
    ) -> PlaceCall | None:
        ctx = dict(observation.context or {})
        sit = _situation_dict(observation, situation)
        tags = _trigger_tags(ctx, sit)

        urgent = bool(ctx.get("urgent"))
        allow_flag = bool(ctx.get("allow_proactive_call"))
        elevated = sit.get("urgency_hint") == "elevated"
        explicit = bool(ctx.get("suggest_call") or ctx.get("call_reason"))
        tag_hit = bool(tags & {"suggest_call", "call_needed", "proactive_call"})

        if not (urgent or allow_flag or (elevated and explicit) or tag_hit):
            return None

        contact = str(
            ctx.get("call_contact")
            or ctx.get("contact_label")
            or self.default_contact
        )
        reason = str(
            ctx.get("call_reason")
            or (sit.get("ambient_note") if tag_hit else "")
            or sit.get("headline")
            or observation.summary
            or "proactive_call"
        )
        extras: dict[str, Any] = {"planner": "ProactiveCallPlanner", "reason": reason}
        if tags:
            extras["trigger_tags"] = sorted(tags)
        return PlaceCall(
            contact_label=contact,
            mode=str(ctx.get("call_mode") or self.default_mode),
            extras=extras,
            priority=10 if urgent else 5,
        )


def suggest_proactive_call(
    observation: Observation,
    situation: SituationBrief | Mapping[str, Any] | None = None,
    *,
    planner: ProactiveCallPlanner | None = None,
) -> PlaceCall | None:
    """模块级便捷入口。"""
    return (planner or ProactiveCallPlanner()).suggest_proactive_call(
        observation, situation
    )
