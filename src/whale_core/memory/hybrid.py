"""混合记忆检索（PR-14）：词项 + 嵌入融合召回。

候选集与打分口径继承 MemoryCurator（EPISODE+PREFERENCE 且 tags 含 "curated"），
在此基础上叠加可选的向量相似度：

    score = confidence * (alpha * lexical
                          + (1 - alpha) * embedding
                          + 0.15 * importance
                          + 0.10 * freshness)

- lexical   = 查询词项交集占比（与 curation 的 relevance 完全一致）
- embedding = cosine(query 向量, summary 向量)；无 embedder 或嵌入失败时为 0.0
- freshness = 稳定事实 1.0；否则 0.5 ** (age_days / half_life_days)

防噪门槛：非空查询下，词项零交集且 embedding < 0.3 的记录不入选，
避免纯向量召回把无关记忆带进上下文。空查询只放 stable 事实（与 curator 一致）。

embedder=None 时整体退化为纯词项召回（候选集与 MemoryCurator 一致）；
embedder 抛异常时本次召回自动退化为纯词项，不向上抛错。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence

from whale_core.memory.curation import _terms
from whale_core.memory.store import MemoryKind, MemoryRecord, MemoryStore

#: 词项零交集时，向量分至少达到该阈值才允许入选（防纯向量噪声）。
EMBEDDING_FLOOR = 0.3


class EmbeddingProvider(Protocol):
    """嵌入端点契约（model 类插件可提供 embedding.embed 能力；外部服务经 provider 注入）。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；任一零向量安全返回 0.0。"""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
    for x in a:
        na += x * x
    for y in b:
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class HashEmbeddingProvider:
    """确定性 stdlib 占位嵌入（hashing trick：词项 → 固定维度稀疏向量）。

    诚实标注：这只是词项散列，没有语义理解能力；它让混合链路在无外部
    服务时可跑可测。真实语义嵌入由 model 插件经 EmbeddingProvider 注入。
    embed 复用 curation._terms 词项（中文单字+bigram、英文/数字 token），
    每个词项经 sha256 散列到固定维度并累加计数，最后 L2 归一化。
    同文本必得同向量（不依赖 PYTHONHASHSEED，跨进程确定）。
    """

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for term in _terms(text):
                digest = hashlib.sha256(term.encode("utf-8")).digest()
                vec[int.from_bytes(digest[:8], "big") % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0.0:
                vec = [v / norm for v in vec]
            vectors.append(vec)
        return vectors


@dataclass(frozen=True)
class HybridHit:
    record: MemoryRecord
    score: float
    lexical_score: float
    embedding_score: float
    reasons: tuple[str, ...]


class HybridCurator:
    """词项 + 嵌入融合召回。embedder=None 时退化为纯词项（与 MemoryCurator 一致口径）。

    融合公式（docstring 冻结）：
        score = confidence * (alpha*lexical + (1-alpha)*embedding
                              + 0.15*importance + 0.10*freshness)
    非空查询：词项零交集且 embedding < 0.3 不入选；空查询只放 stable 事实。
    """

    def __init__(self, store: MemoryStore, *, embedder: EmbeddingProvider | None = None,
                 alpha: float = 0.6, half_life_days: float = 30.0):
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must be within (0, 1)")
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        self.store = store
        self.embedder = embedder
        self.alpha = alpha
        self.half_life_days = half_life_days

    def recall(self, query: str, *, limit: int = 5) -> tuple[HybridHit, ...]:
        if limit <= 0:
            return ()
        current = datetime.now(timezone.utc)
        query_terms = _terms(query)
        records = (self.store.list_by_kind(MemoryKind.EPISODE) +
                   self.store.list_by_kind(MemoryKind.PREFERENCE))
        candidates = [r for r in records if "curated" in r.tags]

        # 一次批量嵌入；任何异常 → 本次召回整体退化纯词项。
        query_vec: list[float] | None = None
        record_vecs: list[list[float]] | None = None
        degraded = self.embedder is None
        if self.embedder is not None and candidates:
            try:
                summaries = [str(r.content.get("summary", "")) for r in candidates]
                vectors = self.embedder.embed([query, *summaries])
                query_vec, record_vecs = vectors[0], vectors[1:]
            except Exception:
                degraded = True
                query_vec, record_vecs = None, None

        matches: list[HybridHit] = []
        for idx, rec in enumerate(candidates):
            content = rec.content
            stable = bool(content.get("stable_fact"))
            terms = _terms(str(content.get("summary", "")))
            common = terms & query_terms
            embedding = (cosine(query_vec, record_vecs[idx])
                         if query_vec is not None and record_vecs is not None else 0.0)
            if query_terms:
                if not common and embedding < EMBEDDING_FLOOR:
                    continue
            elif not stable:
                continue
            lexical = len(common) / max(1, len(query_terms))
            age = max(0.0, (current - rec.created_at).total_seconds() / 86400)
            freshness = 1.0 if stable else math.pow(0.5, age / self.half_life_days)
            importance = float(content.get("importance", 0.5))
            confidence = float(content.get("confidence", 1.0))
            score = confidence * (self.alpha * lexical
                                  + (1 - self.alpha) * embedding
                                  + 0.15 * importance
                                  + 0.10 * freshness)
            channel = []
            if common:
                channel.append(f"词项匹配({len(common)}项)")
            if embedding >= EMBEDDING_FLOOR:
                channel.append(f"嵌入相似{embedding:.2f}")
            if stable:
                channel.append("稳定事实")
            reasons = tuple(channel) + (
                "长期事实不衰减" if stable else f"距今{age:.0f}天",
                f"来源:{content.get('source', 'unknown')}",
                f"证据:{content.get('evidence_id', 'unknown')}",
            ) + (("嵌入不可用，已退化纯词项",) if degraded and self.embedder is not None else ())
            matches.append(HybridHit(rec, score, lexical, embedding, reasons))
        matches.sort(key=lambda hit: (hit.score, hit.record.created_at), reverse=True)
        return tuple(matches[:limit])
