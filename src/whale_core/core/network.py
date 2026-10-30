"""Credential-bearing provider requests must never follow redirects."""

import math
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_provider_request(request, *, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def validate_provider_config(base_url, timeout, *, allow_http=False):
    parts = urlsplit(base_url)
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("provider URL must not contain credentials, query or fragment")
    # Reading .port also validates a malformed/out-of-range port without exposing it.
    _ = parts.port
    if parts.scheme != "https" and not (parts.scheme == "http" and allow_http):
        raise ValueError(
            "HTTPS required except explicitly configured local/private HTTP"
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        raise ValueError("provider timeout must be within (0,120]")
