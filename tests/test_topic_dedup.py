"""life/initiative.py 主题去重（roadmap P2-3）测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox
from whale_core.memory.store import InMemoryStore

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def _draft(draft_id: str, topic: str = "", at: datetime = T0) -> ProactiveDraft:
    return ProactiveDraft(draft_id, "正文", "理由", ("e",), topic=topic, created_at=at)


def _outbox(now_holder: list) -> ProactiveOutbox:
    return ProactiveOutbox(InMemoryStore(), now_fn=lambda: now_holder[0])


class now_holder:  # noqa: N801 - 简易可变时钟
    pass


def _clock():
    holder = [T0]
    return ProactiveOutbox(InMemoryStore(), now_fn=lambda: holder[0]), holder


def test_same_topic_deduped_within_window():
    outbox, holder = _clock()
    assert outbox.propose(_draft("d1", topic="weather"))
    holder[0] += timedelta(hours=1)
    assert not outbox.propose(_draft("d2", topic="weather"))  # 同主题 1h 内被拒
    assert outbox.get("d2") is None  # 未入库


def test_different_topics_not_deduped():
    outbox, _ = _clock()
    assert outbox.propose(_draft("d1", topic="weather"))
    assert outbox.propose(_draft("d2", topic="music"))


def test_topic_deduped_expires_after_window():
    outbox, holder = _clock()
    assert outbox.propose(_draft("d1", topic="weather"))
    holder[0] += timedelta(hours=25)
    assert outbox.propose(_draft("d2", topic="weather"))  # 过窗可再提


def test_empty_topic_never_deduped():
    outbox, _ = _clock()
    assert outbox.propose(_draft("d1"))
    assert outbox.propose(_draft("d2"))


def test_dedup_disabled_with_zero_window():
    outbox = ProactiveOutbox(InMemoryStore(), topic_dedup_hours=0)
    assert outbox.propose(_draft("d1", topic="weather"))
    assert outbox.propose(_draft("d2", topic="weather"))


def test_draft_id_idempotency_still_first():
    outbox, _ = _clock()
    assert outbox.propose(_draft("d1", topic="a"))
    assert not outbox.propose(_draft("d1", topic="b"))  # 同 id 幂等优先


def test_topic_serialization_roundtrip():
    outbox, _ = _clock()
    outbox.propose(_draft("d1", topic="weather:shanghai"))
    restored = outbox.get("d1")
    assert restored is not None and restored.topic == "weather:shanghai"
    # 旧数据无 topic 字段也能恢复
    data = _draft("d2").as_dict()
    assert "topic" in data and data["topic"] == ""


def test_corrupted_topic_index_tolerated():
    outbox, _ = _clock()
    outbox.store.put("life:topics:recent", "garbage")
    assert outbox.propose(_draft("d1", topic="x"))  # 不抛
