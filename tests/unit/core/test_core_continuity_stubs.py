"""P0 可跑 stub：连续性 / 情境推理 / 打扰策略路径。"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from whale_core.actions.intents import PlaceCall, ShowCard
from whale_core.core.agent import WhaleAgent
from whale_core.core.router import StubModelRouter
from whale_core.core.types import Decision, Observation
from whale_core.initiative.policy import (
    AlwaysAllowPolicy,
    DisturbancePolicy,
    ProactiveCallPolicy,
    QuietHoursPolicy,
)
from whale_core.memory.continuity import (
    ContinuityContext,
    build_continuity_context,
    remember_episode,
    remember_preference,
)
from whale_core.memory.store import InMemoryStore, PersonaContinuity
from whale_core.perception.events import (
    ContextCourse,
    ContextLocation,
    ContextWeather,
    PhoneForeground,
)
from whale_core.perception.reasoner import ContextReasoner, SituationBrief

TZ = ZoneInfo("Asia/Shanghai")


class FixedCardRouter(StubModelRouter):
    def route(self, observation: Observation) -> Decision:
        sit = (observation.context or {}).get("situation") or {}
        body = sit.get("headline") or observation.summary
        return Decision(
            intents=(ShowCard(title="提醒", body=str(body)).to_intent(),),
            rationale="p0-card",
        )


class FixedCallRouter(StubModelRouter):
    def route(self, observation: Observation) -> Decision:
        return Decision(
            intents=(PlaceCall(contact_label="鲸鱼").to_intent(),),
            rationale="p0-call",
        )


def test_continuity_context_assembles_persona_and_episodes() -> None:
    store = InMemoryStore()
    store.put_persona_continuity(
        PersonaContinuity(
            persona_name="鲸鱼",
            tone="简洁",
            taboos=("不泄密",),
            last_session_id="s-9",
        )
    )
    remember_episode(store, summary="早上聊了高数", key="ep:math", tags=("course",))
    remember_preference(store, key="pref:quiet", content={"prefer_quiet_before": "22:00"})

    ctx = build_continuity_context(store)
    assert isinstance(ctx, ContinuityContext)
    assert "鲸鱼" in ctx.persona_summary
    assert "简洁" in ctx.persona_summary
    assert len(ctx.recent_episodes) == 1
    assert ctx.recent_episodes[0].content["summary"] == "早上聊了高数"
    assert len(ctx.preferences) == 1
    d = ctx.as_dict()
    assert d["last_session_id"] == "s-9"
    assert "早上聊了高数" in d["recent_episode_summaries"]


def test_context_reasoner_course_weather_location() -> None:
    reasoner = ContextReasoner()
    starts = datetime(2026, 10, 28, 14, 0, tzinfo=TZ)
    brief = reasoner.reason(
        [
            ContextCourse(
                course_title="高数",
                location_label="教一-301",
                starts_at=starts,
            ),
            ContextWeather(condition="小雨", temp_c_range="16-18"),
            ContextLocation(place_label="教学楼", coarse_area="校园"),
        ]
    )
    assert isinstance(brief, SituationBrief)
    assert "高数" in brief.course_note
    assert "小雨" in brief.weather_note
    assert "教学楼" in brief.location_note
    assert brief.urgency_hint in ("quiet_preferred", "elevated")
    assert "高数" in brief.headline
    d = brief.as_dict()
    assert d["course_note"]
    assert "带伞" in " ".join(d["suggestions"]) or brief.suggestions


def test_agent_perceive_injects_continuity_and_situation() -> None:
    store = InMemoryStore()
    store.put_persona_continuity(PersonaContinuity(persona_name="鲸鱼", tone="冷静"))
    remember_episode(store, summary="昨日复习", key="ep:y")
    agent = WhaleAgent(router=FixedCardRouter(), memory=store, policy=AlwaysAllowPolicy())
    events = [ContextCourse(course_title="英语", location_label="外语楼")]
    obs = agent.perceive(events)
    assert "continuity" in obs.context
    assert "situation" in obs.context
    assert "鲸鱼" in obs.context["continuity"]["persona_summary"]
    assert "英语" in obs.context["situation"]["course_note"]
    assert agent.last_continuity() is not None
    assert agent.last_situation() is not None
    assert agent.last_situation().course_note


def test_quiet_hours_no_proactive_call() -> None:
    """静默时段：不主动呼叫（ProactiveCallPolicy + QuietHours）。"""
    quiet = QuietHoursPolicy(start=time(22, 0), end=time(7, 0), tz=TZ)
    call_policy = ProactiveCallPolicy(quiet=quiet)
    agent = WhaleAgent(
        router=FixedCallRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=call_policy,
        memory=InMemoryStore(),
    )
    # 注入 context.now = 23:00 Asia/Shanghai
    class NightAgent(WhaleAgent):
        def perceive(self, events):
            obs = super().perceive(events)
            ctx = dict(obs.context)
            ctx["now"] = datetime(2026, 10, 28, 23, 0, tzinfo=TZ)
            ctx["allow_proactive_call"] = True  # 即便显式允许，静默仍拦
            return Observation(
                event_ids=obs.event_ids,
                summary=obs.summary,
                context=ctx,
                observed_at=obs.observed_at,
            )

    night = NightAgent(
        router=FixedCallRouter(),
        policy=AlwaysAllowPolicy(),
        call_policy=call_policy,
        memory=InMemoryStore(),
    )
    decision, receipts = night.step([ContextCourse(course_title="夜自习")])
    assert all(i.kind != "place_call" for i in decision.intents)
    assert receipts == []


def test_busy_foreground_suppresses_show_card() -> None:
    """忙碌时不 show_card 主动提醒。"""
    quiet = QuietHoursPolicy(start=time(22, 0), end=time(7, 0), tz=TZ)
    # 中午：用 context.now 固定在白天
    policy = DisturbancePolicy(quiet=quiet, busy_packages=("com.busy.app",))

    class DayAgent(WhaleAgent):
        def perceive(self, events):
            obs = super().perceive(events)
            ctx = dict(obs.context)
            ctx["now"] = datetime(2026, 10, 28, 12, 0, tzinfo=TZ)
            return Observation(
                event_ids=obs.event_ids,
                summary=obs.summary,
                context=ctx,
                observed_at=obs.observed_at,
            )

    agent = DayAgent(router=FixedCardRouter(), policy=policy, memory=InMemoryStore())
    decision, receipts = agent.step(
        [PhoneForeground(package="com.busy.app", label="Busy")]
    )
    assert decision.suppress is True
    assert decision.intents == ()
    assert receipts == []


def test_urgent_pierces_quiet_and_busy() -> None:
    """紧急 flag 可穿透静默与忙碌。"""
    quiet = QuietHoursPolicy(start=time(22, 0), end=time(7, 0), tz=TZ)
    policy = DisturbancePolicy(quiet=quiet, busy_packages=("com.busy.app",))
    obs = Observation(
        summary="急",
        context={
            "now": datetime(2026, 10, 28, 23, 30, tzinfo=TZ),
            "foreground_package": "com.busy.app",
            "urgent": True,
        },
    )
    decision = Decision(intents=(ShowCard(title="急").to_intent(),), rationale="u")
    assert policy.should_suppress(obs, decision) is False

    # 主动呼叫：urgent 亦放行
    call_policy = ProactiveCallPolicy(quiet=quiet)
    assert call_policy.allows_proactive_call(obs) is True


def test_course_plus_quiet_hours_no_disturb_path() -> None:
    """演示路径：课表 + 安静时段 → 不打扰（不 show_card）。"""
    quiet = QuietHoursPolicy(start=time(22, 0), end=time(7, 0), tz=TZ)
    policy = DisturbancePolicy(quiet=quiet, busy_packages=("com.busy.app",))
    store = InMemoryStore()
    store.put_persona_continuity(PersonaContinuity(persona_name="鲸鱼", tone="简洁"))
    remember_episode(store, summary="昨晚确认了明早高数", key="ep:course")

    class QuietCourseAgent(WhaleAgent):
        def perceive(self, events):
            obs = super().perceive(events)
            ctx = dict(obs.context)
            # 23:15 在静默时段
            ctx["now"] = datetime(2026, 10, 28, 23, 15, tzinfo=TZ)
            return Observation(
                event_ids=obs.event_ids,
                summary=obs.summary,
                context=ctx,
                observed_at=datetime(2026, 10, 28, 23, 15, tzinfo=TZ),
            )

    agent = QuietCourseAgent(
        router=FixedCardRouter(),
        policy=policy,
        memory=store,
    )
    events = [
        ContextCourse(
            course_title="高数",
            location_label="教一-301",
            starts_at=datetime(2026, 10, 29, 8, 0, tzinfo=TZ),
        ),
        ContextWeather(condition="晴", temp_c_range="12-20"),
        ContextLocation(place_label="宿舍", coarse_area="校园"),
    ]
    decision, receipts = agent.step(events)

    # 情境已推理出课表
    sit = agent.last_situation()
    assert sit is not None
    assert "高数" in sit.course_note
    # 连续性可读
    cont = agent.last_continuity()
    assert cont is not None
    assert "鲸鱼" in cont.persona_summary
    assert any("高数" in (r.content.get("summary") or "") for r in cont.recent_episodes)
    # 安静时段 → 抑制主动提醒
    assert decision.suppress is True
    assert decision.intents == ()
    assert receipts == []


def test_quiet_hours_respects_observation_now() -> None:
    policy = QuietHoursPolicy(start=time(22, 0), end=time(7, 0), tz=TZ)
    night = Observation(
        context={"now": datetime(2026, 10, 28, 23, 0, tzinfo=TZ)},
    )
    day = Observation(
        context={"now": datetime(2026, 10, 28, 12, 0, tzinfo=TZ)},
    )
    dummy = Decision(intents=(ShowCard(title="x").to_intent(),))
    assert policy.should_suppress(night, dummy) is True
    assert policy.should_suppress(day, dummy) is False
