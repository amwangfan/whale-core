"""票据鉴权的浏览器 WebSocket → 现有 whale-io PCM TCP 中继（移植自 PR#4）。

可选依赖 websockets（``whale-core[voice]``）。一次性 60 秒票据经 WebSocket
subprotocol 传递，不进 URL、不落盘、**不进日志**；永久桥凭据只留在服务端
``PCMVoiceProvider``。音频字节不在本层持久化。

握手（AUTH → AUTH_OK → HELLO 显式状态机）由 ``PCMVoiceProvider.connect``
完成，帧格式与 whale-io ``server.py`` 逐字节一致（镜像基线见 pcm_voice.py）；
模拟器层测试通过，**真机联调待安排**。

与 ``api/server.py`` 的 ``/voice/sessions``（realtime base64 chunk/poll）
并存：本模块是 ``voice.pcm_bridge`` 能力启用时的低延迟传输选项。
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
import time

from whale_core.plugins.pcm_voice import (
    PCMHandshakeError,
    pack_frame,
    read_frame,
)

TICKET_TTL_SECONDS = 60
MAX_VOICE_SESSIONS = 4
SESSION_DEADLINE_SECONDS = 600
SAMPLE_RATE = 16000


class VoiceGateway:
    """一次性票据 → WebSocket 中继会话（懒加载 websockets 可选依赖）。"""

    def __init__(self, ctx, *, host: str = "127.0.0.1", port: int = 8081) -> None:
        from websockets.sync.server import serve

        self.ctx, self.host = ctx, host
        self.lock = threading.RLock()
        self.tickets: dict[str, dict] = {}
        self.sessions: set = set()
        self.stopping = threading.Event()
        logger = logging.getLogger("whale.voice.transport")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        self.server = serve(
            self._handle,
            host,
            port,
            subprotocols=["whale"],
            max_size=65536,
            max_queue=8,
            open_timeout=10,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            logger=logger,
        )
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="whale-voice"
        )
        self.thread.start()

    def ticket(self, provider) -> dict:
        """签发一次性短过期票据；票据本身绝不写日志（只记录 session_id）。"""
        with self.lock:
            self.tickets = {
                key: value
                for key, value in self.tickets.items()
                if value["expires"] > time.monotonic()
            }
            if len(self.tickets) + len(self.sessions) >= MAX_VOICE_SESSIONS:
                raise ValueError("at most four voice sessions/tickets")
            ticket = secrets.token_urlsafe(32)
            sid = secrets.token_hex(12)
            self.tickets[ticket] = {
                "expires": time.monotonic() + TICKET_TTL_SECONDS,
                "provider": provider,
                "session_id": sid,
            }
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "localhost") else self.host
        if ":" in host:
            host = "[" + host + "]"
        return {
            "protocol": "whale-io-pcm",
            "url": provider.public_ws_url or f"ws://{host}:{self.port}",
            "ticket": ticket,
            "session_id": sid,
            "sample_rate": SAMPLE_RATE,
        }

    def _provider_enabled(self, provider) -> bool:
        """当前注册表里的 voice_bridge 仍是签发时的同一 provider 才算启用。

        main 的 ApiContext 直接持有 ``manager``（不同于 PR#4 的 ``ctx.bundle``）；
        插件禁用/重装配会替换 registry 里的 provider 对象，会话随即关闭。
        """
        manager = getattr(self.ctx, "manager", None)
        registry = getattr(manager, "registry", None)
        return registry is not None and registry.provider_of("voice_bridge") is provider

    def _handle(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        protocols = ws.request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        supplied = [p.strip() for p in protocols if p.strip() != "whale"]
        with self.lock:
            session = (
                self.tickets.pop(supplied[0], None) if len(supplied) == 1 else None
            )
            if session is not None and session["expires"] > time.monotonic():
                self.sessions.add(ws)
            else:
                session = None
        if session is None:
            ws.close(1008, "invalid or expired session ticket")
            return
        stopped = threading.Event()
        upstream = None
        receiver = None
        try:
            provider = session["provider"]
            if not self._provider_enabled(provider):
                ws.close(1008, "voice capability disabled")
                return
            # connect() 内含完整握手状态机（AUTH → AUTH_OK → HELLO），
            # 鉴权被拒/乱序/超时都会抛 PCMHandshakeError 并释放 socket。
            upstream = provider.connect()
            if provider.last_hello.get("sample", str(SAMPLE_RATE)) != str(SAMPLE_RATE):
                raise ValueError("PCM bridge sample rate mismatch")
            upstream.settimeout(60)
            ws.send(
                json.dumps(
                    {
                        "type": "ready",
                        "session_id": session["session_id"],
                        "sample_rate": SAMPLE_RATE,
                    }
                )
            )

            def incoming_audio():
                try:
                    while not stopped.is_set():
                        kind, data = read_frame(upstream)
                        if kind == 0:
                            if len(data) % 2:
                                raise ValueError("invalid PCM sample length")
                            ws.send(data)
                        # 桥控制字符串可能含私密转写；
                        # 现有音频桥自行记录最终转写。
                except Exception:
                    stopped.set()
                    try:
                        ws.close(1000, "audio bridge closed")
                    except Exception:
                        pass

            receiver = threading.Thread(
                target=incoming_audio, daemon=True, name="whale-pcm-recv"
            )
            receiver.start()
            deadline = time.monotonic() + SESSION_DEADLINE_SECONDS
            while (
                not stopped.is_set()
                and not self.stopping.is_set()
                and time.monotonic() < deadline
            ):
                if not self._provider_enabled(provider):
                    break
                try:
                    message = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                if isinstance(message, bytes):
                    if not message or len(message) % 2 or len(message) > 32768:
                        raise ValueError("invalid audio chunk")
                    upstream.sendall(pack_frame(0, message))
                elif message == "stop":
                    break
                else:
                    raise ValueError("only PCM audio or stop is accepted")
        except (ConnectionClosed, EOFError, OSError, ValueError, PCMHandshakeError):
            try:
                ws.close(1011, "voice bridge unavailable or session ended")
            except Exception:
                pass
        finally:
            stopped.set()
            if upstream:
                try:
                    upstream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                upstream.close()
            if receiver:
                receiver.join(timeout=2)
            with self.lock:
                self.sessions.discard(ws)
            self.ctx.log_event("voice:closed", session["session_id"])

    def close(self) -> None:
        self.stopping.set()
        with self.lock:
            sessions = list(self.sessions)
            self.tickets.clear()
        for ws in sessions:
            try:
                ws.close(1001, "core stopping")
            except Exception:
                pass
        self.server.shutdown()
        self.thread.join(timeout=3)
