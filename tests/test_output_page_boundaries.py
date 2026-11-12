import pytest

from whale_core.devices.output_pages import decode_page


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
