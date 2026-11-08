import asyncio
import base64
import json

import pytest

from whale_core.devices.embedded_wss import GatewayError
from whale_core.devices.observation_codec import observation
from whale_core.devices.transports.base import ObserveRequest
from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport


def mobile_payload():
    return {"observation_id": "obs_from_phone", "captured_at": 1794000000123,
            "screen": {"w": 1080, "h": 2400}, "foreground_package": "com.example",
            "screenshot_ref": "agent:shot:shot-123.jpg", "elements": [{
                "element_id": 1, "bounds": "[10,20][210,80]", "text": "鲸鱼",
                "clickable": True, "fingerprint": "actual-fingerprint"}]}


class ObservationGateway:
    def __init__(self, payload):
        self.payload = payload
        self.offsets = []
    def send_action(self, device, **kwargs):
        return {"action_id": "act_from_phone", "status": "succeeded",
                "result": {"output_ref": "agent:ui:obs_from_phone"}}
    def send_query(self, device, **kwargs):
        offset = kwargs["filters"]["offset"]
        self.offsets.append(offset)
        raw = json.dumps(self.payload, ensure_ascii=False).encode()
        chunk = raw[offset:offset + 37]
        end = offset + len(chunk)
        return {"status": "ok", "data": [{"bytes_b64": base64.b64encode(chunk).decode(),
            "offset": offset, "next_offset": end, "total_bytes": len(raw), "truncated": end < len(raw)}]}


def test_actual_mobile_observation_fields_and_paged_utf8():
    gateway = ObservationGateway(mobile_payload())
    result = asyncio.run(EmbeddedWssTransport(gateway).observe("device", ObserveRequest()))
    assert result.taken_at == 1794000000123
    assert (result.screen_width, result.screen_height) == (1080, 2400)
    assert result.elements[0].center == (110, 50)
    assert result.elements[0].text == "鲸鱼"
    assert result.screenshot_ref == "agent:shot:shot-123.jpg"
    assert len(gateway.offsets) > 1


def test_coordinate_encodings_produce_same_signature():
    payload = mobile_payload()
    first = observation("dev", payload, ObserveRequest())
    payload["elements"][0]["bounds"] = [10, 20, 210, 80]
    payload["elements"][0]["element_id"] = 44
    assert observation("dev", payload, ObserveRequest()).ui_signature == first.ui_signature


def test_bad_element_is_skipped_and_marked_partial():
    payload = mobile_payload()
    payload["elements"].append({"element_id": 2, "bounds": "garbage", "clickable": True})
    result = observation("dev", payload, ObserveRequest())
    assert len(result.elements) == 1 and result.truncated


@pytest.mark.parametrize("change", [
    lambda p: p.update(elements=None), lambda p: p.update(elements=[{}]),
    lambda p: p["elements"].append(dict(p["elements"][0])),
])
def test_invalid_observation_fails_explicitly(change):
    payload = mobile_payload()
    change(payload)
    with pytest.raises(GatewayError):
        asyncio.run(EmbeddedWssTransport(ObservationGateway(payload)).observe("dev", ObserveRequest()))


def test_request_limits_and_screenshot_flags_are_respected():
    payload = mobile_payload()
    payload["elements"].append({"element_id": 2, "bounds": [0, 0, 10, 10]})
    result = observation("dev", payload, ObserveRequest(screenshot=False, max_elements=1))
    assert result.screenshot_ref is None
    assert len(result.elements) == 1 and result.truncated


def test_missing_screenshot_is_never_substituted_with_ui_json():
    payload = mobile_payload()
    del payload["screenshot_ref"]
    result = asyncio.run(EmbeddedWssTransport(ObservationGateway(payload)).observe("dev", ObserveRequest()))
    assert result.screenshot_ref is None


@pytest.mark.parametrize("options", [ObserveRequest(max_elements=0),
    ObserveRequest(max_elements=True), ObserveRequest(screenshot="false")])
def test_invalid_request_fails_before_capture(options):
    gateway = ObservationGateway(mobile_payload())
    def no_action(*args, **kwargs):
        pytest.fail("invalid options must not capture")
    gateway.send_action = no_action
    with pytest.raises(GatewayError):
        asyncio.run(EmbeddedWssTransport(gateway).observe("dev", options))
