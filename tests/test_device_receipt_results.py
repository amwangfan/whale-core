"""Receipt result reconciliation survives late delivery and Core restarts."""
from __future__ import annotations

import threading

import pytest

from whale_core.devices.gateway import ConnectionState, DeviceGateway
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
        restarted._on_action_receipt(connection, {"payload": {
            "action_id": action_id, "state": "failed", "error_code": "internal"}})
        assert restarted.action_status(device_id, idem)["receipt"] == expected
    finally:
        if backend == "sqlite":
            store.close()
