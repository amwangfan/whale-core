import asyncio

import pytest

from whale_core.devices.gateway_v2 import DeviceGatewayV2, GatewayError
from whale_core.devices.gui_agent.testing import FakeScreen, FakeTransport


class Registry:
    def __init__(self, status="active", scopes=("device.observe", "device.tap")):
        self.status = status
        self.scopes = list(scopes)

    def status_of(self, device):
        return self.status

    def scopes_of(self, device):
        return self.scopes


@pytest.mark.parametrize("status,scopes,code", [
    ("revoked", ["device.observe"], "device_revoked"),
    ("pending", ["device.observe"], "unavailable"),
    ("active", [], "scope_denied"),
])
def test_observe_checks_registry_before_selection(status, scopes, code):
    gateway = DeviceGatewayV2(Registry(status, scopes))
    transport = FakeTransport([FakeScreen("home")], "home")
    gateway.register_transport(transport)
    with pytest.raises(GatewayError) as error:
        asyncio.run(gateway.observe("dev"))
    assert error.value.code == code
    assert not transport.operations


@pytest.mark.parametrize("operation", ["execute", "observe"])
@pytest.mark.parametrize("change,code", [("revoke", "device_revoked"), ("scope", "scope_denied")])
def test_authorization_changes_during_selection_are_honored(operation, change, code):
    registry = Registry()

    class ChangingTransport(FakeTransport):
        async def capabilities(self, device):
            await asyncio.sleep(0)
            if change == "revoke":
                registry.status = "revoked"
            else:
                registry.scopes.clear()
            return ["device.observe", "device.tap"]

    transport = ChangingTransport([FakeScreen("home")], "home")
    gateway = DeviceGatewayV2(registry)
    gateway.register_transport(transport)
    with pytest.raises(GatewayError) as error:
        if operation == "observe":
            asyncio.run(gateway.observe("dev"))
        else:
            asyncio.run(gateway.execute("dev", "device.tap", {}, idempotency_key="route-test-key"))
    assert error.value.code == code
    assert not transport.operations
    assert not gateway._inflight
    assert gateway.action_status("dev", "route-test-key") is None


def test_authorized_observation_still_works():
    gateway = DeviceGatewayV2(Registry())
    gateway.register_transport(FakeTransport([FakeScreen("home")], "home"))
    assert asyncio.run(gateway.observe("dev")).device_id == "dev"
