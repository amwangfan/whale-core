"""Queue scans have a prefix, a per-tick budget, and transactional progress."""

from dataclasses import replace

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.notifications.delivery_queue import DeliveryQueue, delivery_key, queue_key
from whale_core.notifications.models import DeliveryRecord, DeliveryStatus, NotificationEvent

DEVICE = "a" * 32
NOW = 1000


def event(index, *, expires_at=2000):
    return NotificationEvent(event_id=f"evt_{index:026d}", notification_id=f"notif_{index:026d}",
                             device_id=DEVICE, category="chat.message", title=str(index), body="",
                             priority="normal", created_at=1, expires_at=expires_at)


def record(index, *, terminal=False):
    e = event(index)
    return DeliveryRecord(delivery_id="", event_id=e.event_id, notification_id=e.notification_id,
                          device_id=DEVICE, provider="", status=(DeliveryStatus.OPENED if terminal
                          else DeliveryStatus.QUEUED), created_at=1, updated_at=1)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    value = InMemoryStore() if request.param == "memory" else SqliteMemoryStore(tmp_path / "queue.db")
    yield value
    if hasattr(value, "db"):
        value.db.close()


def forbid_all_keys(monkeypatch, store):
    def fail():
        raise AssertionError("notification path enumerated the entire store")
    monkeypatch.setattr(store, "keys", fail)


def test_capacity_and_queries_do_not_enumerate_unrelated_keys(store, monkeypatch):
    for i in range(50):
        store.put(f"unrelated:{i}", "content")
    forbid_all_keys(monkeypatch, store)
    queue = DeliveryQueue(store, max_per_device=2)
    for i in range(5):
        queue.enqueue(event(i))
    assert [e.event_id for e in queue.pending_events(DEVICE)] == [event(3).event_id, event(4).event_id]
    assert len(queue.replay_since(DEVICE, now=NOW)) == 2


def test_pending_events_skip_corrupt_or_mismatched_payload(store):
    queue = DeliveryQueue(store)
    queue.enqueue(event(1))
    store.put(queue_key(DEVICE, event(2).event_id), {"event": {"event_id": "bad"}})
    foreign = replace(event(3), device_id="b" * 32)
    store.put(queue_key(DEVICE, foreign.event_id), {"event": foreign.to_payload()})
    assert queue.pending_events(DEVICE) == [event(1)]


def test_expiry_budget_advances_past_unexpired_items(store, monkeypatch):
    queue = DeliveryQueue(store)
    for i in range(1, 4):
        queue.enqueue(event(i, expires_at=10 if i == 3 else 2000))
    forbid_all_keys(monkeypatch, store)
    assert queue.dequeue_expired(now=NOW, max_scanned=1) == []
    assert queue.dequeue_expired(now=NOW, max_scanned=1) == []
    assert queue.dequeue_expired(now=NOW, max_scanned=1) == [(DEVICE, event(3).event_id)]
    # Reach EOF, then wrap so a new key below the previous cursor is visited.
    assert queue.dequeue_expired(now=NOW, max_scanned=1) == []
    queue.enqueue(event(0, expires_at=10))
    assert queue.dequeue_expired(now=NOW, max_scanned=1) == [(DEVICE, event(0).event_id)]


def test_delivery_budget_skips_live_and_corrupt_records(store, monkeypatch):
    queue = DeliveryQueue(store)
    queue.put_delivery(record(1))
    store.put(delivery_key(DEVICE, event(2).notification_id), {"status": "corrupt"})
    queue.put_delivery(record(3, terminal=True))
    forbid_all_keys(monkeypatch, store)
    assert queue.sweep_deliveries(now=NOW, max_age_ms=10, max_scanned=1) == 0
    assert queue.sweep_deliveries(now=NOW, max_age_ms=10, max_scanned=1) == 0
    assert queue.sweep_deliveries(now=NOW, max_age_ms=10, max_scanned=1) == 1
    assert queue.get_delivery(DEVICE, event(1).notification_id) is not None


def test_scan_budget_limits_values_read(store, monkeypatch):
    queue = DeliveryQueue(store)
    for i in range(5):
        queue.enqueue(event(i))
    reads = []
    original = store.get
    monkeypatch.setattr(store, "get", lambda key: (reads.append(key), original(key))[1])
    assert queue.dequeue_expired(now=NOW, max_scanned=2) == []
    assert len(reads) == 2


