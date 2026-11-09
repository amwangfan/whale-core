import pytest

from whale_core.devices.embedded_wss import EmbeddedWssGateway, GatewayError
from whale_core.devices.registry import DeviceRegistry
from whale_core.memory.store import InMemoryStore


@pytest.fixture
def gateway(monkeypatch):
    store = InMemoryStore()
    gateway = EmbeddedWssGateway(DeviceRegistry(store), store)
    monkeypatch.setattr(gateway, "_online_or_raise", lambda _: object())
    monkeypatch.setattr(gateway, "_check_scope", lambda *_: None)
    def no_send(*_):
        pytest.fail("invalid input must not be sent")
    monkeypatch.setattr(gateway, "send_msg", no_send)
    yield gateway
    gateway.close()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0, True, "15", 10**400])
@pytest.mark.parametrize("kind", ["action", "query"])
def test_invalid_timeout_has_no_side_effects(gateway, timeout, kind):
    with pytest.raises(GatewayError) as error:
        if kind == "action":
            gateway.send_action("dev-test", capability="device.tap", timeout_s=timeout,
                                idempotency_key="retry-key-0001")
        else:
            gateway.send_query("dev-test", operation="device.status", timeout_s=timeout)
    assert error.value.code == "invalid_message"
    assert not gateway.pending
    assert gateway.store.get("device:action:dev-test:retry-key-0001") is None


@pytest.mark.parametrize("key", ["", 123, [], "bad key with spaces"])
def test_invalid_key_is_not_reserved(gateway, key):
    with pytest.raises(GatewayError) as error:
        gateway.send_action("dev-test", capability="device.tap", idempotency_key=key)
    assert error.value.code == "invalid_message"
    assert not gateway.pending
    assert gateway.store.get(f"device:action:dev-test:{key}") is None


def test_query_schema_failure_precedes_pending_registration(gateway):
    with pytest.raises(GatewayError) as error:
        gateway.send_query("dev-test", operation="device.status",
                           filters={str(i): i for i in range(17)})
    assert error.value.code == "invalid_message"
    assert not gateway.pending


def test_query_timeout_respects_platform_wait_limit(gateway, monkeypatch):
    monkeypatch.setattr("whale_core.devices.embedded_wss.threading.TIMEOUT_MAX", 100)
    with pytest.raises(GatewayError) as error:
        gateway.send_query("dev-test", operation="device.status", timeout_s=96)
    assert error.value.code == "invalid_message"
    assert not gateway.pending
