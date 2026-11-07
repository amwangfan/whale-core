"""Receipt result reconciliation survives late delivery and Core restarts.

contracts/v2 移植：回执五终态（succeeded/failed/timeout/cancelled/unknown），
终态 first-wins，落库不可翻案（§7.2）。
"""
from __future__ import annotations

import threading

import pytest

from whale_core.devices.embedded_wss import ConnectionState, EmbeddedWssGateway
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore


def _gateway(store):
    # Exercise durable receipt handling without opening a listener.
    gateway = EmbeddedWssGateway.__new__(EmbeddedWssGateway)
    gateway.store = store
    gateway.lock = threading.RLock()
    gateway.pending = {}
    gateway._log_event = None
    return gateway


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("status", ["succeeded", "failed", "timeout",
                                    "cancelled", "unknown"])
def test_terminal_receipt_result_survives_reconciliation(tmp_path, backend, status):
    path = tmp_path / "receipts.sqlite3"
    store = SqliteMemoryStore(path) if backend == "sqlite" else InMemoryStore()
    device_id = "ab" * 16
    action_id = "act_01J9XK3EM8TQ2ZP0VN6W4RA7YC"
    idem = "idem-receipt-test"
    key = f"device:action:{device_id}:{idem}"
    store.put(f"device:action-id:{device_id}:{action_id}", idem)
    store.put(key, {"device_id": device_id, "action_id": action_id,
                    "state": "received", "expires_at": 4102444800000})
    connection = ConnectionState(None)
    connection.device_id = device_id
    payload = {"action_id": action_id, "status": status, "started_at": 120,
               "completed_at": 123,
               "provider_ref": "degraded:notification:test",
               "result": {"summary": "device outcome",
                          "output_ref": "agent:out/act_01J9XK3EM8TQ2ZP0VN6W4RA7YC"}}
    if status == "failed":
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
        # Conflicting/partial duplicate terminal frames cannot erase the result
        # （first terminal wins，§7.2）。
        restarted._on_action_receipt(connection, {"payload": {
            "action_id": action_id, "status": "failed", "error_code": "internal"}})
        assert restarted.action_status(device_id, idem)["receipt"] == expected
    finally:
        if backend == "sqlite":
            store.close()
