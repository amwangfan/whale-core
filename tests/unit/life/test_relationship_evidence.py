"""issue #41：关系证据可解释化（衰减/置信度/纠正）与结构化回复计划测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_core.life.conversation_context import (
    ConversationKind,
    ConversationRef,
    MemorySpaceType,
    Principal,
    ResolvedContext,
)
from whale_core.life.relationship import (
    FAMILIAR_MIN_INTERACTIONS,
    RelationshipResolver,
)
from whale_core.life.response_planner import (
    PLANNING_HINT,
    derive_plan_guidance,
    plan_reply,
)
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord
from whale_core.memory.timeline import GroupTimelineStore

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
GROUP = "group:500"
UID = "u-m1"
PRINCIPAL_ID = f"paired:qq:{UID}"


def _group_ctx(store, space_ids=()):
    conv = ConversationRef("qq", ConversationKind.QQ_GROUP, GROUP)
    return ResolvedContext(
        conversation=conv,
        principal=Principal(PRINCIPAL_ID, False, False, "test"),
        allowed_memory_spaces=tuple(space_ids),
        persona_id=None)


def _timeline_with(store, days_list):
    """写入指定天数前的群消息（sender=UID）；放宽保留窗以容纳旧消息。"""
    tl = GroupTimelineStore(store, max_age_s=365 * 86400)
    key = f"timeline:{GROUP}"
    entries = []
    for i, days_ago in enumerate(days_list):
        entries.append({
            "message_id": f"m{i}", "sender_id": UID, "sender_uid": "",
            "text": "hi", "send_time": "", "mentions": [], "reply_to": [],
            "is_self": False,
            "recorded_at": (NOW - timedelta(days=days_ago)).isoformat(),
        })
    store.put(key, entries)
    return tl


class TestRecencyDecay:
    def test_old_interactions_decay_below_threshold(self):
        store = InMemoryStore()
        # 9 次互动但全是 60 天前：权重 0.2 → 1.8 分 < 门槛 8
        tl = _timeline_with(store, [60] * 9)
        res = RelationshipResolver(store, timeline=tl, now_fn=lambda: NOW)
        st = res.state_for(_group_ctx(store), conversation_id=GROUP)
        assert st.interaction_count == 9
        assert st.interaction_score == 1.8
        assert st.familiarity == "acquainted"  # 不因总数升亲密
        assert any("span_days:60" in e for e in st.evidence)

    def test_recent_interactions_full_weight(self):
        store = InMemoryStore()
        tl = _timeline_with(store, [1, 2, 3, 4, 5, 6, 0, 1])
        res = RelationshipResolver(store, timeline=tl, now_fn=lambda: NOW)
        st = res.state_for(_group_ctx(store), conversation_id=GROUP)
        assert st.interaction_score == 8.0
        assert st.recent_7d == 8
        assert st.familiarity == "familiar"
        assert st.confidence > 0

    def test_stranger_zero_confidence(self):
        store = InMemoryStore()
        tl = _timeline_with(store, [])
        res = RelationshipResolver(store, timeline=tl, now_fn=lambda: NOW)
        st = res.state_for(_group_ctx(store), conversation_id=GROUP)
        assert st.familiarity == "stranger"
        assert st.confidence == 0.0


class TestCorrectionsEvidence:
    def test_corrections_counted_and_facts_exclude_superseded(self):
        store = InMemoryStore()
        spaces = SpaceStore(store)
        ref = spaces.create(MemorySpaceType.MEMBER_PRIVATE, name="m",
                            owner=PRINCIPAL_ID)
        now = datetime.now(timezone.utc)
        store.put_record(MemoryRecord(
            kind=MemoryKind.PREFERENCE, key="k1",
            content={"summary": "我喜欢早睡"}, tags=("member",),
            space_id=ref.space_id, created_at=now))
        store.put_record(MemoryRecord(
            kind=MemoryKind.PREFERENCE, key="k2",
            content={"summary": "我讨厌香菜（已纠正）"},
            tags=("member", "superseded"), space_id=ref.space_id,
            created_at=now))
        ref2 = spaces.ref(ref.space_id)
        ctx = _group_ctx(store, space_ids=[ref2])
        res = RelationshipResolver(store, now_fn=lambda: NOW)
        st = res.state_for(ctx, conversation_id=GROUP)
        assert st.corrections == 1
        assert any("user_corrections:1" in e for e in st.evidence)
        # 被纠正的事实退出自述（可生效的纠正）
        assert all("香菜" not in f for f in st.self_facts)
        assert any("早睡" in f for f in st.self_facts)


class TestPlanGuidance:
    def test_matrix(self):
        assert derive_plan_guidance(anxious=True) == {
            "tone_target": "calm", "length_target": "short"}
        assert derive_plan_guidance(familiarity="stranger", is_group=True) == {
            "tone_target": "neutral", "length_target": "short"}
        assert derive_plan_guidance(familiarity="familiar") == {
            "tone_target": "warm", "length_target": ""}

    def test_plan_action_mapping(self):
        assert plan_reply("", is_group=False, max_segments=2).action == "observe"
        assert plan_reply("[[沉默]]", is_group=False,
                          max_segments=2).action == "observe"
        p = plan_reply("先收下。\n[[稍后补充:等任务结果]]",
                       is_group=False, max_segments=2)
        assert p.action == "follow_up" and p.follow_up
        p2 = plan_reply("好的。", is_group=False, max_segments=2,
                        tone_target="warm", length_target="short")
        assert p2.action == "reply"
        assert p2.tone_target == "warm" and p2.length_target == "short"

    def test_planning_hint_anti_template(self):
        assert "不要每轮都提问" in PLANNING_HINT
        assert "撒娇" in PLANNING_HINT
