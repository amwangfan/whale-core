import pytest

from whale_core.devices.embedded_wss import ConnectionState
from test_v2_wait_lifecycle import local_gateway


@pytest.mark.parametrize("change", ["replacement", "disconnected", "shutdown", "fresh"])
def test_obsolete_sweep_snapshot_cannot_mark_device_offline(change):
    gateway, st = local_gateway()
    gateway.heartbeat_timeout = 10
    st.last_heartbeat = 1
    events = []
    gateway._log_event = lambda *event: events.append(event)
    if change == "replacement":
        new = ConnectionState(object())
        new.device_id = st.device_id
        gateway.online[st.device_id] = new
    elif change == "disconnected":
        gateway.online.clear()
    elif change == "shutdown":
        gateway.stopping.set()
    else:
        st.last_heartbeat = 99
    gateway._expire_heartbeat(st.device_id, st, 100)
    assert not st.offline_marked and events == []


def test_timeout_is_reported_once_and_heartbeat_recovers(monkeypatch):
    gateway, st = local_gateway()
    gateway.heartbeat_timeout = 10
    st.last_heartbeat = 1
    events = []
    gateway._log_event = lambda *event: events.append(event)
    gateway._expire_heartbeat(st.device_id, st, 100)
    gateway._expire_heartbeat(st.device_id, st, 101)
    assert events == [("device:heartbeat_timeout", st.device_id)]
    assert st.offline_marked and st.status == "offline"
    monkeypatch.setattr("whale_core.devices.embedded_wss.time.monotonic", lambda: 102)
    gateway._on_device_heartbeat(st, {"payload": {
        "device_id": st.device_id, "status": "online", "modules": {"screen": "ok"}}})
    gateway._expire_heartbeat(st.device_id, st, 103)
    assert not st.offline_marked and st.modules == {"screen": "ok"}
    assert events[-1] == ("device:online", st.device_id)
    assert len(events) == 2


def test_heartbeat_dispatched_before_replacement_cannot_revive_old_session():
    gateway, st = local_gateway()
    st.offline_marked = True
    gateway.online[st.device_id] = ConnectionState(object())
    gateway._log_event = lambda *_: pytest.fail("old session must not emit online")
    gateway._on_device_heartbeat(st, {"payload": {"device_id": st.device_id, "status": "online"}})
    assert st.offline_marked
