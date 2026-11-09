import asyncio
import threading

import pytest

from whale_core.devices.embedded_wss import ConnectionState, EmbeddedWssGateway, GatewayError, _Pending
from whale_core.devices.transports.base import new_ulid, now_ms
from test_embedded_wss import rig as websocket_rig, _paired_active


def local_gateway():
    gateway = object.__new__(EmbeddedWssGateway)
    gateway.lock = threading.RLock()
    gateway.stopping = threading.Event()
    gateway.pending, gateway.pending_confirm = {}, {}
    gateway._log_event = None
    state = ConnectionState(object())
    state.device_id = "dev"
    gateway.online = {"dev": state}
    return gateway, state


@pytest.mark.parametrize("response_type", ["sensor.result", "action.receipt", "device.confirm.response"])
def test_replacement_releases_only_old_connection_waiters(response_type):
    gateway, old = local_gateway()
    new = ConnectionState(object())
    new.device_id = "dev"
    gateway.online["dev"] = new
    pending = gateway.pending_confirm if response_type == "device.confirm.response" else gateway.pending
    previous, current = _Pending("dev", response_type, old), _Pending("dev", response_type, new)
    pending.update(old=previous, new=current)
    gateway._finish_session(old)
    assert previous.event.is_set() and not current.event.is_set()
    assert pending == {"new": current}
    assert gateway.online["dev"] is new
    if response_type == "action.receipt":
        assert previous.result["status"] == "unknown"
    elif response_type == "device.confirm.response":
        assert previous.result["approved"] is False
    else:
        assert previous.error.code == "unavailable"


@pytest.mark.parametrize("stopping", [True, False])
def test_changed_or_stopping_session_cannot_register(stopping):
    gateway, state = local_gateway()
    if stopping:
        gateway.stopping.set()
    else:
        gateway.online.clear()
    gateway.send_msg = lambda *_: pytest.fail("request must not be sent")
    with pytest.raises(GatewayError):
        gateway._await_response(state, "id", "sensor.query", {}, 1, "sensor.result")
    assert not gateway.pending


def test_ambiguous_send_failure_returns_unknown_and_cleans_waiters():
    gateway, state = local_gateway()
    def broken(*_):
        raise OSError("connection reset after write")
    gateway.send_msg = broken
    result = gateway._await_response(state, "act", "action.request", {}, 1, "action.receipt")
    assert result == {"action_id": "act", "status": "unknown", "provider_ref": "core:connection-lost"}
    assert not gateway.online and not gateway.pending


@pytest.mark.parametrize("method", ["send_notification_event", "send_notification_revoke"])
def test_async_notification_send_runs_off_event_loop_thread(method):
    gateway, _ = local_gateway()
    loop_thread = threading.get_ident()
    calls = []
    def send(device, kind, payload):
        calls.append((threading.get_ident(), device, kind, payload))
    gateway._send_notification = send
    asyncio.run(getattr(gateway, method)("dev", {"id": "notification"}))
    assert calls[0][0] != loop_thread
    assert calls[0][1] == "dev" and calls[0][3] == {"id": "notification"}


@pytest.mark.parametrize("kind", ["query", "action", "confirm"])
@pytest.mark.parametrize("shutdown", [False, True])
def test_real_disconnect_or_shutdown_releases_request_promptly(websocket_rig, kind, shutdown):
    registry, _, gateway = websocket_rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    box = {}
    def ask():
        try:
            if kind == "query":
                box["result"] = gateway.send_query(sim.device_id, operation="screen.context.read", timeout_s=30)
            elif kind == "action":
                box["result"] = gateway.send_action(sim.device_id, capability="phone.notification.post",
                    arguments={"text": "test"}, idempotency_key="disconnect-key-0001", timeout_s=30)
            else:
                box["result"] = gateway.send_confirm_request(sim.device_id, {
                    "challenge_id": new_ulid(), "device_id": sim.device_id, "principal_id": "owner",
                    "channel": "test", "scopes": ["screen.context.read"], "expires_at": now_ms() + 60000},
                    timeout_s=30)
        except GatewayError as exc:
            box["error"] = exc.code
    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    try:
        sim.recv()  # ensure registration and send completed before disconnecting
        if shutdown:
            gateway.close()
        else:
            sim.close()
        thread.join(2)
        assert not thread.is_alive(), "request must not wait out the original 30s timeout"
        if kind == "query":
            assert box["error"] == "unavailable"
        elif kind == "confirm":
            assert box["result"]["approved"] is False
        else:
            assert box["result"]["status"] == "unknown"
            record = gateway.store.get(f"device:action:{sim.device_id}:disconnect-key-0001")
            assert record["state"] == "pending"  # late real receipt is still allowed to reconcile
        assert not gateway.pending and not gateway.pending_confirm
    finally:
        sim.close()
