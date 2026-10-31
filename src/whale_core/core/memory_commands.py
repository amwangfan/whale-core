"""owner 显式记忆指令（记住/忘掉）的共享处理：QQ 工作流与 Runtime 共用。

owner 直聊的显式指令即 consent（单 owner 陪伴场景，
docs/gap-analysis-2026-10-30.md §4 R4）；证据链指向触发消息，
记忆可经「忘掉」或删除语义移除。
"""
from __future__ import annotations

from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import MemoryStore

REMEMBER_PREFIXES: tuple[str, ...] = ("记住：", "记住:", "记住 ")
FORGET_PREFIXES: tuple[str, ...] = ("忘掉：", "忘掉:", "忘记：", "忘记:", "忘掉 ", "忘记 ")


def handle_memory_command(
    store: MemoryStore,
    text: str,
    *,
    source: str,
    evidence_id: str,
    curator: MemoryCurator | None = None,
) -> str | None:
    """识别并执行记忆指令，返回确定性回复文本；非指令返回 None。

    - 记住：<事实> → curated 稳定事实（consent=True；stable_fact=True 不衰减）；
    - 忘掉/忘记：<线索> → 词项召回最相关的一条 curated 记忆并删除；
      只删 curated 记录（curator.forget 语义），聊天情节不受影响。
    """
    text = (text or "").strip()
    if not text:
        return None
    cur = curator or MemoryCurator(store)
    for prefix in REMEMBER_PREFIXES:
        if text.startswith(prefix):
            fact = text[len(prefix):].strip()
            if not fact:
                return "想让我记住什么？"
            cur.remember(
                fact, source=source, evidence_id=evidence_id,
                importance=0.8, confidence=1.0, stable_fact=True, consent=True,
            )
            return f"好，我记住啦：{fact[:50]}"
    for prefix in FORGET_PREFIXES:
        if text.startswith(prefix):
            clue = text[len(prefix):].strip()
            if not clue:
                return "想让我忘掉什么？"
            hits = cur.recall(clue, limit=1)
            if hits and cur.forget(hits[0].record.key):
                summary = str(hits[0].record.content.get("summary", ""))
                return f"已忘掉：{summary[:50]}"
            return "没有找到相关的记忆诶"
    return None
