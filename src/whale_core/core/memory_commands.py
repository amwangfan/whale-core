"""owner 显式记忆指令（记住/忘掉）的共享处理：QQ 工作流与 Runtime 共用。

owner 直聊的显式指令即 consent（单 owner 陪伴场景，
docs/archive/2026-10-30/gap-analysis-2026-10-30.md §4 R4）；证据链指向触发消息，
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
    space_id: str = "",
    allowed_spaces=None,
    owner_facts=None,
    owner_fact_authority=None,
    principal_id: str = "",
) -> str | None:
    """识别并执行记忆指令，返回确定性回复文本；非指令返回 None。

    - 记住：<事实> → curated 稳定事实（consent=True；stable_fact=True 不衰减）；
    - 忘掉/忘记：<线索> → 先走 owner_fact_v1 窄分派（确定性精确匹配：唯一
      命中才删，多义必须澄清，**禁止相似度 top-1 盲删**）；零命中回落既有
      curated 词项召回删除（旧行为不变，chat 情节同样不受影响）。

    S2：``space_id`` 为写入目标空间（调用方按 ResolvedContext 授权决定；
    空串 = 未分区兼容行为）。``allowed_spaces`` 限定「忘掉」的检索范围
    （不能跨授权空间匹配删除目标）。
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
            # 主体归属（生产实证 2026-11-01：裸文本「我最喜欢的…」被模型
            # 误读成鲸鱼自己的偏好）。摘要带说话人，模型才能正确归因。
            summary = f"用户自述：{fact}"
            # 同空间内同义去重（同一事实不重复建档）
            existing = cur.recall(fact, limit=3, allowed_spaces=allowed_spaces)
            for hit in existing:
                old = str(hit.record.content.get("summary", ""))
                if old.removeprefix("用户自述：").strip() == fact:
                    return f"这个我记着呢：{fact[:50]}"
            cur.remember(
                summary, source=source, evidence_id=evidence_id,
                importance=0.8, confidence=1.0, stable_fact=True, consent=True,
                space_id=space_id,
            )
            return f"好，我记住啦：{fact[:50]}"
    for prefix in FORGET_PREFIXES:
        if text.startswith(prefix):
            clue = text[len(prefix):].strip()
            if not clue:
                return "想让我忘掉什么？"
            # owner_fact_v1 窄分派：长期事实的显式遗忘（含修订链墓碑 +
            # 源 episode/短历史抑制），确定性匹配不命中才回落旧路径。
            if (owner_facts is not None and owner_fact_authority is not None
                    and space_id):
                from whale_core.core.owner_fact_host import (
                    explicit_forget_owner_fact)
                meta = store.get(f"space:{space_id}")
                owner_id = (str(meta.get("owner"))
                            if isinstance(meta, dict) and meta.get("owner")
                            else "owner")
                reply, handled = explicit_forget_owner_fact(
                    store, owner_facts, owner_fact_authority,
                    owner_id=owner_id, space_id=space_id, clue=clue,
                    principal_id=principal_id)
                if handled:
                    return reply
            from whale_core.core.companion import _recall
            hits = _recall(cur, clue, allowed_spaces, limit=1)
            if hits and cur.forget(hits[0].record.key):
                summary = str(hits[0].record.content.get("summary", ""))
                return f"已忘掉：{summary[:50]}"
            return "没有找到相关的记忆诶"
    return None
