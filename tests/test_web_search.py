"""plugins/web_search.py 测试：provider 解析/选择链/错误分类/注册——全离线。"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from whale_core.core.runtime import ToolRegistry
from whale_core.plugins.registry import PluginRegistry
from search_web.web_search import (
    BraveProvider,
    DuckDuckGoHtmlProvider,
    SearchError,
    TavilyProvider,
    register_web_search,
    select_provider,
)


def _fake_urlopen(monkeypatch, *, status=200, payload=None, text=None):
    """伪造 HTTP 层：同时覆盖 urlopen（DDG 路径）与 _no_redirect_opener().open
    （Tavily/Brave 路径）。payload（dict→JSON）或 text（HTML）。返回请求捕获器。"""
    captured = {}

    class FakeResp:
        def __init__(self):
            self.status = status

        def read(self, n=-1):
            if payload is not None:
                return json.dumps(payload).encode()
            return (text or "").encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, *, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        if req.data:
            captured["body"] = json.loads(req.data.decode())
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "m", {}, None)
        return FakeResp()

    class FakeOpener:
        def open(self, req, *, timeout=None):
            return fake(req, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    import search_web.web_search as ws
    monkeypatch.setattr(ws, "_no_redirect_opener", lambda: FakeOpener())
    return captured


# ---------- Tavily ----------

def test_tavily_parses_results(monkeypatch):
    captured = _fake_urlopen(monkeypatch, payload={
        "results": [{"title": "T1", "url": "https://a.com", "content": "摘要A"},
                     {"title": "B", "url": "https://b.com", "content": "摘要B"}]})
    p = TavilyProvider(api_key="tvly-test")
    results = p.search("天气", limit=5)
    assert len(results) == 2
    assert results[0].source == "tavily" and results[0].url == "https://a.com"
    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["body"]["max_results"] == 5
    assert captured["headers"].get("Authorization") == "Bearer tvly-test"


def test_tavily_missing_key():
    with pytest.raises(SearchError) as ei:
        TavilyProvider(api_key="").search("x")
    assert ei.value.kind == "config"


def test_tavily_auth_error_no_key_leak(monkeypatch):
    _fake_urlopen(monkeypatch, status=401)
    with pytest.raises(SearchError) as ei:
        TavilyProvider(api_key="tvly-secret").search("x")
    assert ei.value.kind == "auth"
    assert "tvly-secret" not in str(ei.value)


def test_tavily_bad_shape(monkeypatch):
    _fake_urlopen(monkeypatch, payload={"unexpected": True})
    with pytest.raises(SearchError) as ei:
        TavilyProvider(api_key="k").search("x")
    assert ei.value.kind == "bad_response"


# ---------- Brave ----------

def test_brave_parses_results(monkeypatch):
    captured = _fake_urlopen(monkeypatch, payload={
        "web": {"results": [{"title": "T", "url": "https://x.com", "description": "D"}]}})
    p = BraveProvider(api_key="brave-key")
    results = p.search("query")
    assert results[0].source == "brave" and results[0].snippet == "D"
    assert captured["headers"].get("X-subscription-token") == "brave-key"
    assert "q=query" in captured["url"]


def test_brave_429_is_blocked(monkeypatch):
    _fake_urlopen(monkeypatch, status=429)
    with pytest.raises(SearchError) as ei:
        BraveProvider(api_key="k").search("x")
    assert ei.value.kind == "blocked"


# ---------- DuckDuckGo ----------

_DDG_HTML = """
<html><body>
<div class="result">
  <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">示例标题</a>
  <a class="result__snippet">这是摘要文字</a>
