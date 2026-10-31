"""Device inbox should not lose events acknowledged upstream across restarts."""
import pytest

from whale_core.devices.perception import DurableDevicePerceptionAdapter
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "sqlite":
        db = SqliteMemoryStore(tmp_path / "sensors.sqlite3")
        try:
            yield db
        finally:
            db.close()
    else:
        yield InMemoryStore()


def test_device_inbox_survives_adapter_restart_until_consumption_ack(store):
    event = {
        "device_id": "device1", "event_id": "evt1",
        "kind": "screen.memo.created", "occurred_at": 1780000000000,
        "privacy_class": "private", "summary": "noticed a schedule change",
        "evidence_ref": None, "schema_version": "1.0", "received_at": 1780000000001,
    }
    # This is exactly the persistent record the gateway writes BEFORE sensor.ack.
    store.put("device:event:device1:evt1", event)
    first = DurableDevicePerceptionAdapter(store)
    pending = list(first.poll())
    assert len(pending) == 1
    assert pending[0].event_id == "evt1"
    # Calling poll does NOT claim success: crash-and-restart redelivers.
    restarted = DurableDevicePerceptionAdapter(store)
    assert [x.event_id for x in restarted.poll()] == ["evt1"]
    restarted.ack(device_id="device1", event_id="evt1")
    assert list(DurableDevicePerceptionAdapter(store).poll()) == []


def test_device_ack_rejects_nonexistent_event(store):
    durable = DurableDevicePerceptionAdapter(store)
    with pytest.raises(ValueError, match="unknown"):
        durable.ack(device_id="device1", event_id="fake")
