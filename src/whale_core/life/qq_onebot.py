"""W09 QQ 渠道：OneBot v11 适配器（身份入站 + 出站受理端口）。

边界（与 actions/gateway.py 同款约定）：
- 默认不发起任何网络连接：``NullOneBotTransport`` 是默认 stub；
  ``HttpOneBotTransport`` 与 ``OneBotHttpListener`` 只在调用方显式注入本机配置
  （NapCat / LLOneBot 等协议端地址与 token）后才生效，不读环境变量自动连。
- token 只在内存中比较（``hmac.compare_digest``），不写日志、不进异常文本、不回显。
- 入站必须经 ``OwnerChannelRegistry`` 显式配对：未鉴权、未配对事件一律产出 None；
  群聊默认拒绝（``accept_group=False``），群内容永远按不可信输入处理。
- 出站只做「单个预先配对对端」的 ``send_private_msg``，默认 ``enabled=False``；
  能不能发由 initiative 策略层决定（``direct_message`` 渠道强制逐条人工批准），
  本端口只负责传输受理。
- ``send`` 返回 True 仅表示 OneBot 受理（retcode=0），不代表送达或已读；
  任何失败返回 False，由 ``ProactiveOutbox`` 记 unknown，不自动重试。

协议端接法（OneBot v11 HTTP 模式）：入站用协议端「HTTP POST 事件推送」指向
``OneBotHttpListener``；出站用协议端 HTTP API 由 ``HttpOneBotTransport`` 调用。
"""
from __future__ import annotations

import hmac
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Protocol, runtime_checkable

from whale_core.life.channels import Incoming, OwnerChannelRegistry, OwnerEvent
from whale_core.life.initiative import ProactiveDraft

CHANNEL_QQ = "qq"
DEFAULT_ONEBOT_HOST = "127.0.0.1"
DEFAULT_ONEBOT_PORT = 18798  # 示例端口；真实部署由本机配置注入，不构成承诺
MAX_TEXT_CHARS = 4000
MAX_BODY_BYTES = 1_000_000


class OneBotTransportError(RuntimeError):
    """传输层失败（网络、HTTP 状态、响应体解析）。异常文本不含 token 与正文。"""


@runtime_checkable
class OneBotTransport(Protocol):
    def call(self, action: str, params: dict) -> dict:
        """调用一个 OneBot action，返回响应 JSON（dict）；失败抛 OneBotTransportError。"""
        ...


class NullOneBotTransport:
    """默认 stub：记录拟调用、永不触网（与 NullActionGateway 同款语义）。"""

    def __init__(self) -> None:
        self.last_call: tuple[str, dict] | None = None

    def call(self, action: str, params: dict) -> dict:
        self.last_call = (action, params)
        raise OneBotTransportError("stub transport: no real OneBot connection configured")


class HttpOneBotTransport:
    """OneBot v11 HTTP API 客户端（stdlib urllib，同步）。

    仅由调用方注入本机配置后实例化；token 走 Authorization: Bearer 头。
    ``opener`` 可注入以便离线测试，默认 urllib.request.urlopen。
    """

    def __init__(self, base_url: str, *, token: str = "", timeout: float = 10.0,
                 opener: Callable[[urllib.request.Request, float], object] | None = None) -> None:
        if not base_url.strip():
            raise ValueError("base_url required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))

    def call(self, action: str, params: dict) -> dict:
        url = f"{self.base_url}/{action}"
        body = json.dumps(params).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener(request, self.timeout) as response:  # type: ignore[union-attr]
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise OneBotTransportError(f"onebot http status {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise OneBotTransportError("onebot http unreachable") from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OneBotTransportError("onebot response not json")
        if not isinstance(parsed, dict):
            raise OneBotTransportError("onebot response not object")
        return parsed


def _extract_text(message: object) -> str:
    """从 OneBot message（段数组或字符串）提取纯文本；非 text 段（图片/语音/@等）忽略。"""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, Mapping) or segment.get("type") != "text":
                continue
            data = segment.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("text"), str):
                parts.append(data["text"])
        return "".join(parts)
    return ""


def parse_onebot_message(event: object, *, authenticated: bool,
                         accept_group: bool = False) -> Incoming | None:
    """OneBot v11 message 事件 → Incoming；解析不了或按策略不收的一律 None（宁缺勿错）。

    - 只认 post_type=message；private 恒收，group 需显式 accept_group；
    - bot 自己发的消息（user_id == self_id）不处理；
    - message_id 缺失则拒绝（registry 也要求非空）；
    - 文本超长按 MAX_TEXT_CHARS 截断，防超长文本冲击下游。
    """
    if not isinstance(event, Mapping) or event.get("post_type") != "message":
        return None
    user_id = event.get("user_id")
    message_id = event.get("message_id")
    if user_id is None or message_id is None:
        return None
    self_id = event.get("self_id")
    if self_id is not None and str(user_id) == str(self_id):
        return None
    message_type = event.get("message_type")
    if message_type == "private":
        conversation = f"private:{user_id}"
    elif message_type == "group" and accept_group:
        group_id = event.get("group_id")
        if group_id is None:
            return None
        conversation = f"group:{group_id}"
    else:
        return None  # 群聊默认关；频道/临时会话等暂不接入
    text = _extract_text(event.get("message"))
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return Incoming(channel=CHANNEL_QQ, account=str(user_id), conversation=conversation,
                    message_id=str(message_id), text=text,
                    transport_authenticated=bool(authenticated))


