import threading
from types import SimpleNamespace

import pytest

from whale_core.devices.action_records import ActionRecords
from whale_core.devices.embedded_wss import EmbeddedWssGateway, _Pending
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord
from whale_core.memory.sqlite_store import SqliteMemoryStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    backend = InMemoryStore() if request.param == "memory" else SqliteMemoryStore(tmp_path / "actions.sqlite3")
    yield backend
    if hasattr(backend, "db"):
        backend.db.close()


def seed(store, state="pending", created=9950):
    store.put("device:action:dev:idem:key", {"action_id": "act", "device_id": "dev",
        "idempotency_key": "idem:key", "created_at": created, "expires_at": 50, "state": state,
        "receipt": {"status": state, "result": {"summary": "first"}}})
    store.put("device:action-id:dev:act", "idem:key")


def test_nested_rollback_and_transaction_delete_match_backends(store):
    store.put("value", {"nested": [1]})
    store.put("other", 2)
    def fail(tx):
        value = tx.get("value")
        value["nested"].append(3)
        tx.put("value", value)
        tx.delete("other")
        raise RuntimeError("rollback")
    with pytest.raises(RuntimeError):
        store.transact(fail)
    assert store.get("value") == {"nested": [1]} and store.get("other") == 2


def test_transaction_results_and_inputs_are_detached(store):
    original = {"nested": [1]}
    store.transact(lambda tx: tx.put("value", original))
    original["nested"].append(2)
    result = store.transact(lambda tx: tx.get("value"))
    result["nested"].append(3)
    assert store.get("value") == {"nested": [1]}


def test_memory_record_nested_edit_rolls_back(store):
    store.put_record(MemoryRecord(kind=MemoryKind.EPISODE, key="memory-record", content={"nested": [1]}))
    def fail(tx):
        record = tx.get_record("memory-record")
        record.content["nested"].append(2)
        tx.put_record(record)
        raise RuntimeError("rollback")
    with pytest.raises(RuntimeError):
        store.transact(fail)
    assert store.get_record("memory-record").content == {"nested": [1]}


def test_concurrent_first_terminal_wins_across_record_managers(store):
    seed(store)
    barrier = threading.Barrier(8)
    failures = []
    def receive(index):
        try:
            barrier.wait(3)
            ActionRecords(store).receive("dev", {"action_id": "act",
                "status": "succeeded" if index % 2 else "failed", "result": {"summary": str(index)}}, 10000)
        except BaseException as exc:
            failures.append(exc)
    threads = [threading.Thread(target=receive, args=(index,)) for index in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert not failures and not any(t.is_alive() for t in threads)
    result = store.get("device:action:dev:idem:key")
    assert result["state"] == result["receipt"]["status"]
    assert result["state"] in ("succeeded", "failed")
    assert len(store.get("device:receipts:dev:act")) == 8


def test_retention_rechecks_state_after_live_receipt(store):
    seed(store)
    entered, resume = threading.Event(), threading.Event()
    class DelayedStore:
        def __getattr__(self, key): return getattr(store, key)
        def transact(self, fn):
            if threading.current_thread().name == "retention-race" and not entered.is_set():
                entered.set()
                assert resume.wait(3)
            return store.transact(fn)
    stats = []
    worker = threading.Thread(name="retention-race", target=lambda: stats.append(
        ActionRecords(DelayedStore()).sweep(10000, 1000, 100, 10)))
    worker.start()
    try:
        assert entered.wait(3)
        ActionRecords(store).receive("dev", {"action_id": "act", "status": "succeeded"}, 10000)
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert store.get("device:action:dev:idem:key")["state"] == "succeeded"
    assert stats[0]["marked_unknown"] == 0


def test_estimated_unknown_accepts_real_terminal_and_purge_removes_indexes(store):
    seed(store)
    records = ActionRecords(store)
    assert records.sweep(10000, 1000, 100, 10)["marked_unknown"] == 1
    assert store.get("device:action:dev:idem:key")["outcome_source"] == "retention_timeout"
    records.receive("dev", {"action_id": "act", "status": "succeeded"}, 10001)
    assert "outcome_source" not in store.get("device:action:dev:idem:key")
    assert records.sweep(12000, 1000, 100, 1)["deleted"] == 1
    assert all(store.get(key) is None for key in (
        "device:action:dev:idem:key", "device:action-id:dev:act", "device:receipts:dev:act"))


def test_waiter_receives_persisted_terminal_instead_of_conflicting_packet(store):
    seed(store, state="succeeded")
    gateway = object.__new__(EmbeddedWssGateway)
    gateway.store, gateway.lock, gateway._log_event = store, threading.RLock(), None
    pending = _Pending("dev", "action.receipt")
    gateway.pending = {"act": pending}
    gateway._on_action_receipt(SimpleNamespace(device_id="dev"), {"payload": {
        "action_id": "act", "status": "failed", "error_code": "internal"}})
    assert pending.event.is_set()
    assert pending.result["status"] == "succeeded"
    assert pending.result["result"]["summary"] == "first"
