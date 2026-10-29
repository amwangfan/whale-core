"""web.fetch 官方插件测试（PR-5 子任务 B，全部离线）。

网络层全部走注入的 fake opener / fake resolver，不发起任何真实网络请求。
覆盖：SSRF 门控（is_global 肯定式 + 169.254/16 双保险 + allow_private 档）、
scheme 白名单、逐跳重校验的手动重定向、max_bytes 流式截断、HTML 正文提取、
charset 优先级、FetchResult 契约、注册形状与执行器/工具接线。
"""

from __future__ import annotations

import email.message
import io
import socket
import urllib.error

import pytest

from whale_core.core.runtime import ToolRegistry
from whale_core.plugins import PluginKind, PluginRegistry, RiskClass
from whale_core.plugins.web_fetch import (
    FetchError,
    FetchResult,
    WebFetcher,
    register_web_fetch,
)

PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "93.184.216.35"
SIMPLE_HTML = b"<html><head><title>Hello</title></head><body><p>world</p></body></html>"


# ---------------------------------------------------------------------------
# 测试替身（不触网）
# ---------------------------------------------------------------------------


class FakeResponse:
    """流式假响应：headers 用 email.message.Message（与 urllib 真实响应一致）。"""

    def __init__(self, body: bytes, *, content_type: str = "text/html; charset=utf-8"):
        self._buf = io.BytesIO(body)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        self.bytes_served = 0
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        data = self._buf.read(n)
        self.bytes_served += len(data)
        return data

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """脚本化 opener：队列中每项是 FakeResponse 或要抛出的异常。"""

    def __init__(self, script):
        self._script = list(script)
        self.requests: list[str] = []

    def open(self, request, timeout=None):
        self.requests.append(request.full_url)
        if not self._script:
            raise AssertionError("unexpected extra request")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_http_error(code: int, location: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if location is not None:
        headers["Location"] = location
    return urllib.error.HTTPError("http://example.com/", code, f"HTTP {code}", headers, None)


def make_resolver(mapping):
    """host -> ip（字符串或列表）；未命中主机按解析失败处理。"""

    def resolve(host: str, port: int):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        ips = mapping[host]
        if isinstance(ips, str):
            ips = [ips]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return resolve


def make_fetcher(opener: FakeOpener, mapping=None, **kwargs) -> WebFetcher:
    return WebFetcher(
        opener=opener,
        resolver=make_resolver(mapping or {"example.com": PUBLIC_IP}),
        **kwargs,
    )


def fetch_simple(**kwargs) -> FetchResult:
    opener = FakeOpener([FakeResponse(SIMPLE_HTML)])
    return make_fetcher(opener, **kwargs).fetch("http://example.com/")


# ---------------------------------------------------------------------------
# 基本抓取 + FetchResult 契约
# ---------------------------------------------------------------------------


def test_fetch_public_ip_ok():
    result = fetch_simple()
    assert result.url == "http://example.com/"
    assert result.final_url == "http://example.com/"
    assert result.title == "Hello"
    assert result.text == "world"
    assert result.truncated is False


def test_fetch_result_fields_and_truncated_flag():
    body = ("<title>T</title><p>" + "字" * 100 + "</p>").encode("utf-8")
    result = make_fetcher(FakeOpener([FakeResponse(body)]), max_chars=20).fetch(
        "http://example.com/"
    )
    assert isinstance(result, FetchResult)
    assert result.truncated is True
    assert len(result.text) == 20
    assert result.url == result.final_url == "http://example.com/"
    assert result.title == "T"


# ---------------------------------------------------------------------------
# SSRF 门控（is_global 肯定式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",        # 回环
        "10.0.0.1",         # 私网 A
        "192.168.1.1",      # 私网 C
        "172.16.0.1",       # 私网 B 下界
        "172.31.255.255",   # 私网 B 上界
        "::1",              # IPv6 回环
        "100.64.7.7",       # CGNAT 运营商段（is_private/is_reserved 均不覆盖）
        "203.0.113.7",      # 文档保留段（TEST-NET-3）
    ],
)
def test_ssrf_non_global_ips_rejected(ip):
    opener = FakeOpener([FakeResponse(SIMPLE_HTML)])
    fetcher = make_fetcher(opener, {"internal.example": ip})
    with pytest.raises(FetchError):
        fetcher.fetch("http://internal.example/")
    assert opener.requests == []  # 未发起任何请求


def test_ssrf_cloud_metadata_169_254_169_254_rejected():
    """云元数据地址单独点名（is_global 已覆盖，显式拒绝是双保险）。"""
    opener = FakeOpener([FakeResponse(SIMPLE_HTML)])
    fetcher = make_fetcher(opener, {"metadata.internal": "169.254.169.254"})
    with pytest.raises(FetchError):
        fetcher.fetch("http://metadata.internal/latest/meta-data")
    assert opener.requests == []


