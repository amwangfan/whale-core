"""whale-io PCM wire adapter；实时音频路径不经过文字模型。

帧协议（与 whale-io ``infra/call-bridge/audio-bridge/server.py`` 逐字节一致，
镜像基线 whale-io@48f5993）::

    uint32 BE 长度（**仅 payload，不含类型字节**）+ uint8 类型 + payload
    类型 0 = PCM s16le mono 16 kHz；类型 1 = 控制文本（AUTH / HELLO …）

握手状态机（与 whale-io ``handle_client`` 真实帧序一致）::

    无 BRIDGE_TOKEN：server 直接发 HELLO
    有 BRIDGE_TOKEN：client → AUTH <token>
                     server → AUTH_OK（鉴权失败时 server 直接断开，不发 AUTH_FAIL）
                     server → HELLO box mode=<echo|agent|omni> sample=16000 …

客户端显式状态机 ``CONNECT → AUTH_SENT → AUTH_OK → HELLO → READY``：
乱序帧、PCM 抢跑、AUTH_FAIL、错误长度、超时、对端断开一律拒绝并释放 socket。
**禁止以关闭鉴权作为修复手段。**

测试夹具 ``tests/fixtures/whale_io_protocol.py`` 直接复制 whale-io 服务端
帧函数与握手分支（注明镜像 SHA），不走简化替身。真实设备联调待 owner 安排，
联调前本模块仍按"模拟器层已验证、真机未验证"报告。

安全约定与 provider_http/speech 一致：公网地址必须 TLS，WebSocket 公网
入口必须 WSS，URL 不得内嵌凭据；永久桥 token 不出现在 URL、日志或 API 响应。
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import struct

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

MAX_FRAME = 512 * 1024
TYPE_PCM = 0
TYPE_CTRL = 1


class PCMHandshakeError(ConnectionError):
    """PCM 桥握手失败：鉴权被拒、帧序错乱、超时或对端断开。"""


def pack_frame(kind: int, data: bytes) -> bytes:
    """按 whale-io 线格式打包：长度字段**不含**类型字节（5 字节头）。"""
    if kind not in (TYPE_PCM, TYPE_CTRL) or len(data) > MAX_FRAME:
        raise ValueError("invalid PCM frame")
    return struct.pack("!IB", len(data), kind) + data


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    """按 whale-io 线格式读一帧；允许 0 长度 payload（与服务端一致）。"""

    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            part = sock.recv(size - len(result))
            if not part:
                raise EOFError("PCM bridge disconnected")
            result.extend(part)
        return bytes(result)

    header = exact(5)
    length = struct.unpack("!I", header[:4])[0]
    kind = header[4]
    if length > MAX_FRAME:
        raise ValueError("invalid PCM frame length")
    if kind not in (TYPE_PCM, TYPE_CTRL):
        raise ValueError("invalid PCM frame type")
    return kind, exact(length) if length else b""


def _parse_hello(payload: bytes) -> dict[str, str]:
    """解析 ``HELLO box mode=omni sample=16000 voice=xx`` 键值尾巴。"""
    info: dict[str, str] = {}
    try:
        tokens = payload.decode("utf-8", "replace").split()
    except Exception:
        return info
    for token in tokens[1:]:  # 跳过 "HELLO"
        if "=" in token:
            key, _, value = token.partition("=")
            info[key] = value
    return info


class PCMVoiceProvider:
    """现有 whale-io 音频桥的 PCM TCP 客户端（插件 ``voice_bridge``）。

    ``connect()`` 完成**完整握手**（可选 AUTH → AUTH_OK → HELLO）后返回
    就绪 socket；任何一步失败都关闭 socket 并抛 ``PCMHandshakeError``。
    握手结果（mode/sample/voice/tools）在 ``last_hello`` 可查。
    ``healthcheck`` 不触网。永久桥 token 由本对象持有，**不出现在 URL、
    日志或 API 响应**。
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
        self.last_hello: dict[str, str] = {}

    def healthcheck(self) -> bool:
        return True  # 地址/配置校验；不发真实呼叫

    # ---- 握手状态机 -------------------------------------------------

    def _expect_ctrl(self, sock: socket.socket, state: str) -> bytes:
        """在握手阶段读一帧，必须是控制帧；否则抛 PCMHandshakeError。"""
        try:
            kind, payload = read_frame(sock)
        except socket.timeout:
            raise PCMHandshakeError(f"PCM bridge handshake timeout at {state}") from None
        except EOFError:
            # whale-io 鉴权失败时不发 AUTH_FAIL，直接断开（server.py handle_client）
            raise PCMHandshakeError(
                f"PCM bridge closed connection at {state}"
                + ("（鉴权被拒绝或对端要求鉴权）" if state == "AUTH_SENT" else "")
            ) from None
        except (OSError, ValueError) as exc:
            raise PCMHandshakeError(
                f"PCM bridge handshake failed at {state}: {exc}"
            ) from None
        if kind != TYPE_CTRL:
            raise PCMHandshakeError(
                f"PCM bridge sent audio before greeting at {state}（帧序错乱）"
            )
        return payload

    def _handshake(self, sock: socket.socket) -> None:
        """CONNECT → (AUTH_SENT → AUTH_OK) → HELLO → READY。"""
        if self.bridge_token:
            sock.sendall(pack_frame(TYPE_CTRL, ("AUTH " + self.bridge_token).encode()))
            reply = self._expect_ctrl(sock, "AUTH_SENT")
            if reply == b"AUTH_FAIL":
                raise PCMHandshakeError("PCM bridge authentication rejected")
            if not reply.startswith(b"AUTH_OK"):
                raise PCMHandshakeError(
                    "PCM bridge expected AUTH_OK, got unexpected control frame"
                )
        hello = self._expect_ctrl(sock, "EXPECT_HELLO")
        if hello.startswith(b"AUTH_FAIL"):
            raise PCMHandshakeError("PCM bridge authentication rejected")
        if hello.startswith(b"AUTH_OK"):
            raise PCMHandshakeError("PCM bridge sent AUTH_OK without AUTH（帧序错乱）")
        if not hello.startswith(b"HELLO"):
            raise PCMHandshakeError("PCM bridge did not greet with HELLO")
        self.last_hello = _parse_hello(hello)

    def connect(self) -> socket.socket:
        """建连并完成握手；失败时 socket 必被关闭，不泄露半开连接。"""
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self.host
                )
            sock.settimeout(self.timeout)
            self._handshake(sock)
            return sock
        except Exception:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
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
            version="1.1.0",
            kind="model",
            provides=("voice.pcm_bridge",),
            description="浏览器低延迟通话：WebSocket 中继到现有 whale-io PCM 音频桥",
        ),
        provider=provider,
    )
    return provider
