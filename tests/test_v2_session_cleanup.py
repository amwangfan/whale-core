"""Deterministic disconnect/replacement races at the actual connection handler."""
import threading

import pytest

from whale_core.devices.embedded_wss import EmbeddedWssGateway


class Socket:
    def __init__(self):
        self.closed = []

    def recv(self, timeout):
        return "{}"

    def __iter__(self):
        return iter(())

    def close(self, code):
        self.closed.append(code)


@pytest.mark.parametrize("mode", ["normal", "replaced", "ack_failure", "invalid_hello"])
def test_only_current_session_disconnect_marks_device_offline(mode):
    gateway = object.__new__(EmbeddedWssGateway)
    gateway.challenge_ttl = 30
    gateway.lock = threading.RLock()
    gateway.online = {}
    gateway.pending = {}
    gateway.pending_confirm = {}
    gateway.stopping = threading.Event()
    events = []
    gateway._log_event = lambda kind, detail: events.append((kind, detail))
    gateway.send_msg = lambda *_: None
    replacement = object()
    def hello(st, raw):
        if mode == "invalid_hello":
            return False
        st.device_id = "device"
        gateway.online[st.device_id] = st
        if mode == "replaced":
            gateway.online[st.device_id] = replacement
        if mode == "ack_failure":
            raise OSError("connection lost while sending hello_ack")
        return True
    gateway._handle_hello = hello
    socket = Socket()
    gateway._handle(socket)
    assert socket.closed
    if mode == "replaced":
        assert gateway.online == {"device": replacement}
        assert events == []
    elif mode == "invalid_hello":
        assert gateway.online == {}
        assert events == []
    else:
        assert gateway.online == {}
        assert events == [("device:offline", "device")]