def test_ssrf_mixed_resolution_rejected():
    """多 A 记录中**任一**非公网即整体拒绝。"""
    opener = FakeOpener([FakeResponse(SIMPLE_HTML)])
    fetcher = make_fetcher(opener, {"dual.example": [PUBLIC_IP, "10.1.2.3"]})
    with pytest.raises(FetchError):
        fetcher.fetch("http://dual.example/")
    assert opener.requests == []


def test_allow_private_permits_loopback():
    """local 配置档：allow_private=True 时跳过 IP 门控。"""
    opener = FakeOpener([FakeResponse(SIMPLE_HTML)])
    fetcher = make_fetcher(opener, {"localhost": "127.0.0.1"}, allow_private=True)
    result = fetcher.fetch("http://localhost:8080/")
    assert result.title == "Hello"
    assert opener.requests == ["http://localhost:8080/"]


def test_dns_failure_raises_fetch_error():
    def failing(host: str, port: int):
        raise socket.gaierror(-2, "Name or service not known")

    fetcher = WebFetcher(opener=FakeOpener([FakeResponse(SIMPLE_HTML)]), resolver=failing)
    with pytest.raises(FetchError, match="DNS"):
        fetcher.fetch("http://nope.example/")


# ---------------------------------------------------------------------------
# scheme 白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x"])
def test_non_http_scheme_rejected(url):
    def boom_resolver(host: str, port: int):
        raise AssertionError("resolver must not be called for rejected schemes")

    fetcher = WebFetcher(opener=FakeOpener([]), resolver=boom_resolver)
    with pytest.raises(FetchError):
        fetcher.fetch(url)


# ---------------------------------------------------------------------------
# 重定向（逐跳重新校验）
# ---------------------------------------------------------------------------


def test_redirect_followed_to_public_host():
    opener = FakeOpener(
        [
            make_http_error(302, location="http://cdn.example.com/page"),
            FakeResponse(SIMPLE_HTML),
        ]
    )
    fetcher = make_fetcher(
        opener, {"example.com": PUBLIC_IP, "cdn.example.com": PUBLIC_IP_2}
    )
    result = fetcher.fetch("http://example.com/")
    assert result.url == "http://example.com/"
    assert result.final_url == "http://cdn.example.com/page"
    assert opener.requests == ["http://example.com/", "http://cdn.example.com/page"]


def test_redirect_to_loopback_blocked():
    """302 绕回内网：第二跳 SSRF 校验拦截，请求不发出。"""
    opener = FakeOpener([make_http_error(302, location="http://127.0.0.1:8500/admin")])
    fetcher = make_fetcher(opener, {"example.com": PUBLIC_IP, "127.0.0.1": "127.0.0.1"})
    with pytest.raises(FetchError):
        fetcher.fetch("http://example.com/")
    assert opener.requests == ["http://example.com/"]


def test_too_many_redirects():
    opener = FakeOpener(
        [make_http_error(302, location=f"http://example.com/hop{i}") for i in range(6)]
    )
    fetcher = make_fetcher(opener)
    with pytest.raises(FetchError, match="redirect"):
        fetcher.fetch("http://example.com/")
    assert len(opener.requests) == 4  # 首跳 + 3 次跟随，第 4 个 302 拒跟


def test_http_404_raises_fetch_error_with_status():
    opener = FakeOpener([make_http_error(404)])
    with pytest.raises(FetchError, match="404"):
        make_fetcher(opener).fetch("http://example.com/missing")


def test_timeout_raises_fetch_error():
    opener = FakeOpener([TimeoutError("timed out")])
    with pytest.raises(FetchError, match="timed out"):
        make_fetcher(opener).fetch("http://example.com/")


# ---------------------------------------------------------------------------
# 体积护栏
# ---------------------------------------------------------------------------


def test_max_bytes_streaming_cap():
    body = ("<p>" + "x" * 100000 + "</p>").encode("utf-8")
    response = FakeResponse(body)
    fetcher = make_fetcher(FakeOpener([response]), max_bytes=4096)
    result = fetcher.fetch("http://example.com/")
    assert response.bytes_served <= 4096  # 流式截断，未读满整个响应
    assert len(result.text) <= 4096
    assert response.closed is True


# ---------------------------------------------------------------------------
# HTML 正文提取
# ---------------------------------------------------------------------------


BOILERPLATE_HTML = b"""<html><head>
<title> whale docs </title>
<style>body { color: red }</style>
<script>alert("x")</script>
</head><body>
<header>site header junk</header>
<nav>nav junk</nav>
<main>
<h1>Title Heading</h1>
<p>First paragraph.</p>
<ul><li>item one</li><li>item two</li></ul>
<blockquote>quoted text</blockquote>
</main>
<aside>aside junk</aside>
<footer>footer junk</footer>
<form>form junk</form>
<noscript>noscript junk</noscript>
</body></html>"""


