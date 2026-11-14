import pytest

from test_action_record_transactions import store
from test_embedded_wss import api
from whale_core.memory.store import MemoryRecord, MemoryKind
from whale_core.devices.action_records import ActionRecords
from whale_core.devices.embedded_wss import EmbeddedWssGateway


def seed(store, count):
    for i in range(count):
        store.put(f"device:action:dev:{i:04}", {"device_id": "dev", "action_id": str(i),
            "created_at": 1, "expires_at": 1, "state": "succeeded"})
        store.put(f"device:action-id:dev:{i}", f"{i:04}")


def test_scan_is_literal_ordered_and_includes_record_keys(store):
    for key in ("p_%:a", "p_%:b", "p_other", "p_%:c"):
        store.put(key, 1)
    store.put_record(MemoryRecord(kind=MemoryKind.EPISODE, key="p_%:b", content={}))
    store.put_record(MemoryRecord(kind=MemoryKind.EPISODE, key="p_%:d", content={}))
    assert store.scan_keys("p_%:", limit=2) == ["p_%:a", "p_%:b"]
    assert store.scan_keys("p_%:", after="p_%:b", limit=2) == ["p_%:c", "p_%:d"]


def test_bounded_scan_does_not_call_full_keys_and_eventually_visits_late_expirations(store):
    seed(store, 17)
    for i in range(1000): store.put(f"unrelated:{i}", {})
    store.keys = lambda: pytest.fail("full-store listing is forbidden")
    cursor = receipts = ""
    deleted = 0
    for _ in range(30):
        result = ActionRecords(store).sweep(100, 10, 10, 2, max_scan=3,
            cursor=cursor, receipt_cursor=receipts)
        assert result["scanned"] <= 3 and result["receipt_scanned"] <= 3 and result["deleted"] <= 2
        cursor, receipts = result["cursor"], result["receipt_cursor"]
        deleted += result["deleted"]
        if not store.scan_keys("device:action:", limit=1): break
    assert deleted == 17
    assert not store.scan_keys("device:action-id:", limit=1)


def test_orphan_receipt_paging_and_diagnostic_redaction(store):
    for i in range(9): store.put(f"device:receipts:private:{i}", [])
    gateway = object.__new__(EmbeddedWssGateway)
    gateway.store = store
    total = 0
    for _ in range(10):
        stats = gateway.sweep_action_retention(now=100, max_deletes=2, max_scan=3)
        total += stats["deleted"]
        if not stats["more"]: break
    assert total == 9
    status = gateway.retention_status()
    assert status["state"] == "ok" and "private" not in str(status)
    status["state"] = "mutated"
    assert gateway.retention_status()["state"] == "ok"


@pytest.mark.parametrize("limit", [0, True, -1, 10001])
def test_scan_rejects_invalid_budget(store, limit):
    with pytest.raises(ValueError): store.scan_keys("prefix", limit=limit)


def test_failed_scan_preserves_checkpoint_and_hides_exception_payload(store):
    gateway = object.__new__(EmbeddedWssGateway)
    class Broken:
        def scan_keys(self, *args, **kwargs):
            raise RuntimeError("secret payload must not be exposed")
    gateway.store = Broken()
    gateway._retention_cursors = ("action-checkpoint", "receipt-checkpoint")
    with pytest.raises(RuntimeError): gateway.sweep_action_retention(max_scan=3)
    assert gateway._retention_cursors == ("action-checkpoint", "receipt-checkpoint")
    status = gateway.retention_status()
    assert status["state"] == "failed" and status["error"] == "RuntimeError"
    assert "secret" not in str(status) and "checkpoint" not in str(status)


def test_sqlite_prefix_scan_uses_indexes(tmp_path):
    from whale_core.memory.sqlite_store import SqliteMemoryStore
    backend = SqliteMemoryStore(tmp_path / "scan.sqlite")
    try:
        queries = []
        backend.db.set_trace_callback(queries.append)
        backend.scan_keys("device:action:", limit=5)
        backend.db.set_trace_callback(None)
        assert len(queries) == 2
        for query in queries:
            plan = backend.db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            assert any("SEARCH" in row[3] for row in plan)
            assert "LIMIT 5" in query
    finally:
        backend.db.close()


def test_device_list_exposes_only_retention_diagnostics(api):
    from test_embedded_wss import _req
    code, body = _req(api, "GET", "/api/v1/devices")
    assert code == 200 and body["result"]["retention"]["state"] in ("not_run", "ok")
    assert "cursor" not in body["result"]["retention"]
