"""和风天气（QWeather）专用工具：Geo 城市查询 + 实时 + 3 日预报。

认证：``X-QW-Api-Key``（API KEY，非 JWT）。Host 与 Key 只从 env / 构造参数读，
**永不写入日志、异常文本、回执**。响应可能 gzip，本模块自行解压。

缺 ``QWEATHER_API_KEY`` 或 ``QWEATHER_API_HOST`` 时不注册工具；意图路由回落
``web.search``。运行期失败时若宿主注入了 ``fallback_search`` 则回落搜索。

短 TTL 进程内缓存（默认 10 分钟）节省免费额度。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

USER_AGENT = "whale-core/0.1 (+https://github.com/amwangfan/whale-core)"
DEFAULT_CACHE_TTL_S = 600
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# 坐标：十进制，API 要求最多两位小数；传参用 lon,lat。
_COORDS_RE = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*[,，]\s*(-?\d{1,3}(?:\.\d+)?)")


class WeatherError(RuntimeError):
    """和风调用失败分类：config / auth / network / bad_response / quota。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def qweather_configured(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    key = (env.get("QWEATHER_API_KEY") or "").strip()
    host = (env.get("QWEATHER_API_HOST") or "").strip()
    return bool(key and host)


def _normalize_host(host: str) -> str:
    h = (host or "").strip()
    if h.startswith("https://"):
        h = h[len("https://"):]
    elif h.startswith("http://"):
        h = h[len("http://"):]
    return h.strip().rstrip("/")


def _round_coord(value: float) -> str:
    return f"{float(value):.2f}"


def parse_lon_lat(text: str) -> str | None:
    """从位置摘要抽出 lon,lat（两位小数）。支持「约 lat,lng」与「lon,lat」。

    眼镜默认摘要形如「成都郫都…；约 30.7497,103.9315」（先 lat 后 lng），
    和风要求 ``location=lon,lat``，故对「约」前缀按 lat,lng 解读；
    无「约」且数值像中国范围（lon 70–140、lat 15–55）时按 lon,lat 原样。
    """
    if not text:
        return None
    m = _COORDS_RE.search(text)
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    prefix = text[max(0, m.start() - 4): m.start()]
    # 「约 lat,lng」或明显 lat,lng（纬度在前、经度在后）
    if "约" in prefix or (15 <= a <= 55 and 70 <= b <= 140):
        lat, lon = a, b
    elif 70 <= a <= 140 and 15 <= b <= 55:
        lon, lat = a, b
    else:
        lon, lat = (a, b) if abs(a) >= abs(b) else (b, a)
    return f"{_round_coord(lon)},{_round_coord(lat)}"


_WEATHER_QUESTIONISH = re.compile(
    r"(天气|气温|温度|预报|穿什么|空气质量|aqi|下雨|下雪|台风|"
    r"怎么样|如何|合适|多少度|冷不冷|热不热)",
    re.IGNORECASE,
)
_AT_MENTION = re.compile(r"@\S+")


def extract_place_name(text: str) -> str:
    """去掉坐标片段后的地名短串，供 Geo lookup。

    整句天气问句（无独立地名）不得当城市名——否则 Geo 必败并误回落搜索。
    """
    if not text:
        return ""
    cleaned = _AT_MENTION.sub(" ", text)
    cleaned = _COORDS_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[；;]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ；;，,。")
    if not cleaned:
        return ""
    # 问句特征明显且没有「X市/区」类短地名时，视为无地名。
    if _WEATHER_QUESTIONISH.search(cleaned) and not re.search(
            r"[\u4e00-\u9fff]{2,12}(?:省|市|县|区|州|盟|旗|镇)", cleaned):
        return ""
    return cleaned[:80]


@dataclass
class _CacheEntry:
    expires: float
    payload: str


class _TtlCache:
    def __init__(self, ttl_s: float = DEFAULT_CACHE_TTL_S):
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._data: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            ent = self._data.get(key)
            if ent is None:
                return None
            if ent.expires <= now:
                self._data.pop(key, None)
                return None
            return ent.payload

    def put(self, key: str, payload: str) -> None:
        with self._lock:
            self._data[key] = _CacheEntry(
                expires=time.monotonic() + self.ttl_s, payload=payload)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = None


def _opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(_NoRedirect())
    return _OPENER


def _decode_body(raw: bytes, headers) -> bytes:
    encoding = ""
    try:
        encoding = (headers.get("Content-Encoding") or "").lower()
    except Exception:
        encoding = ""
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


class QWeatherClient:
    """最小和风客户端：city lookup / now / 3d。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_host: str | None = None,
        timeout: float = 8.0,
        env: Mapping[str, str] | None = None,
        cache: _TtlCache | None = None,
        opener=None,
    ):
        env = os.environ if env is None else env
        self.api_key = (api_key if api_key is not None else env.get("QWEATHER_API_KEY", "")).strip()
        self.api_host = _normalize_host(
            api_host if api_host is not None else env.get("QWEATHER_API_HOST", ""))
        self.timeout = timeout
        self.cache = cache if cache is not None else _TtlCache()
        self._opener = opener

    def configured(self) -> bool:
        return bool(self.api_key and self.api_host)

    def _get_json(self, path: str, params: Mapping[str, str]) -> dict:
        if not self.configured():
            raise WeatherError("config", "qweather not configured")
        qs = urllib.parse.urlencode(params)
        url = f"https://{self.api_host}{path}?{qs}"
        req = urllib.request.Request(url, headers={
            "X-QW-Api-Key": self.api_key,
            "Accept-Encoding": "gzip",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        opener = self._opener if self._opener is not None else _opener()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = _decode_body(resp.read(MAX_RESPONSE_BYTES), resp.headers)
                data = json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                raise WeatherError("auth", f"qweather auth failed ({status})") from exc
            if status == 429:
                raise WeatherError("quota", "qweather rate limited (429)") from exc
            if 500 <= status < 600:
                raise WeatherError("network", f"qweather server error ({status})") from exc
            if 300 <= status < 400:
                raise WeatherError("bad_response", f"qweather redirect refused ({status})") from exc
            raise WeatherError("bad_response", f"qweather HTTP error ({status})") from exc
        except (urllib.error.URLError, TimeoutError, gzip.BadGzipFile) as exc:
            raise WeatherError("network", f"qweather network error: {type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise WeatherError("bad_response", "qweather returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise WeatherError("bad_response", "qweather response not object")
        code = str(data.get("code") or "")
        if code == "200":
            return data
        if code in ("401", "403"):
            raise WeatherError("auth", f"qweather api code {code}")
        if code in ("402", "429"):
            raise WeatherError("quota", f"qweather api code {code}")
        raise WeatherError("bad_response", f"qweather api code {code or 'missing'}")

    def lookup_city(self, location: str, *, lang: str = "zh") -> dict:
        loc = (location or "").strip()
        if not loc:
            raise WeatherError("bad_response", "empty location")
        data = self._get_json("/geo/v2/city/lookup", {
            "location": loc, "lang": lang, "number": "1",
        })
        locs = data.get("location")
        if not isinstance(locs, list) or not locs:
            raise WeatherError("bad_response", "city not found")
        first = locs[0]
        if not isinstance(first, dict) or not first.get("id"):
            raise WeatherError("bad_response", "city lookup missing id")
        return first

    def weather_now(self, location_id: str, *, lang: str = "zh") -> dict:
        data = self._get_json("/v7/weather/now", {
            "location": location_id, "lang": lang,
        })
        now = data.get("now")
        if not isinstance(now, dict):
            raise WeatherError("bad_response", "weather now missing")
        return now

    def weather_3d(self, location_id: str, *, lang: str = "zh") -> list[dict]:
        data = self._get_json("/v7/weather/3d", {
            "location": location_id, "lang": lang,
        })
        daily = data.get("daily")
        if not isinstance(daily, list) or not daily:
            raise WeatherError("bad_response", "weather 3d missing")
        return [d for d in daily if isinstance(d, dict)]


def format_brief(
    *,
    city: Mapping[str, Any],
    now: Mapping[str, Any],
    daily: list[Mapping[str, Any]],
) -> str:
    """短精炼中文摘要，供模型直接朗读/改写。"""
    name = str(city.get("name") or "")
    adm2 = str(city.get("adm2") or "")
    adm1 = str(city.get("adm1") or "")
    place = name
    if adm2 and adm2 not in place:
        place = f"{adm2}{name}" if name else adm2
    if adm1 and adm1 not in place and adm1.rstrip("省市") not in place:
        place = f"{place}（{adm1}）" if place else adm1

    temp = now.get("temp")
    feels = now.get("feelsLike")
    text = now.get("text") or ""
    wind = f"{now.get('windDir') or ''}{now.get('windScale') or ''}级".strip()
    if wind.endswith("级级"):
        wind = wind[:-1]
    humidity = now.get("humidity")
    precip_now = now.get("precip")

    lines = [f"【和风】{place or '当前位置'}"]
    now_bits = []
    if text:
        now_bits.append(str(text))
    if temp is not None and str(temp) != "":
        now_bits.append(f"{temp}°C")
    if feels is not None and str(feels) != "" and str(feels) != str(temp):
        now_bits.append(f"体感{feels}°C")
    if wind and wind != "级":
        now_bits.append(wind)
    if humidity is not None and str(humidity) != "":
        now_bits.append(f"湿度{humidity}%")
    if precip_now not in (None, "", "0", "0.0"):
        now_bits.append(f"降水{precip_now}mm")
    lines.append("现在：" + "，".join(now_bits) if now_bits else "现在：暂无实况")

    labels = ("今天", "明天", "后天")
    for i, day in enumerate(daily[:2]):
        label = labels[i] if i < len(labels) else str(day.get("fxDate") or "")
        tmin, tmax = day.get("tempMin"), day.get("tempMax")
        day_text = day.get("textDay") or ""
        night_text = day.get("textNight") or ""
        cond = day_text
        if night_text and night_text != day_text:
            cond = f"{day_text}/{night_text}"
        precip = day.get("precip")
        bit = f"{label}：{cond} {tmin}-{tmax}°C"
        if precip not in (None, "", "0", "0.0"):
            bit += f"，降水{precip}mm"
        lines.append(bit)
    return "\n".join(lines)


def resolve_location_param(
    *,
    place: str = "",
    coords: str = "",
    query: str = "",
    location_hint: str = "",
) -> tuple[str, str]:
    """返回 (lookup_key, mode)——mode 为 city|coords。

    优先口述城市 ``place``；否则坐标；否则从 hint/query 解析。
    """
    place = (place or "").strip()
    if place:
        return place, "city"
    coords = (coords or "").strip()
    if coords and "," in coords:
        parsed = parse_lon_lat(coords) or coords
        return parsed, "coords"
    for blob in (location_hint, query):
        parsed = parse_lon_lat(blob or "")
        if parsed:
            return parsed, "coords"
        name = extract_place_name(blob or "")
        if name and name not in ("未知",):
            return name, "city"
    raise WeatherError("bad_response", "no place or coords")


_PLACE_FILLER = re.compile(
    r"^(?:现在|目前|此刻|这会儿|这会|今天|明天|后天|今晚|明晚|今早|明早|今日|明日|"
    r"晚上|早上|上午|下午|中午|夜里|周末|请问|那|那么|你知道|知道|帮我|帮忙|给我|"
    r"看看|查查|查一下|看一下|问一下|想问|我想知道|我想问)+")


class QWeatherService:
    def _lookup_with_retry(self, *, place: str, coords: str, query: str,
                           location_hint: str) -> dict:
        """Geo 查城市：口述地名 → 去前缀地名 → 去「区/县」后缀 → 当前位置 hint/坐标。

        只有全部候选失败才抛 WeatherError（由调用方回落网页搜索）。
        """
        candidates: list[tuple[str, str, str, str]] = []
        raw = (place or "").strip()
        if raw:
            cleaned = _PLACE_FILLER.sub("", raw)
            for cand in (raw, cleaned, cleaned[:-1] if cleaned[-1:] in "区县" else ""):
                if len(cand) >= 2 and (cand, "", "", "") not in candidates:
                    candidates.append((cand, "", "", ""))
        candidates.append(("", coords, query, location_hint))
        if location_hint or coords:
            candidates.append(("", coords, "", location_hint))
        last: WeatherError | None = None
        for p_, c_, q_, h_ in candidates:
            try:
                key, _mode = resolve_location_param(
                    place=p_, coords=c_, query=q_, location_hint=h_)
                return self.client.lookup_city(key)
            except WeatherError as exc:
                last = exc
        raise last or WeatherError("bad_response", "no place or coords")

    def __init__(
        self,
        client: QWeatherClient,
        *,
        fallback_search: Callable[[str], str] | None = None,
    ):
        self.client = client
        self.fallback_search = fallback_search

    def lookup_brief(
        self,
        *,
        place: str = "",
        coords: str = "",
        query: str = "",
        location_hint: str = "",
    ) -> str:
        cache_key = "|".join([
            place.strip(), coords.strip(),
            (location_hint or "")[:80], (query or "")[:80],
        ])
        hit = self.client.cache.get(cache_key)
        if hit is not None:
            return hit
        try:
            city = self._lookup_with_retry(
                place=place, coords=coords, query=query,
                location_hint=location_hint)
            loc_id = str(city["id"])
            now = self.client.weather_now(loc_id)
            daily = self.client.weather_3d(loc_id)
            brief = format_brief(city=city, now=now, daily=daily)
            self.client.cache.put(cache_key, brief)
            return brief
        except WeatherError as exc:
            if self.fallback_search is not None:
                q = (query or place or location_hint or "天气").strip()[:400]
                try:
                    return "【和风暂不可用，已回落网页搜索】\n" + self.fallback_search(q)
                except Exception:
                    pass
            raise WeatherError(exc.kind, f"weather:{exc.kind}") from exc


class _WeatherExecutor:
    def __init__(self, service: QWeatherService):
        self.service = service

    def execute(self, action: str, target: str, args: Mapping[str, Any]) -> tuple[bool, str]:
        try:
            brief = self.service.lookup_brief(
                place=str(args.get("place") or args.get("city") or target or ""),
                coords=str(args.get("coords") or args.get("location") or ""),
                query=str(args.get("query") or ""),
                location_hint=str(args.get("location_hint") or ""),
            )
        except WeatherError as exc:
            return False, f"weather:{exc.kind}"
        except Exception:
            return False, "weather:error"
        return True, brief[:200]


def register_qweather(
    registry: PluginRegistry,
    tools=None,
    *,
    env: Mapping[str, str] | None = None,
    client: QWeatherClient | None = None,
    fallback_search: Callable[[str], str] | None = None,
):
    """宿主注册。未配置 key/host 时跳过（返回 None）。"""
    env = os.environ if env is None else env
    client = client if client is not None else QWeatherClient(env=env)
    if not client.configured():
        return None
    service = QWeatherService(client, fallback_search=fallback_search)

    def _handler(args: Mapping[str, Any]) -> str:
        return service.lookup_brief(
            place=str(args.get("place") or args.get("city") or ""),
            coords=str(args.get("coords") or args.get("location") or ""),
            query=str(args.get("query") or ""),
            location_hint=str(args.get("location_hint") or ""),
        )

    registry.register(
        PluginManifest(
            plugin_id="qweather", name="和风天气", version="1.0.0",
            kind="output", actions=("lookup",),
            action_risk={"lookup": "read_only"},
            description="QWeather 实时与 3 日预报（API KEY）",
        ),
        executor=_WeatherExecutor(service),
    )
    if tools is not None:
        from whale_core.core.runtime import ToolRegistry, ToolSpec
        if not isinstance(tools, ToolRegistry):
            raise TypeError("tools must be a ToolRegistry")
        tools.register(ToolSpec(
            name="weather_now",
            capability="weather.now",
            plugin_id="qweather",
            action="lookup",
            risk_class="read_only",
            description="查询实时天气与今明预报（口述城市优先，否则用坐标/默认位置）",
            handler=_handler,
        ))
    return service
