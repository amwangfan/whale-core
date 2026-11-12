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
#: 会话写回代际（owner_fact_v1 遗忘防线）：clear/delete/遗忘抑制会 bump；
#: 迟到的助手/补发写回携带轮初快照的 generation，失配即丢弃——防止
#: forget 清空短历史后，在途主/重模型回复把旧用户话重建回历史。
_GENERATION_KEY_PREFIX = "chat:histgen:"


# Bounded lock stripes serialize turns/history updates without accumulating a
# lock for every arbitrary remote conversation ID. Reentrant for nested calls.
_CONVERSATION_LOCKS = tuple(threading.RLock() for _ in range(128))


def conversation_lock(store: MemoryStore, conversation_id: str):
    return _CONVERSATION_LOCKS[hash((id(store), _key(conversation_id))) % len(_CONVERSATION_LOCKS)]


def _key(conversation_id: str) -> str:
    return _HISTORY_KEY_PREFIX + (conversation_id or "").strip()[:MAX_CONVERSATION_ID_CHARS]


def _gen_key(conversation_id: str) -> str:
    return _GENERATION_KEY_PREFIX + (conversation_id or "").strip()[:MAX_CONVERSATION_ID_CHARS]


def _gen_value(raw: Any) -> int:
    """代际值的宽容读取：缺失/损坏一律视为 0（与历史读路径同纪律）。"""
    return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _coerce_history(raw: Any) -> list[dict[str, str]]:
    """kv 原始值 → 合法历史条目列表（跳过异形数据；与 load_history 同口径）。"""
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


def history_generation(store: MemoryStore, conversation_id: str) -> int:
    """当前会话写回代际（无记录 = 0）。轮初快照供迟到写回比较。"""
    cid = (conversation_id or "").strip()
    if not cid:
        return 0
    return _gen_value(store.get(_gen_key(cid)))


def bump_history_generation(store: MemoryStore, conversation_id: str) -> int:
    """原子递增并返回新代际（遗忘抑制/管理面使在途写回失效时用）。"""
    cid = (conversation_id or "").strip()
    if not cid:
        return 0
    gk = _gen_key(cid)

    def _tx(tx) -> int:
        new = _gen_value(tx.get(gk)) + 1
        tx.put(gk, new)
        return new

    with conversation_lock(store, cid):
        return int(store.transact(_tx))


def put_record_if_generation(store: MemoryStore, conversation_id: str,
                             generation: int, record) -> bool:
    """代际比较与记录写入在**同一事务**内完成（情节写回守卫）。

    代际失配（期间发生过 clear_history/遗忘抑制）→ 不写入并返回 False；
    避免 check-then-write 竞态让被遗忘的源 episode 复活。
    """
    cid = (conversation_id or "").strip()
    if not cid:
        return False
    gk = _gen_key(cid)

    def _tx(tx) -> bool:
        if _gen_value(tx.get(gk)) != int(generation):
            return False
        tx.put_record(record)
        return True

    with conversation_lock(store, cid):
        return bool(store.transact(_tx))


def patch_history_if_generation(store: MemoryStore, conversation_id: str,
                                generation: int, patch_fn) -> bool:
    """代际守卫下原子改写整段历史（迟到补发/媒体改写等旁路写回用）。

    ``patch_fn(history) -> list | None``：返回 None 表示不改写；返回值整体
    替换历史（调用方负责截断）。代际失配或 patch 返回 None → 不写入。
    """
    cid = (conversation_id or "").strip()
    if not cid:
        return False
    hk, gk = _key(cid), _gen_key(cid)

    def _tx(tx) -> bool:
        if _gen_value(tx.get(gk)) != int(generation):
            return False
        patched = patch_fn(_coerce_history(tx.get(hk)))
        if patched is None:
            return False
        tx.put(hk, patched[-(MAX_HISTORY_TURNS * 2):])
        return True

    with conversation_lock(store, cid):
        return bool(store.transact(_tx))


def load_history(store: MemoryStore, conversation_id: str) -> list[dict[str, str]]:
    """读取会话历史；无记录或数据损坏时返回空列表（宽容降级，不抛）。"""
    cid = (conversation_id or "").strip()
    if not cid:
        return []
    return _coerce_history(store.get(_key(cid)))


def _trim(history: list[dict[str, str]]) -> list[dict[str, str]]:
    return history[-(MAX_HISTORY_TURNS * 2):]


def append_user_message(
    store: MemoryStore,
    conversation_id: str,
    user_text: str,
) -> None:
    """请求一开始就落盘用户原话，供同会话重叠请求立刻可见。

    助手回复尚未生成时，历史可能以一条 user 结尾（半轮）；后续
    ``append_assistant_message`` / ``append_turn`` 再补齐。
    """
    cid = (conversation_id or "").strip()
    if not cid:
        return
    with conversation_lock(store, cid):
        history = load_history(store, cid)
        history.append({"role": "user", "content": (user_text or "")[:MAX_CONTENT_CHARS]})
        store.put(_key(cid), _trim(history))


