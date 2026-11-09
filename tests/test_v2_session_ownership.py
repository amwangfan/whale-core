import json
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed

from whale_core.devices.embedded_wss import ConnectionState, _Pending
from whale_core.devices.schema import ContractValidator, SchemaUnsupported, contracts_dir
from whale_core.devices.transports.base import new_ulid, now_ms
from test_embedded_wss import DeviceSim, rig as websocket_rig, _paired_active
from test_v2_wait_lifecycle import local_gateway
from whale_core.memory.store import InMemoryStore


def test_failed_reconnect_ack_preserves_active_session(websocket_rig, monkeypatch):
    registry, _, gateway = websocket_rig
    old = _paired_active(registry)
    new = DeviceSim()
    new.key, new.device_id = old.key, old.device_id
    old.connect(gateway.port)
    previous = gateway.online_state(old.device_id)
    send = gateway.send_msg
    observed = []

    def fail_ack(st, kind, payload):
        if kind == "device.hello_ack":
            observed.append(gateway.online_state(old.device_id) is previous)
            raise OSError("ack connection lost")
        return send(st, kind, payload)

    monkeypatch.setattr(gateway, "send_msg", fail_ack)
    try:
        with pytest.raises(ConnectionClosed):
            new.connect(gateway.port)
        assert observed == [True]
        assert gateway.online_state(old.device_id) is previous
        old.send(old.frame("future.message", {}))
        old.send(old.frame("device.heartbeat", {}))
        assert old.recv()["payload"]["code"] == "invalid_message"
    finally:
        new.close()
        old.close()


def test_online_notification_cannot_overtake_hello_ack(websocket_rig):
    registry, _, gateway = websocket_rig
    sim = _paired_active(registry)
    gateway._log_event = lambda kind, _: gateway.send_msg(
        gateway.online_state(sim.device_id), "error", {
            "ref_id": new_ulid(), "code": "invalid_message", "message": "online probe"
        }) if kind == "device:online" else None
    try:
        sim.connect(gateway.port)  # asserts first reply is hello_ack
        assert sim.recv()["payload"]["message"] == "online probe"
    finally:
        sim.close()


@pytest.mark.parametrize("kind", ["sensor.result", "action.receipt", "device.confirm.response"])
def test_same_device_other_connection_cannot_consume_waiter(kind):
    gateway, current = local_gateway()
    gateway.store = InMemoryStore()
    old = ConnectionState(object())
    old.device_id = current.device_id
    key = "response-key"
    pending = gateway.pending_confirm if kind == "device.confirm.response" else gateway.pending
    waiter = _Pending(current.device_id, kind, current)
    pending[key] = waiter
    if kind == "sensor.result":
        payload = {"request_id": key, "status": "ok"}
    elif kind == "device.confirm.response":
        payload = {"challenge_id": key, "approved": False}
    else:
        payload = {"action_id": key, "status": "succeeded"}
        gateway.store.put(f"device:action:{current.device_id}:idempotent", {
            "device_id": current.device_id, "action_id": key, "state": "pending"})
        gateway.store.put(f"device:action-id:{current.device_id}:{key}", "idempotent")
    handler = getattr(gateway, "_on_" + kind.replace(".", "_"))
    handler(old, {"payload": payload})
    assert pending[key] is waiter and not waiter.event.is_set()
    handler(current, {"payload": payload})
    assert key not in pending and waiter.event.is_set()


def test_replaced_connection_frame_has_no_side_effects():
    gateway, current = local_gateway()
    old = ConnectionState(object())
    old.device_id = current.device_id
    gateway._dispatch_frame = lambda *_: pytest.fail("stale frame must not dispatch")
    assert gateway._handle_frame(old, '{}') is False


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(dir="downlink"),
    lambda m: m.pop("payload"),
    lambda m: m.update(seq=True),
    lambda m: m.update(extra=True),
    lambda m: m.update(type=""),
])
def test_future_types_still_require_valid_uplink_envelope(mutate):
    gateway, st = local_gateway()
    gateway.contracts = ContractValidator()
    gateway.registry = SimpleNamespace(status_of=lambda _: "active")
    errors = []
    gateway.send_error = lambda _, ref, code, message: errors.append(code)
    msg = {"v": "2.1", "id": new_ulid(), "type": "future.message", "ts": now_ms(),
           "seq": 1, "dir": "uplink", "payload": {}}
    assert gateway._handle_frame(st, json.dumps(msg)) is True
    assert not errors
    mutate(msg)
    assert gateway._handle_frame(st, json.dumps(msg)) is True
    assert errors == ["invalid_message"]


def test_partial_contract_directory_missing_envelope_fails_closed(tmp_path):
    source = contracts_dir() / "error.schema.json"
    (tmp_path / source.name).write_bytes(source.read_bytes())
    with pytest.raises(SchemaUnsupported, match="envelope"):
        ContractValidator(tmp_path)
