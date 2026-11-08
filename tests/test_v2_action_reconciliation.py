import asyncio
import threading
from types import SimpleNamespace

import pytest

from whale_core.devices.embedded_wss import EmbeddedWssGateway, GatewayError
from whale_core.devices.gateway_v2 import DeviceGatewayV2
from whale_core.devices.transports.base import ActionReceipt, ReceiptStatus, new_id, new_ulid, now_ms
from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport
from whale_core.memory.store import InMemoryStore
from whale_core.devices.schema import ContractValidator


def persistent_gateway(state="pending", expired=False):
    gateway = object.__new__(EmbeddedWssGateway)
    gateway.store = InMemoryStore()
    gateway.lock = threading.RLock()
    gateway.pending = {}
    gateway._log_event = None
    gateway._check_scope = lambda *_: None
    gateway._online_or_raise = lambda _: pytest.fail("duplicate must not require a live connection")
    record = {"device_id": "dev", "action_id": "act_original", "capability": "device.tap",
              "state": state, "expires_at": now_ms() + (-1000 if expired else 60000),
              "receipt": {"result": {"summary": "saved"}}}
    gateway.store.put("device:action:dev:existing-key", record)
    gateway.store.put("device:action-id:dev:act_original", "existing-key")
    return gateway


def test_offline_duplicate_uses_same_state_projection_as_status():
    gateway = persistent_gateway(expired=True)
    duplicate = gateway.send_action("dev", capability="device.tap", idempotency_key="existing-key")
    status = gateway.action_status("dev", "existing-key")
    assert duplicate == {**status, "duplicate": True}
    assert status["state"] == "unknown" and status["outcome_source"] == "expires_at"
    assert gateway.store.get("device:action:dev:existing-key")["state"] == "pending"


def test_status_returns_detached_nested_snapshot():
    gateway = persistent_gateway()
    result = gateway.action_status("dev", "existing-key")
    result["receipt"]["result"]["summary"] = "changed"
    assert gateway.action_status("dev", "existing-key")["receipt"]["result"]["summary"] == "saved"


def test_late_real_receipt_reconciles_without_resending():
    gateway = persistent_gateway(expired=True)
    transport = EmbeddedWssTransport(gateway)
    first = asyncio.run(transport.action_status("dev", "existing-key"))
    assert first.status is ReceiptStatus.UNKNOWN
    assert first.provider_ref == "core:estimated-outcome"
    gateway._on_action_receipt(SimpleNamespace(device_id="dev"), {"payload": {
        "action_id": "act_original", "status": "succeeded", "result": {"summary": "done"}}})
    result = asyncio.run(transport.action_status("dev", "existing-key"))
    assert result.status is ReceiptStatus.SUCCEEDED and result.summary == "done"


def test_missing_durable_key_is_not_a_retry():
    assert asyncio.run(EmbeddedWssTransport(persistent_gateway()).action_status("dev", "missing-key")) is None


class StatusTransport:
    kind = "embedded_wss"
    def __init__(self):
        self.sent = self.reads = 0
        self.action_id = None
        self.gate = None
    async def capabilities(self, device):
        return ["device.tap"]
    async def execute(self, device, action):
        self.sent += 1
        self.action_id = action.action_id
        return ActionReceipt(action.action_id, ReceiptStatus.RECEIVED)
    async def action_status(self, device, key):
        self.reads += 1
        if self.gate is not None:
            await self.gate.wait()
        else:
            await asyncio.sleep(0.01)
        return ActionReceipt(self.action_id, ReceiptStatus.SUCCEEDED, summary="durable")


def test_cached_nonterminal_refreshes_original_channel_only_once():
    async def run():
        gateway, transport = DeviceGatewayV2(), StatusTransport()
        gateway.register_transport(transport)
        first = await gateway.execute("dev", "device.tap", {}, idempotency_key="same-key")
        assert not first.status.terminal
        results = await asyncio.gather(*[
            gateway.execute("dev", "device.tap", {}, idempotency_key="same-key") for _ in range(8)])
        assert all(r.status is ReceiptStatus.SUCCEEDED for r in results)
        assert (transport.sent, transport.reads) == (1, 1)
        assert gateway.action_status("dev", "same-key").summary == "durable"
    asyncio.run(run())


def test_cancelled_status_waiter_does_not_cancel_shared_reconciliation():
    async def run():
        gateway, transport = DeviceGatewayV2(), StatusTransport()
        transport.gate = asyncio.Event()
        gateway.register_transport(transport)
        await gateway.execute("dev", "device.tap", {}, idempotency_key="same-key")
        task = asyncio.create_task(gateway.reconcile_action("dev", "same-key"))
        while not transport.reads:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        other = asyncio.create_task(gateway.reconcile_action("dev", "same-key"))
        transport.gate.set()
        assert (await other).status is ReceiptStatus.SUCCEEDED
        assert transport.sent == transport.reads == 1
        assert not gateway._reconciling
    asyncio.run(run())


def test_route_cache_eviction_is_bounded_with_receipts():
    async def run():
        gateway, transport = DeviceGatewayV2(), StatusTransport()
        gateway.max_cached_receipts = 2
        gateway.register_transport(transport)
        for index in range(5):
            await gateway.execute("dev", "device.tap", {}, idempotency_key=str(index))
        assert len(gateway._receipts) == len(gateway._receipt_routes) == 2
        assert await gateway.reconcile_action("dev", "0") is None
        assert transport.sent == 5
    asyncio.run(run())


def test_inflight_task_metadata_survives_durable_status_read():
    gateway = persistent_gateway()
    gateway.contracts = ContractValidator()
    gateway._online_or_raise = lambda _: object()
    gateway._await_response = lambda st, key, *args: {"action_id": key, "status": "received"}
    task, run, observation = new_id("task"), new_id("run"), new_id("obs")
    gateway.send_action("dev", capability="device.tap", arguments={}, idempotency_key="new-task-key",
                        task_id=task, run_id=run, observation_id=observation, correlation_id=new_ulid())
    stored = gateway.action_status("dev", "new-task-key")
    assert (stored["task_id"], stored["run_id"], stored["observation_id"]) == (task, run, observation)
    receipt = asyncio.run(EmbeddedWssTransport(gateway).action_status("dev", "new-task-key"))
    assert receipt.task_id == task and receipt.status is ReceiptStatus.RECEIVED
