"""Bounded provider transport with redirects disabled to protect API credentials."""
from __future__ import annotations
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from search_web.web_fetch import _NoRedirectHandler


def validate_base_url(url: str) -> str:
    from whale_core.plugins.speech import _is_local_or_private_http
    parts = urllib.parse.urlsplit(url)
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("invalid provider endpoint port")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("provider endpoint must not contain credentials, query or fragment")
    if parts.scheme != "https" and not _is_local_or_private_http(url):
        raise ValueError("provider endpoint requires HTTPS or private HTTP")
    return url.rstrip("/")


def open_request(request, *, timeout):
    return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def read_response(response, max_bytes: int) -> bytes:
    raw = response.read(max_bytes+1)
    if len(raw) > max_bytes:
        raise ValueError("provider response exceeds size limit")
    return raw


class ProviderError(RuntimeError):
    """服务桥/外置插件传输失败的分类错误（异常文本不含凭据与正文）。

    ``kind`` 沿用 embedding/speech 的约定：``config``（缺凭据）、``auth``
    （401/403）、``quota``（429）、``server``（5xx）、``network``（连接层）、
    ``bad_response``（解析失败/超限/非 JSON 对象）、其余 HTTP 状态码为
    ``http_<code>``（含被拒跟随的 3xx 重定向）。
    """

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or ("provider call failed: " + kind))
        self.kind = kind


class JsonProvider:
    """显式自配服务端点的有界 JSON 客户端（PR#4 渠道桥接移植，建在本模块原语之上）。

    不复造传输层：URL 校验用 ``validate_base_url``（禁凭据/query/fragment、
    HTTPS 强制、私网/回环 HTTP 例外）；请求用 ``open_request``
    （``_NoRedirectHandler``——3xx 一律抛 HTTPError，Authorization 以
    unredirected header 携带，凭据绝不随跳转外流）；响应用
    ``read_response`` 有界读取。本类只补 JSON POST 便捷封装与
    ``ProviderError`` 错误分类。
    """

    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(self, *, api_key: str = "", base_url: str, model: str = "",
                 timeout: float = 30.0):
        self.base_url = validate_base_url(base_url)
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be within (0, 120]")
        self.api_key, self.model, self.timeout = api_key, model, timeout

    def healthcheck(self):
        """True 或原因字符串（与 PluginManager.healthcheck 约定一致）。"""
        from whale_core.plugins.speech import _is_local_or_private_http
        if self.api_key or _is_local_or_private_http(self.base_url):
            return True
        return "missing credentials"

    def request(self, path: str, payload=None, *, method: str = "POST",
                extra_headers: dict | None = None) -> dict:
        """向 ``base_url + path`` 发 JSON 请求并返回 JSON 对象响应。

        ``path`` 为空串时直接打到 base_url（固定单端点服务）；否则必须是
        同源绝对路径（``/...``）。响应必须是 JSON object 且不超过
        ``MAX_RESPONSE_BYTES``，否则 ``ProviderError("bad_response")``。
        """
        if self.healthcheck() is not True:
            raise ProviderError("config")
        if path and (not path.startswith("/") or "://" in path):
            raise ValueError("path must be empty or an absolute path on the configured origin")
        body = (None if payload is None
                else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        req = urllib.request.Request(self.base_url + path, data=body,
                                     headers=headers, method=method)
        if self.api_key:
            # unredirected header：即便 opener 策略被改，跳转时也不带凭据
            req.add_unredirected_header("Authorization", "Bearer " + self.api_key)
        try:
            with open_request(req, timeout=self.timeout) as response:
                raw = read_response(response, self.MAX_RESPONSE_BYTES)
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            kind = ("auth" if exc.code in (401, 403)
                    else "quota" if exc.code == 429
                    else "server" if exc.code >= 500
                    else "http_" + str(exc.code))
            raise ProviderError(kind) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("network") from None
        except (ValueError, UnicodeError):
            # JSON 解析失败与 read_response 超限统一归为 bad_response
            raise ProviderError("bad_response") from None
        if not isinstance(data, dict):
            raise ProviderError("bad_response")
        return data
