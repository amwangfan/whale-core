"""Authenticated API hosts can expose native realtime sessions through this bridge.

Session IDs are opaque, audio stays in memory, and idle sessions close automatically.
This is a transport adapter; it never invokes the text model or authorizes tool calls.
"""
from __future__ import annotations
import base64
import binascii
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4
from whale_core.plugins.speech import SpeechError


class BridgeError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class _Session:
    session: object
    touched: float = field(default_factory=time.monotonic)
    reader: object = field(default_factory=threading.Lock)
    writer: object = field(default_factory=threading.Lock)


class RealtimeVoiceBridge:
    def __init__(self, provider, *, max_sessions: int = 4, idle_timeout: float = 120):
        if max_sessions < 1 or not 1 <= idle_timeout <= 3600:
            raise ValueError("invalid realtime session limits")
        self.provider, self.max_sessions, self.idle_timeout = provider, max_sessions, idle_timeout
        self._sessions: dict[str,_Session] = {}
        self._lock = threading.RLock()
        self._reserved = 0
        self._stop = threading.Event()
        self._janitor = threading.Thread(target=self._cleanup_loop, daemon=True, name="whale-voice-cleanup")
        self._janitor.start()

    def _cleanup_loop(self):
        while not self._stop.wait(min(30, self.idle_timeout)):
            self.cleanup()

    def cleanup(self) -> int:
        with self._lock:
            stale = [sid for sid,item in self._sessions.items() if time.monotonic()-item.touched > self.idle_timeout]
        for sid in stale:
            self.close(sid)
        return len(stale)

    def open(self, *, instructions: str = "") -> dict:
        if not isinstance(instructions,str) or len(instructions)>20000:
            raise BridgeError("bad_request", "invalid voice instructions")
        self.cleanup()
        with self._lock:
            if self._stop.is_set():
                raise BridgeError("config", "voice service stopped")
            if len(self._sessions)+self._reserved >= self.max_sessions:
                raise BridgeError("quota", "voice session limit reached")
            self._reserved += 1
        try:
            session = self.provider.open_session(instructions=instructions)
            sid = uuid4().hex
            with self._lock:
                if self._stop.is_set():
                    session.close()
                    raise BridgeError("config", "voice service stopped")
                self._sessions[sid] = _Session(session)
            return {"session_id":sid, "status":"connected"}
        except SpeechError as exc:
            raise BridgeError(exc.kind, str(exc)) from None
        finally:
            with self._lock:
                self._reserved -= 1

    def _get(self, sid: str) -> _Session:
        with self._lock:
            item = self._sessions.get(sid)
            if item is None:
                raise BridgeError("not_found", "voice session not found")
            if time.monotonic()-item.touched > self.idle_timeout:
                self.close(sid)
                raise BridgeError("not_found", "voice session expired")
            item.touched = time.monotonic()
            return item

    def _write(self, sid, method, *args):
        item = self._get(sid)
        try:
            with item.writer:
                getattr(item.session,method)(*args)
            return {"session_id":sid, "status":"accepted"}
        except SpeechError as exc:
            self.close(sid)
            raise BridgeError(exc.kind, str(exc)) from None

    def append(self, session_id: str, audio_base64: str) -> dict:
        if not isinstance(audio_base64,str) or not audio_base64 or len(audio_base64)>1400000:
            raise BridgeError("bad_request", "audio chunk exceeds limit or is empty")
        try:
            audio = base64.b64decode(audio_base64, validate=True)
        except (ValueError,binascii.Error):
            raise BridgeError("bad_request", "invalid audio base64") from None
        if not 0 < len(audio) <= 1024*1024:
            raise BridgeError("bad_request", "audio chunk exceeds limit or is empty")
        return self._write(session_id,"append_audio",audio)

    def commit(self, session_id: str) -> dict:
        return self._write(session_id,"commit")

    def cancel(self, session_id: str) -> dict:
        return self._write(session_id,"cancel")

    def receive(self, session_id: str, *, timeout: float = 10) -> dict:
        item = self._get(session_id)
        if not isinstance(timeout,(float,int)) or not 0.1 <= timeout <= 15:
            raise BridgeError("bad_request", "voice receive timeout must be 0.1..15 seconds")
        if not item.reader.acquire(blocking=False):
            raise BridgeError("quota", "a receive request is already active for this session")
        try:
            return item.session.receive(timeout=timeout)
        except SpeechError as exc:
            self.close(session_id)
            raise BridgeError(exc.kind,str(exc)) from None
        finally:
            item.reader.release()

    def close(self, session_id: str) -> dict:
        with self._lock:
            item = self._sessions.pop(session_id,None)
        if item:
            try:
                item.session.close()
            except Exception:
                pass
        return {"session_id":session_id,"status":"closed"}

    def shutdown(self):
        self._stop.set()
        with self._lock:
            ids=list(self._sessions)
        for sid in ids:
            self.close(sid)
        if threading.current_thread() is not self._janitor:
            self._janitor.join(timeout=1)
