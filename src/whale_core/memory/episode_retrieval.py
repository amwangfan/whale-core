"""相关历史情节检索（召回断层修复）：chat 情节按相关性回到 prompt。

问题（owner 实证 2026-11-11）：普通聊天落 ``tags=("chat",)`` 的 EPISODE，
``MemoryCurator.recall`` 只召回 ``curated`` 记录；companion 的近期情节注入
只覆盖最近时间窗内本会话至多 3 条——约 30 轮后，旧经历（宠物名字、生日）
仍在磁盘上却永远无法被回忆。

本模块提供**确定性词项级**相关检索，不依赖额外模型、不依赖关键词白名单：

- 分词复用 ``curation._terms``（中文单字+二元组、英数 token）；
- 词项权重用候选集的 IDF 统计（文档频率）——常见字（「今天」）自然降权、
  罕见实词（「旺财」「生日」）自然升权，没有任何硬编码词表；
- 会话语义：只召回 ``conversation_id`` 相等的本会话 chat 情节。跨会话知识
  仍按既有纪律走显式 curation（curator），原始情节不自动串会话；
- 空间语义：``allowed_spaces=None`` 兼容期旧行为（存储层仍硬排除
  protected_original）；显式元组限域；空元组 fail closed；
  ``include_protected`` 仅主人本人私聊召回路径开启（与 MemoryCurator 一致）；
- 既有排除语义不变：``superseded`` 不召回（G2）、``expires_at`` 到期退出
  （MEM-04）；``curated`` 记录跳过（curator 负责，避免双通道重复注入）；
  paused 是写入侧闸（空间停止记录），不改变召回语义；
- 只读、不晋升：检索不改写任何记录，chat 情节保持原始情节身份，
  **绝不**自动升级为 stable/curated 事实（助手生成的错误也不升级）。

打分：``score = relevance * (0.7 + 0.3 * freshness)``；时间衰减只影响
排序，不否决旧记忆（30 天前说过的宠物生日照样能召回）。防噪双门槛：
加权相关度 ``>= MIN_RELEVANCE``，且查询含多字词项时命中里必须至少有一个
多字词项（单字偶合不算相关；纯单字查询如「辣」不受此限，与 curation
的单字命中语义一致）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from whale_core.memory.curation import _is_expired, _space_ids, _terms
from whale_core.memory.store import MemoryKind, MemoryRecord, MemoryStore

#: 加权相关度门槛（IDF 归一后）；低于此值视为无关，不注入。
MIN_RELEVANCE = 0.15
#: 默认最多补充的相关情节条数（与 companion 近期情节注入同级：背景不是话题）。
DEFAULT_EPISODE_LIMIT = 3
#: 排序用时间半衰期（天）；只影响先后，不做硬淘汰。
HALF_LIFE_DAYS = 30.0
#: 单条注入片段截断上限（字符）；条数与长度都受控。
MAX_SNIPPET_CHARS = 240


@dataclass(frozen=True)
class EpisodeHit:
    """一条相关情节命中（带可解释理由，口径对齐 curation.RecallHit）。"""

    record: MemoryRecord
    score: float
    relevance: float
    reasons: tuple[str, ...]


def dedup_key(text: str) -> str:
    """情节文本的规范化去重键（与 companion._norm_mem 同口径：取用户侧内容，
    压缩空白与标点）。同内容多条记录只算一条，避免重复注入与重复占位。"""
    head = str(text)[:120]
    m = re.match(r"^用户：(.+?)(?:\n|$)", str(text), re.S)
    if m:
        head = m.group(1)
    return re.sub(r"[\s\W]+", "", head)[:40]


def _idf_weights(query_terms: set[str],
                 doc_term_sets: list[set[str]]) -> dict[str, float]:
    """查询词项的 IDF 权重：log(1 + (N+1)/(df+1))，df 为候选集文档频率。

    纯统计、确定性：高频字（寒暄/报时残留）权重低，罕见实词权重高。
    """
    n_docs = len(doc_term_sets)
    weights: dict[str, float] = {}
    for term in query_terms:
        df = sum(1 for terms in doc_term_sets if term in terms)
        weights[term] = math.log(1.0 + (n_docs + 1) / (df + 1))
    return weights


def retrieve_relevant_episodes(
    store: MemoryStore,
    query: str,
    *,
    conversation_id: str = "",
    allowed_spaces=None,
    include_protected: bool = False,
    limit: int = DEFAULT_EPISODE_LIMIT,
    exclude_keys=(),
    now: datetime | None = None,
    half_life_days: float = HALF_LIFE_DAYS,
    min_relevance: float = MIN_RELEVANCE,
) -> tuple[EpisodeHit, ...]:
    """按相关性召回本会话的历史 chat 情节（时间窗之外的旧经历也能回来）。

    - ``allowed_spaces``：MemorySpaceRef 元组 / 空间 id 元组；``()`` fail
      closed；``None`` 兼容期旧行为（protected_original 仍被存储层排除）；
    - ``exclude_keys``：调用方已注入的记录 key（近期窗去重，避免双份）；
    - 返回按 score 降序的至多 ``limit`` 条；同分新记录在前。
    """
    if limit <= 0 or half_life_days <= 0:
        return ()
    space_ids = _space_ids(allowed_spaces)
    if space_ids is not None and not space_ids:
        return ()  # 已解析但零授权空间：查询前即止（fail closed）
    current = now or datetime.now(timezone.utc)
    query_terms = _terms(query)
    if not query_terms:
        return ()  # 空查询没有相关性信号；近期窗注入负责「新鲜度」，这里不兜底
    excluded = {str(k) for k in (exclude_keys or ())}
    conv = str(conversation_id or "")

    candidates: list[tuple[MemoryRecord, set[str]]] = []
    for rec in store.list_by_kind(MemoryKind.EPISODE, space_ids=space_ids,
                                  include_protected=include_protected):
        if rec.key in excluded:
            continue
        if "chat" not in rec.tags:
            continue  # 只服务聊天情节（call 转写等其他来源保持既有边界）
        if "curated" in rec.tags:
            continue  # curated 由 MemoryCurator/HybridCurator 负责（curated 优先）
        if "superseded" in rec.tags:
            continue  # 被更正的旧记忆不再召回（G2）
        if _is_expired(rec.content, current):
            continue  # TTL 到期退出召回（MEM-04）
        if str(rec.content.get("conversation_id") or "") != conv:
            continue  # 会话隔离：别会话的原始情节不注入（跨会话知识走 curated）
        summary = str(rec.content.get("summary") or "")
        terms = _terms(summary)
        if not terms:
            continue
        candidates.append((rec, terms))
    if not candidates:
        return ()

    doc_term_sets = [terms for _, terms in candidates]
    weights = _idf_weights(query_terms, doc_term_sets)
    query_weight = sum(weights.values())
    if query_weight <= 0:
        return ()
    query_has_multi = any(len(t) >= 2 for t in query_terms)

    # 同内容去重：保留分最高（同分最新）的一条，limit 按去重后计。
    best_by_norm: dict[str, EpisodeHit] = {}
    for rec, terms in candidates:
        common = terms & query_terms
        if not common:
            continue
        if query_has_multi and not any(len(t) >= 2 for t in common):
            continue  # 只有单字偶合：视为无关（防「今天/什么」级噪声）
        relevance = sum(weights[t] for t in common) / query_weight
        if relevance < min_relevance:
            continue
        age_days = max(0.0, (current - rec.created_at).total_seconds() / 86400)
        freshness = math.pow(0.5, age_days / half_life_days)
        score = relevance * (0.7 + 0.3 * freshness)
        summary = str(rec.content.get("summary") or "")
        reasons = (
            f"词项匹配({len(common)}项)",
            f"相关度{relevance:.2f}",
            f"距今{age_days:.0f}天",
            "原始情节（未经确认，不是稳定事实）",
        )
        hit = EpisodeHit(rec, score, relevance, reasons)
        norm = dedup_key(summary) or rec.key
        prev = best_by_norm.get(norm)
        if prev is None or (hit.score, rec.created_at) > (prev.score,
                                                           prev.record.created_at):
            best_by_norm[norm] = hit

    hits = sorted(best_by_norm.values(),
                  key=lambda h: (h.score, h.record.created_at), reverse=True)
    return tuple(hits[:limit])
