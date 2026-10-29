"""主循环 stub：perceive → decide → act。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from whale_core.actions.gateway import ActionGateway, NullActionGateway
from whale_core.core.router import ModelRouter, StubModelRouter
from whale_core.core.types import ActionIntent, Decision, Observation, SessionState
from whale_core.initiative.policy import (
    AlwaysAllowPolicy,
    SafeDefaultPolicy,
    DisturbancePolicy,
    InitiativePolicy,
    ProactiveCallPolicy,
)
from whale_core.memory.continuity import (
    ContinuityContext,
    build_continuity_context,
    remember_episode,
)
from whale_core.memory.store import InMemoryStore, MemoryStore
from whale_core.perception.ambient import extract_ambient_events
from whale_core.perception.events import AmbientSpeechSegment, PerceptionEvent
from whale_core.perception.reasoner import ContextReasoner, SituationBrief

if TYPE_CHECKING:
    from whale_core.initiative.call_planner import ProactiveCallPlanner
    from whale_core.initiative.phone_planner import PhoneActionPlanner


def _merge_intent(intents: tuple[ActionIntent, ...], new: ActionIntent) -> tuple[ActionIntent, ...]:
    """按 kind 去重：已有同 kind 则保留原意图，不重复追加。"""
    if any(i.kind == new.kind for i in intents):
        return intents
    return intents + (new,)


class WhaleAgent:
    """智能层主循环骨架；不实现 ADB / 上镜 / 音频桥。"""

    def __init__(
        self,
        *,
        router: ModelRouter | None = None,
        memory: MemoryStore | None = None,
        policy: InitiativePolicy | None = None,
        gateway: ActionGateway | None = None,
        session: SessionState | None = None,
        call_policy: ProactiveCallPolicy | None = None,
        reasoner: ContextReasoner | None = None,
        call_planner: ProactiveCallPlanner | None = None,
        phone_planner: PhoneActionPlanner | None = None,
        remember_ambient: bool = True,
    ) -> None:
        self.router = router or StubModelRouter()
        self.memory = memory or InMemoryStore()
        self.policy = policy if policy is not None else SafeDefaultPolicy()
        self.gateway = gateway or NullActionGateway()
        self.session = session or SessionState()
        self.call_policy = call_policy
        self.reasoner = reasoner or ContextReasoner()
        # None = 不自动提议；传入实例才启用（延迟 import 避免 actions↔core 环）
        self.call_planner = call_planner
        self.phone_planner = phone_planner
        self.remember_ambient = remember_ambient
        self._last_continuity: ContinuityContext | None = None
        self._last_situation: SituationBrief | None = None

    def get_session(self) -> SessionState:
        """Agent 可读的多设备会话状态。"""
        return self.session

    def last_continuity(self) -> ContinuityContext | None:
        """上一轮组装的 ContinuityContext（供测试 / 调试）。"""
        return self._last_continuity

    def last_situation(self) -> SituationBrief | None:
        """上一轮 SituationBrief（供测试 / 调试）。"""
        return self._last_situation

    def perceive(self, events: Sequence[PerceptionEvent]) -> Observation:
        """折叠感知事件为 Observation；注入连续性与情境摘要，供 decide 引用。"""
        ids = [e.event_id for e in events]
        kinds = [e.kind for e in events]
        summary = ", ".join(kinds) if kinds else "idle"

        continuity = build_continuity_context(self.memory)
        situation = self.reasoner.reason(events)
        self._last_continuity = continuity
        self._last_situation = situation

        ctx: dict = {
            "kinds": kinds,
            "continuity": continuity.as_dict(),
            "situation": situation.as_dict(),
        }
        # 情境倾向少打扰时，给策略侧提示（紧急仍可穿透）
        if situation.urgency_hint == "quiet_preferred":
            ctx.setdefault("situation_quiet_preferred", True)

        observation = Observation(
            event_ids=ids, summary=summary, context=ctx
        )
        # 若策略支持，从前台事件补全忙碌上下文
        if isinstance(self.policy, DisturbancePolicy):
            observation = DisturbancePolicy.enrich_busy_from_events(observation, events)
        return observation

    def decide(self, observation: Observation) -> Decision:
        """推理选择；合并策划提议后，经打扰策略与主动呼叫策略过滤。"""
        raw = self.router.route(observation)
        intents = tuple(raw.intents)
        rationale = raw.rationale

        if self.call_planner is not None:
            suggested = self.call_planner.suggest_proactive_call(
                observation, self._last_situation
            )
            if suggested is not None:
                before = len(intents)
                intents = _merge_intent(intents, suggested.to_intent())
                if len(intents) > before:
                    rationale = f"{rationale}; +proactive_call_planner"

        if self.phone_planner is not None:
            phone = self.phone_planner.suggest_phone_action(
                observation, self._last_situation
            )
            if phone is not None:
                before = len(intents)
                intents = _merge_intent(intents, phone.to_intent())
                if len(intents) > before:
                    rationale = f"{rationale}; +phone_action_planner"

        decision = Decision(
            intents=intents,
            rationale=rationale,
            suppress=raw.suppress,
            decided_at=raw.decided_at,
        )

        if self.policy.should_suppress(observation, decision):
            return Decision(
                intents=(),
                rationale=f"suppressed by policy; was: {decision.rationale}",
                suppress=True,
            )
        if self.call_policy is not None:
            decision = self.call_policy.filter_decision(observation, decision)
        return decision

    def act(self, decision: Decision) -> list[str]:
        """把意图交给 ActionGateway（stub 不真连）。"""
        receipts: list[str] = []
        if decision.suppress:
            return receipts
        for intent in decision.intents:
            receipts.append(self.gateway.dispatch(intent))
        return receipts

    def step(self, events: Sequence[PerceptionEvent]) -> tuple[Decision, list[str]]:
        """一轮完整循环。"""
        observation = self.perceive(events)
        self.memory.put(
            f"obs:{observation.observed_at.isoformat()}",
            {
                "summary": observation.summary,
                "event_ids": list(observation.event_ids),
                "situation": dict(observation.context.get("situation") or {}),
                "continuity_summary": (observation.context.get("continuity") or {}).get(
                    "persona_summary", ""
                ),
            },
        )
        # 可选：环境语音文本摘要入 episode（无音频字节）
        if self.remember_ambient:
            for e in events:
                if isinstance(e, AmbientSpeechSegment) or e.kind == "ambient.speech_segment":
                    tags = extract_ambient_events(e)
                    remember_episode(
                        self.memory,
                        summary=tags.text_summary(),
                        key=f"ep:ambient:{e.event_id}",
                        tags=("ambient",),
                        extra={
                            "summary_label": tags.summary_label,
                            "language_hint": tags.language_hint,
                            "duration_ms": tags.duration_ms,
                            # 明确不存音频
                            "audio_bytes": None,
                        },
                    )
        decision = self.decide(observation)
        receipts = self.act(decision)
        # 会话连续性：记录本轮摘要
        turn = observation.summary
        self.session = self.session.touch(turn=turn)
        return decision, receipts
