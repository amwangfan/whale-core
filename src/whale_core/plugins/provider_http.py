"""Bounded provider transport with redirects disabled to protect API credentials."""
from __future__ import annotations
import urllib.parse
import urllib.request
from whale_core.plugins.web_fetch import _NoRedirectHandler


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
