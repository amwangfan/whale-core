"""web.search 官方插件：可插拔搜索 provider（roadmap §8 P0-6）。

provider 选择链（调研依据见 docs/gap-analysis-2026-10-30.md 与调研纪要）：

- 显式 ``WHALE_SEARCH_PROVIDER``（tavily/brave/duckduckgo）优先；
- 否则按 key 存在性：TAVILY_API_KEY → BRAVE_API_KEY → DuckDuckGo 免 key 回退；
- **DuckDuckGo HTML 端点对数据中心 IP 会返回 202 + 反爬挑战页**
  （2026-10 实测 + 社区记录）——检测到该形态抛 ``SearchError(kind="blocked")``，
  诚实降级，绝不解析垃圾页面冒充结果；
- Bing Web Search API 已于 2025-08 退役，不在 provider 目录。

所有 provider：stdlib urllib + JSON/HTMLParser；api_key 只从构造参数或 env 读，
**永不进入日志与异常文本**。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping, Protocol

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginExecutor, PluginRegistry

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
DDG_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "whale-core/0.1 (+https://github.com/amwangfan/whale-core)"
MAX_QUERY_CHARS = 400
MAX_LIMIT = 10


class SearchError(RuntimeError):
    """搜索失败分类：config（缺 key）/ network / bad_response / blocked（反爬）。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str  # "tavily" | "brave" | "duckduckgo"


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]: ...


