"""Smoke：agent stub + QuietHours + Disturbance + Memory + 新契约。"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from whale_core.actions.gateway import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_DASHBOARD_BASE,
    HttpDashboardGateway,
    LocalBridgeGateway,
)
from whale_core.actions.intents import (
    AmbientListen,
    NavigateHint,
    PhoneAction,
    PlaceCall,
    ShowCard,
    Speak,
)
from whale_core.core.agent import WhaleAgent
from whale_core.core.router import StubModelRouter
from whale_core.core.types import ActionIntent, Decision, Observation, SessionState
from whale_core.initiative.policy import (
    DisturbancePolicy,
    ProactiveCallPolicy,
    QuietHoursPolicy,
)
from whale_core.memory.store import (
    InMemoryStore,
    MemoryKind,
    MemoryRecord,
    PersonaContinuity,
)
from whale_core.perception.events import (
    AmbientSpeechSegment,
    ContextCourse,
    ContextLocation,
    ContextWeather,
    NavRouteProgress,
    PhoneForeground,
    ScheduleTick,
)
from whale_core.ui.hud import HudPresentation
from whale_core.ui.look import WhaleLook
from whale_core.ui.persona import WhalePersona
from whale_core.ui.voice import VoiceStyle


class FixedRouter(StubModelRouter):
    """测试用：固定产出一条 ShowCard 意图。"""

    def route(self, observation: Observation) -> Decision:
        intent = ShowCard(title="提醒", body=observation.summary).to_intent()
        return Decision(intents=(intent,), rationale="test-fixed")


class CallRouter(StubModelRouter):
    def route(self, observation: Observation) -> Decision:
        return Decision(
            intents=(PlaceCall(contact_label="鲸鱼", mode="outgoing").to_intent(),),
            rationale="test-call",
        )


def test_in_memory_store_roundtrip() -> None:
    store = InMemoryStore()
    store.put("k", {"v": 1})
    assert store.get("k") == {"v": 1}
    assert "k" in store.keys()
    store.delete("k")
    assert store.get("k") is None


def test_memory_kinds_and_persona_continuity() -> None:
    store = InMemoryStore()
    ep = MemoryRecord(
        kind=MemoryKind.EPISODE,
        key="ep:1",
        content={"summary": "路上聊了课表"},
        tags=("commute",),
    )
    pref = MemoryRecord(
        kind=MemoryKind.PREFERENCE,
        key="pref:quiet",
        content={"prefer_quiet_before": "22:00"},
    )
    store.put_record(ep)
    store.put_record(pref)
    assert len(store.list_by_kind(MemoryKind.EPISODE)) == 1
    assert store.get_record("pref:quiet") is not None

    persona = PersonaContinuity(
        persona_name="鲸鱼",
        tone="简洁",
        last_session_id="conv-1",
        taboos=("不泄密",),
    )
    store.put_persona_continuity(persona)
    loaded = store.get_persona_continuity()
    assert loaded is not None
    assert loaded.persona_name == "鲸鱼"
    assert loaded.last_session_id == "conv-1"
    assert "不泄密" in loaded.taboos


def test_quiet_hours_cross_midnight() -> None:
    policy = QuietHoursPolicy(
        start=time(22, 0),
        end=time(7, 0),
        tz=ZoneInfo("Asia/Shanghai"),
    )
    quiet = datetime(2026, 10, 28, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    awake = datetime(2026, 10, 28, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert policy.in_quiet_hours(quiet) is True
    assert policy.in_quiet_hours(awake) is False


def test_quiet_hours_suppresses_intents() -> None:
    policy = QuietHoursPolicy(
        start=time(0, 0),
        end=time(23, 59, 59),
        tz=ZoneInfo("Asia/Shanghai"),
    )
    agent = WhaleAgent(router=FixedRouter(), policy=policy, memory=InMemoryStore())
    events = [PhoneForeground(package="com.example", label="Example")]
    decision, receipts = agent.step(events)
    assert decision.suppress is True
    assert decision.intents == ()
    assert receipts == []


def test_disturbance_policy_busy_foreground() -> None:
    quiet = QuietHoursPolicy(
        start=time(22, 0),
        end=time(7, 0),
        tz=ZoneInfo("Asia/Shanghai"),
    )
    # 中午不在静默，但前台忙碌应抑制
    policy = DisturbancePolicy(quiet=quiet, busy_packages=("com.busy.app",))
    agent = WhaleAgent(router=FixedRouter(), policy=policy, memory=InMemoryStore())
    events = [PhoneForeground(package="com.busy.app", label="Busy")]
    decision, _ = agent.step(events)
    assert decision.suppress is True


def test_disturbance_policy_urgent_bypasses_busy() -> None:
    policy = DisturbancePolicy(
        quiet=QuietHoursPolicy(
            start=time(22, 0), end=time(7, 0), tz=ZoneInfo("Asia/Shanghai")
        ),
        busy_packages=("com.busy.app",),
    )
    obs = Observation(
        summary="x",
        context={"foreground_package": "com.busy.app", "urgent": True},
    )
    decision = Decision(intents=(ShowCard(title="急").to_intent(),), rationale="u")
    assert policy.should_suppress(obs, decision) is False


def test_proactive_call_policy_filters() -> None:
    from whale_core.initiative.policy import AlwaysAllowPolicy

    quiet = QuietHoursPolicy(
        start=time(22, 0), end=time(7, 0), tz=ZoneInfo("Asia/Shanghai")
    )
    call_policy = ProactiveCallPolicy(quiet=quiet)
    agent = WhaleAgent(
        router=CallRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=call_policy,
        memory=InMemoryStore(),
    )
    # 默认不允许主动呼叫
    decision, receipts = agent.step([ScheduleTick(title="闲")])
    assert all(i.kind != "place_call" for i in decision.intents)
    assert receipts == []


def test_proactive_call_allowed_when_flagged() -> None:
    from whale_core.initiative.policy import AlwaysAllowPolicy

    class FlaggedCallRouter(StubModelRouter):
        def route(self, observation: Observation) -> Decision:
            return Decision(
                intents=(PlaceCall().to_intent(),),
                rationale="ok",
            )

    class FlaggingAgent(WhaleAgent):
        def perceive(self, events):
            obs = super().perceive(events)
            ctx = dict(obs.context)
            ctx["allow_proactive_call"] = True
            return Observation(
                event_ids=obs.event_ids,
                summary=obs.summary,
                context=ctx,
                observed_at=obs.observed_at,
            )

    quiet = QuietHoursPolicy(
        start=time(22, 0), end=time(7, 0), tz=ZoneInfo("Asia/Shanghai")
    )
    # 若当前恰在 quiet，测试会 flake；用「全天非 quiet」窗口：start==end 不可能，改用 0 长度外的白天策略
    # 使用 start=end=00:00 且 end 紧跟 —— 选 start=3:00 end=3:00 视为空区间（start<=end 且 t 永不满足 start<=t<end 若 start==end）
    quiet_never = QuietHoursPolicy(
        start=time(3, 0), end=time(3, 0), tz=ZoneInfo("Asia/Shanghai")
    )
    agent = FlaggingAgent(
        router=FlaggedCallRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=ProactiveCallPolicy(quiet=quiet_never),
        gateway=LocalBridgeGateway(),
        memory=InMemoryStore(),
    )
    decision, receipts = agent.step([ScheduleTick(title="ok")])
    assert len(decision.intents) == 1
    assert decision.intents[0].kind == "place_call"
    assert len(receipts) == 1


def test_agent_step_stores_observation_and_dispatches() -> None:
    gateway = HttpDashboardGateway()
    memory = InMemoryStore()
    session = SessionState(device_id="glass-dev", conversation_id="c1")
    agent = WhaleAgent(
        router=FixedRouter(),
        memory=memory,
        gateway=gateway,
        session=session,
    )
    events = [
        ScheduleTick(title="开会"),
        PhoneForeground(package="com.cal", label="日历"),
    ]
    decision, receipts = agent.step(events)
    assert decision.suppress is False
    assert len(decision.intents) == 1
    assert decision.intents[0].kind == "show_card"
    assert len(receipts) == 1
    assert receipts[0].startswith("stub-dashboard:")
    assert DEFAULT_DASHBOARD_BASE in receipts[0]
    assert any(k.startswith("obs:") for k in memory.keys())
    assert gateway.last_intent is not None
    # session 可读且已 touch
    s = agent.get_session()
    assert s.device_id == "glass-dev"
    assert s.conversation_id == "c1"
    assert "schedule.tick" in s.last_turn


def test_bridge_gateway_stub_defaults() -> None:
    gw = LocalBridgeGateway()
    intent = Speak(text="你好").to_intent()
    receipt = gw.dispatch(intent)
    assert DEFAULT_BRIDGE_HOST in receipt
    assert str(DEFAULT_BRIDGE_PORT) in receipt
    assert intent.kind == "speak"


def test_ui_persona_hud_and_look() -> None:
    persona = WhalePersona()
    assert persona.name == "鲸鱼"
    assert persona.taboos
    style = VoiceStyle()
    assert style.locale == "zh-CN"
    hud = HudPresentation(headline="在路上", lines=("预计 10 分钟",))
    payload = hud.as_show_card_payload()
    assert payload["title"] == "在路上"
    assert "预计" in payload["body"]
    look = WhaleLook(mood="surprise", surprise_hud=True, surprise_motif="confetti.soft")
    meta = look.as_hud_meta()
    assert meta["look_surprise"] is True
    assert meta["look_motif"] == "confetti.soft"


def test_action_intent_from_helpers() -> None:
    card = ShowCard(title="t", body="b").to_intent()
    assert isinstance(card, ActionIntent)
    assert card.kind == "show_card"
    phone = PhoneAction(action="open_app", target="com.example.maps").to_intent()
    assert phone.kind == "phone_action"
    assert phone.payload["executor"] == "phone_control_bridge"
    nav = NavigateHint(route_label="去图书馆", message="下个路口左转", eta_minutes=8).to_intent()
    assert nav.kind == "navigate_hint"
    listen = AmbientListen(enabled=True, reason_note="全天感知").to_intent()
    assert listen.kind == "ambient_listen"
    assert listen.payload["enabled"] is True


def test_new_perception_event_kinds() -> None:
    assert AmbientSpeechSegment(duration_ms=1200, summary_label="课堂讨论").kind == (
        "ambient.speech_segment"
    )
    assert ContextCourse(course_title="高数").kind == "context.course"
    assert ContextWeather(condition="小雨", temp_c_range="16-18").kind == "context.weather"
    assert ContextLocation(place_label="教学楼", coarse_area="校园").kind == (
        "context.location"
    )
    assert NavRouteProgress(route_label="回家", next_step="直行", eta_minutes=12).kind == (
        "nav.route_progress"
    )
