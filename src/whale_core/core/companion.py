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
from whale_core.memory.history import append_turn, load_history, conversation_lock

class TextCompanion(Protocol):
    def reply(self, text: str, *, persona: str = "", memories=(), history=(),
              images=()) -> str: ...

class WhaleCompanion:
    def __init__(self, store: MemoryStore, model: TextCompanion, *, curator: MemoryCurator | None = None):
        self.store, self.model, self.curator = store, model, curator

    def chat(self, text: str, *, conversation_id: str = "", user_text: str | None = None,
             images: Sequence[bytes] = (), turn_key: str = "",
             episodic: bool = True) -> str:
        with conversation_lock(self.store, conversation_id):
            return self._chat(text, conversation_id=conversation_id, user_text=user_text,
                              images=images, turn_key=turn_key, episodic=episodic)

    def _chat(self, text: str, *, conversation_id: str = "", user_text: str | None = None,
              images: Sequence[bytes] = (), turn_key: str = "",
              episodic: bool = True) -> str:
        original = text if user_text is None else user_text
        ctx = build_continuity_context(self.store)
        if self.curator is not None:
            memories = [str(hit.record.content.get("summary", ""))
                        for hit in self.curator.recall(original)]
            # 修复「只写不读」断层：近期对话情节作为会话记忆补充注入——
            # curated 事实在前（证据语义优先），近期聊天情节在后，去重封顶 8 条。
            for record in ctx.recent_episodes:
                if "chat" not in record.tags:
                    continue
                summary = str(record.content.get("summary", ""))
                if summary and summary not in memories:
                    memories.append(summary)
            memories = memories[:8]
        else:
            # Legacy chat-only behavior kept for backwards compatibility; opt in
            # to curated retrieval to avoid injecting raw transcripts by default.
            memories = [str(r.content.get("summary", "")) for r in ctx.recent_episodes]
        # 多轮历史：显式传 conversation_id 才启用（默认保持单轮行为，兼容旧调用方）。
        history = load_history(self.store, conversation_id) if conversation_id else ()
        kwargs = {"persona": ctx.persona_summary, "memories": memories, "history": history}
        if images:
            kwargs["images"] = images
        response = self.model.reply(text, **kwargs)
        if episodic:
            # G2 情节质量：同一对话轮（turn_key，如 task_id）内的 ack/补发等
            # 多次系统中间对话**合并回写**同一条情节（后者覆盖前者，包含最终
            # 答复），不再每句聊天都制造一条低价值记忆；纯内部调用
            # （episodic=False，如参考图描述）不写情节。
            ep_key = (f"ep:chat:{conversation_id}:{turn_key}" if turn_key
                      else f"ep:chat:{uuid4().hex}")
            remember_episode(self.store, summary=f"用户：{original[:500]}\n鲸鱼：{response[:500]}",
                             key=ep_key, tags=("chat",),
                             extra={"conversation_id": conversation_id[:160],
                                    "layer": "episode"})
        if conversation_id:
            append_turn(self.store, conversation_id, original, response)
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
