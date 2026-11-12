import asyncio
import base64

import pytest

from whale_core.devices.embedded_wss import GatewayError
from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport


class PagedGateway:
    def __init__(self, content, mutate=None):
        self.content, self.mutate, self.offsets = content, mutate, []

    def send_query(self, device, **kwargs):
        assert kwargs["filters"]["page_format"] == "bytes.v1"
        offset, limit = kwargs["filters"]["offset"], kwargs["filters"]["max_bytes"]
        self.offsets.append(offset)
        chunk = self.content[offset:offset + min(limit, 997)]
        end = offset + len(chunk)
        item = {"bytes_b64": base64.b64encode(chunk).decode(), "offset": offset,
                "next_offset": end, "total_bytes": len(self.content), "truncated": end < len(self.content)}
        if self.mutate:
            self.mutate(item)
        return {"status": "ok", "data": [item], "truncated": item["truncated"]}


def test_full_multibyte_and_binary_output_across_many_pages():
    content = ("鲸鱼🐋" * 20000).encode() + b"\x00\xff\x13"
    gateway = PagedGateway(content)
    assert asyncio.run(EmbeddedWssTransport(gateway).read_output_complete("dev", "agent:ui:obs")) == content
    assert len(gateway.offsets) > 100
    assert gateway.offsets == sorted(set(gateway.offsets))


@pytest.mark.parametrize("content", [b"", b"small"])
def test_empty_and_small_outputs(content):
    assert asyncio.run(EmbeddedWssTransport(PagedGateway(content)).read_output_complete("dev", "ref")) == content


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(next_offset=0), lambda p: p.update(offset=4),
    lambda p: p.update(total_bytes=True), lambda p: p.update(source_truncated=True),
    lambda p: p.update(truncated=False),
])
def test_malformed_pages_are_rejected(mutate):
    with pytest.raises(GatewayError):
        asyncio.run(EmbeddedWssTransport(PagedGateway(b"a" * 2000, mutate)).read_output_complete("dev", "ref"))


def test_read_budget_stops_further_queries():
    gateway = PagedGateway(b"a" * 20000)
    with pytest.raises(GatewayError, match="预算"):
        asyncio.run(EmbeddedWssTransport(gateway).read_output_complete("dev", "ref", max_bytes=1000))
    assert gateway.offsets == [0]  # declared total rejects the source on its first page


@pytest.mark.parametrize("offset,limit", [(-1, 1), (True, 1), (0, 0), (0, float("inf"))])
def test_invalid_read_arguments_do_not_send(offset, limit):
    gateway = PagedGateway(b"x")
    with pytest.raises(GatewayError):
        asyncio.run(EmbeddedWssTransport(gateway).read_output("dev", "ref", offset=offset, max_bytes=limit))
    assert not gateway.offsets
