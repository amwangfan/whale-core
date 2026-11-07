"""Invalid local requests must not consume idempotency keys or write to the wire."""
import json
import threading

import pytest

from whale_core.devices.gateway import ConnectionState, DeviceGateway, GatewayError
from whale_core.devices.registry import DeviceRegistry
from whale_core.devices.schema import ContractValidator
from whale_core.memory.store import InMemoryStore

CAP = 'phone.notification.post'


def rig():
    gateway = DeviceGateway.__new__(DeviceGateway)
    gateway.store = InMemoryStore()
    gateway.registry = DeviceRegistry(gateway.store)
    pair = gateway.registry.request_pairing(label='dispatch', pubkey_hex='34' * 32, scopes=[CAP])
    gateway.registry.approve(pair['code'])
    gateway.lock = threading.RLock()
    gateway.contracts = ContractValidator()
    gateway.pending = {}
    gateway._log_event = None
    state = ConnectionState(None)
    state.device_id = pair['device_id']
    gateway.online = {state.device_id: state}
    sent = []
    class Socket:
        def send(self, raw):
            frame = json.loads(raw)
            sent.append(frame)
            gateway._on_action_receipt(state, {'payload': {
                'action_id': frame['payload']['action_id'], 'state': 'executed',
            }})
    state.ws = Socket()
    return gateway, state, sent


def test_rejected_parameters_do_not_poison_idempotency_key():
    gateway, state, sent = rig()
    idem = 'same-key-after-validation-error'
    with pytest.raises(GatewayError) as error:
        gateway.send_action(state.device_id, capability=CAP,
                            args={str(i): i for i in range(33)}, idempotency_key=idem)
    assert error.value.code == 'invalid_message'
    assert not sent and not gateway.pending
    assert gateway.store.get(f'device:action:{state.device_id}:{idem}') is None
    assert gateway.send_action(state.device_id, capability=CAP, args={}, idempotency_key=idem)['state'] == 'executed'
    assert len(sent) == 1
    with pytest.raises(GatewayError) as duplicate:
        gateway.send_action(state.device_id, capability=CAP, args={}, idempotency_key=idem)
    assert duplicate.value.code == 'duplicate'


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf'), True, 'bad'])
def test_invalid_timeout_has_no_dispatch_side_effect(timeout):
    gateway, state, sent = rig()
    with pytest.raises(GatewayError) as error:
        gateway.send_action(state.device_id, capability=CAP, timeout_s=timeout)
    assert error.value.code == 'invalid_message'
    assert not sent and not gateway.pending


def test_concurrent_send_sequence_matches_wire_order():
    gateway, state, _ = rig()
    first_entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
    frames, failures = [], []
    class Socket:
        def send(self, raw):
            frame = json.loads(raw)
            if frame['seq'] == 1:
                first_entered.set()
                assert release.wait(2)
            frames.append(frame['seq'])
    state.ws = Socket()
    def send(done=None):
        try:
            gateway.send_msg(state, 'error', {'ref_id': 'a' * 16, 'code': 'internal', 'message': 'test'})
        except BaseException as exc:
            failures.append(exc)
        finally:
            if done:
                done.set()
    first = threading.Thread(target=send)
    second = threading.Thread(target=send, args=(second_done,))
    first.start()
    try:
        assert first_entered.wait(2)
        second.start()
        assert not second_done.wait(.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not failures
    assert frames == [1, 2]


@pytest.mark.parametrize('key', ['', 123, [], {}])
def test_malformed_explicit_key_is_rejected_without_creating_random_action(key):
    gateway, state, sent = rig()
    with pytest.raises(GatewayError) as error:
        gateway.send_action(state.device_id, capability=CAP, idempotency_key=key)
    assert error.value.code == 'invalid_message'
    assert not sent and not gateway.pending