</div>
</body></html>
"""


def test_ddg_html_parsing_and_uddg_unwrap(monkeypatch):
    _fake_urlopen(monkeypatch, text=_DDG_HTML)
    results = DuckDuckGoHtmlProvider().search("测试")
    assert len(results) == 1
    assert results[0].url == "https://example.com/page"
    assert results[0].title == "示例标题"
    assert "摘要" in results[0].snippet
    assert results[0].source == "duckduckgo"


def test_ddg_202_blocked(monkeypatch):
    _fake_urlopen(monkeypatch, status=202, text="<html>anomaly</html>")
    with pytest.raises(SearchError) as ei:
        DuckDuckGoHtmlProvider().search("x")
    assert ei.value.kind == "blocked"


def test_ddg_anomaly_page_detected(monkeypatch):
    _fake_urlopen(monkeypatch, text="<html><body>anomaly challenge</body></html>")
    with pytest.raises(SearchError) as ei:
        DuckDuckGoHtmlProvider().search("x")
    assert ei.value.kind == "blocked"


# ---------- 选择链 ----------

def test_select_provider_chain():
    assert isinstance(select_provider(env={"TAVILY_API_KEY": "t"}), TavilyProvider)
    assert isinstance(select_provider(env={"BRAVE_API_KEY": "b"}), BraveProvider)
    assert isinstance(select_provider(env={}), DuckDuckGoHtmlProvider)
    # 显式选择优先于 key 存在性
    assert isinstance(select_provider(env={"WHALE_SEARCH_PROVIDER": "brave",
                                         "TAVILY_API_KEY": "t"}), BraveProvider)


# ---------- 注册 ----------

def test_register_manifest_and_executor():
    registry = PluginRegistry()

    class StubProvider:
        def search(self, query, *, limit=5):
            from search_web.web_search import SearchResult
            return [SearchResult("t", "https://a.com", "s", "stub")]

    register_web_search(registry, provider=StubProvider())
    manifest = registry.manifest("web_search")
    assert manifest is not None
    assert manifest.resolved_kind.value == "output"
    assert manifest.risk_of("search").value == "read_only"
    executor = registry.executor("web_search")
    ok, receipt = executor.execute("search", "", {"query": "测试"})
    assert ok and "1 results" in receipt


def test_register_tool_into_registry():
    registry = PluginRegistry()
    tools = ToolRegistry()

    class StubProvider:
        def search(self, query, *, limit=5):
            from search_web.web_search import SearchResult
            return [SearchResult("标题", "https://a.com", "摘要", "stub")]

    register_web_search(registry, tools, provider=StubProvider())
    spec = tools.get("search_web")
    assert spec is not None and spec.capability == "web.search"
    out = spec.handler({"query": "q"})
    assert "https://a.com" in out and "标题" in out


def test_tool_handler_empty_result_text():
    class EmptyProvider:
        def search(self, query, *, limit=5):
            return []

    registry, tools = PluginRegistry(), ToolRegistry()
    register_web_search(registry, tools, provider=EmptyProvider())
    assert tools.get("search_web").handler({"query": "q"}) == "没有找到结果"


def test_query_truncation_and_limit_clamp(monkeypatch):
    captured = _fake_urlopen(monkeypatch, payload={"results": []})
    TavilyProvider(api_key="k").search("x" * 1000, limit=99)
    assert len(captured["body"]["query"]) == 400
    assert captured["body"]["max_results"] == 10


def test_empty_query_rejected():
    with pytest.raises(SearchError):
        TavilyProvider(api_key="k").search("   ")


def test_redirect_refused_as_bad_response(monkeypatch):
    """安全审查回归：API 端点 3xx 一律拒绝（防跨主机认证头泄漏）。"""
    _fake_urlopen(monkeypatch, status=302)
    with pytest.raises(SearchError) as ei:
        TavilyProvider(api_key="k").search("x")
    assert ei.value.kind == "bad_response"


def test_bing_provider_parses_and_unwraps(monkeypatch):
    """Bing HTML：b_algo 结果解析 + /ck/a 重定向解包（u=a1<base64>）。"""
    import base64 as b64
    real = "https://example.com/real-page"
    wrapped = "https://www.bing.com/ck/a?u=a1" + b64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    html = ('<li class="b_algo"><h2><a href="' + wrapped + '">标题一</a></h2>'
            '<div><p>这是摘要</p></div></li>')
    from search_web.web_search import BingHtmlProvider
    import urllib.request

    class Resp:
        def read(self, n=-1):
            return html.encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
    results = BingHtmlProvider().search("查询", limit=3)
    assert len(results) == 1
    assert results[0].title == "标题一"
    assert results[0].url == real  # 解包成功
    assert results[0].source == "bing"


def test_bing_provider_no_results_raises_blocked(monkeypatch):
    import urllib.request
    from search_web.web_search import BingHtmlProvider, SearchError

    class Resp:
        def read(self, n=-1):
            return b"<html>no results</html>"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
    try:
        BingHtmlProvider().search("x")
    except SearchError as e:
        assert e.kind == "blocked"
        return
    raise AssertionError("expected blocked")


def test_select_provider_bing():
    from search_web.web_search import BingHtmlProvider, select_provider
    p = select_provider({"WHALE_SEARCH_PROVIDER": "bing"})
    assert isinstance(p, BingHtmlProvider)
