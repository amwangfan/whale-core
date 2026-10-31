"""G1：群公开时间线（GroupTimeline）。

定位（PRD §2.2）：群里**发生过什么**的短期公共证据——每条消息保留真实
发送者、回复关系、@ 集合、消息 ID 与时间。它不是任何人的长期个人档案，
进入时间线 ≠ 写入个人记忆（成员记忆走 G2 的授权管道）。

- 存储：kv 键 ``timeline:{conversation_id}``，双后端一致；
- 保留策略：条数上限 + 时长上限，追加时裁剪（默认 200 条 / 48 小时）；
- 幂等：message_id 去重（上游至少一次投递）；
- 渲染：供模型上下文使用的带归属短标签序列。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from whale_core.memory.store import MemoryStore

MAX_ENTRIES = 200
MAX_AGE_S = 48 * 3600


def _key(conversation_id: str) -> str:
    return f"timeline:{conversation_id}"


class GroupTimelineStore:
    def __init__(self, store: MemoryStore, *, max_entries: int = MAX_ENTRIES,
                 max_age_s: float = MAX_AGE_S):
        self.store = store
        self.max_entries = max(1, min(int(max_entries), 2000))
        self.max_age_s = max_age_s

    def append(self, conversation_id: str, *, message_id: str, sender_id: str,
               text: str, sender_uid: str = "", send_time: str = "",
               mentions=(), reply_to=(), is_self: bool = False) -> bool:
        """追加一条群消息。message_id 重复 → False（幂等）。返回是否新写入。"""
        if not message_id or not conversation_id:
            return False
        key = _key(conversation_id)
        entries = self.store.get(key)
        entries = list(entries) if isinstance(entries, list) else []
        if any(e.get("message_id") == message_id for e in entries):
            return False
        entries.append({
            "message_id": message_id,
            "sender_id": sender_id,
            "sender_uid": sender_uid,
            "text": text[:2000],
            "send_time": send_time,
            "mentions": [str(m) for m in mentions][:16],
            "reply_to": [str(x) for x in reply_to] if reply_to else [],
            "is_self": bool(is_self),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })
        self.store.put(key, self._trim(entries))
        return True

    def _trim(self, entries: list[dict]) -> list[dict]:
        if self.max_age_s > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - self.max_age_s
            entries = [e for e in entries if _ts(e.get("recorded_at")) >= cutoff]
        return entries[-self.max_entries:]

    def recent(self, conversation_id: str, *, limit: int = 50) -> tuple[dict, ...]:
        entries = self.store.get(_key(conversation_id))
        if not isinstance(entries, list):
            return ()
        return tuple(self._trim(list(entries))[-limit:])

    def clear(self, conversation_id: str) -> None:
        self.store.delete(_key(conversation_id))

    def render_for_model(self, conversation_id: str, *, limit: int = 20) -> str:
        """模型可读的带归属时间线（可信容器格式；正文按不可信数据处理）。

        每条一行：``[#msg尾4|uin尾4] 正文`` —— 归属映射回真实消息 ID，
        群成员不坍缩成同一个 user（ID-01）。正文中的换行压平，防止伪造
        行结构冒充他人发言或系统消息。
        """
        lines = []
        # G6 红队中-4：uin 尾 4 可碰撞（同名「群友0001」会被模型串人）。
        # 改用 sender 的稳定短哈希，并在本次渲染内保证唯一。
        entries = self.recent(conversation_id, limit=limit)
        used_labels: set[str] = set()

        def _who(e) -> str:
            if e.get("is_self"):
                return "我"
            import hashlib
            base = hashlib.sha1(
                str(e.get("sender_id") or "?").encode()).hexdigest()[:6]
            label = base
            n = 0
            while label in used_labels:
                n += 1
                label = hashlib.sha1(f"{base}#{n}".encode()).hexdigest()[:6]
            used_labels.add(label)
            return f"群友{label}"

        for e in entries:
            label = e["message_id"][-4:]
            # 正文压平 + 转义方括号（防伪造行结构/伪造归属标签）
            text = " ".join(str(e.get("text") or "").split())[:500] \
                .replace("[", "［").replace("]", "］")
            lines.append(f"[{label}|{_who(e)}] {text}")
        return "\n".join(lines)


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError):
        return 0.0
