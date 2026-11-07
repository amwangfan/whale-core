"""Replies from another paired device cannot consume a pending request."""
import threading

import pytest

from whale_core.devices.gateway import ConnectionState, DeviceGateway, _Pending
from whale_core.memory.store import InMemoryStore


def gateway():
    instance = DeviceGateway.__new__(DeviceGateway)
    instance.lock = threading.RLock()
    instance.pending = {}
    instance.pending_confirm = {}
    instance.store = InMemoryStore()
    instance._log_event = None
    return instance


def connection(device_id):
    state = ConnectionState(None)
    state.device_id = device_id
    return state


@pytest.mark.parametrize('kind', ['sensor', 'confirm'])
def test_wrong_device_does_not_complete_or_remove_pending(kind):
    gw = gateway()
    pending = _Pending('owner-device', 'sensor.result' if kind == 'sensor' else 'device.confirm.response')
    ref = 'request-123'
    if kind == 'sensor':
        requests = gw.pending
        handler = gw._on_sensor_result
        payload = {'request_id': ref, 'status': 'ok', 'data': []}
    else:
        requests = gw.pending_confirm
        handler = gw._on_device_confirm_response
        payload = {'challenge_id': ref, 'approved': True, 'signature': 'test'}
    requests[ref] = pending
    handler(connection('other-device'), {'payload': payload})
    assert requests[ref] is pending
    assert not pending.event.is_set()
    handler(connection('owner-device'), {'payload': payload})
    assert ref not in requests
    assert pending.event.is_set()
    original = pending.result
    handler(connection('owner-device'), {'payload': {**payload, 'status': 'failed'}})
    assert pending.result == original


def test_action_receipt_requires_original_device_mapping():
    gw = gateway()
    pending = _Pending('owner-device', 'action.receipt')
    action_id, idem = 'action-123', 'idempotency-123'
    gw.pending[action_id] = pending
    gw.store.put(f'device:action-id:owner-device:{action_id}', idem)
    gw.store.put(f'device:action:owner-device:{idem}', {'action_id': action_id, 'state': 'pending'})
    message = {'payload': {'action_id': action_id, 'state': 'executed'}}
    gw._on_action_receipt(connection('other-device'), message)
    assert not pending.event.is_set()
    assert not gw.store.get(f'device:receipts:other-device:{action_id}')
    # Even the correct device must send the expected response kind.
    gw._on_sensor_result(connection('owner-device'), {'payload': {
        'request_id': action_id, 'status': 'ok', 'data': [],
    }})
    assert not pending.event.is_set() and gw.pending[action_id] is pending
    gw._on_action_receipt(connection('owner-device'), message)
    assert pending.event.is_set()
    assert pending.result['state'] == 'executed'


def test_type_mismatch_does_not_consume_query_waiter():
    gw = gateway()
    pending = _Pending('owner-device', 'sensor.result')
    gw.pending['query-id'] = pending
    assert gw._take_pending(gw.pending, 'query-id', 'owner-device', 'action.receipt') is None
    assert gw.pending['query-id'] is pending
    gw._on_sensor_result(connection('owner-device'), {'payload': {'request_id': 'query-id', 'status': 'ok'}})
    assert pending.event.is_set() and not gw.pending