def test_html_extraction_strips_boilerplate():
    result = make_fetcher(FakeOpener([FakeResponse(BOILERPLATE_HTML)])).fetch(
        "http://example.com/"
    )
    assert result.title == "whale docs"
    for kept in ("Title Heading", "First paragraph.", "item one", "item two", "quoted text"):
        assert kept in result.text
    for dropped in (
        "nav junk",
        "footer junk",
        "site header junk",
        "aside junk",
        "form junk",
        "noscript junk",
        "alert",
        "color: red",
    ):
        assert dropped not in result.text


def test_html_entities_unescaped():
    body = b"<title>A &amp; B</title><p>Tom &amp; Jerry &lt;3 &#65;BC</p>"
    result = make_fetcher(FakeOpener([FakeResponse(body)])).fetch("http://example.com/")
    assert result.title == "A & B"
    assert result.text == "Tom & Jerry <3 ABC"


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


def test_charset_from_content_type_header_gbk():
    text = "中文标题与正文"
    body = f"<title>{text}</title><p>{text}</p>".encode("gbk")
    response = FakeResponse(body, content_type="text/html; charset=gbk")
    result = make_fetcher(FakeOpener([response])).fetch("http://example.com/")
    assert result.title == text
    assert text in result.text


def test_charset_from_meta_when_header_missing():
    text = "元标记探测"
    body = (
        "<html><head><meta charset='gbk'><title>t</title></head>"
        f"<body><p>{text}</p></body></html>"
    ).encode("gbk")
    response = FakeResponse(body, content_type="text/html")  # 无 charset
    result = make_fetcher(FakeOpener([response])).fetch("http://example.com/")
    assert text in result.text


def test_invalid_charset_falls_back_to_utf8():
    text = "回退路径"
    body = f"<p>{text}</p>".encode("utf-8")
    response = FakeResponse(body, content_type="text/html; charset=not-a-charset")
    result = make_fetcher(FakeOpener([response])).fetch("http://example.com/")
    assert text in result.text


# ---------------------------------------------------------------------------
# 注册：manifest 形状 / 执行器 / 工具
# ---------------------------------------------------------------------------


def test_register_manifest_shape():
    registry = PluginRegistry()
    register_web_fetch(registry, fetcher=make_fetcher(FakeOpener([])))
    manifest = registry.manifest("web_fetch")
    assert manifest is not None
    assert manifest.resolved_kind is PluginKind.OUTPUT
    assert manifest.actions == ("fetch",)
    assert registry.risk_of("web_fetch", "fetch") is RiskClass.READ_ONLY
    assert registry.kind_of("web_fetch") is PluginKind.OUTPUT


def test_register_executor_receipt_ok_and_failure():
    registry = PluginRegistry()
    opener = FakeOpener([FakeResponse(SIMPLE_HTML)])
    register_web_fetch(registry, fetcher=make_fetcher(opener))
    executor = registry.executor("web_fetch")
    assert executor is not None

    ok, receipt = executor.execute("fetch", "", {"url": "http://example.com/"})
    assert ok is True
    assert "Hello" in receipt and len(receipt) <= 200

    ok, receipt = executor.execute("fetch", "", {"url": "http://nope.example/"})
    assert ok is False
    assert "fetch failed" in receipt

    ok, receipt = executor.execute("burn", "", {"url": "http://example.com/"})
    assert ok is False
    assert "unsupported action" in receipt


def test_register_tool_spec_in_tool_registry():
    registry = PluginRegistry()
    tools = ToolRegistry()
    opener = FakeOpener([FakeResponse(SIMPLE_HTML)])
    register_web_fetch(registry, tools, fetcher=make_fetcher(opener))

    spec = tools.get("fetch_url")
    assert spec is not None
    assert spec.capability == "web.fetch"
    assert spec.risk_class == RiskClass.READ_ONLY.value
    assert spec.plugin_id == "web_fetch" and spec.action == "fetch"
    assert spec.handler is not None
    text = spec.handler({"url": "http://example.com/"})
    assert text == "world"


def test_register_without_tools_skips_tool_spec():
    registry = PluginRegistry()
    returned = register_web_fetch(registry, fetcher=make_fetcher(FakeOpener([])))
    assert isinstance(returned, WebFetcher)
    assert registry.manifest("web_fetch") is not None


def test_register_duplicate_plugin_id_raises():
    registry = PluginRegistry()
    register_web_fetch(registry, fetcher=make_fetcher(FakeOpener([])))
    with pytest.raises(ValueError, match="already registered"):
        register_web_fetch(registry, fetcher=make_fetcher(FakeOpener([])))
