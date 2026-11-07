from datetime import datetime, timezone

from whale_core.life.discovery import DiscoveryPlanner
from whale_core.life.exploration import ExplorationManager
from whale_core.life.exploration_chat import (
    build_exploration_context, complete_recommendation,
    maybe_remember_offer, resolve_pending_recommendation,
)
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox
from whale_core.memory.store import InMemoryStore


def test_diary_context_can_answer_what_was_seen_yesterday():
    store = InMemoryStore()
    manager = ExplorationManager(
        store, planner=DiscoveryPlanner(allowed_hosts=frozenset({"www.bilibili.com"})),
        timezone_name="Asia/Shanghai")
    now = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
    manager.write_diary(
        "实际读取 1 个公开视频页面；没有播放视频本体。",
        sources=("https://www.bilibili.com/video/BV1Abc123/",),
        items=[{"title": "FPGA 入门", "url": "https://www.bilibili.com/video/BV1Abc123/",
                "source_name": "哔哩哔哩", "summary": "介绍 FPGA", "content_type": "video",
                "inspected": True, "inspection_method": "public_page_description"}],
        now=datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc))
    text, _ = build_exploration_context(store, "你昨天刷视频看了什么？", now=now)
    assert "FPGA 入门" in text
    assert "视频本体未播放" in text


def test_offer_is_bound_to_conversation_and_only_explicit_yes_resolves():
    store = InMemoryStore()
    draft = ProactiveDraft(
        draft_id="discovery:BV1Abc123", text="发现 B 站视频《FPGA 入门》。要看看吗？",
        reason="interest match", evidence=("https://www.bilibili.com/video/BV1Abc123/",),
        topic="FPGA", channel="hud")
    ProactiveOutbox(store).propose(draft)
    candidates = [store.get("life:draft:" + draft.draft_id)]
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    assert maybe_remember_offer(store, "glasses:s1",
                                "发现 B 站视频《FPGA 入门》。要看看吗？",
                                candidates, now=now)
    assert resolve_pending_recommendation(
        store, "glasses:other", "要", now=now) is None
    assert resolve_pending_recommendation(
        store, "glasses:s1", "嗯，先等等", now=now) is None
    decision = resolve_pending_recommendation(store, "glasses:s1", "要", now=now)
    assert decision and decision["decision"] == "accept"
    assert decision["draft"]["draft_id"] == draft.draft_id
    complete_recommendation(store, "glasses:s1", decision["draft"],
                            outcome="accepted", now=now)
    assert resolve_pending_recommendation(store, "glasses:s1", "要", now=now) is None
    assert store.get("life:ledger:" + draft.draft_id)["status"] == "accepted"


def test_negative_reply_closes_offer_without_sending():
    store = InMemoryStore()
    draft = ProactiveDraft(
        draft_id="discovery:BV1Abc123", text="《FPGA 入门》要看看吗？",
        reason="interest match", evidence=("https://www.bilibili.com/video/BV1Abc123/",),
        topic="FPGA", channel="hud")
    ProactiveOutbox(store).propose(draft)
    candidate = store.get("life:draft:" + draft.draft_id)
    maybe_remember_offer(store, "glasses:s1", draft.text, [candidate])
    decision = resolve_pending_recommendation(store, "glasses:s1", "不要")
    assert decision and decision["decision"] == "reject"
    complete_recommendation(store, "glasses:s1", decision["draft"], outcome="rejected")
    assert store.get("life:ledger:" + draft.draft_id)["status"] == "closed"