def _clamp(query: str, limit: int) -> tuple[str, int]:
    q = (query or "").strip()[:MAX_QUERY_CHARS]
    if not q:
        raise SearchError("bad_response", "empty query")  # 空查询属调用方错误
    return q, max(1, min(int(limit), MAX_LIMIT))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """API 客户端不应被重定向（跨主机跳转会把 Authorization 带给目标——安全审查实证）。
    3xx 一律按 bad_response 处理，如需迁移端点应改配置而不是跟跳转。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = None
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _no_redirect_opener():
    global _NO_REDIRECT_OPENER
    if _NO_REDIRECT_OPENER is None:
        _NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())
    return _NO_REDIRECT_OPENER


def _http_json(
    req: urllib.request.Request, *, timeout: float, kind_map_auth: bool = True
) -> Any:
    """共享 HTTP+JSON 调用与错误分类；响应体/异常文本永不回显 Authorization 内容。"""
    try:
        with _no_redirect_opener().open(req, timeout=timeout) as resp:
            return json.loads(
                resp.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            )
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in (401, 403) and kind_map_auth:
            raise SearchError("auth", f"search auth failed ({status})") from exc
        if status == 429:
            raise SearchError("blocked", "search rate limited (429)") from exc
        if 500 <= status < 600:
            raise SearchError("network", f"search server error ({status})") from exc
        if 300 <= status < 400:
            raise SearchError(
                "bad_response", f"search endpoint redirect refused ({status})"
            ) from exc
        raise SearchError("bad_response", f"search HTTP error ({status})") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SearchError(
            "network", f"search network error: {type(exc).__name__}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SearchError("bad_response", "search returned invalid JSON") from exc


class TavilyProvider:
    """Tavily 搜索（默认档；免费额度 1000 credits/月）。key: 构造或 TAVILY_API_KEY。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
    ):
        env = os.environ if env is None else env
        self.api_key = api_key if api_key is not None else env.get("TAVILY_API_KEY", "")
        self.timeout = timeout

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        if not self.api_key:
            raise SearchError("config", "TAVILY_API_KEY not configured")
        q, n = _clamp(query, limit)
        body = json.dumps({"query": q, "max_results": n}).encode()
        req = urllib.request.Request(
            TAVILY_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        data = _http_json(req, timeout=self.timeout)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise SearchError("bad_response", "tavily response missing results")
        out = []
        for item in results[:n]:
            if not isinstance(item, dict):
                continue
            out.append(
                SearchResult(
                    title=str(item.get("title") or "")[:300],
                    url=str(item.get("url") or "")[:2000],
                    snippet=str(item.get("content") or "")[:500],
                    source="tavily",
                )
            )
        return out


class BraveProvider:
    """Brave 搜索。key: 构造或 BRAVE_API_KEY。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
    ):
        env = os.environ if env is None else env
        self.api_key = api_key if api_key is not None else env.get("BRAVE_API_KEY", "")
        self.timeout = timeout

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        if not self.api_key:
            raise SearchError("config", "BRAVE_API_KEY not configured")
        q, n = _clamp(query, limit)
        url = BRAVE_URL + "?q=" + urllib.parse.quote(q) + f"&count={n}"
        req = urllib.request.Request(
            url,
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        data = _http_json(req, timeout=self.timeout)
        web = data.get("web") if isinstance(data, dict) else None
        results = (web or {}).get("results")
        if not isinstance(results, list):
            raise SearchError("bad_response", "brave response missing web.results")
        out = []
        for item in results[:n]:
            if not isinstance(item, dict):
                continue
            out.append(
                SearchResult(
                    title=str(item.get("title") or "")[:300],
                    url=str(item.get("url") or "")[:2000],
                    snippet=str(item.get("description") or "")[:500],
                    source="brave",
                )
            )
        return out


class _DdgParser(HTMLParser):
    """DDG HTML 端点的结果解析器（.result__a / .result__snippet）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._in_a = False
        self._in_snippet = False
        self._cur: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "") or ""
        if tag == "a" and "result__a" in cls:
            self._in_a = True
            self._cur = {
                "url": self._unwrap(attrs_d.get("href", "") or ""),
                "title": "",
            }
        elif "result__snippet" in cls and tag in ("a", "td", "div"):
            self._in_snippet = True
            self._cur.setdefault("snippet", "")

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._cur["title"] = self._cur.get("title", "") + data
        elif self._in_snippet:
            self._cur["snippet"] = self._cur.get("snippet", "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_a:
            self._in_a = False
            if self._cur.get("url"):
                self.results.append(self._cur)
            self._cur = {}
        elif tag in ("a", "td", "div") and self._in_snippet:
            self._in_snippet = False
            # snippet 可能后于链接出现：回填到最后一条
            if self.results and self._cur.get("snippet"):
                self.results[-1]["snippet"] = self._cur["snippet"].strip()
            self._cur = {}

    @staticmethod
    def _unwrap(href: str) -> str:
        """/l/?uddg=<urlencoded> 跳转链接解出真实 URL。"""
        if "uddg=" in href:
            qs = urllib.parse.urlparse(href).query
            for part in qs.split("&"):
                if part.startswith("uddg="):
                    return urllib.parse.unquote(part[5:])
        return href


class DuckDuckGoHtmlProvider:
    """DuckDuckGo HTML 免 key 回退档。

    注意：数据中心 IP 会被反爬（HTTP 202 + anomaly 挑战页）——检测到即
    SearchError(kind="blocked")，诚实降级；适合家用宽带等住宅 IP 环境。
    """

    def __init__(self, *, timeout: float = 10.0):
        self.timeout = timeout

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        q, n = _clamp(query, limit)
        url = DDG_URL + "?q=" + urllib.parse.quote(q)
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                html = resp.read(1024 * 1024).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 202:
                raise SearchError(
                    "blocked", "duckduckgo rate-limited (202 anomaly page)"
                ) from exc
            if exc.code == 429:
                raise SearchError("blocked", "duckduckgo rate limited (429)") from exc
            raise SearchError("network", f"duckduckgo HTTP error ({exc.code})") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SearchError(
                "network", f"duckduckgo network error: {type(exc).__name__}"
            ) from exc
        if status == 202 or "anomaly" in html.lower() and "result__a" not in html:
            raise SearchError("blocked", "duckduckgo rate-limited (202 anomaly page)")
        parser = _DdgParser()
        parser.feed(html)
        out = []
        for item in parser.results[:n]:
            out.append(
                SearchResult(
                    title=item.get("title", "").strip()[:300],
                    url=item.get("url", "")[:2000],
                    snippet=item.get("snippet", "")[:500],
                    source="duckduckgo",
                )
            )
        return [r for r in out if r.url]


def select_provider(env: Mapping[str, str] | None = None) -> SearchProvider:
    """显式 env 选择优先；否则按 key 存在性 tavily → brave → duckduckgo 回退。"""
    env = os.environ if env is None else env
    explicit = (env.get("WHALE_SEARCH_PROVIDER") or "").strip().lower()
    if explicit == "tavily":
        return TavilyProvider(env=env)
    if explicit == "brave":
        return BraveProvider(env=env)
    if explicit in ("duckduckgo", "ddg"):
        return DuckDuckGoHtmlProvider()
    if (env.get("TAVILY_API_KEY") or "").strip():
        return TavilyProvider(env=env)
    if (env.get("BRAVE_API_KEY") or "").strip():
        return BraveProvider(env=env)
    return DuckDuckGoHtmlProvider()


class _SearchExecutor:
    """插件执行器：PluginGateway 路径的回执适配（短回执，不含全文）。"""

    def __init__(self, provider: SearchProvider):
        self.provider = provider

    def healthcheck(self):
        key = getattr(self.provider, "api_key", None)
        return "missing credentials" if key == "" else True

    def execute(
        self, action: str, target: str, args: Mapping[str, Any]
    ) -> tuple[bool, str]:
        query = str(args.get("query") or target or "")
        try:
            results = self.provider.search(query, limit=int(args.get("limit", 5) or 5))
        except SearchError as exc:
            return False, f"search:{exc.kind}"
        except Exception:
            return False, "search:error"
        return True, f"{len(results)} results for {query[:60]!r}"


def register_web_search(
    registry: PluginRegistry,
    tools=None,
    *,
    provider: SearchProvider | None = None,
):
    """宿主一行注册：manifest（output 类，search 动作 read_only）+ executor，
    tools 给定时同时挂 ToolSpec(search_web, web.search, read_only)。"""
    provider = provider if provider is not None else select_provider()

    def _handler(args: Mapping[str, Any]) -> str:
        results = provider.search(
            str(args.get("query") or ""), limit=int(args.get("limit", 5) or 5)
        )
        if not results:
            return "没有找到结果"
        lines = [f"{r.title} — {r.snippet}（{r.url}）" for r in results[:5]]
        return "\n".join(lines)

    registry.register(
        PluginManifest(
            plugin_id="web_search",
            name="网页搜索",
            version="1.0.0",
            kind="output",
            actions=("search",),
            action_risk={"search": "read_only"},
            description="可插拔搜索 provider（tavily/brave/duckduckgo 回退链）",
        ),
        executor=_SearchExecutor(provider),
    )
    if tools is not None:
        from whale_core.core.runtime import ToolRegistry, ToolSpec

        if not isinstance(tools, ToolRegistry):
            raise TypeError("tools must be a ToolRegistry")
        tools.register(
            ToolSpec(
                name="search_web",
                capability="web.search",
                description="搜索网页并返回带来源的结果",
                handler=_handler,
            )
        )
    return provider
