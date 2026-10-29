"""ActionGateway：把意图交给基础设施。

HttpDashboardGateway / LocalBridgeGateway / PhoneControlGateway 均为 stub，
不发起真实网络连接、不启子进程、不写 ADB。
域名示例：https://rayneo.wangfanlab.com/turbo
桥默认：127.0.0.1:18799
禁止写入真实公网/Tailscale IP、Token、密钥。
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from whale_core.core.types import ActionIntent

# 文档约定的示例端点（非密钥、非真实设备 IP）
DEFAULT_DASHBOARD_BASE = "https://rayneo.wangfanlab.com/turbo"
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18799


@runtime_checkable
class ActionGateway(Protocol):
    def dispatch(self, intent: ActionIntent) -> str:
        """派发意图，返回回执字符串（stub 可返回描述性假回执）。"""
        ...


class NullActionGateway:
    """丢弃所有意图。"""

    def dispatch(self, intent: ActionIntent) -> str:
        return f"null:{intent.kind}"


class HttpDashboardGateway:
    """卡片 / HUD 推送网关 stub：记录目标 base_url，不发 HTTP。"""

    def __init__(self, base_url: str = DEFAULT_DASHBOARD_BASE) -> None:
        self.base_url = base_url.rstrip("/")
        self.last_intent: ActionIntent | None = None

    def dispatch(self, intent: ActionIntent) -> str:
        self.last_intent = intent
        # stub：不 requests / 不 aiohttp
        return f"stub-dashboard:{self.base_url}:{intent.kind}"


class LocalBridgeGateway:
    """VoIP 来电音频桥网关 stub：默认 127.0.0.1:18799，不建 TCP。"""

    def __init__(
        self,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self.last_intent: ActionIntent | None = None

    def dispatch(self, intent: ActionIntent) -> str:
        self.last_intent = intent
        return f"stub-bridge:{self.host}:{self.port}:{intent.kind}"


class PhoneControlGateway:
    """手机控制桥 stub：记录 last_intent，回执 stub-phone-control:{action}:{target}。

    监测（PhoneForeground）≠ 控制；本网关填补控制侧契约。
    **不**起子进程 / ADB / Accessibility。
    """

    def __init__(self) -> None:
        self.last_intent: ActionIntent | None = None
        self.dispatch_log: list[str] = []

    def dispatch(self, intent: ActionIntent) -> str:
        self.last_intent = intent
        payload = dict(intent.payload or {})
        action = str(payload.get("action") or intent.kind)
        target = str(payload.get("target") or "")
        receipt = f"stub-phone-control:{action}:{target}"
        self.dispatch_log.append(receipt)
        return receipt


class AmbientListenGateway:
    """环境听写启停 stub：只记 log，不碰麦克风。"""

    def __init__(self) -> None:
        self.last_intent: ActionIntent | None = None
        self.dispatch_log: list[str] = []

    def dispatch(self, intent: ActionIntent) -> str:
        self.last_intent = intent
        enabled = bool((intent.payload or {}).get("enabled"))
        receipt = f"stub-ambient-listen:{'on' if enabled else 'off'}"
        self.dispatch_log.append(receipt)
        return receipt



class MockHttpActionGateway:
    """把意图记成「拟发送」的 HTTP 请求，默认不真连。

    Base URL 仅来自构造参数或环境变量 ``WHALE_ACTIONS_HTTP_BASE``
    （缺省 ``http://127.0.0.1:18796``）。不读、不写、不编造 Token。
    若设置 ``WHALE_ACTIONS_HTTP_LIVE=1`` 仍不发送——本 stub 永远 mock，
    真实 HTTPS 走 ``HttpsDashboardGateway``（需调用方注入 endpoint+token）。
    """

    def __init__(self, base_url: str | None = None) -> None:
        import os

        raw = (base_url or os.environ.get("WHALE_ACTIONS_HTTP_BASE") or "").strip()
        self.base_url = (raw or "http://127.0.0.1:18796").rstrip("/")
        self.last_intent: ActionIntent | None = None
        self.last_mock: dict | None = None
        self.dispatch_log: list[str] = []

    def dispatch(self, intent: ActionIntent) -> str:
        self.last_intent = intent
        if intent.kind == "shell_whitelist":
            path = "/v1/actions/shell"
        elif intent.kind == "shell_command":
            path = "/v1/actions/shell_command"
        else:
            path = "/v1/actions/card"
        url = f"{self.base_url}{path}"
        body = {
            "kind": intent.kind,
            "payload": dict(intent.payload or {}),
            "reason": intent.reason,
        }
        self.last_mock = {"method": "POST", "url": url, "json": body}
        receipt = f"mock-http:POST:{url}:{intent.kind}"
        self.dispatch_log.append(receipt)
        return receipt


class CompositeActionGateway:
    """按 intent.kind 路由到子网关。

    默认映射：
    - show_card / navigate_hint → dashboard
    - place_call / speak → bridge
    - phone_action → phone_control
    - ambient_listen → ambient_listen stub
    - shell_whitelist / shell_command → mock_http（拟 POST，不真连）
    - plugin_action → plugin（未注入时为 Null，回执与 fallback 一致）
    """

    def __init__(
        self,
        *,
        dashboard: ActionGateway | None = None,
        bridge: ActionGateway | None = None,
        phone_control: ActionGateway | None = None,
        ambient_listen: ActionGateway | None = None,
        mock_http: ActionGateway | None = None,
        plugin: ActionGateway | None = None,
        fallback: ActionGateway | None = None,
        kind_map: Mapping[str, str] | None = None,
    ) -> None:
        self.dashboard = dashboard or HttpDashboardGateway()
        self.bridge = bridge or LocalBridgeGateway()
        self.phone_control = phone_control or PhoneControlGateway()
        self.ambient_listen = ambient_listen or AmbientListenGateway()
        self.mock_http = mock_http or MockHttpActionGateway()
        self.plugin = plugin or NullActionGateway()
        self.fallback = fallback or NullActionGateway()
        self.kind_map = dict(
            kind_map
            or {
                "show_card": "dashboard",
                "navigate_hint": "dashboard",
                "place_call": "bridge",
                "speak": "bridge",
                "phone_action": "phone_control",
                "ambient_listen": "ambient_listen",
                "shell_whitelist": "mock_http",
                "shell_command": "mock_http",
                "plugin_action": "plugin",
            }
        )
        self.last_intent: ActionIntent | None = None
        self.last_receipt: str | None = None

    def _resolve(self, kind: str) -> ActionGateway:
        route = self.kind_map.get(kind, "")
        if route == "dashboard":
            return self.dashboard
        if route == "bridge":
            return self.bridge
        if route == "phone_control":
            return self.phone_control
        if route == "ambient_listen":
            return self.ambient_listen
        if route == "plugin":
            return self.plugin
        if route == "mock_http":
            return self.mock_http
        return self.fallback

    def dispatch(self, intent: ActionIntent) -> str:
        self.last_intent = intent
        receipt = self._resolve(intent.kind).dispatch(intent)
        self.last_receipt = receipt
        return receipt
