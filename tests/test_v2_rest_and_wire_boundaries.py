import json
from types import SimpleNamespace

import pytest

from whale_core.api.device_inputs import action_options, confirmation_options, query_options
from whale_core.devices.embedded_wss import ConnectionState, EmbeddedWssGateway, GatewayError, MAX_FRAME_BYTES
from whale_core.devices.schema import ContractValidator
from test_embedded_wss import api as device_api, rig as websocket_rig, _api_rig, _req, _paired_active


@pytest.mark.parametrize("options", [
    {"timeout": True}, {"timeout": "nan"}, {"timeout": float("inf")}, {"timeout": 0},
    {"max_items": 1.5}, {"max_items": False}, {"max_items": "garbage"},
    {"filters": []}, {"filters": False},
])
def test_query_options_reject_implicit_fallbacks(options):
    with pytest.raises(ValueError):
        query_options(options)


def test_valid_numeric_strings_and_management_clamp_are_preserved():
    assert query_options({"timeout": "300", "max_items": "12"}) == {
        "filters": {}, "max_items": 12, "timeout_s": 60.0}
    assert action_options({"args": {"text": "鲸鱼"}})["arguments"] == {"text": "鲸鱼"}
    assert action_options({"arguments": {}, "args": {"ignored": True}})["arguments"] == {}
    assert confirmation_options({"grant_max_uses": "9007199254740993"})["grant_max_uses"] == 9007199254740993


@pytest.mark.parametrize("options", [{"arguments": []}, {"arguments": False}, {"task_id": 123},
    {"authorized_scope": "device.tap"}, {"idempotency_key": True}])
def test_action_fields_are_not_silently_discarded(options):
    with pytest.raises(ValueError):
        action_options(options)


@pytest.mark.parametrize("options", [{"ttl_s": "nan"}, {"ttl_s": 0}, {"grant_ttl_s": float("inf")},
    {"grant_max_uses": 0}, {"grant_max_uses": True}, {"grant_max_uses": 1.5}, {"principal_id": []}])
def test_invalid_confirmation_cannot_create_a_challenge(options):
    with pytest.raises(ValueError):
        confirmation_options(options)


@pytest.mark.parametrize("route,body", [
    ("query", {"operation": "screen.context.read", "max_items": "bad"}),
    ("action", {"capability": "phone.notification.post", "arguments": [], "idempotency_key": "bad-key-0001"}),
    ("action", {"capability": "phone.notification.post", "arguments": {"text": float("nan")},
                "idempotency_key": "bad-key-0002"}),
    ("confirm", {"scopes": ["screen.context.read"], "ttl_s": "nan"}),
    ("confirm", {"scopes": ["screen.context.read"], "grant_max_uses": False}),
    ("action", {"capability": "phone.notification.post", "authorized_scope": [],
                "idempotency_key": "empty-scope-0001"}),
    ("action", {"capability": "phone.notification.post", "task_id": "",
                "idempotency_key": "empty-task-0001"}),
])
def test_invalid_rest_requests_are_400_and_do_not_reserve_state(device_api, route, body):
    sim, gateway = _api_rig(device_api)
    sim.connect(gateway.port)
    try:
        before = set(gateway.store.keys())
        status, response = _req(device_api, "POST", f"/api/v1/devices/{sim.device_id}/{route}", body=body)
        assert status == 400 and "error" in response
        added = set(gateway.store.keys()) - before
        assert not any(key.startswith(("device:action:", "otp:device-challenge:")) for key in added)
    finally:
        sim.close()


@pytest.mark.parametrize("arguments", [{"text": "鲸" * MAX_FRAME_BYTES}, {"number": float("nan")},
    {"number": float("inf")}, {"unsupported": object()}])
def test_bad_wire_data_is_rejected_before_idempotency_reservation(websocket_rig, arguments):
    registry, _, gateway = websocket_rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    try:
        with pytest.raises(GatewayError) as error:
            gateway.send_action(sim.device_id, capability="phone.notification.post", arguments=arguments,
                                idempotency_key="wire-bad-key-0001")
        assert error.value.code == "invalid_message"
        assert gateway.store.get(f"device:action:{sim.device_id}:wire-bad-key-0001") is None
        assert not gateway.pending
    finally:
        sim.close()


def test_invalid_ref_id_still_produces_valid_error_frame():
    gateway = object.__new__(EmbeddedWssGateway)
    gateway.contracts = ContractValidator()
    messages = []
    state = ConnectionState(SimpleNamespace(send=lambda encoded: messages.append(json.loads(encoded))))
    gateway.send_error(state, "!" * 26, "invalid_message", "bad frame")
    assert len(messages) == 1
    assert gateway.contracts.validate("error", messages[0]) is None


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e10000"])
def test_nonstandard_json_is_rejected_without_disconnect(number):
    gateway = object.__new__(EmbeddedWssGateway)
    errors = []
    gateway.send_error = lambda st, ref, code, message: errors.append(code)
    assert gateway._handle_frame(object(), '{"type":"future", "data":' + number + '}') is True
    assert errors == ["invalid_message"]
