import io
import threading
from email.message import Message
from types import SimpleNamespace

import pytest

from whale_core.life.bounded_call import BoundedCall
from whale_core.memory.owner_fact_worker import OwnerFactWorker
from whale_core.security.json_input import loads
from whale_core.devices.embedded_wss import EmbeddedWssGateway


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "bad", 10**1000])
def test_invalid_call_timeout_never_invokes_model(value):
    calls = []
    gate = BoundedCall(lambda: calls.append(True))
    assert gate(default="fallback", timeout_s=value) == "fallback"
    assert calls == [] and not gate.busy


@pytest.mark.parametrize("options", [{"poll_interval": 0}, {"poll_interval": float("nan")},
    {"model_timeout": float("inf")}, {"lease_seconds": True}, {"max_retries": 1.5}, {"max_retries": -1}])
def test_worker_configuration_rejected_before_thread_creation(options):
    args = dict(call_gate=None, worker_id="test", lease_seconds=30,
                model_timeout=10, poll_interval=0.2, max_retries=2, now=None)
    with pytest.raises(ValueError):
        OwnerFactWorker(None, None, None, **{**args, **options})


@pytest.mark.parametrize("raw", ['{"n":NaN}', '{"n":1e10000}', '{"a":1,"a":2}',
    '{"payload":{"a":1,"a":2}}'])
def test_http_and_device_inputs_share_strict_json(raw):
    with pytest.raises(ValueError):
        loads(raw)
    gateway = object.__new__(EmbeddedWssGateway)
    errors = []
    gateway.send_error = lambda st, ref, code, detail: errors.append(code)
    assert gateway._handle_frame(object(), raw) is True
    assert errors == ["invalid_message"]
    from whale_core.api.server import _Handler
    headers = Message()
    encoded = raw.encode()
    headers["Content-Length"] = str(len(encoded))
    assert _Handler._body_json(SimpleNamespace(headers=headers, rfile=io.BytesIO(encoded))) is None


def test_valid_json_keeps_precision_and_unicode():
    assert loads('{"n":9007199254740993,"text":"鲸鱼"}') == {
        "n": 9007199254740993, "text": "鲸鱼"}


def test_closed_model_gate_never_accepts_more_work():
    calls = []
    gate = BoundedCall(lambda: calls.append(True))
    gate.close()
    gate.close()
    assert gate(default="closed") == "closed" and calls == []


def test_closing_does_not_cancel_an_external_call_or_allow_another():
    entered, release = threading.Event(), threading.Event()
    def model():
        entered.set()
        assert release.wait(3)
        return "completed"
    gate = BoundedCall(model)
    results = []
    caller = threading.Thread(target=lambda: results.append(gate()))
    caller.start()
    try:
        assert entered.wait(2)
        gate.close()
        assert gate.busy and gate(default="closed") == "closed"
    finally:
        release.set()
        caller.join(3)
    assert results == ["completed"] and not gate.busy


def test_host_closes_owned_gate_even_when_worker_stop_fails():
    from whale_core.core.owner_fact_host import OwnerFactStack
    closed = []
    class Worker:
        def stop(self, **kwargs):
            raise RuntimeError("stop failed")
    stack = OwnerFactStack(worker=Worker(), call_gate=SimpleNamespace(close=lambda: closed.append(True)))
    stack.shutdown()
    assert closed == [True]
