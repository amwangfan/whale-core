"""whale-io 原生渠道适配器骨架：dashboard 卡片 / ring-hub 呼叫 / WeQ 直发（PR#4 移植）。

**联调状态（诚实标注——未联调不得宣称真实发送能力）**：

- dashboard 卡片契约（``POST dashboard_url``，``{title, body, layout_hint}``）
  来自 whale-io 单体仓的既有约定，**未与真实 dashboard 服务联调**；
- ring-hub 契约（``POST ring_url + /v1/ring``，``{device_id, name, number}``）
  同样**未与真实呼叫服务联调**；且 PR#4 原实现中 ``number`` 恒为 ``"***"``
  掩码占位（不携带真实号码）——本骨架保留该语义，它只表达「请求呼叫」
  信号，未经真机验收前不得当作电话能力；
- WeQ ``direct_message`` 不在此自建 sender（整合纪律：不建第二套 sender/
  队列；核心不反向依赖 ``weq_channel`` 拆分包）。宿主把既有
  ``weq_channel.weq_send.WeQWebSendPort``（或任意满足
  ``life.initiative.OutboundPort`` 的实现）经 ``weq_sender`` 注入。

所有目的地址与凭据均为 owner 显式配置。发送前的全局暂停、渠道白名单、
静默时段、额度与单次审批在 ProactiveOutbox，本模块不绕过。返回的
``accepted`` 只表示渠道受理，不代表送达或已读；``delivered`` 只透传服务端
自述，未经独立核验。
"""

from __future__ import annotations

from whale_core.plugins.provider_http import JsonProvider

__all__ = ["WhaleIOChannelProvider"]


class WhaleIOChannelProvider:
    def __init__(self, *, dashboard_url: str = "", dashboard_token: str = "",
                 ring_url: str = "", ring_token: str = "", weq_sender=None,
                 timeout: float = 15):
        # URL/凭据/超时的全部校验在 JsonProvider（provider_http）构造期完成；
        # 未填写的端点保持 None，对应渠道调用明确失败而不冒充送达。
        self.dashboard = (
            JsonProvider(base_url=dashboard_url, api_key=dashboard_token,
                         timeout=timeout)
            if dashboard_url else None
        )
        self.ring = (
            JsonProvider(base_url=ring_url, api_key=ring_token, timeout=timeout)
            if ring_url else None
        )
        self.weq = weq_sender

    def healthcheck(self):
        """True 或原因字符串（与 PluginManager.healthcheck 约定一致）。"""
        if self.dashboard or self.ring or self.weq:
            return True
        return "configure a dashboard, ring hub or WeQ endpoint"

    def send(self, channel: str, target: str, text: str, *, idempotency_key: str) -> dict:
        if channel == "hud" and self.dashboard:
            # 未与真实 dashboard 联调：卡片契约 {title, body, layout_hint}
            value = self.dashboard.request(
                "", {"title": "鲸鱼", "body": text[:800], "layout_hint": "default"})
            return {"accepted": value.get("ok", True) is not False}
        if channel == "call" and self.ring:
            # 未与真实 ring-hub 联调：number 掩码占位，不携带真实号码
            value = self.ring.request(
                "/v1/ring",
                {"device_id": target, "name": text[:40] or "鲸鱼", "number": "***"})
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
            return {"accepted": bool(self.weq.send(draft))}
        raise ValueError("native channel endpoint is not configured for this channel")
