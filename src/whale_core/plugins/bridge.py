"""自定义服务桥的 HTTP 客户端：渠道消息桥 + 外置 manifest 插件执行器（PR#4 移植）。

分层：内置 provider 实现，非插件框架契约（builtin-provider，issue #31）。

线协议（PR#4 ``docs/service-bridges.md`` 定义，本模块只实现客户端传输层，
不部署桥、不附带桥实现）：

- ``POST /sessions``：实时会话协商。请求 ``{session_id, instructions, model}``，
  响应 ``{session_id, url}``——url 是交给浏览器/设备直连的短寿命连接地址，
  音频在设备与桥之间直达，不经过文本编排层；
- ``POST /messages``：渠道消息投递。请求
  ``{channel, target, text, idempotency_key}``，桥必须按幂等键去重；
  响应 ``{"accepted": true}`` 仅表示渠道受理，不代表送达或已读；
- ``POST /execute``：外置 manifest 插件执行。请求
  ``{plugin_id, action, target, args}``，响应 ``{"ok": bool, "receipt": str}``。

**联调状态（诚实标注）**：上述自定义桥契约**未与真实部署的桥服务联调**；
线上启用前需对目标桥按契约验收。发送前的全局暂停、渠道白名单、静默时段、
额度与单次审批都在宿主侧（ProactiveOutbox / PluginGateway），本模块不绕过
也不替代。

``HttpPluginExecutor`` 与 ``loader.py`` 互补：loader 经 importlib 同进程加载
**可信**目录插件；本执行器服务 ``transport=http`` 的外置插件——核心只发
HTTP 请求，**从不 import 第三方 Python 代码**。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from whale_core.plugins.manifest import PLUGIN_ID_RE
from whale_core.plugins.provider_http import JsonProvider, ProviderError

__all__ = ["HttpBridgeProvider", "HttpPluginExecutor", "ProviderError"]

_EXECUTE_CONFIG_KEYS = frozenset({"api_key", "base_url", "model", "timeout"})
_RECEIPT_MAX_CHARS = 500
_PERSONA_MAX_CHARS = 4000
_TEXT_MAX_CHARS = 8000


class HttpBridgeProvider(JsonProvider):
    """自定义渠道/实时会话桥客户端（service-bridges 契约的 ``protocol=http``）。

    传输安全全部继承 JsonProvider / provider_http：HTTPS 强制（私网 HTTP
    例外）、禁 URL 凭据、无重定向、有界响应、错误分类。
    """

    def start_session(self, *, persona: str = "", session_id: str = "") -> dict:
        """协商实时会话，返回 ``{"session_id", "url"}``（url 供设备直连）。"""
        data = self.request("/sessions", {
            "session_id": session_id,
            "instructions": persona[:_PERSONA_MAX_CHARS],
            "model": self.model,
        })
        url = data.get("url", "")
        if not isinstance(url, str) or urlsplit(url).scheme not in ("https", "wss", "http", "ws"):
            raise ProviderError("bad_response")
        return {"session_id": str(data.get("session_id") or session_id), "url": url}

    def send(self, channel: str, target: str, text: str, *, idempotency_key: str) -> dict:
        """投递渠道消息；``idempotency_key`` 原样透传，桥侧按它幂等去重。"""
        if not isinstance(channel, str) or not channel:
            raise ValueError("channel is required")
        if not isinstance(target, str) or not target:
            raise ValueError("target is required")
        if not isinstance(text, str) or len(text) > _TEXT_MAX_CHARS:
            raise ValueError("text exceeds channel message limit")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required (bridge must deduplicate on it)")
        return self.request("/messages", {
            "channel": channel,
            "target": target,
            "text": text,
            "idempotency_key": idempotency_key,
        })


class HttpPluginExecutor:
    """外置 manifest 插件的 HTTP 执行器（满足 registry.PluginExecutor 协议）。

    ``config`` 只接受 JsonProvider 的连接键（``base_url``/``api_key``/
    ``model``/``timeout``）——清单不含凭据，实际值由宿主私有配置注入。
    """

    def __init__(self, plugin_id: str, config):
        if not isinstance(plugin_id, str) or not PLUGIN_ID_RE.fullmatch(plugin_id):
            raise ValueError("invalid plugin_id")
        unknown = set(config) - _EXECUTE_CONFIG_KEYS
        if unknown:
            raise ValueError("unknown executor config keys: " + ",".join(sorted(unknown)))
        self.plugin_id = plugin_id
        self.client = JsonProvider(**config)

    def healthcheck(self):
        return self.client.healthcheck()

    def execute(self, action: str, target: str, args) -> tuple[bool, str]:
        """调用桥 ``POST /execute``；返回 ``(ok, receipt)`` 短回执（有界截断）。"""
        data = self.client.request("/execute", {
            "plugin_id": self.plugin_id,
            "action": action,
            "target": target,
            "args": dict(args),
        })
        # receipt 是短文本回执，长度有界；凭据不得出现在回执中（契约要求桥自制）。
        return data.get("ok") is True, str(
            data.get("receipt") or "no execution receipt")[:_RECEIPT_MAX_CHARS]
