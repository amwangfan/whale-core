"""Ticket-authenticated browser WebSocket -> existing whale-io PCM TCP relay.

Optional dependency websockets. One-use 60s tickets stay out of URLs and logs;
permanent bridge credentials never leave the server. Audio isn't persisted here.
"""

from __future__ import annotations
import json
import logging
import secrets
import socket
import threading
import time
from whale_core.plugins.pcm_voice import pack_frame, read_frame


class VoiceGateway:
    def __init__(self, ctx, *, host="127.0.0.1", port=8081):
        from websockets.sync.server import serve

        self.ctx, self.host = ctx, host
        self.lock = threading.RLock()
        self.tickets = {}
        self.sessions = set()
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

    def ticket(self, provider):
        with self.lock:
            self.tickets = {
                key: value
                for key, value in self.tickets.items()
                if value["expires"] > time.monotonic()
            }
            if len(self.tickets) + len(self.sessions) >= 4:
                raise ValueError("at most four voice sessions/tickets")
            ticket = secrets.token_urlsafe(32)
            sid = secrets.token_hex(12)
            self.tickets[ticket] = {
                "expires": time.monotonic() + 60,
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
            "sample_rate": 16000,
        }

    def _handle(self, ws):
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
            self.ctx.bundle.manager.refresh()
            if self.ctx.bundle.registry.provider_of("voice_bridge") is not provider:
                ws.close(1008, "voice capability disabled")
                return
            from whale_core.core.budget import reserve

            reserve(self.ctx.store, "media_jobs")
            upstream = provider.connect()
            # Verify bridge greeting before exposing a ready call to the browser.
            kind, greeting = read_frame(upstream)
            if kind != 1 or not greeting.startswith(b"HELLO"):
                raise ValueError("PCM bridge rejected authentication or greeting")
            upstream.settimeout(60)
            ws.send(
                json.dumps(
                    {
                        "type": "ready",
                        "session_id": session["session_id"],
                        "sample_rate": 16000,
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
                        # Bridge control strings may contain private transcripts.
                        # Existing audio bridge records final transcripts itself.
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
            deadline = time.monotonic() + 600
            while (
                not stopped.is_set()
                and not self.stopping.is_set()
                and time.monotonic() < deadline
            ):
                self.ctx.bundle.manager.refresh()
                if self.ctx.bundle.registry.provider_of("voice_bridge") is not provider:
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
        except (ConnectionClosed, EOFError, OSError, ValueError):
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

    def close(self):
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
