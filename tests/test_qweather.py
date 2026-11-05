"""和风天气插件：mock HTTP，不触网、不读真实 key/host。"""
from __future__ import annotations

import gzip
import io
import json
from typing import Any

import pytest

from qweather.weather import (
    QWeatherClient,
    QWeatherService,
    WeatherError,
    format_brief,
    parse_lon_lat,
    qweather_configured,
    register_qweather,
)
from whale_core.plugins.registry import PluginRegistry
from whale_core.core.runtime import ToolRegistry


class _FakeResp:
    def __init__(self, payload: dict, *, gzip_body: bool = True):
        raw = json.dumps(payload).encode()
        self._raw = gzip.compress(raw) if gzip_body else raw
        self.headers = {"Content-Encoding": "gzip"} if gzip_body else {}

    def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[str] = []

    def open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req.get_full_url())
        self.calls.append(url)
        # auth header present, value never asserted in clear text beyond non-empty
        hdrs = {k.lower(): v for k, v in req.header_items()}
        assert hdrs.get("x-qw-api-key"), "missing X-QW-Api-Key header"
        for prefix, payload in self.routes.items():
            if prefix in url:
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResp(payload)
        raise AssertionError(f"unexpected url: {url}")


CITY = {
    "code": "200",
    "location": [{
        "name": "郫都", "id": "101270107", "lat": "30.79", "lon": "103.88",
        "adm2": "成都", "adm1": "四川省", "country": "中国",
    }],
}
NOW = {
    "code": "200",
    "now": {
        "temp": "16", "feelsLike": "14", "text": "雾",
        "windDir": "西北风", "windScale": "2", "humidity": "85", "precip": "0.0",
    },
}
DAILY = {
    "code": "200",
    "daily": [
        {"fxDate": "2026-11-05", "tempMax": "20", "tempMin": "13",
         "textDay": "多云", "textNight": "多云", "precip": "0.0"},
        {"fxDate": "2026-11-06", "tempMax": "21", "tempMin": "14",
         "textDay": "多云", "textNight": "阴", "precip": "0.5"},
    ],
}


def test_parse_lon_lat_swaps_approx_latlng():
    assert parse_lon_lat("成都郫都区；约 30.7497,103.9315") == "103.93,30.75"
    assert parse_lon_lat("103.93,30.75") == "103.93,30.75"


def test_qweather_configured_requires_both():
    assert qweather_configured({"QWEATHER_API_KEY": "x", "QWEATHER_API_HOST": ""}) is False
    assert qweather_configured({"QWEATHER_API_KEY": "", "QWEATHER_API_HOST": "h.example"}) is False
    assert qweather_configured({
        "QWEATHER_API_KEY": "x",
        "QWEATHER_API_HOST": "https://example.re.qweatherapi.com",
    }) is True


def test_lookup_brief_mock_http():
    opener = _FakeOpener({
        "/geo/v2/city/lookup": CITY,
        "/v7/weather/now": NOW,
        "/v7/weather/3d": DAILY,
    })
    client = QWeatherClient(
        api_key="test-key", api_host="example.re.qweatherapi.com",
        opener=opener, cache=None)
    # fresh cache
    from qweather.weather import _TtlCache
    client.cache = _TtlCache(ttl_s=600)
    svc = QWeatherService(client)
    brief = svc.lookup_brief(place="郫都")
    assert "【和风】" in brief
    assert "16" in brief and "雾" in brief
    assert "今天" in brief and "明天" in brief
    assert len(opener.calls) == 3
    # cache hit: no extra HTTP
    again = svc.lookup_brief(place="郫都")
    assert again == brief
    assert len(opener.calls) == 3


def test_coords_path_uses_lon_lat():
    opener = _FakeOpener({
        "/geo/v2/city/lookup": CITY,
        "/v7/weather/now": NOW,
        "/v7/weather/3d": DAILY,
    })
    from qweather.weather import _TtlCache
    client = QWeatherClient(
        api_key="k", api_host="example.re.qweatherapi.com",
        opener=opener, cache=_TtlCache())
    svc = QWeatherService(client)
    svc.lookup_brief(location_hint="约 30.7497,103.9315")
    assert "location=103.93%2C30.75" in opener.calls[0] or "103.93,30.75" in opener.calls[0]


def test_fallback_search_on_auth_error():
    import urllib.error
    err = urllib.error.HTTPError("https://x", 403, "no", hdrs={}, fp=io.BytesIO())
    opener = _FakeOpener({"/geo/v2/city/lookup": err})
    from qweather.weather import _TtlCache
    client = QWeatherClient(
        api_key="k", api_host="example.re.qweatherapi.com",
        opener=opener, cache=_TtlCache())
    seen = []

    def fb(q: str) -> str:
        seen.append(q)
        return "搜索结果：晴"

    svc = QWeatherService(client, fallback_search=fb)
    out = svc.lookup_brief(place="北京", query="北京今天天气")
    assert "回落网页搜索" in out
    assert "搜索结果：晴" in out
    assert seen == ["北京今天天气"]


def test_register_skips_without_config():
    reg = PluginRegistry()
    tools = ToolRegistry()
    assert register_qweather(reg, tools, env={}) is None
    assert tools.for_capability("weather.now") == []


def test_register_mounts_tool():
    reg = PluginRegistry()
    tools = ToolRegistry()
    opener = _FakeOpener({
        "/geo/v2/city/lookup": CITY,
        "/v7/weather/now": NOW,
        "/v7/weather/3d": DAILY,
    })
    from qweather.weather import _TtlCache
    client = QWeatherClient(
        api_key="k", api_host="example.re.qweatherapi.com",
        opener=opener, cache=_TtlCache())
    svc = register_qweather(reg, tools, client=client)
    assert svc is not None
    specs = tools.for_capability("weather.now")
    assert len(specs) == 1 and specs[0].name == "weather_now"
    out = specs[0].handler({"place": "郫都"})
    assert "【和风】" in out


def test_format_brief_compact():
    text = format_brief(city=CITY["location"][0], now=NOW["now"], daily=DAILY["daily"])
    assert "湿度85%" in text
    assert "13-20°C" in text
