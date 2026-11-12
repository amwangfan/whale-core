import pytest
import asyncio

from whale_core.devices.output_pages import decode_page, collect_pages, OutputPage


def test_oversized_base64_is_rejected_before_decoder_allocation(monkeypatch):
    monkeypatch.setattr("whale_core.devices.output_pages.base64.b64decode",
                        lambda *args, **kwargs: pytest.fail("must reject before decoding"))
    with pytest.raises(ValueError, match="请求长度"):
        decode_page({"data": [{"bytes_b64": "AAAA" * 10000}]}, 0, 1)


@pytest.mark.parametrize("text,limit", [("鲸", 2), ("🐋", 3), ("abc", 2)])
def test_text_budget_uses_bytes_even_with_character_precheck(text, limit):
    with pytest.raises(ValueError):
        decode_page({"data": [{"text": text}]}, 0, limit)


@pytest.mark.parametrize("offset,limit", [(True, 1), (-1, 1), (0, False), (0, 0), (0, 49153)])
def test_invalid_request_boundary(offset, limit):
    with pytest.raises(ValueError):
        decode_page({"data": [{"text": "a"}]}, offset, limit)


def test_legacy_page_also_obeys_requested_byte_limit():
    with pytest.raises(ValueError):
        decode_page({"data": [{"text": "鲸"}]}, 0, 2)


def test_nonobject_response_fails_explicitly():
    with pytest.raises(ValueError):
        decode_page([], 0, 1)


@pytest.mark.parametrize("extra", [{"offset": 0}, {"next_offset": None}, {"total_bytes": None},
    {"truncated": "false"}, {"source_truncated": 1}])
def test_partial_metadata_and_nonboolean_flags_fail(extra):
    with pytest.raises(ValueError):
        decode_page({"data": [{"text": "a", **extra}]}, 0, 1)


@pytest.mark.parametrize("page", [OutputPage(b"a", False, 1, 2),
    OutputPage(b"a", False, True, 1), OutputPage(b"a", "false", None, None),
    OutputPage(b"aa", False, None, None)])
def test_collector_validates_provider_pages(page):
    async def fetch(offset, limit):
        return page
    with pytest.raises(ValueError):
        asyncio.run(collect_pages(fetch, max_bytes=1))


def test_metadata_cannot_appear_midstream():
    async def fetch(offset, limit):
        return OutputPage(b"a", True, 1, None) if offset == 0 else OutputPage(b"b", False, 2, 2)
    with pytest.raises(ValueError):
        asyncio.run(collect_pages(fetch))
