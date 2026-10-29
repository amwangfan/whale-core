"""web.fetch 官方插件：带 SSRF 防护的只读网页抓取（PR-5）。

安全要点（与仓内约定一致：护栏在核心，插件不自带信任）：

- **仅 http/https**，其余 scheme 一律拒绝（file/ftp/gopher……）；
- **SSRF 防护（默认拒绝非公网）**：主机经 ``resolver`` 解析后，**所有**
  地址必须 ``ipaddress.is_global`` 为真才放行（肯定式白名单——回环、私网、
  CGNAT 100.64.0.0/10、文档保留段 203.0.113.0/24 等一并拦下）；另显式拒绝
  169.254.0.0/16（链路本地 / 云元数据）作双保险。
  ``allow_private=True`` 跳过 IP 门控——**仅供 local 配置档抓取宿主自知的
  内网服务**；开启后任意 URL 可达回环/内网/云元数据地址，切勿对不可信输入
  启用；
- **重定向手动跟随，最多 3 跳，每一跳重新做 SSRF 校验**（防 302 绕回内网）。
  默认 opener 挂 ``_NoRedirectHandler``，把 3xx 交还本模块逐跳处理
  （``urllib.request.urlopen`` 默认跟跳转，必须接管）；
- **体积/超时护栏**：响应流式读取，至多 ``max_bytes`` 即停；
  ``Accept-Encoding: identity``（不处理压缩）；默认超时 10s；
- **正文提取**：HTMLParser 子类剥掉 script/style/nav/footer/header/aside/
  form/noscript（含内容），收集 title/p/h1-h3/li/blockquote；合并空白；
  实体转义交给 HTMLParser（convert_charrefs=True）；输出截断 max_chars；
- **编码**：Content-Type 的 charset 优先，缺失/非法时嗅探 <meta charset>，
  最终回退 utf-8（errors="replace"）；
- 错误一律 ``FetchError``：消息含状态码 / 错误类型，除 URL 外不含其它信息。

网络层全部可注入（``opener`` / ``resolver``），测试不触网。
"""

from __future__ import annotations

import codecs
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping

from whale_core.core.runtime import ToolRegistry, ToolSpec
from whale_core.plugins.manifest import PluginManifest, RiskClass
from whale_core.plugins.registry import PluginRegistry

PLUGIN_ID = "web_fetch"
FETCH_ACTION = "fetch"
TOOL_NAME = "fetch_url"
TOOL_CAPABILITY = "web.fetch"

USER_AGENT = "whale-core/0.1 (+https://github.com/amwangfan/whale-core)"
MAX_REDIRECTS = 3

_READ_CHUNK = 65536
_SNIFF_BYTES = 4096
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class FetchResult:
    """一次抓取的结果。``url`` 为请求 URL，``final_url`` 为重定向后的最终 URL。"""

    url: str
    final_url: str
    title: str
    text: str  # 提取正文（截断至 max_chars）
    truncated: bool  # 正文是否被 max_chars 截断


class FetchError(RuntimeError):
    """抓取失败：消息含状态码 / 错误类型，除 URL 外不含其它（敏感）信息。"""


