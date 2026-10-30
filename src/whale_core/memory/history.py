"""会话历史：多轮上下文环形缓冲，按会话持久化在 MemoryStore kv。

- 键：``chat:history:{conversation_id}``（conversation_id 截断 160 字符）；
- 结构：``[{"role": "user"|"assistant", "content": str}, ...]``，最多保留
  ``MAX_HISTORY_TURNS`` 轮（2*turns 条消息），与 core/deepseek.py 的 history
  清洗上限对齐；
- 单条 content 截断 ``MAX_CONTENT_CHARS``（与模型侧 text 截断一致）；
- 读路径对损坏/异形数据宽容（跳过非法条目）——历史是上下文不是指令；
- 纯函数、离线可测；序列化由 MemoryStore 实现负责（JSON）。
"""
from __future__ import annotations

from typing import Any
import threading

from whale_core.memory.store import MemoryStore

MAX_HISTORY_TURNS = 12
MAX_CONTENT_CHARS = 4000
MAX_CONVERSATION_ID_CHARS = 160
_HISTORY_KEY_PREFIX = "chat:history:"


# Bounded lock stripes serialize turns/history updates without accumulating a
# lock for every arbitrary remote conversation ID. Reentrant for nested calls.
_CONVERSATION_LOCKS = tuple(threading.RLock() for _ in range(128))


def conversation_lock(store: MemoryStore, conversation_id: str):
    return _CONVERSATION_LOCKS[hash((id(store), _key(conversation_id))) % len(_CONVERSATION_LOCKS)]


def _key(conversation_id: str) -> str:
    return _HISTORY_KEY_PREFIX + (conversation_id or "").strip()[:MAX_CONVERSATION_ID_CHARS]


def load_history(store: MemoryStore, conversation_id: str) -> list[dict[str, str]]:
    """读取会话历史；无记录或数据损坏时返回空列表（宽容降级，不抛）。"""
    cid = (conversation_id or "").strip()
    if not cid:
        return []
    raw = store.get(_key(cid))
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            out.append({"role": role, "content": content})
    return out


def append_turn(
    store: MemoryStore,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """追加一轮对话并截断到最近 MAX_HISTORY_TURNS 轮。"""
    cid = (conversation_id or "").strip()
    if not cid:
        return
    with conversation_lock(store, cid):
        history = load_history(store, cid)
        history.append({"role": "user", "content": (user_text or "")[:MAX_CONTENT_CHARS]})
        history.append({"role": "assistant", "content": (assistant_text or "")[:MAX_CONTENT_CHARS]})
        store.put(_key(cid), history[-(MAX_HISTORY_TURNS * 2):])


def clear_history(store: MemoryStore, conversation_id: str) -> None:
    """清空某会话历史（用户数据删除语义的一部分）。"""
    cid = (conversation_id or "").strip()
    if cid:
        with conversation_lock(store, cid):
            store.delete(_key(cid))


def delete_conversation(store: MemoryStore, conversation_id: str) -> dict:
    """Delete history and chat episodes explicitly linked to this conversation.

    Imported/legacy records without a conversation_id cannot safely be inferred;
    owners may delete those individual memories separately.
    """
    from whale_core.memory.store import MemoryKind
    cid = (conversation_id or "").strip()[:MAX_CONVERSATION_ID_CHARS]
    if not cid:
        raise ValueError("conversation_id is required")
    with conversation_lock(store, cid):
        journals = []
        for key in store.keys():
            if key.startswith("runtime:task:"):
                journal = store.get(key)
                if isinstance(journal, dict) and journal.get("conversation_id") == cid:
                    task_id = key.removeprefix("runtime:task:")
                    task = store.get(f"task:{task_id}") or {}
                    if task.get("status") in {"running", "pending_approval"} or task.get("in_flight"):
                        raise ValueError("conversation has active or unresolved tasks")
                    journals.append((key, task_id))
        had_history = store.get(_key(cid)) is not None
        clear_history(store, cid)
        removed = 0
        for record in store.list_by_kind(MemoryKind.EPISODE):
            if record.content.get("conversation_id") == cid and "chat" in record.tags:
                store.delete(record.key)
                removed += 1
        # Execution journals and public receipts are derived conversation data.
        deleted_ids = set()
        for key, task_id in journals:
            store.delete(key)
            store.delete(f"task:{task_id}")
            deleted_ids.add(task_id)
        if deleted_ids:
            store.put("tasks:recent", [task_id for task_id in store.get("tasks:recent") or []
                                      if task_id not in deleted_ids])
        return {"history_deleted": had_history, "episodes_deleted": removed}
