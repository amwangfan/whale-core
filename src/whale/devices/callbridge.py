"""东京呼叫信令桥客户端（whale-io infra/call-bridge 的 ring_hub）。

v1 呼出一期：核心侧只发信令——ring 让主人手机起「鲸鱼来电」（接听后音频走桥的
PCM TCP 媒体面，不经核心），hangup 挂断/取消。手机侧零改动（addon 的信令长轮询
是既有面）。二期（设备链路原生 call.invite）随手机侧同启，不预建 stub。

安全纪律（与 channels/weq.py 同款）：
- URL 构造期校验：仅 http(s)；回环/内网允许 http，远程必须 https；拒绝内嵌凭据；
  不得带 query/fragment；HTTP 重定向不跟随（凭据不外流到未授权主机）；
- SIGNAL_TOKEN 只经 secrets 注入，不进日志/异常/回执文本；
- 回执三态如实：delivered→ok（已振铃）；queued→unknown（手机离线排队，可能稍后
  振铃，不谎报已响）；非 2xx → 由 kernel.http 如实映射（401/403=unauthorized 等）。
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from ..kernel.errors import WhaleError
from ..kernel.http import http_client, is_private_http, request_json

SERVICE = "呼叫桥"
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")   # tailscale CGNAT 覆盖网（加密 overlay）


def _is_tailnet_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in TAILNET_V4
    except ValueError:
        return False


def validate_bridge_url(raw: str) -> str:
    """构造期校验信令桥地址；不安全即 WhaleError(invalid_params)，错误文本不含凭据。

    http 放行面：回环/内网（is_private_http）+ tailnet 覆盖网（100.64.0.0/10，
    wireguard 自带加密——注意 CGNAT 段在 ipaddress.is_private 里是 False，须显式放行）。
    """
    url = (raw or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WhaleError("invalid_params", "call_bridge.url 必须是 http(s) 地址")
    if parts.username is not None or parts.password is not None:
        raise WhaleError("invalid_params", "call_bridge.url 不得内嵌凭据（token 请放在 secrets）")
    if parts.query or parts.fragment:
        raise WhaleError("invalid_params", "call_bridge.url 不得带 query/fragment")
    if parts.scheme == "http" and not (is_private_http(url) or _is_tailnet_host(parts.hostname)):
        raise WhaleError("invalid_params", "call_bridge.url 远程地址必须 HTTPS（http 仅限本机/内网/tailnet）")
    return url


def accept_ring_outcome(body: Any) -> dict[str, Any]:
    """ring 回执门闩：桥的 {ok, delivered, queued, msg} → 三态如实，不粉饰。"""
    if not isinstance(body, dict):
        return {"status": "error", "error": "桥返回了非预期内容"}
    if not body.get("ok"):
        return {"status": "error", "error": str(body.get("msg") or "桥未受理")[:200]}
    if body.get("delivered"):
        return {"status": "ok", "delivered": True}
    if body.get("queued"):
        return {"status": "unknown", "queued": True,
                "error": "手机离线，来电已排队（上线后振铃）"}
    return {"status": "error", "error": "桥回执既未投递也未排队（如实打回）"}


class BridgeSignalClient:
    """ring_hub 信令客户端：POST /v1/ring、POST /v1/hangup（Bearer 鉴权，重定向不跟随）。"""

    def __init__(self, app: Any, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        if not token:
            raise WhaleError("invalid_params", "call_bridge 缺少 token（fail closed）")
        self.base_url = validate_bridge_url(base_url)
        self._token = token
        self.http = http_client(app, timeout=timeout, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def ring(self, device_id: str, *, name: str = "", number: str = "") -> dict[str, Any]:
        """触发 device_id 手机的「鲸鱼来电」。返回 accept_ring_outcome 三态。
        name 恒传（含空串=来电匿名：hub 区分未传/显式空后生效）；number 空即不传（保持 0/空显示）。"""
        params = {"device_id": device_id, "name": name}
        if number:
            params["number"] = number
        body = await request_json(self.http, "POST", self.base_url + "/v1/ring", SERVICE,
                                  params=params, headers=self._headers())
        return accept_ring_outcome(body)

    async def hangup(self, device_id: str) -> dict[str, Any]:
        """挂断/取消该手机的来电或通话。"""
        body = await request_json(self.http, "POST", self.base_url + "/v1/hangup", SERVICE,
                                  params={"device_id": device_id}, headers=self._headers())
        if isinstance(body, dict) and body.get("ok"):
            return {"status": "ok"}
        return {"status": "error", "error": "桥未确认挂断"}

    async def close(self) -> None:
        await self.http.aclose()