def append_assistant_message(
    store: MemoryStore,
    conversation_id: str,
    assistant_text: str,
    *,
    generation: int | None = None,
) -> bool:
    """在已落盘的用户话之后追加助手回复（不重复写 user）。

    ``generation`` 非 None 时启用写回代际守卫：与当前 ``chat:histgen`` 不一致
    （期间发生过 clear/遗忘抑制）则**原子丢弃**本次写回——防止 forget 清空
    短历史后，迟到的主/重模型回复把旧用户话重建回历史。check 与写入在同一个
    ``store.transact`` 事务内完成，避免 check-then-write 竞态。
    返回是否实际写入。
    """
    cid = (conversation_id or "").strip()
    if not cid or not (assistant_text or "").strip():
        return False
    entry = {"role": "assistant", "content": (assistant_text or "")[:MAX_CONTENT_CHARS]}
    with conversation_lock(store, cid):
        if generation is None:
            history = load_history(store, cid)
            history.append(entry)
            store.put(_key(cid), _trim(history))
            return True
        hk, gk = _key(cid), _gen_key(cid)

        def _tx(tx) -> bool:
            if _gen_value(tx.get(gk)) != int(generation):
                return False  # 写回已失效（历史被清/遗忘抑制）：丢弃
            history = _coerce_history(tx.get(hk))
            history.append(entry)
            tx.put(hk, _trim(history))
            return True

        return bool(store.transact(_tx))


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
        store.put(_key(cid), _trim(history))


def append_assistant_followup(
    store: MemoryStore,
    conversation_id: str,
    user_text: str,
    followup_text: str,
    *,
    generation: int | None = None,
) -> bool:
    """并行链路第二条：并入最近一轮（同一用户原话）的助手消息；找不到则新追加一轮。

    ``generation`` 非 None 时代际守卫在**同一事务内**比对：失配则整条丢弃，
    尤其是「找不到旧轮」的 fallback——forget 清空历史后，迟到的补发不得用
    ``append_turn`` 把旧用户话重建回历史（Astra 红队 2026-11-12）。
    返回是否实际写入。
    """
    cid = (conversation_id or "").strip()
    if not cid or not (followup_text or "").strip():
        return False
    user_norm = (user_text or "")[:MAX_CONTENT_CHARS]
    merged_text = (followup_text or "")[:MAX_CONTENT_CHARS]
    with conversation_lock(store, cid):
        if generation is None:
            history = load_history(store, cid)
            for i in range(len(history) - 1, 0, -1):
                if (history[i]["role"] == "assistant" and history[i - 1]["role"] == "user"
                        and history[i - 1]["content"] == user_norm):
                    merged = (history[i]["content"] + "\n" + merged_text).strip()
                    history[i] = {"role": "assistant", "content": merged[:MAX_CONTENT_CHARS]}
                    store.put(_key(cid), history[-(MAX_HISTORY_TURNS * 2):])
                    return True
            append_turn(store, cid, user_text, followup_text)
            return True
        hk, gk = _key(cid), _gen_key(cid)

        def _tx(tx) -> bool:
            if _gen_value(tx.get(gk)) != int(generation):
                return False  # 写回已失效：丢弃（含 fallback，不重建用户话）
            history = _coerce_history(tx.get(hk))
            for i in range(len(history) - 1, 0, -1):
                if (history[i]["role"] == "assistant" and history[i - 1]["role"] == "user"
                        and history[i - 1]["content"] == user_norm):
                    merged = (history[i]["content"] + "\n" + merged_text).strip()
                    history[i] = {"role": "assistant", "content": merged[:MAX_CONTENT_CHARS]}
                    tx.put(hk, history[-(MAX_HISTORY_TURNS * 2):])
                    return True
            # fallback（旧轮已被 12 轮环形窗挤出）：仅当代际未变才允许重建整轮。
            history.append({"role": "user", "content": user_norm})
            history.append({"role": "assistant", "content": merged_text})
            tx.put(hk, history[-(MAX_HISTORY_TURNS * 2):])
            return True

        return bool(store.transact(_tx))


def clear_history(store: MemoryStore, conversation_id: str) -> None:
    """清空某会话历史（用户数据删除语义的一部分）。

    原子地同时递增写回代际（同一事务内 delete+bump）：清空之后，仍持有旧
    代际快照的在途助手/补发写回一律失效——遗忘/删除不会被迟到写回复活。
    """
    cid = (conversation_id or "").strip()
    if cid:
        hk, gk = _key(cid), _gen_key(cid)
        with conversation_lock(store, cid):
            def _tx(tx) -> None:
                tx.delete(hk)
                tx.put(gk, _gen_value(tx.get(gk)) + 1)

            store.transact(_tx)


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
