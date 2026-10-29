"""Real dashboard adapter with injected HTTPS endpoint and bearer; never logs credentials."""
from __future__ import annotations
import json
import urllib.request
from whale_core.core.types import ActionIntent

class HttpsDashboardGateway:
    def __init__(self, endpoint: str, writer_token: str, timeout: float = 5.0):
        if not endpoint.startswith("https://"):
            raise ValueError("Dashboard endpoint must use HTTPS")
        if not writer_token:
            raise ValueError("Writer token required")
        self.endpoint, self.writer_token, self.timeout = endpoint, writer_token, timeout

    def dispatch(self, intent: ActionIntent) -> str:
        if intent.kind != "show_card":
            raise ValueError("Only show_card is supported")
        data = json.dumps({"title": str(intent.payload.get("title", "")),
                           "body": str(intent.payload.get("body", "")),
                           "layout_hint": str(intent.payload.get("layout_hint", "default"))},
                          ensure_ascii=False).encode()
        req = urllib.request.Request(self.endpoint, data=data, method="POST",
                                     headers={"Authorization": f"Bearer {self.writer_token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            if response.status >= 300:
                raise RuntimeError("Dashboard publish failed")
            return f"dashboard:http-{response.status}"