def _default_resolver(host: str, port: int):
    """默认解析器：``socket.getaddrinfo``（流式套接字地址族）。"""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """不跟跳转：把 3xx 以 HTTPError 形式交还，由 WebFetcher 逐跳校验后跟随。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class _TextExtractor(HTMLParser):
    """正文提取器：丢弃 boilerplate 标签（含内容），收集 title/段落级文本。"""

    SKIP_TAGS = frozenset(
        {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}
    )
    KEEP_TAGS = frozenset({"title", "p", "h1", "h2", "h3", "li", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # 实体转义交给 HTMLParser
        self._skip_depth = 0
        self._keep_stack: list[str] = []
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._skip_depth:
            if tag in self.SKIP_TAGS:
                self._skip_depth += 1
            return
        if tag in self.SKIP_TAGS:
            self._skip_depth = 1
        elif tag in self.KEEP_TAGS:
            self._keep_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in self.SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag in self.KEEP_TAGS and tag in self._keep_stack:
            while self._keep_stack:
                if self._keep_stack.pop() == tag:
                    break
            if tag != "title":
                self.text_parts.append("\n")  # 块级分隔，最终合并为单空格

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._keep_stack:
            return
        if self._keep_stack[-1] == "title":
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def _squash(parts: Iterable[str]) -> str:
    """合并空白：连续空白（含换行）折叠为单空格，首尾去空白。"""
    return " ".join("".join(parts).split())


def _get_header(headers: Any, name: str) -> str:
    """大小写不敏感地取响应头（兼容 email.message.Message 与普通映射）。"""
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except AttributeError:
        return ""
    if value is None:
        items = getattr(headers, "items", None)
        if callable(items):
            lowered = name.lower()
            for key, val in items():
                if str(key).lower() == lowered:
                    return str(val)
        return ""
    return str(value)


def _valid_charset(name: str) -> bool:
    if not name:
        return False
    try:
        codecs.lookup(name)
    except (LookupError, ValueError):
        return False
    return True


def _charset_from_content_type(content_type: str) -> str:
    for part in (content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            candidate = part.split("=", 1)[1].strip().strip("\"'").lower()
            return candidate if _valid_charset(candidate) else ""
    return ""


def _sniff_meta_charset(body: bytes) -> str:
    match = _META_CHARSET_RE.search(body[:_SNIFF_BYTES])
    if not match:
        return ""
    candidate = match.group(1).decode("ascii", "replace").lower()
    return candidate if _valid_charset(candidate) else ""


def _read_limited(response: Any, max_bytes: int) -> bytes:
    """流式读取，至多 max_bytes 即停（绝不先 read() 全部）。"""
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        chunk = response.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)


class WebFetcher:
    """只读网页抓取器：SSRF 防护 + 手动重定向 + 体积/超时护栏 + 正文提取。

    - ``timeout``：单跳超时秒数（默认 10.0）；
    - ``max_bytes``：响应体读取上限（默认 512KB，流式截断）；
    - ``max_chars``：提取正文输出上限（``truncated`` 标记是否截断）；
    - ``allow_private``：**危险，仅供 local 配置档**——True 时跳过 IP 门控，
      内网/回环/云元数据地址全部可达；切勿对不可信输入启用；
    - ``opener`` / ``resolver``：网络层注入点（默认不跟跳转的 urllib opener
      与 ``socket.getaddrinfo``），测试用 fake 替身、不触网。
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_bytes: int = 524288,
        max_chars: int = 8000,
        allow_private: bool = False,
        opener: Any = None,
        resolver: Callable[[str, int], Any] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self._timeout = float(timeout)
        self._max_bytes = int(max_bytes)
        self._max_chars = int(max_chars)
        self._allow_private = bool(allow_private)
        self._opener = opener if opener is not None else urllib.request.build_opener(
            _NoRedirectHandler()
        )
        self._resolver = resolver if resolver is not None else _default_resolver

    def fetch(self, url: str) -> FetchResult:
        """抓取 ``url`` 并提取正文。失败一律 FetchError。"""
        original = (url or "").strip()
        current = original
        redirects = 0
        while True:
            self._check_url(current)  # 每一跳（含重定向目标）都重新校验
            request = urllib.request.Request(
                current,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Encoding": "identity",
                },
            )
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in _REDIRECT_CODES:
                    if redirects >= MAX_REDIRECTS:
                        raise FetchError(
                            f"too many redirects (>{MAX_REDIRECTS})"
                        ) from None
                    location = exc.headers.get("Location") if exc.headers else None
                    if not location:
                        raise FetchError(f"HTTP {exc.code}: missing Location") from None
                    current = urllib.parse.urljoin(current, str(location))
                    redirects += 1
                    continue
                raise FetchError(f"HTTP error {exc.code}") from None
            except (TimeoutError, socket.timeout) as exc:
                raise FetchError(
                    f"request timed out after {self._timeout:g}s"
                ) from exc
            except urllib.error.URLError as exc:
                raise FetchError(
                    f"request failed: {type(exc.reason).__name__}"
                ) from exc
            except OSError as exc:
                raise FetchError(f"request failed: {type(exc).__name__}") from exc
            try:
                body = _read_limited(response, self._max_bytes)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            return self._build_result(original, current, response, body)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _check_url(self, url: str) -> None:
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise FetchError(f"invalid URL: {url!r}") from None
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise FetchError(f"unsupported scheme {scheme or '(none)'!r}: http/https only")
        host = parts.hostname
        if not host:
            raise FetchError(f"invalid URL (missing host): {url!r}")
        try:
            port = parts.port
        except ValueError:
            raise FetchError(f"invalid URL (bad port): {url!r}") from None
        if self._allow_private:
            return
        self._check_host(host, port if port is not None else (443 if scheme == "https" else 80))

    def _check_host(self, host: str, port: int) -> None:
        """SSRF 门控：解析出的**所有** IP 必须 is_global 才放行（肯定式）。

        另显式拒绝 169.254.0.0/16（链路本地/云元数据）作双保险——is_global
        对它们已是 False。
        """
        try:
            infos = self._resolver(host, port)
        except FetchError:
            raise
        except Exception as exc:  # socket.gaierror / UnicodeError / ...
            raise FetchError(
                f"DNS resolution failed: {type(exc).__name__}"
            ) from exc
        addresses: set[Any] = set()
        for info in infos or ():
            try:
                addresses.add(ipaddress.ip_address(info[4][0]))
            except (TypeError, IndexError, ValueError):
                raise FetchError("DNS resolution failed: malformed address") from None
        if not addresses:
            raise FetchError("DNS resolution failed: no addresses")
        for ip in addresses:
            if ip.version == 4 and ip in _LINK_LOCAL_V4:
                raise FetchError("blocked link-local address (cloud metadata range)")
            if not ip.is_global:
                raise FetchError("blocked non-global address (SSRF guard)")

    def _build_result(
        self, original: str, final: str, response: Any, body: bytes
    ) -> FetchResult:
        content_type = _get_header(getattr(response, "headers", None), "Content-Type")
        charset = (
            _charset_from_content_type(content_type)
            or _sniff_meta_charset(body)
            or "utf-8"
        )
        html = body.decode(charset, errors="replace")
        extractor = _TextExtractor()
        extractor.feed(html)
        extractor.close()
        title = _squash(extractor.title_parts)
        text = _squash(extractor.text_parts)
        truncated = len(text) > self._max_chars
        if truncated:
            text = text[: self._max_chars]
        return FetchResult(
            url=original, final_url=final, title=title, text=text, truncated=truncated
        )


