import asyncio

import pytest

from whale_core.devices.output_pages import OutputPage, collect_pages
from whale_core.devices.embedded_wss import GatewayError
from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport
from test_v2_output_paging import PagedGateway


@pytest.mark.parametrize("options", [{"timeout_s": True}, {"timeout_s": float("nan")},
    {"timeout_s": 0}, {"timeout_s": 10**1000}, {"max_pages": False}, {"max_pages": 0}])
def test_invalid_limits_never_query_device(options):
    gateway = PagedGateway(b"abc")
    with pytest.raises(GatewayError) as error:
        asyncio.run(EmbeddedWssTransport(gateway).read_output_complete("dev", "ref", **options))
    assert error.value.code == "invalid_message" and gateway.offsets == []


def test_page_budget_bounds_tiny_page_sources():
    calls = []
    async def fetch(offset, limit):
        calls.append(offset)
        return OutputPage(b"a", True, offset + 1, 100)
    with pytest.raises(ValueError, match="分页请求预算"):
        asyncio.run(collect_pages(fetch, max_pages=3))
    assert calls == [0, 1, 2]


def test_total_deadline_cancels_fetch_and_runs_its_cleanup():
    cleaned = []
    async def fetch(offset, limit):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)
    with pytest.raises(TimeoutError):
        asyncio.run(collect_pages(fetch, timeout_s=0.02))
    assert cleaned == [True]


def test_transport_maps_total_timeout_and_preserves_external_cancellation():
    class Slow(EmbeddedWssTransport):
        async def _read_page(self, *args, **kwargs):
            await asyncio.Event().wait()
    with pytest.raises(GatewayError) as error:
        asyncio.run(Slow(None).read_output_complete("dev", "ref", timeout_s=0.02))
    assert error.value.code == "expired"
    async def cancel():
        task = asyncio.create_task(Slow(None).read_output_complete("dev", "ref"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())


def test_known_oversized_output_stops_after_first_page():
    gateway = PagedGateway(b"x" * 10000)
    with pytest.raises(GatewayError, match="预算"):
        asyncio.run(EmbeddedWssTransport(gateway).read_output_complete("dev", "ref", max_bytes=1000))
    assert gateway.offsets == [0]
