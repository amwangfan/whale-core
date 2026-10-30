"""Explicit bridge protocol; does not pretend whale-io implements these routes.

A bridge adapter must implement sessions and channels documented in service-bridges.md.
Realtime session negotiation returns a short-lived browser/device connection URL;
audio travels directly between device and bridge, never through text orchestration.
"""

from urllib.parse import urlsplit
from whale_core.plugins.http_provider import JsonProvider, ProviderError


class HttpBridgeProvider(JsonProvider):
    def start_session(self, *, persona="", session_id=""):
        data = self.request(
            "/sessions",
            {
                "session_id": session_id,
                "instructions": persona[:4000],
                "model": self.model,
            },
        )
        url = data.get("url", "")
        if not isinstance(url, str) or urlsplit(url).scheme not in (
            "https",
            "wss",
            "http",
            "ws",
        ):
            raise ProviderError("bad_response")
        return {"session_id": str(data.get("session_id") or session_id), "url": url}

    def send(self, channel, target, text, *, idempotency_key):
        return self.request(
            "/messages",
            {
                "channel": channel,
                "target": target,
                "text": text,
                "idempotency_key": idempotency_key,
            },
        )


class HttpPluginExecutor:
    """External manifest transport. Never imports third-party Python code."""

    def __init__(self, plugin_id, config):
        self.plugin_id = plugin_id
        self.client = JsonProvider(**config)

    def healthcheck(self):
        return self.client.healthcheck()

    def execute(self, action, target, args):
        data = self.client.request(
            "/execute",
            {
                "plugin_id": self.plugin_id,
                "action": action,
                "target": target,
                "args": dict(args),
            },
        )
        return data.get("ok") is True, str(
            data.get("receipt") or "no execution receipt"
        )[:500]
