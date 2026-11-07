from datetime import datetime, timedelta, timezone

from whale_core.memory.governance import (
    confirm_fact, expire_pending_facts, propose_fact, supersede,
)
from whale_core.memory.store import InMemoryStore, MemoryKind


def test_pending_facts_expire_and_keep_their_memory_space():
    store = InMemoryStore()
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    key = propose_fact(store, "用户正在准备模拟电路考试",
                       source="conversation", evidence_id="message:1",
                       ttl_days=1, space_id="owner-working")
    record = store.get_record(key)
    assert record.space_id == "owner-working"
    assert expire_pending_facts(store, now=now + timedelta(days=2)) == 1
    assert store.get_record(key) is None


def test_confirm_and_correct_preserve_memory_space_acl():
    store = InMemoryStore()
    key = propose_fact(store, "用户喜欢钢琴",
                       source="conversation", evidence_id="message:2",
                       space_id="member-private")
    assert confirm_fact(store, key)
    confirmed = store.list_by_kind(MemoryKind.PREFERENCE,
                                   space_ids=("member-private",))
    assert len(confirmed) == 1
    assert store.list_by_kind(MemoryKind.PREFERENCE, space_ids=("other-space",)) == []

    new_key = supersede(store, confirmed[0].key, "用户正在学习钢琴",
                        source="owner-correction", evidence_id="message:3")
    updated = store.get_record(new_key)
    assert updated.space_id == "member-private"
    assert "superseded" in store.get_record(confirmed[0].key).tags
