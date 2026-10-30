"""Whale-io PCM wire adapter; no text model in the realtime audio path."""

from __future__ import annotations
import ipaddress
import socket
import ssl
import struct

MAX_FRAME = 512 * 1024


def pack_frame(kind, data):
    if kind not in (0, 1) or len(data) > MAX_FRAME - 1:
        raise ValueError("invalid PCM frame")
    return struct.pack("!I", len(data) + 1) + bytes([kind]) + data


def read_frame(sock):
    def exact(size):
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
    def __init__(
        self,
        *,
        host="127.0.0.1",
        port=18799,
        bridge_token="",
        tls=False,
        public_ws_url="",
        timeout=15.0,
    ):
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

    def healthcheck(self):
        return True  # address/configuration check; does not make a phone call

    def connect(self):
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
