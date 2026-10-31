"""PR-17 自主探索/养鱼能力离线测试：InterestTracker + ExplorationManager。

全离线：内容一律由测试手工构造 DiscoveredContent 喂入；时间由注入的假时钟
（now_fn / now）控制；持久化用 tmp_path 下的 SqliteMemoryStore 重开验证。
"""
from datetime import datetime, timedelta, timezone

import pytest

from whale_core.life.discovery import DiscoveredContent, DiscoveryPlanner
from whale_core.life.exploration import (
    BACKOFF_KEY,
    INTERESTS_KEY,
    LAST_AT_KEY,
    DiaryEntry,
    ExplorationManager,
    Interest,
    InterestTracker,
)
from whale_core.life.initiative import ProactiveOutbox
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore

ALLOWED = frozenset({"example.com"})


class Clock:
    """可推进的假时钟（UTC）。"""

    def __init__(self, start=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        return self.t


def make_planner(max_candidates=5):
    return DiscoveryPlanner(allowed_hosts=ALLOWED, max_candidates=max_candidates)


def make_item(item_id="a1", title="AI 领域的新研究", url="https://example.com/a1",
              source_name="ExampleNews", summary="", inspected=False):
    return DiscoveredContent(item_id=item_id, title=title, url=url,
                             source_name=source_name, summary=summary,
                             inspected=inspected)


def make_manager(store=None, clock=None, base_interval_s=100, max_backoff=4):
    store = store or InMemoryStore()
    clock = clock or Clock()
    mgr = ExplorationManager(store, planner=make_planner(), now_fn=clock,
                             base_interval_s=base_interval_s,
                             max_backoff=max_backoff)
    return mgr, store, clock


# ---------------------------------------------------------------- InterestTracker

def test_add_returns_interest_with_default_weight():
    tracker = InterestTracker(InMemoryStore())
    interest = tracker.add("人工智能")
    assert interest == Interest(topic="人工智能", weight=1.0)
    assert tracker.topics() == (interest,)


def test_add_dedup_case_insensitive():
    tracker = InterestTracker(InMemoryStore())
    tracker.add("AI")
    tracker.add("ai")
    tracker.add("Ai")
    topics = tracker.topics()
    assert len(topics) == 1
    assert topics[0].topic == "AI"  # 展示名保留首次登记的大小写


def test_add_custom_weight_and_readd_updates_weight():
    tracker = InterestTracker(InMemoryStore())
    tracker.add("AI", weight=1.5)
    assert tracker.topics()[0].weight == 1.5
    tracker.add("ai", weight=1.8)  # 大小写不敏感：更新而非重复
    topics = tracker.topics()
    assert len(topics) == 1
    assert topics[0].weight == 1.8


def test_add_empty_topic_raises():
    tracker = InterestTracker(InMemoryStore())
    with pytest.raises(ValueError):
        tracker.add("   ")


def test_adjust_clamps_to_max():
    tracker = InterestTracker(InMemoryStore())
    tracker.add("AI", weight=1.5)
    interest = tracker.adjust("ai", 2.0)
    assert interest.weight == 2.0


def test_adjust_clamps_to_min():
    tracker = InterestTracker(InMemoryStore())
    tracker.add("AI", weight=0.2)
    interest = tracker.adjust("AI", -5.0)
    assert interest.weight == 0.0


def test_adjust_unknown_topic_returns_none():
    tracker = InterestTracker(InMemoryStore())
    assert tracker.adjust("不存在", 0.2) is None


def test_remove_case_insensitive():
    tracker = InterestTracker(InMemoryStore())
    tracker.add("Whale")
    assert tracker.remove("whale") is True
    assert tracker.topics() == ()
    assert tracker.remove("whale") is False


def test_topics_sorted_by_weight_desc():
    tracker = InterestTracker(InMemoryStore())
    tracker.add("低", weight=0.5)
    tracker.add("高", weight=1.9)
    tracker.add("中", weight=1.0)
    assert [i.topic for i in tracker.topics()] == ["高", "中", "低"]


def test_as_interest_list_returns_names():
    tracker = InterestTracker(InMemoryStore())
    tracker.add("AI", weight=1.2)
    tracker.add("养鱼", weight=1.5)
    assert tracker.as_interest_list() == ["养鱼", "AI"]


def test_interests_persist_across_sqlite_reopen(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = SqliteMemoryStore(path)
    tracker = InterestTracker(store)
    tracker.add("AI", weight=1.5)
    tracker.adjust("ai", 0.2)
    tracker.add("养鱼")
    store.close()
    reopened = SqliteMemoryStore(path)
    try:
        topics = InterestTracker(reopened).topics()
        assert [(i.topic, i.weight) for i in topics] == [("AI", 1.7), ("养鱼", 1.0)]
        assert reopened.get(INTERESTS_KEY) is not None
    finally:
        reopened.close()


# ------------------------------------------------------------- maybe_explore

def test_no_interests_no_exploration():
    mgr, store, _ = make_manager()
    drafts = mgr.maybe_explore([make_item()])
    assert drafts == ()
    assert store.get(LAST_AT_KEY) is None
    assert store.get("life:draft:discovery:a1") is None


def test_explore_proposes_matching_drafts_into_outbox():
    mgr, store, _ = make_manager()
    mgr.tracker.add("AI")
    drafts = mgr.maybe_explore([make_item(), make_item(item_id="b2", title="AI 养鱼交叉话题",
                                                       url="https://example.com/b2")])
    assert len(drafts) == 2
    assert {d.draft_id for d in drafts} == {"discovery:a1", "discovery:b2"}
    # 草稿进 Outbox：store 里 life:draft: 可见，可经 ProactiveOutbox 读回
    assert store.get("life:draft:discovery:a1") is not None
    outbox = ProactiveOutbox(store)
    assert outbox.get("discovery:a1").evidence == ("https://example.com/a1",)
    assert outbox.ledger("discovery:a1")["status"] == "draft"
    # 真实探索刷新时间戳并记日记
    assert store.get(LAST_AT_KEY) is not None
    day_entries = mgr.diary("2025-01-01")
    assert len(day_entries) == 1
    assert "https://example.com/a1" in day_entries[0].sources


def test_explore_filters_unallowed_host_and_plain_http():
    mgr, store, _ = make_manager()
    mgr.tracker.add("AI")
    items = [
        make_item(item_id="bad1", url="https://evil.example.org/ai"),  # 主机不在白名单
        make_item(item_id="bad2", url="http://example.com/ai"),        # 非 https
        make_item(item_id="ok1", url="https://example.com/ok"),
    ]
    drafts = mgr.maybe_explore(items)
    assert [d.draft_id for d in drafts] == ["discovery:ok1"]
    assert store.get("life:draft:discovery:bad1") is None
    assert store.get("life:draft:discovery:bad2") is None


def test_explore_without_match_still_updates_last_at():
    mgr, store, _ = make_manager()
    mgr.tracker.add("量子计算")
    drafts = mgr.maybe_explore([make_item(title="完全不相关的菜谱")])
    assert drafts == ()
    assert store.get(LAST_AT_KEY) is not None  # 真实探索（零产出）也刷新时间戳


def test_uninspected_item_not_blocked_reason_marks_unchecked():
    mgr, _, _ = make_manager()
    mgr.tracker.add("AI")
    drafts = mgr.maybe_explore([make_item(inspected=False)])
    assert len(drafts) == 1
    assert "已核查内容：False" in drafts[0].reason  # 未核查如实标注，不阻断


def test_inspected_item_reason_marks_checked():
    mgr, _, _ = make_manager()
    mgr.tracker.add("AI")
    drafts = mgr.maybe_explore([make_item(inspected=True)])
    assert "已核查内容：True" in drafts[0].reason


# ------------------------------------------------------------------ backoff

def test_immediate_second_call_gated_by_interval():
    mgr, _, clock = make_manager(base_interval_s=100)
    mgr.tracker.add("AI")
    assert len(mgr.maybe_explore([make_item()])) == 1
    clock.advance(99)
    assert mgr.maybe_explore([make_item(item_id="b2", url="https://example.com/b2")]) == ()


def test_explore_again_after_interval_elapsed():
    mgr, store, clock = make_manager(base_interval_s=100)
    mgr.tracker.add("AI")
    mgr.maybe_explore([make_item()])
    clock.advance(101)
    drafts = mgr.maybe_explore([make_item(item_id="b2", url="https://example.com/b2")])
    assert [d.draft_id for d in drafts] == ["discovery:b2"]
    assert store.get(LAST_AT_KEY) == clock.t.isoformat()


def test_ignored_feedback_doubles_interval():
    mgr, _, clock = make_manager(base_interval_s=100)
    mgr.tracker.add("AI")
    mgr.maybe_explore([make_item()])
    mgr.feedback("AI", "ignored")
    assert mgr.backoff_level() == 1
    clock.advance(150)  # < 100 * 2**1 = 200
    assert mgr.maybe_explore([make_item()]) == ()
    clock.advance(60)  # 累计 210 >= 200
    assert len(mgr.maybe_explore([make_item()])) == 1


def test_ignored_backoff_capped_at_max():
    mgr, store, _ = make_manager(max_backoff=4)
    for _ in range(10):
        mgr.feedback("AI", "ignored")
    assert mgr.backoff_level() == 4
    assert store.get(BACKOFF_KEY) == 4


def test_liked_resets_backoff_and_raises_weight():
    mgr, _, _ = make_manager()
    mgr.tracker.add("AI", weight=1.0)
    mgr.feedback("AI", "ignored")
    mgr.feedback("AI", "ignored")
    assert mgr.backoff_level() == 2
    mgr.feedback("AI", "liked")
    assert mgr.backoff_level() == 0
    assert mgr.tracker.topics()[0].weight == pytest.approx(1.2)


def test_disliked_lowers_weight_clamped_and_resets_backoff():
    mgr, _, _ = make_manager()
    mgr.tracker.add("AI", weight=0.2)
    mgr.feedback("AI", "ignored")
    mgr.feedback("AI", "disliked")
    assert mgr.backoff_level() == 0
    assert mgr.tracker.topics()[0].weight == 0.0  # 0.2 - 0.3 钳制到 0


def test_feedback_rejects_unknown_signal():
    mgr, _, _ = make_manager()
    with pytest.raises(ValueError):
        mgr.feedback("AI", "meh")


# -------------------------------------------------------------------- diary

def test_diary_write_and_read_by_day():
    mgr, _, _ = make_manager()
    mgr.write_diary("第一天的事", now=datetime(2025, 1, 1, 10, tzinfo=timezone.utc))
    mgr.write_diary("第一天第二条", now=datetime(2025, 1, 1, 23, tzinfo=timezone.utc))
    mgr.write_diary("第二天的事", now=datetime(2025, 1, 2, 8, tzinfo=timezone.utc))
    day1 = mgr.diary("2025-01-01")
    assert [e.note for e in day1] == ["第一天的事", "第一天第二条"]
    assert all(isinstance(e, DiaryEntry) and e.day == "2025-01-01" for e in day1)
    assert [e.note for e in mgr.diary("2025-01-02")] == ["第二天的事"]
    assert mgr.diary("2025-01-03") == ()


def test_diary_note_truncated_sources_kept():
    mgr, _, _ = make_manager()
    long_note = "鱼" * 800
    mgr.write_diary(long_note, sources=["https://example.com/x", "https://example.com/y"],
                    now=datetime(2025, 3, 2, tzinfo=timezone.utc))
    (entry,) = mgr.diary("2025-03-02")
    assert len(entry.note) == 500
    assert entry.sources == ("https://example.com/x", "https://example.com/y")


def test_diary_persists_across_sqlite_reopen(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = SqliteMemoryStore(path)
    mgr = ExplorationManager(store, planner=make_planner(), now_fn=Clock())
    mgr.write_diary("持久化的一天", sources=["https://example.com/d"],
                    now=datetime(2025, 5, 6, tzinfo=timezone.utc))
    store.close()
    reopened = SqliteMemoryStore(path)
    try:
        mgr2 = ExplorationManager(reopened, planner=make_planner(), now_fn=Clock())
        (entry,) = mgr2.diary("2025-05-06")
        assert entry.note == "持久化的一天"
        assert entry.sources == ("https://example.com/d",)
    finally:
        reopened.close()
