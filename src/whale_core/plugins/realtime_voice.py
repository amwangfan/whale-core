"""Native Realtime WebSocket sessions; audio never traverses the text LLM.

Works with the OpenAI-compatible event protocol used by whale-io's Omni bridge.
The optional websockets dependency is loaded only when opening a session.
"""
from __future__ import annotations
import base64
import json
import urllib.parse
from typing import Callable
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.speech import SpeechError


class RealtimeVoiceSession:
    def __init__(self, connection):
        self._connection = connection
        self.closed = False

    def _send(self, event: dict) -> None:
        if self.closed:
            raise SpeechError("config", "realtime session closed")
        try:
            self._connection.send(json.dumps(event))
        except Exception as exc:
            raise SpeechError("network", "realtime send failed: "+type(exc).__name__) from None

    def append_audio(self, pcm: bytes) -> None:
        if not isinstance(pcm, bytes) or not pcm or len(pcm) > 1024*1024:
            raise ValueError("audio chunk must contain 1..1048576 bytes")
        self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    def commit(self) -> None:
        self._send({"type": "input_audio_buffer.commit"})
        self._send({"type": "response.create"})

    def cancel(self) -> None:
        self._send({"type": "response.cancel"})
        self._send({"type": "input_audio_buffer.clear"})

    def receive(self, timeout: float = 30) -> dict:
        if self.closed:
            raise SpeechError("config", "realtime session closed")
        try:
            raw = self._connection.recv(timeout=timeout)
            if len(raw) > 2*1024*1024:
                raise ValueError("event too large")
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("missing event type")
            return event
        except (ValueError, UnicodeError) as exc:
            raise SpeechError("bad_response", "invalid realtime event") from None
        except TimeoutError:
            return {"type": "whale.heartbeat"}
        except Exception as exc:
            raise SpeechError("network", "realtime receive failed: "+type(exc).__name__) from None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OpenAICompatibleRealtime:
    def __init__(self, *, url: str, api_key: str, model: str, voice: str = "alloy",
                 connector: Callable | None = None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError("realtime endpoint must be wss without embedded credentials")
        if not model.strip():
            raise ValueError("realtime model is required")
        self.url, self.api_key, self.model, self.voice = url, api_key, model, voice
        self._connector = connector

    def open_session(self, *, instructions: str = "") -> RealtimeVoiceSession:
        if not self.api_key:
            raise SpeechError("config", "realtime API key not configured")
        connect = self._connector
        if connect is None:
            try:
                from websockets.sync.client import connect
            except ImportError:
                raise SpeechError("config", 'install whale-core[voice] to enable realtime voice') from None
        parts = urllib.parse.urlsplit(self.url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        query["model"] = self.model
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        try:
            connection = connect(url, additional_headers={"Authorization": "Bearer "+self.api_key},
                                 open_timeout=10, max_size=2*1024*1024)
            session = RealtimeVoiceSession(connection)
            session._send({"type": "session.update", "session": {
                "modalities": ["text", "audio"], "voice": self.voice,
                "input_audio_format": "pcm16", "output_audio_format": "pcm16",
                "turn_detection": None, "instructions": instructions[:20000]}})
            return session
        except Exception as exc:
            # A failed initial session.update happens after connecting but before
            # the bridge owns the session. Close that untracked socket as well.
            if 'connection' in locals():
                try:
                    connection.close()
                except Exception:
                    pass
            if isinstance(exc, SpeechError):
                raise
            raise SpeechError("network", "realtime connection failed: "+type(exc).__name__) from None


def register_realtime_voice(registry, provider):
    registry.register(PluginManifest(plugin_id="voice_realtime", name="原生实时语音", version="1.0.0",
        kind="model", provides=("voice.realtime",), description="OpenAI兼容原生音频WebSocket；独立实时会话"), provider=provider)
    return provider