def bearer_authenticated(header_value: str | None, expected_token: str) -> bool:
    """Authorization: Bearer <token> 恒定时间比较；未配置期望 token 时 fail closed。"""
    if not expected_token or not header_value:
        return False
    scheme, _, credentials = header_value.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return False
    return hmac.compare_digest(credentials.strip(), expected_token)


class OneBotInboundBridge:
    """事件 → Incoming → OwnerChannelRegistry 配对。纯逻辑，不触网、不落日志。"""

    def __init__(self, registry: OwnerChannelRegistry, *, accept_group: bool = False) -> None:
        self.registry = registry
        self.accept_group = accept_group

    def handle_event(self, event: object, *, authenticated: bool) -> OwnerEvent | None:
        incoming = parse_onebot_message(event, authenticated=authenticated,
                                        accept_group=self.accept_group)
        if incoming is None:
            return None
        return self.registry.accept(incoming)

    def handle_http_request(self, headers: Mapping[str, str], body: bytes | None, *,
                            expected_token: str) -> tuple[int, OwnerEvent | None]:
        """HTTP POST 事件推送入口的共用逻辑：返回 (状态码, owner 事件或 None)。

        401 token 不符（或未配置 token）；413 body 缺失/超限；400 body 不是 JSON 对象；
        204 处理完成（无论是否产出 owner 事件）。鉴权通过后事件才按 authenticated=True
        进入配对；响应不回显 token 或正文。
        """
        authorization: str | None = None
        for key, value in headers.items():
            if key.lower() == "authorization":
                authorization = value
                break
        if not bearer_authenticated(authorization, expected_token):
            return 401, None
        if body is None or len(body) > MAX_BODY_BYTES:
            return 413, None
        try:
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, None
        if not isinstance(event, Mapping):
            return 400, None
        return 204, self.handle_event(event, authenticated=True)


class OneBotHttpListener:
    """接收协议端「HTTP POST 事件推送」的 stdlib HTTP 服务（NapCat 等）。

    - 必须配置非空 token，否则拒绝启动（fail closed）；
    - 默认只绑 127.0.0.1；对外暴露必须经自建反代/隧道并另行评估风险；
    - 不记录请求内容（access log 静默），只回状态码；
    - start() 幂等；stop() 关闭套接字并等待线程退出。
    """

    def __init__(self, bridge: OneBotInboundBridge, *, expected_token: str,
                 host: str = DEFAULT_ONEBOT_HOST, port: int = DEFAULT_ONEBOT_PORT) -> None:
        if not expected_token:
            raise ValueError("OneBotHttpListener requires a non-empty token (fail closed)")
        self.bridge = bridge
        self.expected_token = expected_token
        self.host = host
        self.requested_port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """实际绑定端口（port=0 随机绑定后取真实值）。"""
        return int(self._server.server_address[1]) if self._server else self.requested_port

    def start(self) -> None:
        if self._server is not None:
            return
        bridge, token = self.bridge, self.expected_token

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self._read_capped_body()
                code, _ = bridge.handle_http_request(dict(self.headers), body,
                                                     expected_token=token)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                # 探活只回状态码，不暴露任何配置或数据。
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _read_capped_body(self) -> bytes | None:
                raw = self.headers.get("Content-Length")
                try:
                    length = int(raw) if raw is not None else -1
                except ValueError:
                    return None
                if length < 0 or length > MAX_BODY_BYTES:
                    return None
                return self.rfile.read(length) if length > 0 else b""

            def log_message(self, *args):
                pass  # 静默 access log：不含正文、不含 token

        self._server = ThreadingHTTPServer((self.host, self.requested_port), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="onebot-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)


class OneBotOutboundPort:
    """direct_message 草稿的 OutboundPort 实现：OneBot send_private_msg。

    - 构造时绑定单个对端 user_id（已配对 owner 的 QQ 号）；不支持群发、不支持换目标；
    - 默认 enabled=False：构造了也不发，必须显式开启；
    - 是否走到 send 由 ProactiveOutbox 策略层决定（direct_message 强制逐条人工批准）；
    - True 仅表示 OneBot 受理（retcode=0 / status=ok）；任何异常都记 False，
      账本落 unknown，不自动重试。
    """

    def __init__(self, transport: OneBotTransport, *, user_id: str, enabled: bool = False,
                 channel: str = "direct_message", max_text_chars: int = MAX_TEXT_CHARS) -> None:
        if not str(user_id).isdigit():
            raise ValueError("user_id must be a numeric QQ id")
        if max_text_chars <= 0:
            raise ValueError("max_text_chars must be positive")
        self.transport = transport
        self.user_id = str(user_id)
        self.enabled = enabled
        self.channel = channel
        self.max_text_chars = max_text_chars
        self.last_receipt: dict | None = None

    def send(self, draft: ProactiveDraft) -> bool:
        if not self.enabled or draft.channel != self.channel:
            return False
        text = draft.text
        if not text.strip() or len(text) > self.max_text_chars:
            return False
        try:
            receipt = self.transport.call("send_private_msg", {
                "user_id": int(self.user_id),
                "message": [{"type": "text", "data": {"text": text}}],
            })
        except Exception:
            return False
        self.last_receipt = receipt
        return receipt.get("retcode") == 0 or receipt.get("status") == "ok"