class _WebFetchExecutor:
    """插件动作执行器：返回短文本回执（不含正文，与回执脱敏约定一致）。"""

    def __init__(self, fetcher: WebFetcher) -> None:
        self._fetcher = fetcher

    def execute(self, action: str, target: str, args: Mapping[str, Any]) -> tuple[bool, str]:
        if action != FETCH_ACTION:
            return False, f"unsupported action {action!r}"
        url = str(args.get("url") or target or "").strip()
        if not url:
            return False, "missing url"
        try:
            result = self._fetcher.fetch(url)
        except FetchError as exc:
            return False, f"fetch failed: {exc}"[:200]
        title = result.title or "(untitled)"
        receipt = f"fetched: {title} ({len(result.text)} chars)"
        if result.truncated:
            receipt += " [truncated]"
        return True, receipt[:200]


def register_web_fetch(
    registry: PluginRegistry,
    tools: ToolRegistry | None = None,
    *,
    fetcher: WebFetcher | None = None,
) -> WebFetcher:
    """宿主一行注册：manifest（output 类、fetch 动作 read_only）+ executor，
    可选同时把 ``fetch_url`` 工具挂进 ToolRegistry（read_only 直接跑）。

    重复注册同 plugin_id 由 PluginRegistry 既有约束抛 ValueError。
    """
    fetcher = fetcher or WebFetcher()
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        name="网页抓取（官方 web.fetch）",
        version="0.1.0",
        author="whale-core",
        description="带 SSRF 防护的只读网页正文提取（http/https，最多 3 跳重定向）",
        homepage="https://github.com/amwangfan/whale-core",
        kind="output",
        actions=(FETCH_ACTION,),
        action_risk={FETCH_ACTION: RiskClass.READ_ONLY.value},
    )
    registry.register(manifest, executor=_WebFetchExecutor(fetcher))
    if tools is not None:
        tools.register(
            ToolSpec(
                name=TOOL_NAME,
                capability=TOOL_CAPABILITY,
                description="抓取网页并提取正文（只读；SSRF 防护 + 体积护栏）",
                risk_class=RiskClass.READ_ONLY.value,
                plugin_id=PLUGIN_ID,
                action=FETCH_ACTION,
                handler=lambda args: fetcher.fetch(str(args.get("url") or "")).text,
            )
        )
    return fetcher
