"""Output reads use an explicit scope; action scopes never imply read access.

No device service or socket is started: exercise the real API/gateway query path
with an in-process transport that validates the actual contract before replying.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from whale_core.api.devices_api import _downlink
from whale_core.devices.gateway import ConnectionState, DeviceGateway
from whale_core.devices.registry import DeviceRegistry
from whale_core.devices.schema import ContractValidator
from whale_core.memory.store import InMemoryStore

READ = "device.agent.output.read"


def rig(scopes):
    store = InMemoryStore()
    registry = DeviceRegistry(store)
    pairing = registry.request_pairing(label="output-test", pubkey_hex="12" * 32,
                                       scopes=scopes)
    registry.approve(pairing["code"])
    device_id = pairing["device_id"]
    # Avoid DeviceGateway.__init__, which would start a listening socket.
    gateway = DeviceGateway.__new__(DeviceGateway)
    gateway.registry = registry
    gateway.contracts = ContractValidator()
    gateway.lock = threading.RLock()
    state = ConnectionState(None)
    state.device_id = device_id
    gateway.online = {device_id: state}
    gateway.pending = {}
    sent = []

    def send_msg(st, kind, payload):
        assert ContractValidator().validate(kind, {
            "v": "1.0", "id": "a" * 32, "type": kind, "ts": 1,
            "seq": 1, "dir": "downlink", "payload": payload,
        }) is None
        sent.append((kind, payload))
        pending = gateway.pending[payload["request_id"]]
        pending.result = {"status": "ok", "data": [{"text": "bounded output"}]}
        pending.event.set()

    gateway.send_msg = send_msg
    return registry, gateway, device_id, sent


class Handler:
    def __init__(self, operation=READ, filters=None):
        self.body = {"operation": operation,
                     "filters": filters if filters is not None else {"output_ref": "agent:out:once-1.log"}}
        self.ctx = SimpleNamespace(log_event=lambda *args: None)

    def _body_json(self):
        return self.body

    def _send(self, status, body):
        self.status = status
        self.response = json.loads(body)


def test_output_read_api_to_gateway_preserves_scope_and_filters():
    _, gateway, device_id, sent = rig([READ])
    handler = Handler()
    _downlink(handler, gateway, device_id, op="query")
    assert handler.status == 200
    assert handler.response["result"]["result"]["data"] == [{"text": "bounded output"}]
    assert sent[0][0] == "sensor.query"
    assert sent[0][1]["operation"] == READ
    assert sent[0][1]["filters"] == handler.body["filters"]
    assert gateway.pending == {}


@pytest.mark.parametrize("scopes", [
    ["device.agent.control"], ["device.agent.observe"], ["device.agent.shell"],
    ["device.agent.control", "device.agent.observe", "device.agent.shell"],
])
def test_legacy_action_scopes_cannot_read_output(scopes):
    registry, gateway, device_id, sent = rig(scopes)
    assert READ not in registry.scopes_of(device_id)
    handler = Handler()
    _downlink(handler, gateway, device_id, op="query")
    assert handler.status == 403
    assert not sent
    assert gateway.pending == {}


def test_output_scope_does_not_imply_observe_or_shell_query():
    _, gateway, device_id, sent = rig([READ])
    for operation in ("device.agent.observe", "device.agent.shell"):
        handler = Handler(operation)
        _downlink(handler, gateway, device_id, op="query")
        assert handler.status == 403
    assert not sent


def test_revoked_output_scope_is_not_usable():
    registry, gateway, device_id, sent = rig([READ])
    registry.revoke(device_id)
    handler = Handler()
    _downlink(handler, gateway, device_id, op="query")
    assert handler.status == 403
    assert not sent


def test_unknown_operation_is_still_rejected_before_transport():
    _, gateway, device_id, sent = rig([READ])
    handler = Handler("device.agent.output.anything")
    _downlink(handler, gateway, device_id, op="query")
    assert handler.status == 400
    assert not sent
