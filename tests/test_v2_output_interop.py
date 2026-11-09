"""Exercise the output shapes emitted by Mobile Agent2 through the async adapter."""
import asyncio
import base64

import pytest

from whale_core.devices.embedded_wss import GatewayError
from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport


class OutputGateway:
    def __init__(self, data, truncated=False):
        self.result = {"status": "ok", "data": data, "truncated": truncated}

    def send_query(self, device_id, **kwargs):
        assert kwargs["operation"] == "device.output.read"
        return self.result


@pytest.mark.parametrize("key", ["data_base64", "bytes_b64"])
def test_binary_output_is_preserved(key):
    image = b"\xff\xd8\x00\x01\xff\xd9"
    transport = EmbeddedWssTransport(OutputGateway([
        {"content_type": "image_jpeg", key: base64.b64encode(image).decode()}]))
    assert asyncio.run(transport.read_output("dev", "agent:shot:shot-test.jpg")) == (image, False)


@pytest.mark.parametrize("outer,inner", [(False, True), (True, False), (False, False)])
def test_both_mobile_and_envelope_truncation_are_visible(outer, inner):
    transport = EmbeddedWssTransport(OutputGateway([{"text": "鲸鱼", "truncated": inner}], outer))
    assert asyncio.run(transport.read_output("dev", "agent:out:test")) == ("鲸鱼".encode(), outer or inner)


@pytest.mark.parametrize("data", [[], [{}], [{"data_base64": "@@"}], [{"data_base64": "鲸鱼"}]])
def test_missing_or_corrupt_output_is_an_error(data):
    with pytest.raises(GatewayError) as error:
        asyncio.run(EmbeddedWssTransport(OutputGateway(data)).read_output("dev", "agent:out:test"))
    assert error.value.code == "invalid_message"


def test_empty_output_is_valid():
    transport = EmbeddedWssTransport(OutputGateway([{"text": ""}]))
    assert asyncio.run(transport.read_output("dev", "agent:out:test")) == (b"", False)
