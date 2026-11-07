"""Receipt result reconciliation survives late delivery and Core restarts."""
from __future__ import annotations

import threading

import pytest

from whale_core.devices.gateway import ConnectionState, DeviceGateway, _Pending
from whale_core.devices.registry import now_ms
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore


def _gateway(store):
    # Exercise durable receipt handling without opening a listener.
    gateway = DeviceGateway.__new__(DeviceGateway)
    gateway.store = store
    gateway.lock = threading.RLock()
    gateway.pending = {}
    gateway._log_event = None
    return gateway


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("state", ["executed", "failed", "unknown"])
def test_terminal_receipt_result_survives_reconciliation(tmp_path, backend, state):
    path = tmp_path / "receipts.sqlite3"
    store = SqliteMemoryStore(path) if backend == "sqlite" else InMemoryStore()
    device_id, action_id, idem = "device-test", "action-test", "idem-receipt-test"
    key = f"device:action:{device_id}:{idem}"
    store.put(f"device:action-id:{device_id}:{action_id}", idem)
    store.put(key, {"device_id": device_id, "action_id": action_id,
                    "state": "accepted", "expires_at": 1})
    connection = ConnectionState(None)
    connection.device_id = device_id
    payload = {"action_id": action_id, "state": state, "executed_at": 123,
               "provider_ref": "degraded:notification:test",
               "result": {"summary": "device outcome", "output_ref": "opaque-123"}}
    if state == "failed":
        payload["error_code"] = "unavailable"
    expected = {k: v for k, v in payload.items() if k != "action_id"}
    gateway = _gateway(store)
    try:
        # No pending HTTP waiter: the late receipt must still be queryable.
        gateway._on_action_receipt(connection, {"payload": payload})
        assert gateway.action_status(device_id, idem)["receipt"] == expected
        if backend == "sqlite":
            store.close()
            store = SqliteMemoryStore(path)
        restarted = _gateway(store)
        assert restarted.action_status(device_id, idem)["receipt"] == expected
        # Conflicting/partial duplicate terminal frames cannot erase the result.
        waiter = _Pending(device_id, 'action.receipt')
        restarted.pending[action_id] = waiter
        restarted._on_action_receipt(connection, {"payload": {
            "action_id": action_id, "state": "failed", "error_code": "internal"}})
        assert restarted.action_status(device_id, idem)["receipt"] == expected
        assert waiter.result == {'action_id': action_id, **expected}
    finally:
        if backend == "sqlite":
            store.close()


@pytest.mark.parametrize('backend', ['memory', 'sqlite'])
@pytest.mark.parametrize('terminal', ['executed', 'failed', 'unknown'])
def test_late_device_receipt_reconciles_retention_timeout_after_restart(tmp_path, backend, terminal):
    path = tmp_path / 'late.sqlite3'
    store = SqliteMemoryStore(path) if backend == 'sqlite' else InMemoryStore()
    device, action, idem = 'late-device', 'late-action', 'late-idempotency'
    key = f'device:action:{device}:{idem}'
    now = now_ms()
    store.put(f'device:action-id:{device}:{action}', idem)
    store.put(key, {'device_id': device, 'action_id': action, 'state': 'accepted',
                    'created_at': now - 2 * 86400000, 'expires_at': now - 25 * 3600000})
    connection = ConnectionState(None)
    connection.device_id = device
    try:
        gw = _gateway(store)
        assert gw.sweep_action_retention(now=now)['marked_unknown'] == 1
        assert store.get(key)['outcome_source'] == 'retention_timeout'
        gw._on_action_receipt(connection, {'payload': {'action_id': action, 'state': 'received'}})
        assert store.get(key)['state'] == 'unknown'  # intermediate is not a result
        if backend == 'sqlite':
            store.close()
            store = SqliteMemoryStore(path)
        restarted = _gateway(store)
        payload = {'action_id': action, 'state': terminal, 'result': {'summary': 'actual late outcome'}}
        if terminal == 'failed':
            payload['error_code'] = 'unavailable'
        restarted._on_action_receipt(connection, {'payload': payload})
        record = restarted.action_status(device, idem)
        assert record['state'] == terminal
        assert record['outcome_source'] == 'device_receipt'
        assert record['receipt']['result'] == payload['result']
        assert store.get(f'device:action-id:{device}:{action}') == idem
        restarted._on_action_receipt(connection, {'payload': {'action_id': action, 'state': 'failed'}})
        assert restarted.action_status(device, idem)['receipt'] == record['receipt']
    finally:
        if backend == 'sqlite':
            store.close()


@pytest.mark.parametrize('backend', ['memory', 'sqlite'])
def test_retention_and_receipt_writes_do_not_lose_terminal_result(tmp_path, monkeypatch, backend):
    store = SqliteMemoryStore(tmp_path / 'race.sqlite3') if backend == 'sqlite' else InMemoryStore()
    gw = _gateway(store)
    now = now_ms()
    device, action, idem = 'race-device', 'race-action', 'race-idempotency'
    key = f'device:action:{device}:{idem}'
    store.put(f'device:action-id:{device}:{action}', idem)
    store.put(key, {'device_id': device, 'action_id': action, 'state': 'pending',
                    'created_at': now - 2 * 86400000, 'expires_at': now - 25 * 3600000})
    entered, release, receipt_done = threading.Event(), threading.Event(), threading.Event()
    failures = []
    original_get = store.get
    def gated_get(name):
        result = original_get(name)
        if name == key and threading.current_thread().name == 'retention':
            snapshot = dict(result)
            entered.set()
            assert release.wait(3)
            return snapshot
        return result
    monkeypatch.setattr(store, 'get', gated_get)
    state = ConnectionState(None)
    state.device_id = device
    def sweep():
        try:
            gw.sweep_action_retention(now=now)
        except BaseException as exc:
            failures.append(exc)
    def receipt():
        try:
            gw._on_action_receipt(state, {'payload': {'action_id': action, 'state': 'executed'}})
        except BaseException as exc:
            failures.append(exc)
        finally:
            receipt_done.set()
    first = threading.Thread(target=sweep, name='retention')
    second = threading.Thread(target=receipt, name='receipt')
    first.start()
    try:
        assert entered.wait(3)
        second.start()
        assert not receipt_done.wait(.05)
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    try:
        assert not failures and receipt_done.is_set()
        assert gw.action_status(device, idem)['state'] == 'executed'
    finally:
        if backend == 'sqlite':
            store.close()
