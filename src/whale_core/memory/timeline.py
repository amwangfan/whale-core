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

    def render_for_model(self, conversation_id: str, *, limit: int = 20,
                         self_uid: str = "") -> str:
        """模型可读的带归属时间线（可信容器格式；正文按不可信数据处理）。

        每条一行：``[#msg尾4|群友hash]（↩回复#尾4） 正文`` —— 归属映射回
        真实消息 ID，群成员不坍缩成同一个 user（ID-01）。正文中的换行压平，
        防止伪造行结构冒充他人发言或系统消息。

        群友标签纪律（issue #15）：
        - 标签是 ``sender_id`` 的纯函数：同一发送者在同一次渲染与不同窗口
          内保持一致；绝不按消息条数重新编号（同一人连发两条不能变成
          两个「群友」）；
        - 不同 sender 的短哈希碰撞用**确定性**消歧：按 sender_id 排序后
          逐步加长哈希前缀（6→8→10…），结果只取决于窗口内的 sender 集合，
          与消息顺序无关；
        - 昵称永不作为身份键；标签不含任何 QQ UIN 片段。
        """
        entries = self.recent(conversation_id, limit=limit)
        labels = _stable_sender_labels(entries)
        msg_label = {e["message_id"]: e["message_id"][-4:] for e in entries}

        def _reply_marks(e) -> str:
            """回复/@ 关系标注（模型可见「回复谁/@谁」，防引用伪造归属）。

            只标注两类确定性事实：reply_to 的消息尾号、是否 @ 了鲸鱼自己
            （self_uid 命中 mentions）。被 @ 的第三方 uid 不做猜测性归属，
            避免把错误身份喂给模型。
            """
            marks = []
            for ref in e.get("reply_to") or ():
                tail = msg_label.get(ref) or str(ref)[-4:]
                marks.append(f"↩回复#{tail}")
            if self_uid and self_uid in (e.get("mentions") or ()):
                marks.append("@我")
            return (" " + " ".join(marks)) if marks else ""

        lines = []
        for e in entries:
            # 正文压平 + 转义方括号（防伪造行结构/伪造归属标签）
            text = " ".join(str(e.get("text") or "").split())[:500] \
                .replace("[", "［").replace("]", "］")
            lines.append(
                f"[{msg_label[e['message_id']]}|{labels[e['message_id']]}]"
                f"{_reply_marks(e)} {text}")
        return "\n".join(lines)


def _stable_sender_labels(entries: tuple[dict, ...]) -> dict[str, str]:
    """message_id → 稳定归属标签（「我」或「群友<hash>」）。

    同一 sender_id 恒同标签；碰撞按 sender_id 排序后确定性加长前缀。
    """
    senders: list[str] = sorted({
        str(e.get("sender_id") or "?") for e in entries if not e.get("is_self")
    })
    sender_label = _unique_hashes(senders)
    return {
        e["message_id"]: ("我" if e.get("is_self")
                          else f"群友{sender_label[str(e.get('sender_id') or '?')]}")
        for e in entries
    }


def _unique_hashes(ids: list[str], *, digest_fn=None) -> dict[str, str]:
    """每个 id 分配唯一短哈希：优先 6 位，碰撞集合内同步加长前缀。

    ``digest_fn`` 仅测试注入（制造确定性碰撞）；生产固定 sha1。
    """
    if digest_fn is None:
        import hashlib
        digest_fn = lambda s: hashlib.sha1(s.encode()).hexdigest()
    digest = {i: digest_fn(i) for i in ids}
    # 只加长碰撞者：无碰撞的 id 恒为 6 位（跨窗口稳定性最大化）。
    length = {i: 6 for i in ids}
    while True:
        labels = {i: d[:length[i]] for i, d in digest.items()}
        counts: dict[str, int] = {}
        for lab in labels.values():
            counts[lab] = counts.get(lab, 0) + 1
        collided = {i for i, lab in labels.items() if counts[lab] > 1}
        if not collided:
            return labels
        for i in collided:
            length[i] += 2  # 确定性：同一 id 集合必得同一结果


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError):
        return 0.0
