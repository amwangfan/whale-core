"""whale-io 音频桥线协议测试夹具——**直接复制**自 whale-io 源码，禁止简化。

镜像基线：
  repo   : https://github.com/amwangfan/whale-io
  commit : 48f59933b78045e7a6e32a4d3ecb77b36302fd42 (2026-10-30)
  file   : infra/call-bridge/audio-bridge/server.py

复制内容（逐字节，含注释中的关键行为）：
  - ``read_frame`` / ``write_frame``：uint32 BE 长度（**仅 payload**）+
    uint8 类型 + payload；长度上限 1_000_000；0 长度 payload 合法。
  - ``handle_client`` 的握手分支：有 TOKEN 时先读 ``AUTH <token>``，
    校验失败**直接抛错断开（不发 AUTH_FAIL）**，成功则发 ``AUTH_OK``，
    之后由模式 handler 发 ``HELLO box mode=... sample=16000``。

whale-io 侧若修改协议，应同步更新本夹具并在 whale-core 测试中暴露差异。
"""
from __future__ import annotations

import hmac
import socket
import struct
import threading

# ---- 复制自 server.py（常量） ----
TYPE_PCM = 0
TYPE_CTRL = 1
SAMPLE_RATE = 16000


# ---- 复制自 server.py:recv_exact ----
def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf.extend(chunk)
    return bytes(buf)


# ---- 复制自 server.py:read_frame ----
def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    hdr = recv_exact(sock, 5)
    length = struct.unpack("!I", hdr[:4])[0]
    typ = hdr[4]
    if length > 1_000_000:
        raise ValueError(f"frame too large: {length}")
    payload = recv_exact(sock, length) if length else b""
    return typ, payload


# ---- 复制自 server.py:write_frame（lock 由调用方保证单线程） ----
def write_frame(sock: socket.socket, typ: int, payload: bytes) -> None:
    sock.sendall(struct.pack("!IB", len(payload), typ) + payload)


class WhaleIoBridgeFixture:
    """模拟 whale-io ``handle_client`` 真实帧序的 TCP 服务端夹具。

    参数与 server.py 语义一一对应：
      token       — 对应 BRIDGE_TOKEN（空串 = 未设置，直接 HELLO）
      mode        — echo / agent / omni（决定 HELLO 文本）
      auth_delay  — 模拟 server 在 8s 鉴权窗口内不响应（超时测试）
      script      — "ok" 之外的异常剧本：
                    "auth_fail_frame"  鉴权失败也发 AUTH_FAIL（mode handler 行为）
                    "hello_first"      乱序：AUTH_OK 之前先发 HELLO
                    "pcm_first"        乱序：握手阶段直接发 PCM
                    "close_on_connect"  accept 后立刻断开
    """

    def __init__(
        self,
        *,
        token: str = "",
        mode: str = "echo",
        auth_delay: float = 0.0,
        script: str = "ok",
        echo_audio: bool = True,
    ) -> None:
        self.token = token
        self.mode = mode
        self.auth_delay = auth_delay
        self.script = script
        self.echo_audio = echo_audio
        self.received: list[tuple[int, bytes]] = []
        self.errors: list[str] = []
        self.client_closed = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    # ---- 服务端逻辑（镜像 handle_client 分支顺序） ----

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            if self.script == "close_on_connect":
                return
            if self.token:
                # server.py: conn.settimeout(8) → 读 AUTH 帧 → 校验
                conn.settimeout(8)
                typ, payload = read_frame(conn)
                self.received.append((typ, payload))
                if typ != TYPE_CTRL or not payload.startswith(b"AUTH "):
                    raise PermissionError("audio authentication required")
                if not hmac.compare_digest(
                    payload[5:].decode("utf-8", "replace"), self.token
                ):
                    if self.script == "auth_fail_frame":
                        # mode handler 的 AUTH_FAIL 分支（server.py handle_echo 等）
                        write_frame(conn, TYPE_CTRL, b"AUTH_FAIL")
                    # 真实 handle_client：直接断开，不发 AUTH_FAIL
                    raise PermissionError("audio authentication failed")
                conn.settimeout(None)
                if self.auth_delay:
                    import time

                    time.sleep(self.auth_delay)
                # server.py: conn.sendall(struct.pack("!IB", 7, TYPE_CTRL) + b"AUTH_OK")
                if self.script == "hello_first":
                    write_frame(conn, TYPE_CTRL, self._hello())  # 乱序
                elif self.script == "pcm_first":
                    write_frame(conn, TYPE_PCM, b"\x00\x01" * 160)  # 乱序
                write_frame(conn, TYPE_CTRL, b"AUTH_OK")
                if self.script in ("hello_first", "pcm_first"):
                    return  # 乱序剧本：AUTH_OK 之后不再补 HELLO
            elif self.script == "pcm_first":
                write_frame(conn, TYPE_PCM, b"\x00\x01" * 160)
                return
            write_frame(conn, TYPE_CTRL, self._hello())
            self._serve(conn)
        except Exception as exc:  # noqa: BLE001 - 夹具记录后关闭
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self.client_closed.set()

    def _hello(self) -> bytes:
        return f"HELLO box mode={self.mode} sample=16000".encode()

    def _serve(self, conn: socket.socket) -> None:
        """echo 剧本：回送一帧音频后挂住，等客户端关闭。"""
        while True:
            try:
                typ, payload = read_frame(conn)
            except Exception:
                return
            self.received.append((typ, payload))
            if typ == TYPE_PCM and self.echo_audio:
                try:
                    write_frame(conn, TYPE_PCM, payload)
                except Exception:
                    return

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass
        self._thread.join(timeout=3)
