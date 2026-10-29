"""P1 可跑 stub：主动呼叫提议 / 手机控制 / 环境语音。"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from whale_core.actions.gateway import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    CompositeActionGateway,
    LocalBridgeGateway,
    PhoneControlGateway,
)
from whale_core.actions.intents import AmbientListen, PhoneAction, PlaceCall, ShowCard
from whale_core.core.agent import WhaleAgent
from whale_core.core.router import StubModelRouter
from whale_core.core.types import Decision, Observation
from whale_core.initiative.call_planner import ProactiveCallPlanner
from whale_core.initiative.phone_planner import PhoneActionPlanner
from whale_core.initiative.policy import (
    AlwaysAllowPolicy,
    ProactiveCallPolicy,
    QuietHoursPolicy,
)
from whale_core.memory.continuity import build_continuity_context
from whale_core.memory.store import InMemoryStore, MemoryKind
from whale_core.perception.ambient import extract_ambient_events
from whale_core.perception.events import AmbientSpeechSegment, ContextWeather, ScheduleTick
from whale_core.perception.reasoner import ContextReasoner

TZ = ZoneInfo("Asia/Shanghai")


class EmptyRouter(StubModelRouter):
    """不主动产出意图，依赖策划器提议。"""

    def route(self, observation: Observation) -> Decision:
        return Decision(intents=(), rationale="p1-empty")


class DayContextAgent(WhaleAgent):
    """注入固定白天 now + 可选 context 覆盖。"""

    def __init__(self, *args, extra_ctx: dict | None = None, now: datetime | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._extra_ctx = dict(extra_ctx or {})
        self._now = now or datetime(2026, 10, 28, 14, 0, tzinfo=TZ)

    def perceive(self, events):
        obs = super().perceive(events)
        ctx = dict(obs.context)
        ctx["now"] = self._now
        ctx.update(self._extra_ctx)
        return Observation(
            event_ids=obs.event_ids,
            summary=obs.summary,
            context=ctx,
            observed_at=obs.observed_at,
        )


def test_proactive_call_allow_non_quiet_place_call_bridge_receipt() -> None:
    """主动呼叫：allow + 非 quiet → place_call + bridge 回执含 127.0.0.1:18799。"""
    quiet_never = QuietHoursPolicy(start=time(3, 0), end=time(3, 0), tz=TZ)
    gateway = CompositeActionGateway(bridge=LocalBridgeGateway())
    agent = DayContextAgent(
        router=EmptyRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=ProactiveCallPolicy(quiet=quiet_never),
        call_planner=ProactiveCallPlanner(),
        gateway=gateway,
        memory=InMemoryStore(),
        extra_ctx={"allow_proactive_call": True, "call_contact": "鲸鱼"},
        now=datetime(2026, 10, 28, 14, 0, tzinfo=TZ),
    )
    decision, receipts = agent.step([ScheduleTick(title="午间")])
    assert any(i.kind == "place_call" for i in decision.intents)
    assert len(receipts) == 1
    assert DEFAULT_BRIDGE_HOST in receipts[0]
    assert str(DEFAULT_BRIDGE_PORT) in receipts[0]
    assert "place_call" in receipts[0]
    assert receipts[0].startswith("stub-bridge:")


def test_proactive_call_quiet_without_urgent_no_place_call() -> None:
    """主动呼叫：quiet + 无 urgent → 无 place_call。"""
    quiet = QuietHoursPolicy(start=time(22, 0), end=time(7, 0), tz=TZ)
    agent = DayContextAgent(
        router=EmptyRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=ProactiveCallPolicy(quiet=quiet),
        call_planner=ProactiveCallPlanner(),
        gateway=LocalBridgeGateway(),
        memory=InMemoryStore(),
        extra_ctx={"allow_proactive_call": True},
        now=datetime(2026, 10, 28, 23, 0, tzinfo=TZ),
    )
    decision, receipts = agent.step([ScheduleTick(title="夜")])
    assert all(i.kind != "place_call" for i in decision.intents)
    assert receipts == []


def test_phone_action_phone_control_gateway_receipt() -> None:
    """PhoneAction → PhoneControlGateway（或 Composite）回执。"""
    phone_gw = PhoneControlGateway()
    gateway = CompositeActionGateway(phone_control=phone_gw)
    agent = DayContextAgent(
        router=EmptyRouter(),
        policy=AlwaysAllowPolicy(),
        phone_planner=PhoneActionPlanner(),
        gateway=gateway,
        memory=InMemoryStore(),
        extra_ctx={
            "suggest_phone_action": True,
            "phone_action_kind": "open_app",
            "phone_action_target": "com.example.maps",
        },
    )
    decision, receipts = agent.step([ScheduleTick(title="去图书馆")])
    assert any(i.kind == "phone_action" for i in decision.intents)
    assert receipts == ["stub-phone-control:open_app:com.example.maps"]
    assert phone_gw.last_intent is not None
    assert phone_gw.last_intent.kind == "phone_action"

    # 直接派发 post_notification
    intent = PhoneAction(action="post_notification", target="alerts").to_intent()
    assert PhoneControlGateway().dispatch(intent) == "stub-phone-control:post_notification:alerts"


def test_ambient_speech_segment_situation_and_memory_no_audio() -> None:
    """AmbientSpeechSegment → situation.ambient_note 含 summary_label；记忆无 audio bytes。"""
    reasoner = ContextReasoner()
    segment = AmbientSpeechSegment(
        duration_ms=2500,
        language_hint="zh",
        summary_label="课堂讨论",
        source="ambient",
    )
    brief = reasoner.reason([segment, ContextWeather(condition="晴")])
    assert "课堂讨论" in brief.ambient_note
    assert "环境语音事件已提取" in brief.suggestions
    assert "课堂讨论" in brief.as_dict()["ambient_note"]

    tags = extract_ambient_events(segment)
    assert tags.summary_label == "课堂讨论"
    assert tags.duration_ms == 2500
    assert "audio" not in tags.as_dict()

    store = InMemoryStore()
    agent = WhaleAgent(
        router=EmptyRouter(),
        policy=AlwaysAllowPolicy(),
        memory=store,
        remember_ambient=True,
    )
    agent.step([segment])
    sit = agent.last_situation()
    assert sit is not None
    assert "课堂讨论" in sit.ambient_note

    episodes = store.list_by_kind(MemoryKind.EPISODE)
    assert episodes
    for ep in episodes:
        content = dict(ep.content or {})
        # 不得出现真实音频字节
        assert content.get("audio_bytes") in (None, "", b"", [])
        blob = str(content)
        assert "b'\\x" not in blob
        assert isinstance(content.get("summary"), str)
        assert "课堂讨论" in content.get("summary", "") or content.get("summary_label") == "课堂讨论"

    # 连续性组装仍可读文本摘要
    cont = build_continuity_context(store)
    assert any("课堂讨论" in s or "环境语音" in s for s in cont.as_dict()["recent_episode_summaries"])


def test_ambient_listen_intent_receipt() -> None:
    """AmbientListen intent 经 Composite 有回执；AlwaysAllow 下不受静默误伤。"""
    gateway = CompositeActionGateway()
    # 直接派发
    on_intent = AmbientListen(enabled=True, reason_note="全天感知").to_intent()
    off_intent = AmbientListen(enabled=False).to_intent()
    assert gateway.dispatch(on_intent) == "stub-ambient-listen:on"
    assert gateway.dispatch(off_intent) == "stub-ambient-listen:off"

    class ListenRouter(StubModelRouter):
        def route(self, observation: Observation) -> Decision:
            return Decision(
                intents=(AmbientListen(enabled=True, reason_note="demo").to_intent(),),
                rationale="p1-listen",
            )

    # 静默时段下 AlwaysAllow：listen 启停不被 Disturbance 误伤（本测用 AlwaysAllow）
    quiet = QuietHoursPolicy(start=time(22, 0), end=time(7, 0), tz=TZ)
    agent = DayContextAgent(
        router=ListenRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=ProactiveCallPolicy(quiet=quiet),
        gateway=gateway,
        memory=InMemoryStore(),
        now=datetime(2026, 10, 28, 23, 30, tzinfo=TZ),
    )
    decision, receipts = agent.step([ScheduleTick(title="夜")])
    assert any(i.kind == "ambient_listen" for i in decision.intents)
    assert "stub-ambient-listen:on" in receipts


def test_planner_does_not_duplicate_place_call_kind() -> None:
    class AlreadyCallRouter(StubModelRouter):
        def route(self, observation: Observation) -> Decision:
            return Decision(
                intents=(PlaceCall(contact_label="已有").to_intent(), ShowCard(title="x").to_intent()),
                rationale="has-call",
            )

    quiet_never = QuietHoursPolicy(start=time(3, 0), end=time(3, 0), tz=TZ)
    agent = DayContextAgent(
        router=AlreadyCallRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=ProactiveCallPolicy(quiet=quiet_never),
        call_planner=ProactiveCallPlanner(),
        gateway=CompositeActionGateway(),
        memory=InMemoryStore(),
        extra_ctx={"allow_proactive_call": True},
    )
    decision, _ = agent.step([ScheduleTick(title="x")])
    place_calls = [i for i in decision.intents if i.kind == "place_call"]
    assert len(place_calls) == 1
    assert place_calls[0].payload.get("contact_label") == "已有"
