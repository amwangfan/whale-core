"""whale-io PCM wire adapter（移植自 PR#4）；实时音频路径不经过文字模型。

帧协议（与 whale-io ``infra/call-bridge/audio-bridge`` 一致）::

    uint32 BE 长度（含类型字节）+ uint8 类型 + payload
    类型 0 = PCM16 音频；类型 1 = 控制文本（AUTH / HELLO …）

**协议按 PR#4 契约实现，尚未与真实 whale-io 桥联调**；离线回环测试覆盖
帧往返、AUTH 帧与地址校验。安全约定与 provider_http/speech 一致：公网
地址必须 TLS，WebSocket 公网入口必须 WSS，URL 不得内嵌凭据。
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import struct

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

MAX_FRAME = 512 * 1024


def pack_frame(kind: int, data: bytes) -> bytes:
    if kind not in (0, 1) or len(data) > MAX_FRAME - 1:
        raise ValueError("invalid PCM frame")
    return struct.pack("!I", len(data) + 1) + bytes([kind]) + data


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            part = sock.recv(size - len(result))
            if not part:
                raise EOFError("PCM bridge disconnected")
            result.extend(part)
        return bytes(result)

    length = struct.unpack("!I", exact(4))[0]
    if not 1 <= length <= MAX_FRAME:
        raise ValueError("invalid PCM frame length")
    data = exact(length)
    if data[0] not in (0, 1):
        raise ValueError("invalid PCM frame type")
    return data[0], data[1:]


class PCMVoiceProvider:
    """现有 whale-io 音频桥的 PCM TCP 客户端（插件 ``voice_bridge``）。

    只做地址/配置校验与连接建立（含可选 TLS 与 AUTH 帧）；``healthcheck``
    不触网。永久桥 token 由本对象持有，**不出现在 URL、日志或 API 响应**。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 18799,
        bridge_token: str = "",
        tls: bool = False,
        public_ws_url: str = "",
        timeout: float = 15.0,
    ) -> None:
        if (
            not isinstance(host, str)
            or not host
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ValueError("invalid PCM address")
        private = host == "localhost"
        try:
            address = ipaddress.ip_address(host)
            private = private or address.is_loopback or address.is_private
        except ValueError:
            pass
        if not tls and not private:
            raise ValueError("public PCM bridge requires TLS")
        if not isinstance(tls, bool) or not 0 < timeout <= 120:
            raise ValueError("invalid PCM transport settings")
        if "\n" in bridge_token or "\r" in bridge_token or len(bridge_token) > 500:
            raise ValueError("invalid bridge token")
        if public_ws_url:
            from urllib.parse import urlsplit

            u = urlsplit(public_ws_url)
            if (
                u.scheme not in ("ws", "wss")
                or not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
            ):
                raise ValueError("invalid public WebSocket URL")
            if u.scheme == "ws" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("remote browser audio requires WSS")
        self.host, self.port, self.bridge_token, self.tls = (
            host,
            port,
            bridge_token,
            tls,
        )
        self.public_ws_url, self.timeout = public_ws_url, timeout

    def healthcheck(self) -> bool:
        return True  # 地址/配置校验；不发真实呼叫

    def connect(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self.host
                )
            if self.bridge_token:
                sock.sendall(pack_frame(1, ("AUTH " + self.bridge_token).encode()))
            sock.settimeout(self.timeout)
            return sock
        except Exception:
            sock.close()
            raise


def register_pcm_voice(registry: PluginRegistry, provider: PCMVoiceProvider):
    """注册 ``voice_bridge`` 插件（kind=model，provides=voice.pcm_bridge）。

    与 ``realtime_voice``（voice.realtime，base64 chunk/poll 会话）并存：
    本插件是浏览器 AudioWorklet + WebSocket 的低延迟传输选项，两者互不替代。
    """
    registry.register(
        PluginManifest(
            plugin_id="voice_bridge",
            name="实时语音 · whale-io PCM 桥",
            version="1.0.0",
            kind="model",
            provides=("voice.pcm_bridge",),
            description="浏览器低延迟通话：WebSocket 中继到现有 whale-io PCM 音频桥",
        ),
        provider=provider,
    )
    return provider
