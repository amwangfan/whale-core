"""History updates must preserve independent concurrent user actions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.notifications.models import NotificationEvent
from whale_core.notifications.services.notification_history import NotificationHistory, history_key

DEVICE = "a" * 32


def event(index):
    return NotificationEvent(event_id=f"evt_{index:026d}", notification_id=f"notif_{index:026d}",
                             device_id=DEVICE, category="chat.message", title=str(index), body="",
                             priority="normal", created_at=index, expires_at=10000)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    value = InMemoryStore() if request.param == "memory" else SqliteMemoryStore(tmp_path / "history.db")
    yield value
    if hasattr(value, "db"):
        value.db.close()


def race_reads(store, monkeypatch, operations):
    """Force old nontransactional readers to observe the same stale snapshot."""
    original = store.get
    barrier = Barrier(2)

    def get(key):
        result = original(key)
        if key == history_key(DEVICE):
            barrier.wait(timeout=5)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "get", get)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(operation) for operation in operations]
            for future in futures:
                future.result(timeout=10)


def test_concurrent_adds_do_not_lose_history(store, monkeypatch):
    first, second = NotificationHistory(store), NotificationHistory(store)
    race_reads(store, monkeypatch, [lambda: first.add(event(1)), lambda: second.add(event(2))])
    assert {row["notification_id"] for row in first.list(DEVICE)} == {
        event(1).notification_id, event(2).notification_id}


@pytest.mark.parametrize("all_read", [False, True])
def test_read_and_add_both_survive(store, monkeypatch, all_read):
    first, second = NotificationHistory(store), NotificationHistory(store)
    first.add(event(1))
    mark = (lambda: first.mark_all_read(DEVICE)) if all_read else (
        lambda: first.mark_read(DEVICE, event(1).notification_id))
    race_reads(store, monkeypatch, [mark, lambda: second.add(event(2))])
    assert first.get(DEVICE, event(1).notification_id)["read"] is True
    assert first.get(DEVICE, event(2).notification_id) is not None


def test_duplicate_event_does_not_reset_explicit_read(store):
    history = NotificationHistory(store)
    history.add(event(1))
    history.mark_read(DEVICE, event(1).notification_id)
    history.add(event(1))
    assert history.unread_count(DEVICE) == 0
    assert len(history.list(DEVICE)) == 1
    # A new event replacing the same notification is new content, hence unread.
    history.add(replace(event(1), event_id=event(2).event_id, body="updated"))
    assert history.unread_count(DEVICE) == 1


def test_read_count_is_atomic_across_history_instances(store, monkeypatch):
    first, second = NotificationHistory(store), NotificationHistory(store)
    first.add(event(1))
    counts = []
    race_reads(store, monkeypatch, [lambda: counts.append(first.mark_all_read(DEVICE)),
                                  lambda: counts.append(second.mark_all_read(DEVICE))])
    assert sorted(counts) == [0, 1]


def test_sqlite_connections_share_history_update_transaction(tmp_path):
    first = SqliteMemoryStore(tmp_path / "shared.db")
    second = SqliteMemoryStore(tmp_path / "shared.db")
    try:
        histories = [NotificationHistory(first), NotificationHistory(second)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: histories[i % 2].add(event(i)), range(40)))
        assert len(histories[0].list(DEVICE)) == 40
    finally:
        first.db.close()
        second.db.close()


@pytest.mark.parametrize("limit", [True, 0.5, 1.5, 0, -1])
def test_history_capacity_requires_positive_integer(limit):
    with pytest.raises(ValueError):
        NotificationHistory(InMemoryStore(), limit=limit)
