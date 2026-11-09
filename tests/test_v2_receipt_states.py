import asyncio

import pytest

from whale_core.devices.transports.base import ActionRequest, ReceiptStatus
from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport


class RecordedGateway:
    def __init__(self, record):
        self.record = record
        self.keys = []

    def send_action(self, device, **kwargs):
        self.keys.append(kwargs["idempotency_key"])
        return {"duplicate": True, "action_id": "act_original", **self.record}


def request():
    return ActionRequest("act_retry", "device.tap", {}, "existing-key-0001")


@pytest.mark.parametrize("state", ["pending", "received"])
def test_duplicate_inflight_remains_nonterminal(state):
    gateway = RecordedGateway({"state": state})
    receipt = asyncio.run(EmbeddedWssTransport(gateway).execute("dev", request()))
    assert receipt.status is ReceiptStatus.RECEIVED
    assert not receipt.status.terminal
    assert receipt.action_id == "act_original"
    assert receipt.completed_at is None
    assert gateway.keys == ["existing-key-0001"]


def test_record_state_overrides_stale_embedded_receipt():
    gateway = RecordedGateway({"state": "unknown", "receipt": {
        "status": "received", "completed_at": 123}})
    receipt = asyncio.run(EmbeddedWssTransport(gateway).execute("dev", request()))
    assert receipt.status is ReceiptStatus.UNKNOWN
    assert receipt.action_id == "act_original"


def test_terminal_receipt_keeps_result_and_timestamps():
    gateway = RecordedGateway({"state": "succeeded", "receipt": {
        "status": "succeeded", "started_at": 100, "completed_at": 200,
        "result": {"summary": "clicked", "output_ref": "agent:out:test"}}})
    receipt = asyncio.run(EmbeddedWssTransport(gateway).execute("dev", request()))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    assert (receipt.started_at, receipt.completed_at) == (100, 200)
    assert receipt.summary == "clicked"
    assert receipt.output_ref == "agent:out:test"
