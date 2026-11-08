import threading
from types import SimpleNamespace

import pytest

from whale_core.devices.embedded_wss import EmbeddedWssGateway, GatewayError
from whale_core.devices.schema import ContractValidator
from whale_core.devices.transports.base import new_ulid, now_ms

DEVICE = "a" * 32


@pytest.fixture
def gateway():
    # Only the synchronous request/wait boundary is under test; no sockets needed.
    gateway = object.__new__(EmbeddedWssGateway)
    gateway.contracts = ContractValidator()
    gateway.lock = threading.RLock()
    gateway.pending_confirm = {}
    gateway.pending = {}
    gateway.stopping = threading.Event()
    gateway.online = {device: SimpleNamespace(device_id=device, offline_marked=False)
                      for device in (DEVICE, "b" * 32)}
    gateway._online_or_raise = lambda device: gateway.online[device]
    gateway.send_msg = lambda *_: pytest.fail("invalid request must not be sent")
    return gateway


def payload(device=DEVICE):
    return {"challenge_id": new_ulid(), "device_id": device, "principal_id": "owner",
            "channel": "test", "scopes": ["device.observe"], "expires_at": now_ms() + 60000}


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0, True, "5"])
def test_invalid_timeout_does_not_register_waiter(gateway, timeout):
    with pytest.raises(GatewayError) as error:
        gateway.send_confirm_request(DEVICE, payload(), timeout_s=timeout)
    assert error.value.code == "invalid_message"
    assert not gateway.pending_confirm


@pytest.mark.parametrize("body", [{}, payload("b" * 32)])
def test_invalid_payload_or_target_does_not_register_waiter(gateway, body):
    with pytest.raises(GatewayError) as error:
        gateway.send_confirm_request(DEVICE, body)
    assert error.value.code == "invalid_message"
    assert not gateway.pending_confirm


@pytest.mark.parametrize("second_device", [DEVICE, "b" * 32])
def test_duplicate_challenge_cannot_replace_original_waiter(gateway, second_device):
    sent = threading.Event()
    sent_payloads = []
    def send(st, kind, body):
        sent_payloads.append(body)
        sent.set()
    gateway.send_msg = send
    body = payload()
    results = []
    thread = threading.Thread(target=lambda: results.append(
        gateway.send_confirm_request(DEVICE, body, timeout_s=1)))
    thread.start()
    try:
        assert sent.wait(2)
        original = gateway.pending_confirm[body["challenge_id"]]
        with pytest.raises(GatewayError) as error:
            gateway.send_confirm_request(second_device, {**body, "device_id": second_device})
        assert error.value.code == "invalid_message"
        assert gateway.pending_confirm[body["challenge_id"]] is original
        assert len(sent_payloads) == 1
        gateway._on_device_confirm_response(SimpleNamespace(device_id=DEVICE), {"payload": {
            "challenge_id": body["challenge_id"], "approved": False, "reason": "declined"}})
        thread.join(2)
        assert not thread.is_alive()
        assert results == [{"approved": False, "reason": "declined", "signature": None}]
        assert not gateway.pending_confirm
    finally:
        for pending in gateway.pending_confirm.values():
            pending.event.set()
        thread.join(2)
