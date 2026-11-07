"""DeviceGatewayV2 选路/幂等/降级测试（FakeTransport 复用 gui_agent.testing）。"""
from __future__ import annotations

import asyncio

from whale_core.devices.gateway_v2 import DeviceGatewayV2, GatewayError
from whale_core.devices.gui_agent.testing import (
    FakeScreen,
    FakeTransition,
    FakeTransport,
)
from whale_core.devices.transports.base import (
    ActionReceipt,
    ReceiptStatus,
    now_ms,
)


def run(coro):
    return asyncio.run(coro)


def gui_transport():
    return FakeTransport(
        [FakeScreen("home", elements=[],
                    transitions=[FakeTransition("device.tap", {}, "")])],
        "home")


class ShellTransport(FakeTransport):
    def __init__(self, fail: bool = False):
        super().__init__([FakeScreen("home")], "home", kind="ssh")
        self._fail = fail

    async def capabilities(self, device_id):
        return ["device.shell", "device.file.read", "device.file.write",
                "device.file.list", "device.logcat"]

    async def execute(self, device_id, action):
        self.operations.append({"capability": action.capability})
        if self._fail:
            return ActionReceipt(
                action_id=action.action_id, status=ReceiptStatus.FAILED,
                summary="通道故障", error_code="unavailable", completed_at=now_ms())
        return ActionReceipt(
            action_id=action.action_id, status=ReceiptStatus.SUCCEEDED,
            summary="ok", completed_at=now_ms())


class FlakyGuiTransport(FakeTransport):
    def __init__(self):
        super().__init__([FakeScreen("home")], "home", kind="adb")

    async def capabilities(self, device_id):
        return ["device.observe", "device.tap", "device.shell"]

    async def execute(self, device_id, action):
        if action.capability == "device.shell":
            return ActionReceipt(
                action_id=action.action_id, status=ReceiptStatus.FAILED,
                summary="adb shell 失败", error_code="internal",
                completed_at=now_ms())
        return await super().execute(device_id, action)


class TestRouting:
    def test_shell_prefers_ssh(self):
        gw = DeviceGatewayV2()
        gui = gui_transport()
        shell = ShellTransport()
        gw.register_transport(gui)
        gw.register_transport(shell)
        r = run(gw.execute("dev1", "device.shell", {"command": "id"},
                           idempotency_key="k1"))
        assert r.status is ReceiptStatus.SUCCEEDED
        assert shell.operations and not gui.operations

    def test_gui_prefers_adb(self):
        gw = DeviceGatewayV2()
        gui = gui_transport()
        shell = ShellTransport()
        gw.register_transport(shell)
        gw.register_transport(gui)
        r = run(gw.execute("dev1", "device.tap", {"x": 1, "y": 2},
                           idempotency_key="k2"))
        assert r.status is ReceiptStatus.SUCCEEDED
        assert gui.operations and not shell.operations

    def test_ssh_preferred_when_both_ssh_and_adb_offer_shell(self):
        gw = DeviceGatewayV2()
        flaky = FlakyGuiTransport()  # kind=adb, shell 会 FAILED/internal
        shell = ShellTransport()
        gw.register_transport(flaky)
        gw.register_transport(shell)
        r = run(gw.execute("dev1", "device.shell", {"command": "id"},
                           idempotency_key="k3"))
        assert r.status is ReceiptStatus.SUCCEEDED
        assert shell.operations and not flaky.operations

    def test_no_channel_unavailable(self):
        gw = DeviceGatewayV2()
        r = run(gw.execute("dev1", "device.tap", {"x": 1, "y": 2},
                           idempotency_key="k4"))
        assert r.status is ReceiptStatus.FAILED
        assert r.error_code == "unavailable"


