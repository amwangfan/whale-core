"""Deterministic interleavings at provider awaits and receipt transactions."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.notifications import DeliveryStatus, NotificationGateway
from whale_core.notifications.delivery_receipts import DeliveryReceipts
from whale_core.notifications.delivery_queue import DeliveryQueue

DEVICE = "a" * 32
NOW = 1_794_052_800_000


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    value = InMemoryStore() if request.param == "memory" else SqliteMemoryStore(tmp_path / "notify.db")
    yield value
    if hasattr(value, "db"):
        value.db.close()


class Provider:
    name = "whale_wss"
    available = True

    def __init__(self, callback=None):
        self.callback = callback
        self.calls = 0

    def is_online(self, device):
        return True

    async def send(self, device, event):
        self.calls += 1
        if self.callback:
            await self.callback(event)
        return "delivery-1"

    async def revoke(self, *args, **kwargs):
        return False


def payload(record, status="opened"):
    return dict(device_id=DEVICE, notification_id=record.notification_id,
                event_id=record.event_id, status=status, received_at=NOW + 1,
                action_id="open", submitted_at=NOW + 2)


@pytest.mark.parametrize("status", ["displayed", "opened", "dismissed", "failed"])
def test_receipt_during_send_survives_late_provider_completion(store, status):
    async def receipt(event):
        record = gateway.get_delivery(DEVICE, event.notification_id)
        gateway.handle_receipt(payload(record, status))

    provider = Provider(receipt)
    gateway = NotificationGateway(store, providers=[provider], now_fn=lambda: NOW)
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    record = gateway.get_delivery(DEVICE, nid)
    assert record.status.value == status
    assert record.attempts == 1
    assert gateway.queue.pending_events(DEVICE) == []


def test_terminal_receipt_during_failed_send_stops_fallback(store):
    async def receipt_then_fail(event):
        gateway.handle_receipt(payload(gateway.get_delivery(DEVICE, event.notification_id)))
        raise RuntimeError("provider failed after receipt")

    first = Provider(receipt_then_fail)
    fallback = Provider()
    fallback.name = "fallback"
    gateway = NotificationGateway(store, providers=[first, fallback], now_fn=lambda: NOW)
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    assert gateway.get_delivery(DEVICE, nid).status is DeliveryStatus.OPENED
    assert fallback.calls == 0


def test_revoke_during_send_is_not_overwritten_or_replayed(store):
    async def revoke(event):
        await gateway.revoke(DEVICE, event.notification_id)

    provider = Provider(revoke)
    gateway = NotificationGateway(store, providers=[provider], now_fn=lambda: NOW)
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    record = gateway.get_delivery(DEVICE, nid)
    assert record.revoked
    assert asyncio.run(gateway.handle_sync(DEVICE)) == 0
    assert provider.calls == 1


@pytest.mark.parametrize("receipt_first", [False, True])
def test_action_handler_claim_is_once_even_after_action_receipt(store, receipt_first):
    calls = []
    gateway = NotificationGateway(store, providers=[Provider()], now_fn=lambda: NOW,
                                  action_handler=lambda **kw: calls.append(kw))
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    record = gateway.get_delivery(DEVICE, nid)
    if receipt_first:
        gateway.handle_receipt(payload(record, "action_submitted"))
    # Different handler/queue instances must share the persisted claim.
    other = DeliveryReceipts(DeliveryQueue(store), action_handler=lambda **kw: calls.append(kw))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: (gateway.receipts if i % 2 else other).handle_action(payload(record)), range(24)))
    assert len(calls) == 1
    assert gateway.get_delivery(DEVICE, nid).status is DeliveryStatus.ACTION_SUBMITTED


@pytest.mark.parametrize("status", ["opened", "dismissed", "expired", "failed"])
def test_action_after_other_terminal_state_does_not_call_handler(store, status):
    calls = []
    gateway = NotificationGateway(store, providers=[Provider()], now_fn=lambda: NOW,
                                  action_handler=lambda **kw: calls.append(kw))
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    record = gateway.get_delivery(DEVICE, nid)
    gateway.handle_receipt(payload(record, status))
    gateway.handle_action(payload(record))
    assert calls == []


def test_terminal_notification_is_not_dispatched_again(store):
    provider = Provider()
    gateway = NotificationGateway(store, providers=[provider], now_fn=lambda: NOW)
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    event = gateway.queue.pending_events(DEVICE)[0]
    gateway.handle_receipt(payload(gateway.get_delivery(DEVICE, nid)))
    record = asyncio.run(gateway.router.dispatch(event))
    assert record.status is DeliveryStatus.OPENED
    assert provider.calls == 1


def test_action_claim_serializes_across_sqlite_connections(tmp_path):
    first = SqliteMemoryStore(tmp_path / "shared.db")
    second = SqliteMemoryStore(tmp_path / "shared.db")
    calls = []
    try:
        gateway = NotificationGateway(first, providers=[Provider()], now_fn=lambda: NOW,
                                      action_handler=lambda **kw: calls.append(kw))
        nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
        record = gateway.get_delivery(DEVICE, nid)
        other = DeliveryReceipts(DeliveryQueue(second), action_handler=lambda **kw: calls.append(kw))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: (gateway.receipts if i % 2 else other).handle_action(payload(record)), range(24)))
        assert len(calls) == 1
        assert second.get(f"notify:delivery:{DEVICE}:{nid}")["action_id"] == "open"
    finally:
        first.db.close()
        second.db.close()


def test_handler_failure_is_not_retried_on_duplicate_action(store):
    calls = []

    def fail(**kwargs):
        # Runs after the receipt transaction; a fresh transaction is allowed.
        store.transact(lambda tx: tx.put("handler-observed", True))
        calls.append(kwargs)
        raise RuntimeError("business handler failed")

    gateway = NotificationGateway(store, providers=[Provider()], now_fn=lambda: NOW,
                                  action_handler=fail)
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    record = gateway.get_delivery(DEVICE, nid)
    gateway.handle_action(payload(record))
    gateway.handle_action(payload(record))
    assert len(calls) == 1
    assert store.get("handler-observed") is True


def test_revoked_action_never_reaches_handler(store):
    calls = []
    gateway = NotificationGateway(store, providers=[Provider()], now_fn=lambda: NOW,
                                  action_handler=lambda **kw: calls.append(kw))
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    record = gateway.get_delivery(DEVICE, nid)
    asyncio.run(gateway.revoke(DEVICE, nid))
    gateway.handle_action(payload(record))
    assert calls == []
    assert gateway.get_delivery(DEVICE, nid).revoked


def test_stale_sync_snapshot_does_not_count_revoked_delivery(store, monkeypatch):
    provider = Provider()
    gateway = NotificationGateway(store, providers=[provider], now_fn=lambda: NOW)
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    event = gateway.queue.pending_events(DEVICE)[0]
    asyncio.run(gateway.revoke(DEVICE, nid))
    # Emulate a snapshot captured before revoke removed its queue entry.
    monkeypatch.setattr(gateway.queue, "replay_since", lambda *args: [event])
    assert asyncio.run(gateway.handle_sync(DEVICE)) == 0
    assert provider.calls == 1


def test_expiry_queue_and_ledger_rollback_together(store, monkeypatch):
    gateway = NotificationGateway(store, now_fn=lambda: NOW)
    nid = asyncio.run(gateway.notify(DEVICE, "chat.message", "hello", ""))
    record = gateway.get_delivery(DEVICE, nid)
    original = store.transact

    class FailWrite:
        def __init__(self, tx):
            self.get, self.delete = tx.get, tx.delete

        def put(self, key, value):
            raise RuntimeError("simulated ledger write failure")

    monkeypatch.setattr(store, "transact", lambda fn: original(lambda tx: fn(FailWrite(tx))))
    with pytest.raises(RuntimeError, match="ledger write"):
        gateway.sweep_expired(now=NOW + 10**9)
    assert gateway.queue.get_entry(DEVICE, record.event_id) is not None
    assert gateway.get_delivery(DEVICE, nid).status is DeliveryStatus.QUEUED
