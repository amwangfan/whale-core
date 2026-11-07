"""可选外部出口（修正方案：ntfy/Bark/PushDeer 只是可选出口，不承担默认链路）。

纪律：
- **默认关闭**：``NtfyProvider`` 必须显式 ``enabled=True`` + endpoint + token
  才可用；``available`` 为 False 时 router 不选它，``send`` 直接抛
  ``NotificationProviderError("disabled")``（fail closed）；
- **最小文本**：只发 ``title + body`` 纯文本——actions/target/group_key 等
  结构化字段不出 core（外部服务泄露面最小化）；
- **有界重试**：默认 2 次尝试、指数退避；耗尽抛错由 router 归台账；
- Bark / PushDeer：仅占位，实例化即 ``NotImplementedError``。

真实 HTTP 走 stdlib ``urllib``（在 ``asyncio.to_thread`` 里执行，不阻塞
事件循环）；测试注入 ``post_fn`` 即可全程零网络。
"""
from __future__ import annotations

import asyncio
import threading
import urllib.request
from typing import Callable

from whale_core.notifications.models import DeliveryStatus, NotificationEvent
from whale_core.notifications.providers.base import NotificationProviderError

#: 有界重试默认：2 次尝试、0.5s 起步指数退避
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_BASE_DELAY_S = 0.5

#: post_fn 契约： (url, headers, body_text, timeout_s) -> (http_status, resp_text)
HttpPostFn = Callable[[str, dict, str, float], "tuple[int, str]"]


def _urllib_post(url: str, headers: dict, body_text: str,
                 timeout_s: float) -> "tuple[int, str]":
    """默认真实发送（stdlib urllib；仅 enabled=True 时才可能被调到）。"""
    req = urllib.request.Request(
        url, data=body_text.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        return resp.status, resp.read(4096).decode("utf-8", "replace")


class NtfyProvider:
    """ntfy 出口（可选，默认关闭）。name="ntfy"。"""

    name = "ntfy"

    def __init__(self, *, enabled: bool = False, endpoint: str = "",
                 token: str = "", timeout_s: float = 5.0,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 base_delay_s: float = DEFAULT_BASE_DELAY_S,
                 post_fn: HttpPostFn | None = None,
                 sleep_fn: Callable[[float], None] | None = None) -> None:
        if enabled and (not endpoint or not token):
            raise ValueError("enabled=True 时 endpoint 与 token 必填（显式开启）")
        if max_attempts <= 0:
            raise ValueError("max_attempts 须为正整数")
        self._enabled = bool(enabled)
        self._endpoint = endpoint
        self._token = token
        self._timeout_s = float(timeout_s)
        self._max_attempts = int(max_attempts)
        self._base_delay_s = float(base_delay_s)
        self._post = post_fn or _urllib_post
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._sent: dict[str, DeliveryStatus] = {}

    @property
    def available(self) -> bool:
        """router 选路依据：只有用户显式开启的外部出口才参与兜底。"""
        return self._enabled

    async def send(self, device_id: str, event: NotificationEvent) -> str:
        if not self._enabled:
            raise NotificationProviderError("disabled", "ntfy 出口未开启（默认关闭）")
        # 最小文本：title + body，结构化字段（actions/target）不出门
        body_text = event.title if not event.body else f"{event.title}\n{event.body}"
        headers = {"Authorization": f"Bearer {self._token}"}
        last_error = "unknown"
        for attempt in range(1, self._max_attempts + 1):
            try:
                status, _ = await asyncio.to_thread(
                    self._post, self._endpoint, headers, body_text, self._timeout_s)
            except Exception as exc:  # noqa: BLE001 —— 网络失败一律转为有界重试
                last_error = type(exc).__name__
                status = 0
            else:
                if 200 <= status < 300:
                    delivery_id = f"ntfy:{event.event_id}"
                    with self._lock:
                        self._sent[delivery_id] = DeliveryStatus.DELIVERED
                    return delivery_id
                last_error = f"http_{status}"
            if attempt < self._max_attempts:
                delay = self._base_delay_s * (2 ** (attempt - 1))
                if self._sleep is not None:
                    self._sleep(delay)
                else:
                    await asyncio.sleep(delay)
        raise NotificationProviderError(
            "unavailable", f"ntfy 发送失败（{self._max_attempts} 次尝试）：{last_error}")

    async def revoke(self, device_id: str, notification_id: str, *,
                     reason: str = "") -> bool:
        # ntfy 无撤回语义：诚实 no-op
        return False

    async def get_status(self, delivery_id: str) -> DeliveryStatus | None:
        with self._lock:
            return self._sent.get(delivery_id)


class BarkProvider:
    """占位：Bark 出口尚未实现（需要时按 NtfyProvider 同款显式开启纪律补齐）。"""

    name = "bark"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "BarkProvider 尚未实现：外部出口默认关闭，启用前请先补实现与配置")


class PushDeerProvider:
    """占位：PushDeer 出口尚未实现。"""

    name = "pushdeer"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "PushDeerProvider 尚未实现：外部出口默认关闭，启用前请先补实现与配置")
