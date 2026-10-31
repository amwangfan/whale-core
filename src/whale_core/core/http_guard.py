"""出站 HTTP 安全门控——核心单一实现（SSRF / 重定向 / 对端复核 / 限量读取）。

供所有出站 HTTP 调用方共用（web.fetch 插件、媒体输入验证器、后续任何
artifact/attachment 下载），避免每个调用方各自发明一份策略：

- **仅白名单 scheme**（默认 http/https，可收窄到仅 https）；
- **SSRF 肯定式门控**：主机解析出的**所有**地址必须 ``ipaddress.is_global``
  才放行（回环/私网/CGNAT/文档保留段一并拦下）；显式拒绝 169.254.0.0/16
  （链路本地/云元数据）与 NAT64 段（64:ff9b::/96、64:ff9b:1::/48——其
  is_global 在 Python 3.13 误判为 True）。``allow_private=True`` 跳过 IP
  门控——**仅供宿主自知的内网服务**，切勿对不可信输入启用；
- **TOCTOU/DNS rebinding 防线**：连接建立后以 getpeername() 复核真实对端
  IP，不过即断开抛错（fail-closed）；
- **重定向不自动跟随**：``NoRedirectHandler`` 把 3xx 交还调用方逐跳重新
  校验（防 302 绕回内网）；
- **限量流式读取**：至多 max_bytes 即停，可带总时限防 trickle 挂死。

默认不读取 HTTP(S)_PROXY：转发代理隐藏实际目标 peer，不能维持同等 SSRF
保证。网络层全部可注入（opener/resolver），测试不触网。
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_NAT64_WK = ipaddress.ip_network("64:ff9b::/96")       # NAT64 well-known
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")  # NAT64 local-use

_READ_CHUNK = 65536


class HttpGuardError(RuntimeError):
    """出站请求被安全门控拒绝或失败：消息不含敏感信息。"""


def ip_allowed(ip) -> bool:
    """SSRF 肯定式门控（URL 校验与连接层 peer 复核共用同一判定）。"""
    if ip.version == 4 and ip in _LINK_LOCAL_V4:
        return False
    if ip.version == 6 and (ip in _NAT64_WK or ip in _NAT64_LOCAL):
        return False
    return bool(ip.is_global)


def default_resolver(host: str, port: int):
    """默认解析器：``socket.getaddrinfo``（流式套接字地址族）。"""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """不跟跳转：把 3xx 交还调用方逐跳校验后手动跟随。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class PeerCheckedConnectionMixin:
    """``connect()`` 后、发请求前复核实际对端 IP（TOCTOU/rebinding 防线）。

    子类可覆盖 ``_ip_check`` / ``_error_type`` 以接入宿主自己的错误类型
    （如 web.fetch 插件的 FetchError）。
    """

    _ip_check: Callable[[Any], bool] = staticmethod(ip_allowed)
    _error_type = HttpGuardError

    def connect(self) -> None:  # noqa: D102
        super().connect()
        peer = self.sock.getpeername()[0]
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            ip = None
        if ip is None or not self._ip_check(ip):
            try:
                self.close()
            finally:
                raise self._error_type(
                    "blocked non-global peer after connect (rebinding guard)"
                )


class PeerCheckedHTTP(PeerCheckedConnectionMixin, http.client.HTTPConnection):
    pass


class PeerCheckedHTTPS(PeerCheckedConnectionMixin, http.client.HTTPSConnection):
    pass


class PeerCheckedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):  # noqa: D102
        return self.do_open(PeerCheckedHTTP, req)


class PeerCheckedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):  # noqa: D102
        return self.do_open(PeerCheckedHTTPS, req)


def build_guarded_opener(allow_private: bool = False, extra_handlers=()):
    """默认 opener：不跟跳转、不走代理；非公网档挂 peer 复核处理器。"""
    handlers = [urllib.request.ProxyHandler({}), NoRedirectHandler(), *extra_handlers]
    if not allow_private:
        handlers += [PeerCheckedHTTPHandler(), PeerCheckedHTTPSHandler()]
    return urllib.request.build_opener(*handlers)


class UrlPolicy:
    """URL 安全策略：scheme 白名单 + SSRF 门控（可注入 resolver 离线测试）。

    每个跳转目标都必须重新 ``check`` 一次。
    """

    def __init__(
        self,
        *,
        allow_private: bool = False,
        schemes: tuple[str, ...] = ("http", "https"),
        resolver: Callable[[str, int], Any] | None = None,
    ) -> None:
        if not schemes:
            raise ValueError("schemes must be non-empty")
        self._allow_private = bool(allow_private)
        self._schemes = tuple(s.lower() for s in schemes)
        self._resolver = resolver if resolver is not None else default_resolver

    def check(self, url: str) -> tuple[str, str, int]:
        """校验 URL，返回 (scheme, host, port)；拒绝时抛 HttpGuardError。"""
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            raise HttpGuardError(f"invalid URL: {url!r}") from None
        scheme = (parts.scheme or "").lower()
        if scheme not in self._schemes:
            raise HttpGuardError(
                f"unsupported scheme {scheme or '(none)'!r}: {'/'.join(self._schemes)} only"
            )
        host = parts.hostname
        if not host:
            raise HttpGuardError(f"invalid URL (missing host): {url!r}")
        try:
            port = parts.port
        except ValueError:
            raise HttpGuardError(f"invalid URL (bad port): {url!r}") from None
        if port is None:
            port = 443 if scheme == "https" else 80
        if not self._allow_private:
            self._check_host(host, port)
        return scheme, host, port

    def _check_host(self, host: str, port: int) -> None:
        try:
            infos = self._resolver(host, port)
        except HttpGuardError:
            raise
        except Exception as exc:  # socket.gaierror / UnicodeError / ...
            raise HttpGuardError(
                f"DNS resolution failed: {type(exc).__name__}"
            ) from exc
        addresses: set[Any] = set()
        for info in infos or ():
            try:
                addresses.add(ipaddress.ip_address(info[4][0]))
            except (TypeError, IndexError, ValueError):
                raise HttpGuardError(
                    "DNS resolution failed: malformed address"
                ) from None
        if not addresses:
            raise HttpGuardError("DNS resolution failed: no addresses")
        for ip in addresses:
            if not ip_allowed(ip):
                raise HttpGuardError("blocked non-global address (SSRF guard)")


def read_limited(response: Any, max_bytes: int, deadline: float | None = None) -> bytes:
    """流式读取，至多 max_bytes 即停；可选总时限（防 trickle 响应挂死）。"""
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        if deadline is not None and time.monotonic() > deadline:
            raise HttpGuardError("total deadline exceeded while reading body")
        chunk = response.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)
