"""Unifies text companionship and realtime transcripts without blocking the audio loop."""
from __future__ import annotations
import queue
import threading
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4
from whale_core.memory.store import MemoryStore
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.continuity import remember_episode, build_continuity_context

class TextCompanion(Protocol):
    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...

class WhaleCompanion:
    def __init__(self, store: MemoryStore, model: TextCompanion, *, curator: MemoryCurator | None = None):
        self.store, self.model, self.curator = store, model, curator

    def chat(self, text: str) -> str:
        ctx = build_continuity_context(self.store)
        if self.curator is not None:
            memories = [str(hit.record.content.get("summary", ""))
                        for hit in self.curator.recall(text)]
        else:
            # Legacy chat-only behavior kept for backwards compatibility; opt in
            # to curated retrieval to avoid injecting raw transcripts by default.
            memories = [str(r.content.get("summary", "")) for r in ctx.recent_episodes]
        response = self.model.reply(text, persona=ctx.persona_summary, memories=memories)
        remember_episode(self.store, summary=f"用户：{text[:500]}\n鲸鱼：{response[:500]}",
                         key=f"ep:chat:{uuid4().hex}", tags=("chat",))
        return response

class RealtimeTranscriptRecorder:
    """Bounded, nonblocking queue for transcripts; no audio and no DeepSeek call.
    Events dropped on overload rather than delaying realtime sound.
    """
    def __init__(self, store: MemoryStore, capacity: int = 64):
        self.store = store
        self.events: queue.Queue[tuple[str, str, str]] = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self.failed_count = 0
        self._worker = threading.Thread(target=self._run, daemon=True, name="whale-memory")
        self._worker.start()

    def offer(self, session_id: str, role: str, text: str) -> bool:
        if self._stop.is_set() or role not in ("user", "assistant") or not text.strip():
            return False
        try:
            self.events.put_nowait((session_id[:100], role, text[:2000]))
            return True
        except queue.Full:
            return False

    def _run(self):
        while not self._stop.is_set() or not self.events.empty():
            try:
                sid, role, text = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                remember_episode(self.store, summary=text, key=f"ep:call:{uuid4().hex}",
                                 tags=("call", role, f"session:{sid}"),
                                 extra={"role": role, "session_id": sid})
            except Exception:
                # A failed disk write must not kill the consumer thread and
                # leave close() blocked on unfinished queue items.
                # Keep only failure count; never log private transcript text.
                self.failed_count += 1
            finally:
                self.events.task_done()

    def close(self):
        self._stop.set()
        self.events.join()
        self._worker.join(timeout=3)