def test_capacity_failure_rolls_back_new_admission(store, monkeypatch):
    queue = DeliveryQueue(store, max_per_device=1)
    queue.enqueue(event(1))
    original = store.transact

    class FailDelete:
        def __init__(self, tx):
            self.get, self.put = tx.get, tx.put

        def delete(self, key):
            raise RuntimeError("eviction failed")

    monkeypatch.setattr(store, "transact", lambda fn: original(lambda tx: fn(FailDelete(tx))))
    with pytest.raises(RuntimeError, match="eviction"):
        queue.enqueue(event(2))
    assert queue.get_entry(DEVICE, event(1).event_id) is not None
    assert queue.get_entry(DEVICE, event(2).event_id) is None


def test_failed_expiry_does_not_advance_past_failed_key(store, monkeypatch):
    queue = DeliveryQueue(store)
    queue.enqueue(event(1))
    queue.enqueue(event(2, expires_at=10))
    assert queue.dequeue_expired(now=NOW, max_scanned=1) == []
    with monkeypatch.context() as patch:
        patch.setattr(store, "transact", lambda fn: (_ for _ in ()).throw(RuntimeError("write failed")))
        with pytest.raises(RuntimeError, match="write failed"):
            queue.dequeue_expired(now=NOW, max_scanned=1)
    assert queue.dequeue_expired(now=NOW, max_scanned=1) == [(DEVICE, event(2).event_id)]


@pytest.mark.parametrize("bad", [True, 0.5, 1.5, 0, -1])
def test_queue_capacity_requires_positive_integer(bad):
    with pytest.raises(ValueError):
        DeliveryQueue(InMemoryStore(), max_per_device=bad)


@pytest.mark.parametrize("method", ["dequeue_expired", "sweep_deliveries"])
def test_zero_budget_performs_no_scan(method, monkeypatch):
    store = InMemoryStore()
    queue = DeliveryQueue(store)
    monkeypatch.setattr(store, "scan_keys", lambda *a, **kw: pytest.fail("disabled sweep scanned"))
    expected = [] if method == "dequeue_expired" else 0
    assert getattr(queue, method)(max_scanned=0) == expected
    assert getattr(queue, method)(max_deletes=0) == expected


def test_queries_eviction_and_expiry_cross_page_boundaries(store, monkeypatch):
    monkeypatch.setattr("whale_core.notifications.delivery_queue.SCAN_PAGE_SIZE", 2)
    queue = DeliveryQueue(store, max_per_device=10)
    for i in range(5):
        queue.enqueue(event(i, expires_at=10))
    assert len(queue.pending_events(DEVICE)) == 5
    assert len(queue.dequeue_expired(now=NOW, max_scanned=3)) == 3
    assert len(queue.dequeue_expired(now=NOW, max_scanned=3)) == 2
    for i in range(5):
        queue.enqueue(event(i))
    small = DeliveryQueue(store, max_per_device=2)
    small.enqueue(event(5))
    assert [e.event_id for e in small.pending_events(DEVICE)] == [event(4).event_id, event(5).event_id]


def test_legacy_store_without_prefix_scan_remains_supported(store):
    class Legacy:
        def __init__(self):
            self.get, self.put, self.keys, self.transact = store.get, store.put, store.keys, store.transact

    queue = DeliveryQueue(Legacy(), max_per_device=2)
    for i in range(4):
        queue.enqueue(event(i, expires_at=10))
    assert len(queue.pending_events(DEVICE)) == 2
    assert len(queue.dequeue_expired(now=NOW, max_scanned=1)) == 1


def test_corrupt_ledger_does_not_block_expired_queue_item(store):
    queue = DeliveryQueue(store)
    queue.enqueue(event(1, expires_at=10))
    key = delivery_key(DEVICE, event(1).notification_id)
    store.put(key, {"status": "corrupt"})
    assert queue.dequeue_expired(now=NOW) == [(DEVICE, event(1).event_id)]
    assert store.get(key) == {"status": "corrupt"}  # do not invent a replacement ledger


def test_retention_rechecks_record_before_deleting(store, monkeypatch):
    queue = DeliveryQueue(store)
    old = record(1, terminal=True)
    queue.put_delivery(old)
    original = store.transact
    refreshed = False

    def transact(fn):
        nonlocal refreshed
        if not refreshed:
            refreshed = True
            queue.put_delivery(replace(old, updated_at=NOW))
        return original(fn)

    monkeypatch.setattr(store, "transact", transact)
    assert queue.sweep_deliveries(now=NOW, max_age_ms=10) == 0
    assert queue.get_delivery(DEVICE, old.notification_id).updated_at == NOW


@pytest.mark.parametrize("bad", [-1, True, 0.5])
@pytest.mark.parametrize("method", ["dequeue_expired", "sweep_deliveries"])
def test_invalid_scan_budget_is_rejected(method, bad):
    with pytest.raises(ValueError):
        getattr(DeliveryQueue(InMemoryStore()), method)(max_scanned=bad)
