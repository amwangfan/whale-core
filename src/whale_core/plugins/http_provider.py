"""Bounded JSON transport for explicit, owner-configured service bridges."""

from __future__ import annotations
import json
import math
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from whale_core.core.deepseek import _is_local_or_private_http


class ProviderError(RuntimeError):
    def __init__(self, kind):
        super().__init__("Service call failed: " + kind)
        self.kind = kind


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # credentials must never follow a redirect to another host


class JsonProvider:
    def __init__(self, *, api_key="", base_url, model="", timeout=30.0):
        p = urlsplit(base_url)
        if not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError(
                "base_url must be an origin/path without credentials, query or fragment"
            )
        if p.scheme != "https" and not _is_local_or_private_http(base_url):
            raise ValueError("HTTPS required except private/loopback HTTP")
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be within (0, 120]")
        self.api_key, self.base_url, self.model, self.timeout = (
            api_key,
            base_url.rstrip("/"),
            model,
            timeout,
        )

    def healthcheck(self):
        return (
            True
            if self.api_key or _is_local_or_private_http(self.base_url)
            else "missing credentials"
        )

    def request(self, path, payload=None, *, method="POST", extra_headers=None):
        if self.healthcheck() is not True:
            raise ProviderError("config")
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode()
        )
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                req, timeout=self.timeout
            ) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ProviderError("response_too_large")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ProviderError("bad_response")
                return data
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                "auth"
                if exc.code in (401, 403)
                else "quota"
                if exc.code == 429
                else "http_" + str(exc.code)
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError("network") from exc
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("bad_response") from exc