class TestDispatchReliability:
    def test_parallel_same_key_is_sent_once(self):
        class SlowShell(ShellTransport):
            async def execute(self, device_id, action):
                await asyncio.sleep(0.03)
                return await super().execute(device_id, action)

        async def exercise():
            gw = DeviceGatewayV2()
            transport = SlowShell()
            gw.register_transport(transport)
            results = await asyncio.gather(*[
                gw.execute("dev1", "device.shell", {"command": "id"},
                           idempotency_key="parallel")
                for _ in range(8)
            ])
            assert len(transport.operations) == 1
            assert len({r.action_id for r in results}) == 1

        run(exercise())

    def test_ambiguous_failure_does_not_try_second_transport(self):
        class FailingEmbedded(ShellTransport):
            def __init__(self):
                super().__init__()
                self.kind = "embedded_wss"

            async def execute(self, device_id, action):
                self.operations.append({"capability": action.capability})
                return ActionReceipt(
                    action_id=action.action_id, status=ReceiptStatus.FAILED,
                    error_code="internal", completed_at=now_ms())

        gw = DeviceGatewayV2()
        first = FailingEmbedded()
        adb = FlakyGuiTransport()
        gw.register_transport(first)
        gw.register_transport(adb)
        # Shell 优先 embedded_wss（无 SSH）；即使 internal 也不跨通道重发。
        receipt = run(gw.execute("dev1", "device.shell", {"command": "echo ok"},
                                 idempotency_key="ambiguous"))
        assert receipt.status is ReceiptStatus.FAILED
        assert len(first.operations) == 1
        assert not adb.operations

    def test_transport_exception_is_unknown_and_never_replayed(self):
        class CrashingShell(ShellTransport):
            async def execute(self, device_id, action):
                self.operations.append({"capability": action.capability})
                raise RuntimeError("socket lost after send")

        gw = DeviceGatewayV2()
        transport = CrashingShell()
        gw.register_transport(transport)
        first = run(gw.execute("dev1", "device.shell", {"command": "echo 1"},
                               idempotency_key="unknown-on-exception"))
        second = run(gw.execute("dev1", "device.shell", {"command": "echo 1"},
                                idempotency_key="unknown-on-exception"))
        assert first.status is ReceiptStatus.UNKNOWN
        assert second.action_id == first.action_id
        assert len(transport.operations) == 1

    def test_in_memory_receipts_have_configurable_capacity(self):
        gw = DeviceGatewayV2()
        gw.max_cached_receipts = 2
        gw.register_transport(gui_transport())
        for i in range(3):
            run(gw.execute("dev1", "device.tap", {"x": i, "y": i},
                           idempotency_key=f"k{i}"))
        assert gw.action_status("dev1", "k0") is None
        assert gw.action_status("dev1", "k1") is not None
        assert gw.action_status("dev1", "k2") is not None


class TestIdempotency:
    def test_same_key_returns_recorded_receipt(self):
        gw = DeviceGatewayV2()
        gui = gui_transport()
        gw.register_transport(gui)
        r1 = run(gw.execute("dev1", "device.tap", {"x": 1, "y": 2},
                            idempotency_key="same-key"))
        calls = gui.execute_calls
        r2 = run(gw.execute("dev1", "device.tap", {"x": 9, "y": 9},
                            idempotency_key="same-key"))
        assert gui.execute_calls == calls  # 未重放
        assert r2.action_id == r1.action_id
        assert r2.status == r1.status

    def test_action_status_readonly(self):
        gw = DeviceGatewayV2()
        gw.register_transport(gui_transport())
        run(gw.execute("dev1", "device.tap", {"x": 1, "y": 2},
                       idempotency_key="look-up"))
        got = gw.action_status("dev1", "look-up")
        assert got is not None and got.status is ReceiptStatus.SUCCEEDED
        assert gw.action_status("dev1", "missing") is None


class TestScopeGate:
    def test_registry_scope_denied(self):
        class FakeRegistry:
            def status_of(self, d):
                return "active"

            def scopes_of(self, d):
                return ["device.observe"]

        gw = DeviceGatewayV2(registry=FakeRegistry())
        gw.register_transport(gui_transport())
        try:
            run(gw.execute("dev1", "device.shell", {"command": "id"},
                           idempotency_key="k5"))
            assert False, "应抛 scope_denied"
        except GatewayError as e:
            assert e.code == "scope_denied"

    def test_registry_revoked(self):
        class FakeRegistry:
            def status_of(self, d):
                return "revoked"

            def scopes_of(self, d):
                return ["device.tap"]

        gw = DeviceGatewayV2(registry=FakeRegistry())
        gw.register_transport(gui_transport())
        try:
            run(gw.execute("dev1", "device.tap", {"x": 1, "y": 2},
                           idempotency_key="k6"))
            assert False
        except GatewayError as e:
            assert e.code == "device_revoked"


class TestHealth:
    def test_health_report(self):
        gw = DeviceGatewayV2()
        gw.register_transport(gui_transport())
        report = run(gw.health_report("dev1"))
        assert report.get("adb") == "connected"


class TestCapabilities:
    def test_enum_matches_contract(self):
        from whale_core.devices.capabilities import (
            action_capabilities,
            all_capabilities,
            query_operations,
        )
        assert len(all_capabilities()) == 32
        assert "device.shell" in action_capabilities()
        assert "device.output.read" in query_operations()
        assert "device.output.read" not in action_capabilities()
