"""Native adapters for whale-io dashboard, ring-hub and existing WeQ sender.

Uses existing published contracts; does not deploy bridges or send without the
host's outbox approval. All destinations and credentials are owner configuration.
"""

from whale_core.plugins.http_provider import JsonProvider


class WhaleIOChannelProvider:
    def __init__(
        self,
        *,
        dashboard_url="",
        dashboard_token="",
        ring_url="",
        ring_token="",
        weq_url="",
        weq_token="",
        weq_account="",
        owner_uid="",
        timeout=15,
    ):
        self.dashboard = (
            JsonProvider(
                base_url=dashboard_url, api_key=dashboard_token, timeout=timeout
            )
            if dashboard_url
            else None
        )
        self.ring = (
            JsonProvider(base_url=ring_url, api_key=ring_token, timeout=timeout)
            if ring_url
            else None
        )
        self.weq = None
        if weq_url:
            from whale_core.life.weq_send import WeQWebSendPort

            self.weq = WeQWebSendPort(
                weq_url,
                weq_token,
                target_uid=owner_uid,
                expected_account_uin=weq_account or None,
            )

    def healthcheck(self):
        return (
            True
            if self.dashboard or self.ring or self.weq
            else "configure a dashboard, ring hub or WeQ endpoint"
        )

    def send(self, channel, target, text, *, idempotency_key):
        if channel == "hud" and self.dashboard:
            # Existing dashboard endpoint accepts the full card JSON at its URL.
            value = self.dashboard.request(
                "", {"title": "鲸鱼", "body": text[:800], "layout_hint": "default"}
            )
            return {"accepted": value.get("ok", True) is not False}
        if channel == "call" and self.ring:
            value = self.ring.request(
                "/v1/ring",
                {"device_id": target, "name": text[:40] or "鲸鱼", "number": "***"},
            )
            return {
                "accepted": value.get("ok") is True,
                "delivered": value.get("delivered") is True,
            }
        if channel == "direct_message" and self.weq:
            from whale_core.life.initiative import ProactiveDraft

            draft = ProactiveDraft(
                idempotency_key,
                text,
                "owner-approved channel request",
                ("api:" + idempotency_key,),
                channel="direct_message",
            )
            return {"accepted": self.weq.send(draft)}
        raise ValueError("native channel endpoint is not configured for this channel")
